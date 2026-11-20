import json

from drift_check.models import Finding
from drift_check.reporter import Reporter


def findings(tmp_path):
    return [
        Finding("ERROR", str(tmp_path / "pkg" / "app.py"), 5, "openai", "ChatCompletion.create",
                "Method 'ChatCompletion.create' no longer exists.\nSecond line, 100%"),
        Finding("WARNING", str(tmp_path / "b.py"), 2, "sdk", "fn", "Deprecated in the target version: x", kind="import"),
    ]


def test_github_annotations(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    Reporter().github_annotations(findings(tmp_path))
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == ("::error file=pkg/app.py,line=5,title=drift-check (openai)::"
                        "Method 'ChatCompletion.create' no longer exists.%0ASecond line, 100%25")
    assert lines[1].startswith("::warning file=b.py,line=2,")


def test_annotations_only_inside_github_actions(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    Reporter().report(findings(tmp_path))
    assert "::error" not in capsys.readouterr().out
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    Reporter().report(findings(tmp_path))
    assert "::error" in capsys.readouterr().out


def test_json_report(tmp_path):
    out = tmp_path / "r.json"
    Reporter().report(findings(tmp_path), label="Upgrade analysis", json_path=str(out))
    data = json.loads(out.read_text())
    assert data["summary"] == {"errors": 1, "warnings": 1}
    assert data["findings"][1]["kind"] == "import"
