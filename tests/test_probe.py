"""`drift-check probe`: live-server checks, against a local fake OpenAI server."""
import os
import textwrap

import pytest
from typer.testing import CliRunner

from drift_check import probe
from drift_check.cli import app
from drift_check.models import CallSite

from tests import fake_openai


@pytest.fixture
def server():
    srv, url = fake_openai.start()
    fake_openai.Handler.requests.clear()
    yield url
    srv.shutdown()


def chat(model=None, **kwargs):
    literals = dict(kwargs)
    if model is not None:
        literals["model"] = model
    names = (["model"] if model is not None else []) + list(kwargs)
    return CallSite("app.py", 1, "openai", "OpenAI.chat.completions.create", names, False, literals)


# ---------- grouping ----------

def test_identical_calls_share_one_request():
    cases, _ = probe.build_cases([chat("gpt-4o", temperature=0.2), chat("gpt-4o", temperature=0.2)])
    assert len(cases) == 1 and len(cases[0].call_sites) == 2


def test_non_literal_model_is_reported_not_guessed():
    call = CallSite("app.py", 3, "openai", "OpenAI.chat.completions.create", ["model"], False, {})
    cases, unprobeable = probe.build_cases([call])
    assert cases == [] and "--model" in unprobeable[0][1]
    cases, _ = probe.build_cases([call], model_override="gpt-4o")
    assert cases[0].model == "gpt-4o"


def test_placeholders_and_skips():
    call = CallSite("a.py", 1, "openai", "OpenAI.chat.completions.create",
                    ["model", "max_tokens", "tools", "stream"], False, {"model": "m"})
    (case,), _ = probe.build_cases([call])
    assert case.params == {"max_tokens": 16}          # placeholder used, stream excluded
    assert case.skipped_params == ["tools"]


def test_only_chat_calls_are_probed():
    other = CallSite("a.py", 1, "openai", "OpenAI.embeddings.create", ["model"], False, {"model": "e"})
    assert probe.build_cases([other]) == ([], [])


# ---------- server classification ----------

def run(server_url, call, key=fake_openai.API_KEY):
    (case,), _ = probe.build_cases([call])
    out = []
    for sent, result in probe.run_case(server_url, key, case):
        out.extend((f.severity, f.message) for f in probe.findings_for(sent, result))
    return out


def test_accepted_call_is_silent(server):
    assert run(server, chat("gpt-4o", max_tokens=50)) == []


def test_max_tokens_on_reasoning_model_is_error(server):
    [(sev, msg)] = run(server, chat("o3-mini", max_tokens=50))
    assert sev == "ERROR" and "max_completion_tokens" in msg


def test_every_rejected_parameter_is_reported(server):
    findings = run(server, chat("o3-mini", max_tokens=50, top_p=0.9))
    assert [s for s, _ in findings] == ["ERROR", "ERROR"]
    assert "max_tokens" in findings[0][1] and "top_p" in findings[1][1]


def test_rejected_literal_value_is_error(server):
    [(sev, _)] = run(server, chat("o3-mini", temperature=0.2))
    assert sev == "ERROR"


def test_rejected_placeholder_value_is_only_warning():
    case = probe.ProbeCase("m", {"temperature": 1}, [], [], [chat("m")])
    result = probe.ProbeResult("REJECTED", 400, "bad value", "temperature", "unsupported_value")
    [f] = probe.findings_for(case, result)
    assert f.severity == "WARNING"


def test_retired_model_is_error(server):
    [(sev, msg)] = run(server, chat("retired-model"))
    assert sev == "ERROR" and "does not exist" in msg


def test_rate_limit_is_inconclusive(server):
    [(sev, _)] = run(server, chat("busy-model"))
    assert sev == "WARNING"


def test_bad_key_is_auth_error(server):
    (case,), _ = probe.build_cases([chat("gpt-4o")])
    assert probe.send_probe(server, "wrong", case).status == "AUTH_ERROR"


def test_unreachable_server_is_network_error():
    (case,), _ = probe.build_cases([chat("gpt-4o")])
    assert probe.send_probe("http://127.0.0.1:9/v1", "k", case, timeout=2).status == "NETWORK_ERROR"


# ---------- CLI ----------

PROJECT = """
    from openai import OpenAI

    class Service:
        def __init__(self):
            self.client = OpenAI()

        def ask(self):
            return self.client.chat.completions.create(
                model="o3-mini", messages=[{"role": "user", "content": "hi"}], max_tokens=200
            )
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "service.py").write_text(textwrap.dedent(PROJECT))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_probe_fails_build_on_rejection(project, server, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", fake_openai.API_KEY)
    result = CliRunner().invoke(app, ["probe", "--base-url", server])
    assert result.exit_code == 1, result.output
    assert "max_completion_tokens" in result.output
    assert fake_openai.API_KEY not in result.output


def test_cli_probe_passes_when_fixed(project, server, monkeypatch):
    (project / "service.py").write_text(textwrap.dedent(PROJECT).replace("max_tokens", "max_completion_tokens"))
    monkeypatch.setenv("OPENAI_API_KEY", fake_openai.API_KEY)
    result = CliRunner().invoke(app, ["probe", "--base-url", server])
    assert result.exit_code == 0, result.output


def test_cli_dry_run_sends_nothing(project, server, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["probe", "--base-url", server, "--dry-run"])
    assert result.exit_code == 0 and "o3-mini" in result.output
    assert fake_openai.Handler.requests == []


def test_cli_missing_key_is_setup_error(project, server, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["probe", "--base-url", server])
    assert result.exit_code == 2 and "OPENAI_API_KEY" in result.output


def test_cli_bad_key_is_setup_error(project, server, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-key")
    result = CliRunner().invoke(app, ["probe", "--base-url", server])
    assert result.exit_code == 2 and "wrong-key" not in result.output
