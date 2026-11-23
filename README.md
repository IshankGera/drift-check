# drift-check 🔎

[![PyPI version](https://img.shields.io/pypi/v/py-drift-check)](https://pypi.org/project/py-drift-check/)
[![Monthly downloads](https://img.shields.io/pypi/dm/py-drift-check)](https://pypistats.org/packages/py-drift-check)
[![Total downloads](https://static.pepy.tech/badge/py-drift-check)](https://pepy.tech/projects/py-drift-check)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Know which lines of your Python code a dependency upgrade will break, before you upgrade.**

Changelogs list every change in a release. Linters and type checkers only see the versions you already have installed. `drift-check` answers one question: *"If I upgrade this package right now, what in **my** code breaks?"*

It builds a throwaway sandbox with the new version, compares it with the code you actually use, and reports only what affects you. New features and changes to things you never call are ignored.

## 📦 Installation

```bash
uv tool install py-drift-check    # recommended
pip install py-drift-check        # or with pip
```

`uv` is installed automatically and is used to build the sandboxes.

## 🧭 Four checks, from fastest to deepest

| Command | What it checks | Needs |
|---|---|---|
| `drift-check upgrade` | Your calls against the new version's API: removed methods and imports, removed/renamed keyword arguments, new required arguments, positional-argument changes, deprecations | Nothing extra. Fast, never runs your code |
| `drift-check typecheck` | Runs a type checker before and after the upgrade and reports **only new** type errors. Catches cases the call check cannot see, such as methods on your own subclasses of the package's classes | Your project's dependencies (copied into a sandbox) |
| `drift-check test` | Runs your tests with the new version and compares with today: newly failing tests, new deprecation warnings, and calls into the package that **no test runs** | `pytest` tests |
| `drift-check probe` | Optional add-on for LLM apps: checks `chat.completions.create()` calls against the provider's server, or against built-in rules with `--offline` | An API key, except with `--offline` |

All commands share the same exit codes, so they drop straight into CI:

| Code | Meaning |
|---|---|
| `0` | Safe (warnings may be present) |
| `1` | Breaking changes found |
| `2` | Setup error: package not declared/installed, version not found, invalid wheel, or sandbox build failed |

## 💻 Quickstart

Run from your project folder, with its virtual environment present (`.venv`, `venv`, `env`, or an activated one). To point at a different interpreter, set `DRIFT_CHECK_PYTHON=/path/to/python`.

```bash
drift-check upgrade openai --to 2.0.0
drift-check typecheck pydantic --to 2.9.2
drift-check test openai --to 2.0.0 --pytest-args "tests/unit"
drift-check probe --offline
```

Test against an internal or unpublished package with a local wheel:

```bash
drift-check upgrade my-sdk --to 2.0.0 --local-wheel ./dist/my_sdk-2.0.0-py3-none-any.whl
```

The baseline (your current version) is read from `requirements.txt`, `pyproject.toml` (`[project]` dependencies, optional dependencies and `[dependency-groups]`), or `uv.lock`, and must match what is installed.

## 🔬 `upgrade`: how it works

1. **Baseline check:** confirms the installed version matches your declared dependency.
2. **AST scan:** reads your code without running it and records every call into the package: module functions, `from pkg import fn` imports, aliased and deep imports, constructors, and client objects (`client = OpenAI()`, `self.client`, clients imported from another module, and chained calls).
3. **Sandbox:** installs the target version into a temporary virtual environment with `uv`.
4. **Signature check:** inspects the real signatures in the sandbox, following type hints through chains like `client.chat.completions.create`, including lazily imported types.
5. **Compare with today:** argument problems are reported only if your current version accepts the call and the new one does not, so nothing that is already broken (or misread) shows up as noise.

```text
│ ❌ ERROR │ app.py:1 │ mock_sdk │ Import 'old_method' from 'mock_sdk' no longer exists in the target version.
│ ❌ ERROR │ app.py:4 │ mock_sdk │ Required parameter 'region' is not passed (it is required in the target version).
│ ⚠ WARN   │ app.py:7 │ pydantic │ Deprecated in the target version: `parse_obj_as` is deprecated. Use
│          │          │          │ `pydantic.TypeAdapter.validate_python` instead.
```

## 🤖 CI: GitHub Action

`action.yml` in this repository is a ready-made GitHub Action. It checks one or more `name==version` upgrades, fails the job on breaking changes, and shows findings as inline annotations on the pull request.

```yaml
- uses: IshankGera/drift-check@v0.4.0
  with:
    updates: |
      openai==2.0.0
      pydantic==2.9.2
```

[`examples/dependabot-drift-check.yml`](examples/dependabot-drift-check.yml) is a complete workflow for Dependabot pull requests. It installs the **old** versions as the baseline and checks your code against the **new** ones.

Every command also accepts `--json-report report.json`. Inside GitHub Actions, findings are printed as `::error` / `::warning` annotations automatically.

## 🌐 `probe`: server-side checks for LLM apps (optional)

The Python SDK can accept a parameter that the **server** rejects at runtime. For example, OpenAI's reasoning models reject `max_tokens` and ask for `max_completion_tokens`, while the SDK still accepts both. No static check can see this, so `probe` offers two modes:

```bash
drift-check probe --offline                         # built-in rules, no key, nothing sent
drift-check probe --offline --rules my_rules.json   # add your own rules
drift-check probe --dry-run                         # show what a live probe would send
export OPENAI_API_KEY=sk-...
drift-check probe                                   # live: one tiny request per unique model + parameters
drift-check probe --base-url https://api.groq.com/openai/v1 --api-key-env GROQ_API_KEY
```

**Live mode:** requests go directly from your machine or CI runner to the provider, with your own key. Nothing is sent unless you run `probe` without `--offline` or `--dry-run`. The key is never printed. `--max-requests` (default 20) caps the number of requests. In CI, a project-scoped key with a small spending limit is recommended.

**Probe limits:** chat completions only, OpenAI-style `Bearer` auth (Azure OpenAI is not supported yet), and one base URL per run. Offline rules only know what is in the rules file.

## ⚠️ Known limitations

This tool is an alpha.

* **Dynamic code:** calls built with `getattr`, dictionaries passed as `**kwargs`, or `*args` cannot be checked statically (`typecheck` and `test` help here).
* **Objects passed around:** clients passed in as function arguments or returned from functions are not traced by `upgrade` (`typecheck` covers them).
* **Behaviour changes:** a function that keeps its signature but behaves differently is only caught by `test`.
* **Deprecations:** detected from `@deprecated` markers and unconditional `warnings.warn(..., DeprecationWarning)` calls. Warnings raised only for some arguments are not reported by `upgrade` (they show up in `test`).
* **Import names:** the package name must match its import name (e.g. `openai`, `stripe`). Packages like `pyyaml` → `yaml` are not mapped yet.

## License

MIT
