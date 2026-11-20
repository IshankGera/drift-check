import os
from pathlib import Path

from drift_check import testrun
from drift_check.fullenv import ProjectSandbox
from drift_check.models import CallSite

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3">
  <testcase classname="tests.test_app" name="test_ok"/>
  <testcase classname="tests.test_app" name="test_bad"><failure message="boom"/></testcase>
  <testcase classname="tests.test_app.TestX" name="test_err"><error message="setup"/></testcase>
</testsuite></testsuites>"""


def test_parse_junit(tmp_path):
    path = tmp_path / "j.xml"
    path.write_text(JUNIT)
    failed, total = testrun.parse_junit(path)
    assert total == 3
    assert failed == {"tests.test_app::test_bad", "tests.test_app.TestX::test_err"}


def test_readable_test_id(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("")
    assert testrun.readable_test_id("tests.test_app.TestX::test_a", tmp_path) == "tests/test_app.py::TestX::test_a"
    assert testrun.readable_test_id("unknown.mod::t", tmp_path) == "unknown.mod::t"


def run(failed=(), warnings=()):
    return testrun.TestRun(set(failed), 2, list(warnings), True, "")


def test_only_newly_failing_tests_are_errors(tmp_path):
    findings = testrun.build_findings("sdk", run(["t::a"]), run(["t::a", "t::b"]), tmp_path, [], None)
    assert [(f.severity, f.file_path) for f in findings] == [("ERROR", "t::b")]


def test_deprecations_only_new_and_only_in_project(tmp_path):
    app = str(tmp_path / "app.py")
    lib = str(tmp_path / ".venv" / "lib" / "x.py")
    old = {"category": "DeprecationWarning", "message": "old one", "filename": app, "lineno": 2}
    new = {"category": "PydanticDeprecatedSince20", "message": "new one", "filename": app, "lineno": 5}
    inside_lib = {"category": "DeprecationWarning", "message": "lib", "filename": lib, "lineno": 1}
    other = {"category": "UserWarning", "message": "not deprecation", "filename": app, "lineno": 7}
    findings = testrun.build_findings("sdk", run(warnings=[old]), run(warnings=[old, new, inside_lib, other]),
                                      tmp_path, [], None)
    assert [(f.line_number, f.message) for f in findings] == [(5, "Deprecation warning while running tests: new one")]


def test_coverage_risk(tmp_path):
    app = str(tmp_path / "app.py")
    calls = [
        CallSite(app, 3, "sdk", "covered", [], False),
        CallSite(app, 8, "sdk", "uncovered", [], False),
        CallSite(app, 1, "sdk", "imported", [], False, kind="import"),
        CallSite(str(tmp_path / "never_loaded.py"), 2, "sdk", "other", [], False),
    ]
    executed = {os.path.realpath(app): {1, 2, 3}}
    findings = testrun.build_findings("sdk", run(), run(), tmp_path, calls, executed)
    assert sorted(f.method_name for f in findings) == ["other", "uncovered"]
    assert all("Coverage risk" in f.message for f in findings)


def test_no_baseline_is_flagged_in_message(tmp_path):
    [f] = testrun.build_findings("sdk", None, run(["t::a"]), tmp_path, [], None)
    assert "no baseline run" in f.message


def test_sandbox_freeze_filter(tmp_path, monkeypatch):
    monkeypatch.setattr("drift_check.fullenv.find_uv", lambda: "uv")
    sb = ProjectSandbox("python", "OpenAI", "2.0.0")
    assert not sb._keep("openai==1.40.0")
    assert not sb._keep("-e file:///proj")
    assert not sb._keep("# comment")
    assert sb._keep("httpx==0.27.0")
    assert sb._keep("mypkg @ file:///wheels/mypkg.whl")
    sb.cleanup()
