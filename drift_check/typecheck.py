"""
`drift-check typecheck`: run a type checker against the current and the upgraded environment,
and report only the errors the upgrade introduces.

The code is identical in both runs; only the installed packages differ. So every new error is
caused by the upgrade, and errors that already exist today are filtered out (no noise).
Uses basedpyright, which ships as a regular Python package (no Node.js or npm download).
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from drift_check.models import Finding

CHECKER_PACKAGE = "basedpyright"

# Folders a project never wants type-checked
EXCLUDE_DIRS = [".venv", "venv", "env", ".git", "node_modules", "__pycache__", "build", "dist", ".tox", ".nox"]

Diagnostic = Tuple[str, int, str, str]  # (file, line, rule, message)


class TypeCheckError(Exception):
    """The type checker could not be run."""


def write_config(root: Path, directory: Path) -> Path:
    """
    The same config is used for both runs, so results are comparable whatever the
    project's own type-checker settings are.
    """
    config = {
        "include": [str(root)],
        "exclude": [str(root / d) for d in EXCLUDE_DIRS] + ["**/node_modules", "**/__pycache__"],
        "typeCheckingMode": "standard",
        "reportMissingModuleSource": "none",
    }
    path = directory / "pyrightconfig.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def run_checker(uv: str, python_exec: str, config: Path, root: Path) -> Dict:
    cmd = [uv, "tool", "run", "--quiet", "--from", CHECKER_PACKAGE, CHECKER_PACKAGE,
           "--outputjson", "--pythonpath", python_exec, "--project", str(config)]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    # Exit code 1 just means "errors were found"; anything unparsable is a real failure
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        detail = (proc.stderr or proc.stdout or "").strip()[:1500]
        raise TypeCheckError(f"{CHECKER_PACKAGE} failed (exit {proc.returncode}).\n{detail}") from None


def errors_of(report: Dict, root: Path) -> Set[Diagnostic]:
    found: Set[Diagnostic] = set()
    for d in report.get("generalDiagnostics", []):
        if d.get("severity") != "error":
            continue
        file = os.path.realpath(d.get("file", ""))
        line = d.get("range", {}).get("start", {}).get("line", 0) + 1
        found.add((file, line, d.get("rule", ""), " ".join(d.get("message", "").split())))
    return found


def new_errors(baseline: Set[Diagnostic], target: Set[Diagnostic]) -> List[Diagnostic]:
    """Errors present only after the upgrade, ordered by file and line."""
    return sorted(target - baseline)


def to_findings(diagnostics: List[Diagnostic], package: str) -> List[Finding]:
    findings = []
    for file, line, rule, message in diagnostics:
        label = f" [{rule}]" if rule else ""
        if len(message) > 300:
            message = message[:297] + "..."
        findings.append(Finding("ERROR", file, line, package, rule or "type-error",
                                f"New type error after the upgrade{label}: {message}"))
    return findings


def check(uv: str, project_python: str, sandbox_python: str, root: Path, package: str,
          log=None) -> List[Finding]:
    with tempfile.TemporaryDirectory(prefix="drift-check-types-") as tmp:
        config = write_config(root, Path(tmp))
        if log:
            log("Type-checking with your current versions...")
        baseline = errors_of(run_checker(uv, project_python, config, root), root)
        if log:
            log("Type-checking with the upgraded versions...")
        target = errors_of(run_checker(uv, sandbox_python, config, root), root)
    return to_findings(new_errors(baseline, target), package)
