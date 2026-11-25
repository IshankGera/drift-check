"""
Map a distribution (what you `pip install`) to the module names it provides (what you `import`).

    pyyaml          -> yaml, _yaml
    pillow          -> PIL
    beautifulsoup4  -> bs4
    scikit-learn    -> sklearn

Read from the installed package's own metadata (top_level.txt, or the files it installed),
in the PROJECT's interpreter, so it works for any package without a hard-coded list.
"""
import json
import subprocess
from typing import List

_SCRIPT = r'''
import json, sys
import importlib.metadata as md

names = set()
try:
    dist = md.distribution(sys.argv[1])
except Exception:
    print("[]")
    raise SystemExit(0)

top = dist.read_text("top_level.txt")
if top:
    names.update(line.strip().replace("/", ".") for line in top.splitlines() if line.strip())

if not names:
    for f in dist.files or []:
        parts = f.parts
        if not parts or parts[0] in ("..", "__pycache__"):
            continue
        first = parts[0]
        if first.endswith((".dist-info", ".egg-info", ".data", ".pth")):
            continue
        if len(parts) == 1:
            if first.endswith(".py"):
                names.add(first[:-3])
            elif first.endswith((".so", ".pyd")):
                names.add(first.split(".")[0])
        else:
            names.add(first)

print(json.dumps(sorted(n for n in names if n.isidentifier())))
'''


def fallback_name(distribution: str) -> str:
    """'my-pkg' -> 'my_pkg': the usual import name when nothing better is known."""
    return distribution.strip().lower().replace("-", "_").replace(".", "_")


def import_names(python_exec: str, distribution: str) -> List[str]:
    """Top-level import names provided by `distribution`, as installed for `python_exec`."""
    names: List[str] = []
    try:
        proc = subprocess.run([python_exec, "-c", _SCRIPT, distribution], capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            names = json.loads(proc.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        names = []
    # Metadata missing (or package not installed): fall back to the usual name
    return names or [fallback_name(distribution)]
