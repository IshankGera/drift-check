# drift-check 🔎

**Stop guessing if a dependency upgrade, or an LLM provider's server, will break your Python code.**

Upgrading Python packages in a large or legacy codebase often feels like a gamble. Will moving a dependency to a new major version break everything? `drift-check` is an experimental CLI tool that analyzes your code against a **future** dependency version *before* you actually upgrade.

Instead of generic linting, `drift-check` spins up an isolated sandbox, extracts the new API signatures, and reports exactly which lines of your codebase will break if you apply the upgrade.

## 🚀 How It Works

1. **Baseline Verification:** Parses your `requirements.txt` and active `.venv` to confirm the installed version matches what you declared.
2. **PyPI Resolution:** Validates and normalizes your target version (e.g., resolving `15` to `15.0.0` automatically).
3. **Targeted Sandboxing:** Uses `uv` (installed automatically as a dependency) to silently spin up a temporary virtual environment containing the future target package.
4. **AST Intersection Analysis:** Reads your Python files without executing them, identifying only the API calls your code actually makes, and cross-references them against the target sandbox.
5. **CI/CD Ready:** Exits with code `1` if breaking changes are found, blocking bad dependency updates from merging.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | No breaking changes (warnings may be present) |
| `1` | Breaking changes found: a used method or parameter no longer exists |
| `2` | Setup error: package not declared/installed, version not found, invalid wheel, or sandbox build failed |

## 📦 Installation

Install globally via `uv` (recommended):
```bash
uv tool install py-drift-check

```

Or via pip:

```bash
pip install py-drift-check

```

## 💻 Quickstart

Navigate to your Python project directory and run the `upgrade` command:

```bash
# Check if upgrading 'openai' to v1.52.0 will break your current code
drift-check upgrade openai --to 1.52.0

```

### Testing Internal or Unpublished Packages

If you are developing an internal SDK or a private package, you can bypass PyPI entirely and analyze your code against a local `.whl` file:

```bash
drift-check upgrade mock-sdk --to 2.0.0 --local-wheel ./dist/mock_sdk-2.0.0-py3-none-any.whl

```

## 🌐 Server Probe for OpenAI-compatible APIs

The Python SDK can accept a parameter that the **server** rejects at runtime. For example, OpenAI's reasoning models reject `max_tokens` and ask for `max_completion_tokens`, while the SDK still accepts both. `drift-check upgrade` cannot see this, so there is a separate, opt-in command:

```bash
export OPENAI_API_KEY=sk-...
drift-check probe                                   # uses model="..." literals from your code
drift-check probe --model o3-mini                   # when the model comes from config/env
drift-check probe --base-url https://api.groq.com/openai/v1 --api-key-env GROQ_API_KEY
drift-check probe --dry-run                         # show what would be sent, send nothing
```

How it works:

1. Finds every `chat.completions.create(...)` call (including `self.client...`, clients imported from other modules, and chained calls).
2. Groups them by model and parameters, and sends **one tiny request per unique combination** ("Reply with the single word: ok"), using your values where they are literals in the code and safe placeholders otherwise.
3. Turns server rejections into findings. If a server rejects one parameter, the probe drops it and retries, so one run lists every incompatible parameter.

```text
│ ❌ ERROR │ llm.py:7 │ openai │ Model 'o3-mini': Server (400): Unsupported parameter: 'max_tokens'
│          │          │        │ is not supported with this model. Use 'max_completion_tokens' instead.
```

**Privacy and cost:** requests go directly from your machine or CI runner to the provider, with your own key. Nothing is sent anywhere else, and nothing is sent unless you run `probe`. Each request is a few tokens, billed to your provider account. The key is never printed. `--max-requests` (default 20) caps the number of requests.

**Probe limits:** chat completions only, OpenAI-style `Bearer` auth (Azure OpenAI is not supported yet), and one base URL per run. Parameters whose value is neither a literal nor in the placeholder list (e.g. `tools`) are reported as "not probed".

## 📊 Example Output

`drift-check` intentionally ignores new features in the target version to filter out noise, strictly reporting **actionable breaking changes**:

```text
Starting Upgrade Analysis for openai -> 1.52.0...
Verifying environment baseline...
✓ Baseline established: openai == 1.45.0

Resolving target version '1.52.0' on PyPI...
✓ Target resolved: 1.52.0

Building isolated sandbox for openai==1.52.0...
Scanning codebase for API calls...

Drift Check Results:
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Status     ┃ Location      ┃ Package  ┃ Message                                                 ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ ❌ ERROR   │ service.py:42 │ openai   │ Parameter 'max_tokens' is no longer supported.          │
│ ❌ ERROR   │ test_api.py:8 │ openai   │ Method 'old_method' no longer exists in target version. │
│ ⚠ WARN     │ router.py:12  │ openai   │ Target API uses dynamic **kwargs. (Confidence: LOW)     │
└────────────┴───────────────┴──────────┴─────────────────────────────────────────────────────────┘

Build Failed: Incompatible API calls detected in target version.

```

## ⚠️ Features & Current Limitations

This tool is in active development.

**Supported Features:**

* Strict environment baseline enforcement.
* PyPI fallback resolution prompts (in CI or any non-interactive shell, it fails with exit code `2` and suggests the closest release instead of prompting).
* Unpacking modern `TypedDict` and `Unpack` type-hints for strict kwargs evaluation.
* Seamless `uv` sandbox management.
* Local `.whl` file bypass support (the wheel's name and version are validated against `--to`).
* Client-object SDKs (e.g. `client = OpenAI(); client.chat.completions.create(...)`) are resolved by following property and attribute type hints. When a hop has no type hint, the call is reported as a low-confidence warning instead of an error.

**Known Limitations:**

* **Dependency Files:** Currently only supports reading baselines from `requirements.txt` (support for `pyproject.toml` and `uv.lock` is planned).
* **Variable Inference:** Clients are traced when created in the same file, stored on `self`, imported from another module (`from app.llm import client`), or chained (`OpenAI().chat...`). Clients passed in as function arguments or returned from functions are not traced yet.
* **Dynamic Kwargs:** Cannot statically verify dynamic arguments passed at the call site (e.g., `client.create(**my_dict)`).
* **Positional and required arguments:** Only keyword arguments you pass are checked. A positional argument count change, or a parameter that became required, is not detected yet.
* **Import names:** The package name must match its import name (e.g. `openai`, `stripe`). Packages like `pyyaml` → `yaml` are not mapped yet.

## License

MIT

