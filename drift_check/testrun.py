"""
`drift-check test`: run the project's tests with the upgraded package, and compare with today.

Reports:
  - tests that pass today but fail after the upgrade              (ERROR)
  - deprecation warnings that appear only after the upgrade      (WARNING)
  - calls into the package that no test executes (coverage risk) (WARNING)
"""
import json
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from drift_check.models import CallSite, Finding

# Loaded into pytest with `-p`; records every warning pytest sees during the run.
WARNING_PLUGIN = '''
import json, os

_records = []


def pytest_warning_recorded(warning_message, when, nodeid, location):
    w = warning_message
    category = getattr(w, "category", None)
    _records.append({
        "category": getattr(category, "__name__", str(category)),
        "message": str(w.message),
        "filename": w.filename,
        "lineno": w.lineno,
    })


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("DRIFT_CHECK_WARNINGS_OUT")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_records, f)
'''
PLUGIN_MODULE = "drift_check_pytest_warnings"

WARNING_FILTERS = ["-W", "default::DeprecationWarning",
                   "-W", "default::PendingDeprecationWarning",
                   "-W", "default::FutureWarning"]

Warning_ = Tuple[str, int, str]  # (file, line, message)


# ---------------------------------------------------------------- running

class TestRun:
    def __init__(self, failed: Set[str], total: int, warnings: List[dict], ran: bool, output: str):
        self.failed = failed
        self.total = total
        self.warnings = warnings
        self.ran = ran
        self.output = output


def has_pytest(python_exec: str) -> bool:
    proc = subprocess.run([python_exec, "-c", "import pytest"], capture_output=True)
    return proc.returncode == 0


def run_pytest(python_exec: str, root: Path, pythonpath: str, pytest_args: List[str],
               coverage_file: Optional[Path] = None) -> TestRun:
    with tempfile.TemporaryDirectory(prefix="drift-check-pytest-") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / f"{PLUGIN_MODULE}.py").write_text(WARNING_PLUGIN, encoding="utf-8")
        junit = tmp_path / "junit.xml"
        warnings_out = tmp_path / "warnings.json"

        pytest_cmd = ["-m", "pytest", "-p", PLUGIN_MODULE, "-p", "no:cacheprovider", "-q",
                      f"--junitxml={junit}", *WARNING_FILTERS, *pytest_args]
        if coverage_file is not None:
            cmd = [python_exec, "-m", "coverage", "run", f"--data-file={coverage_file}", *pytest_cmd]
        else:
            cmd = [python_exec, *pytest_cmd]

        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), pythonpath])
        env["DRIFT_CHECK_WARNINGS_OUT"] = str(warnings_out)
        proc = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True)

        failed, total = parse_junit(junit) if junit.exists() else (set(), 0)
        warnings = json.loads(warnings_out.read_text(encoding="utf-8")) if warnings_out.exists() else []
        return TestRun(failed, total, warnings, ran=junit.exists(), output=(proc.stdout + proc.stderr)[-3000:])


def coverage_json(python_exec: str, data_file: Path, root: Path) -> Dict[str, Set[int]]:
    """{real file path: executed line numbers} from a coverage data file."""
    out = data_file.with_suffix(".json")
    proc = subprocess.run([python_exec, "-m", "coverage", "json", f"--data-file={data_file}",
                           "-o", str(out), "--ignore-errors"], cwd=root, capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists():
        return {}
    data = json.loads(out.read_text(encoding="utf-8"))
    executed = {}
    for file, info in data.get("files", {}).items():
        path = file if os.path.isabs(file) else os.path.join(root, file)
        executed[os.path.realpath(path)] = set(info.get("executed_lines", []))
    return executed


# ---------------------------------------------------------------- analysis (pure functions)

def parse_junit(path: Path) -> Tuple[Set[str], int]:
    """(ids of failed/errored tests, number of tests) from a pytest JUnit XML report."""
    failed, total = set(), 0
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        total += 1
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        if case.find("failure") is not None or case.find("error") is not None:
            failed.add(test_id)
    return failed, total


def readable_test_id(test_id: str, root: Path) -> str:
    """'tests.test_app.TestX::test_a' -> 'tests/test_app.py::TestX::test_a' when the file exists."""
    classname, _, name = test_id.partition("::")
    parts = classname.split(".")
    for i in range(len(parts), 0, -1):
        candidate = Path(*parts[:i]).with_suffix(".py")
        if (root / candidate).is_file():
            return "::".join([candidate.as_posix(), *parts[i:], name])
    return test_id


def project_deprecations(records: Iterable[dict], root: Path) -> Set[Warning_]:
    """Deprecation warnings whose reported location is in the project's own code."""
    root_real = os.path.realpath(root)
    found: Set[Warning_] = set()
    for r in records:
        category = r.get("category", "")
        if "Deprecat" not in category and category != "FutureWarning":
            continue
        file = os.path.realpath(r.get("filename", ""))
        if not file.startswith(root_real + os.sep):
            continue
        if any(part in (".venv", "venv", "site-packages") for part in Path(file).parts):
            continue
        found.add((file, int(r.get("lineno", 0)), " ".join(str(r.get("message", "")).split())))
    return found


def uncovered_call_sites(call_sites: Iterable[CallSite], executed: Dict[str, Set[int]]) -> List[CallSite]:
    """Calls into the package on lines that no test executed."""
    result = []
    for call in call_sites:
        if call.kind == "import":
            continue
        lines = executed.get(os.path.realpath(call.file_path))
        if lines is None or call.line_number not in lines:
            result.append(call)
    return result


def build_findings(package: str, baseline: Optional[TestRun], target: TestRun, root: Path,
                   call_sites: List[CallSite], executed: Optional[Dict[str, Set[int]]]) -> List[Finding]:
    findings: List[Finding] = []

    # 1. Tests that break because of the upgrade
    broken = target.failed - (baseline.failed if baseline else set())
    for test_id in sorted(broken):
        note = "" if baseline else " (no baseline run, so it may also fail today)"
        findings.append(Finding("ERROR", readable_test_id(test_id, root), 0, package, "test",
                                f"Test fails with the upgraded version{note}."))

    # 2. Deprecation warnings that only appear after the upgrade
    new_warnings = project_deprecations(target.warnings, root)
    if baseline:
        new_warnings -= project_deprecations(baseline.warnings, root)
    for file, line, message in sorted(new_warnings):
        if len(message) > 200:
            message = message[:197] + "..."
        findings.append(Finding("WARNING", file, line, package, "deprecation",
                                f"Deprecation warning while running tests: {message}"))

    # 3. Coverage risk: calls into the package that no test runs
    if executed is not None:
        for call in uncovered_call_sites(call_sites, executed):
            findings.append(Finding("WARNING", call.file_path, call.line_number, package, call.method_name,
                                    f"'{call.method_name}' is never run by your tests, so a break here "
                                    f"would not be caught. (Coverage risk)", kind=call.kind))
    return findings
