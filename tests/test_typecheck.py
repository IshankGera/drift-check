import json
import os
from pathlib import Path

from drift_check import typecheck


def report(*diags):
    return {"generalDiagnostics": [
        {"file": f, "severity": sev, "message": msg, "rule": rule,
         "range": {"start": {"line": line - 1, "character": 0}}}
        for f, line, sev, rule, msg in diags
    ]}


def test_only_new_errors_are_reported(tmp_path):
    app = str(tmp_path / "app.py")
    before = typecheck.errors_of(report(
        (app, 3, "error", "reportX", "already broken today"),
        (app, 4, "warning", "reportY", "just a warning"),
    ), tmp_path)
    after = typecheck.errors_of(report(
        (app, 3, "error", "reportX", "already broken today"),
        (app, 9, "error", "reportAttributeAccessIssue", 'Cannot access attribute "save"'),
    ), tmp_path)
    [new] = typecheck.new_errors(before, after)
    assert new[1:] == (9, "reportAttributeAccessIssue", 'Cannot access attribute "save"')

    [finding] = typecheck.to_findings([new], "mysdk")
    assert finding.severity == "ERROR" and finding.line_number == 9
    assert "[reportAttributeAccessIssue]" in finding.message


def test_config_excludes_virtualenvs(tmp_path):
    cfg = json.loads(typecheck.write_config(tmp_path, tmp_path).read_text())
    assert str(tmp_path / ".venv") in cfg["exclude"]
    assert cfg["include"] == [str(tmp_path)]
