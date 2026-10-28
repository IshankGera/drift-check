"""
`drift-check probe`: checks OpenAI-compatible chat calls against the LIVE server.

The Python SDK accepts parameters that a given server/model may reject at runtime
(e.g. `max_tokens` on OpenAI reasoning models -> "use max_completion_tokens").
This module sends one minimal request per unique (model, parameters) combination
found in the codebase and turns server rejections into findings.

Requests go directly from the user's machine to the provider with the user's own key.
Nothing is sent anywhere unless the user runs `drift-check probe`.
"""
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from drift_check.models import CallSite, Finding

DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Method suffixes (as produced by the analyzer) that hit POST /chat/completions
CHAT_METHOD_SUFFIXES = ("chat.completions.create",)

# Never sent: either set by the probe itself or client-side only.
EXCLUDED_PARAMS = {
    "model", "messages", "stream", "stream_options",
    "extra_headers", "extra_query", "extra_body", "timeout",
}

# Safe, cheap placeholder values for parameters whose value isn't a literal in the code.
# Only the parameter NAME is under test here, so values are chosen to be valid almost everywhere.
PLACEHOLDERS: Dict[str, Any] = {
    "max_tokens": 16,
    "max_completion_tokens": 16,
    "temperature": 1,
    "top_p": 1,
    "n": 1,
    "presence_penalty": 0,
    "frequency_penalty": 0,
    "seed": 1,
    "logprobs": False,
    "user": "drift-check-probe",
    "stop": ["\n\n\n"],
    "parallel_tool_calls": None,  # only meaningful with tools; skipped below
    "store": False,
    "reasoning_effort": "low",
    "service_tier": "auto",
}

PING_MESSAGES = [{"role": "user", "content": "Reply with the single word: ok"}]


@dataclass
class ProbeCase:
    model: str
    params: Dict[str, Any]
    literal_params: List[str]          # params whose value came from the user's code
    skipped_params: List[str]          # params that couldn't be probed safely
    call_sites: List[CallSite] = field(default_factory=list)

    @property
    def key(self) -> Tuple[str, str]:
        return self.model, json.dumps(self.params, sort_keys=True, default=str)


@dataclass
class ProbeResult:
    status: str                    # OK | REJECTED | INCONCLUSIVE | AUTH_ERROR | NETWORK_ERROR
    http_status: Optional[int] = None
    message: str = ""
    param: Optional[str] = None
    code: Optional[str] = None


def is_chat_call(call: CallSite) -> bool:
    return call.package_name == "openai" and call.method_name.endswith(CHAT_METHOD_SUFFIXES)


def build_cases(call_sites: List[CallSite], model_override: Optional[str] = None):
    """
    Groups chat call sites into unique probe requests.
    Returns (cases, unprobeable) where unprobeable is a list of (call, reason).
    """
    cases: Dict[Tuple[str, str], ProbeCase] = {}
    unprobeable: List[Tuple[CallSite, str]] = []

    for call in call_sites:
        if not is_chat_call(call):
            continue
        model = call.literal_kwargs.get("model") if "model" in call.literal_kwargs else None
        if model_override:
            model = model_override
        if not isinstance(model, str) or not model:
            unprobeable.append((call, "Model is not a literal in the code. Pass --model to probe it."))
            continue

        params: Dict[str, Any] = {}
        literal_params, skipped = [], []
        for name in call.kwargs_passed:
            if name in EXCLUDED_PARAMS:
                continue
            if name in call.literal_kwargs and call.literal_kwargs[name] is not None:
                params[name] = call.literal_kwargs[name]
                literal_params.append(name)
            elif PLACEHOLDERS.get(name) is not None:
                params[name] = PLACEHOLDERS[name]
            else:
                skipped.append(name)

        case = ProbeCase(model, params, literal_params, skipped)
        existing = cases.setdefault(case.key, case)
        existing.call_sites.append(call)

    return list(cases.values()), unprobeable


def send_probe(base_url: str, api_key: str, case: ProbeCase, timeout: float = 60.0) -> ProbeResult:
    body = {"model": case.model, "messages": PING_MESSAGES, **case.params}
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "drift-check-probe",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            resp.read()
            return ProbeResult("OK", resp.status)
    except urllib.error.HTTPError as e:
        return classify_error(e.code, e.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        reason = getattr(e, "reason", e)
        return ProbeResult("NETWORK_ERROR", message=str(reason))


def classify_error(http_status: int, raw_body: str) -> ProbeResult:
    try:
        err = json.loads(raw_body).get("error", {})
        if isinstance(err, str):
            err = {"message": err}
    except Exception:
        err = {"message": raw_body[:300]}

    message = (err.get("message") or "").strip() or f"HTTP {http_status}"
    param, code = err.get("param"), err.get("code")

    if http_status in (401, 403):
        return ProbeResult("AUTH_ERROR", http_status, message, param, code)
    if http_status in (400, 404, 422):
        return ProbeResult("REJECTED", http_status, message, param, code)
    # 429 rate limit, 5xx, etc. say nothing about compatibility
    return ProbeResult("INCONCLUSIVE", http_status, message, param, code)


def findings_for(case: ProbeCase, result: ProbeResult) -> List[Finding]:
    """
    REJECTED -> ERROR when the server rejected the parameter itself, the model, or a value
    taken from the user's code. If it only rejected a placeholder VALUE we chose, it is a
    WARNING, because the user's real value may be fine.
    """
    findings: List[Finding] = []
    for call in case.call_sites:
        def add(severity: str, message: str):
            findings.append(Finding(severity, call.file_path, call.line_number,
                                    call.package_name, call.method_name, message))

        if result.status == "OK":
            for name in case.skipped_params:
                add("WARNING", f"Parameter '{name}' was not probed (value is not a literal). (Confidence: LOW)")
            continue

        server_msg = f"Server ({result.http_status}): {result.message}"
        if result.status == "REJECTED":
            placeholder_value_only = (
                result.param in case.params
                and result.param not in case.literal_params
                and result.code not in ("unsupported_parameter", "unknown_parameter")
                and "unrecognized" not in result.message.lower()
                and "unsupported parameter" not in result.message.lower()
            )
            if placeholder_value_only:
                add("WARNING", f"Server rejected the probe's test value for '{result.param}'. "
                               f"Your real value may be fine. {server_msg}")
            else:
                add("ERROR", f"Model '{case.model}': {server_msg}")
        else:
            add("WARNING", f"Could not verify against the server. {server_msg}")
    return findings


def run_case(base_url: str, api_key: str, case: ProbeCase, max_rounds: int = 5) -> List[Tuple[ProbeCase, ProbeResult]]:
    """
    A server reports only the first rejected parameter. After a parameter-level rejection,
    drop that parameter and probe again, so one run surfaces every incompatible parameter.
    """
    rounds: List[Tuple[ProbeCase, ProbeResult]] = []
    current = case
    for _ in range(max_rounds):
        result = send_probe(base_url, api_key, current)
        rounds.append((current, result))
        if result.status != "REJECTED" or not result.param or result.param not in current.params:
            break
        remaining = {k: v for k, v in current.params.items() if k != result.param}
        current = ProbeCase(current.model, remaining,
                            [p for p in current.literal_params if p != result.param],
                            current.skipped_params, current.call_sites)
    return rounds
