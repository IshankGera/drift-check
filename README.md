```markdown
# drift-check 

**drift-check** is an experimental CLI tool that detects API signature mismatches between your Python codebase and your installed dependencies. 

If you've ever upgraded a package (like OpenAI, FastAPI, or Pydantic) only to have your app crash at runtime because a parameter was renamed or removed, `drift-check` is built to catch that *before* you run your code.

## How It Works
Instead of executing your application, `drift-check` uses an AST (Abstract Syntax Tree) to read your function calls, spins up a secure subprocess inside your project's `.venv`, and dynamically validates your arguments against the actual signatures of your installed packages.

## Installation

Install globally via `uv` (recommended):
```bash
uv tool install drift-check

```

Or via pip:

```bash
pip install drift-check

```

## Quickstart

Navigate to any Python project and run:

```bash
drift-check run .

```

## Example Output

```text
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Status     ┃ Location           ┃ Package  ┃ Message                                             ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ ❌ ERROR   │ main.py:19         │ openai   │ Unknown parameter passed: 'model_name'              │
│ ⚠ WARN     │ database.py:42     │ asyncpg  │ Unable to verify forwarded keyword argument         │
│ ✓ OK       │ router.py:12       │ httpx    │ All parameters valid.                               │
└────────────┴────────────────────┴──────────┴─────────────────────────────────────────────────────┘

```

## Features & Current Limitations (v0.1.0)

This tool is in active development. Please report edge cases!

**Supported:**

*  Dynamic runtime signature inspection
*  Automatic `.venv` detection
*  Unpacking modern `TypedDict` type-hints (e.g., OpenAI SDK)
*  Standard library filtering

**Known Limitations (v0.1.0):**

*  **Instantiated Variables:** Currently struggles to trace methods called on class instances (e.g., `app.post` in FastAPI).
*  **Dynamic Kwargs:** Cannot statically verify parameters passed dynamically via `**kwargs` at the call site.

## License

MIT

```
```