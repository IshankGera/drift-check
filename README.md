```markdown
# drift-check 🔎

**Stop guessing if a dependency upgrade will break your Python code.**

Upgrading Python packages in a large or legacy codebase often feels like a gamble. Will moving a dependency to a new major version break everything? `drift-check` is an experimental CLI tool that analyzes your code against a **future** dependency version *before* you actually upgrade.

Instead of generic linting, `drift-check` spins up an isolated sandbox, extracts the new API signatures, and reports exactly which lines of your codebase will break if you apply the upgrade.

## 🚀 How It Works

1. **Baseline Verification:** Parses your `requirements.txt` and active `.venv` to mathematically prove your current environment is stable.
2. **PyPI Resolution:** Validates and normalizes your target version (e.g., resolving `15` to `15.0.0` automatically).
3. **Targeted Sandboxing:** Uses `uv` to silently spin up a temporary virtual environment containing the future target package.
4. **AST Intersection Analysis:** Reads your Python files without executing them, identifying only the API calls your code actually makes, and cross-references them against the target sandbox.
5. **CI/CD Ready:** Exits with code `1` if breaking changes are found, blocking bad dependency updates from merging.

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

## ⚠️ Features & Current Limitations (v0.2.0)

This tool is in active development.

**Supported Features:**

* Strict environment baseline enforcement.
* PyPI fallback resolution prompts.
* Unpacking modern `TypedDict` and `Unpack` type-hints for strict kwargs evaluation.
* Seamless `uv` sandbox management.
* Local `.whl` file bypass support.

**Known Limitations:**

* **Dependency Files:** Currently only supports reading baselines from `requirements.txt` (support for `pyproject.toml` and `uv.lock` is planned).
* **Variable Inference:** Currently struggles to trace methods called on complex instantiated variables across multiple files (e.g., `app.post` in FastAPI frameworks).
* **Dynamic Kwargs:** Cannot statically verify dynamic arguments passed at the call site (e.g., `client.create(**my_dict)`).

## License

MIT

```