"""v0.5.0: import-name mapping, SARIF output, latest release lookup, `outdated` command."""
import io
import json
import sys
import textwrap

import pytest
from typer.testing import CliRunner

from drift_check import cli, importnames, pypi
from drift_check.dependencies import BaselineStatus, ResolvedBaseline
from drift_check.models import Finding
from drift_check.reporter import Reporter, rule_for


# ---------- import-name mapping ----------

def test_import_names_from_metadata():
    # pytest is installed for the test run: distribution "pytest" provides "pytest" and "_pytest"
    names = importnames.import_names(sys.executable, "pytest")
    assert "pytest" in names and "_pytest" in names


def test_import_names_for_distribution_with_different_module_name():
    # typing_extensions is a single-module distribution; packaging is a package dir
    assert "packaging" in importnames.import_names(sys.executable, "packaging")


def test_unknown_distribution_falls_back():
    assert importnames.import_names(sys.executable, "No-Such-Dist.x") == ["no_such_dist_x"]


# ---------- SARIF ----------

def sample_findings(tmp_path):
    return [
        Finding("ERROR", str(tmp_path / "src" / "app.py"), 5, "openai", "ChatCompletion.create",
                "Method 'ChatCompletion.create' no longer exists in the target version."),
        Finding("WARNING", str(tmp_path / "app.py"), 2, "pydantic", "parse_obj_as",
                "Deprecated in the target version: use TypeAdapter"),
        Finding("ERROR", "tests/test_app.py::test_x", 0, "sdk", "test", "Test fails with the upgraded version."),
    ]


def test_sarif_report(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    out = tmp_path / "r.sarif"
    Reporter().report(sample_findings(tmp_path), sarif_path=str(out), quiet=True)
    doc = json.loads(out.read_text())
    assert doc["version"] == "2.1.0"
    run = doc["runs"][0]
    assert run["tool"]["driver"]["name"] == "drift-check"
    assert {r["id"] for r in run["tool"]["driver"]["rules"]} == {"removed-api", "deprecated-api", "test-failure"}
    locs = [(r["ruleId"], r["level"], r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
             r["locations"][0]["physicalLocation"]["region"]["startLine"]) for r in run["results"]]
    assert locs == [("removed-api", "error", "src/app.py", 5),
                    ("deprecated-api", "warning", "app.py", 2),
                    ("test-failure", "error", "tests/test_app.py", 1)]


@pytest.mark.parametrize("message,rule", [
    ("Import 'x' from 'y' no longer exists in the target version.", "removed-import"),
    ("Parameter 'p' is no longer supported.", "removed-parameter"),
    ("Required parameter 'r' is not passed (it is required in the target version).", "missing-required-argument"),
    ("Takes at most 1 positional argument(s) in the target version, but 2 are passed.", "too-many-positional"),
    ("'fn' is never run by your tests, so a break here would not be caught. (Coverage risk)", "untested-call"),
    ("Parameter 'k' could not be verified. Target API accepts dynamic **kwargs.", "unverifiable"),
])
def test_rule_ids(message, rule):
    assert rule_for(Finding("ERROR", "a.py", 1, "p", "m", message))[0] == rule


# ---------- latest release ----------

def fake_pypi(monkeypatch, releases):
    payload = json.dumps({"releases": releases}).encode()
    monkeypatch.setattr(pypi.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(payload))


def test_latest_release_skips_prereleases_and_yanked(monkeypatch):
    ok = [{"yanked": False}]
    fake_pypi(monkeypatch, {"1.0": ok, "1.10.0": ok, "2.0.0rc1": ok, "1.11.0": [{"yanked": True}], "1.12.0": []})
    assert pypi.latest_release("pkg") == "1.10.0"


def test_latest_release_network_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(pypi.urllib.request, "urlopen", boom)
    assert pypi.latest_release("pkg") is None


# ---------- outdated ----------

@pytest.fixture
def outdated_project(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("brokenpkg==1.0\nsafepkg==1.0\nunused==1.0\ncurrent==2.0\n")
    (tmp_path / "app.py").write_text(textwrap.dedent("""
        import brokenpkg, safepkg
        brokenpkg.old()
        safepkg.fn(x=1)
    """))
    monkeypatch.chdir(tmp_path)
    installed = {"brokenpkg": "1.0", "safepkg": "1.0", "unused": "1.0", "current": "2.0"}
    latest = {"brokenpkg": "2.0", "safepkg": "1.5", "unused": "3.0", "current": "2.0"}

    def resolve(self, name, spec):
        return ResolvedBaseline(name, spec, installed[name], BaselineStatus.MATCH, installed[name])

    def check(name, version, wheel, python, calls, verbose=False):
        if name == "brokenpkg":
            return [Finding("ERROR", calls[0].file_path, calls[0].line_number, "brokenpkg", "old",
                            "Method 'old' no longer exists in the target version.")]
        return []

    monkeypatch.setattr(cli.BaselineResolver, "resolve", resolve)
    monkeypatch.setattr(cli, "latest_release", lambda name: latest[name])
    monkeypatch.setattr(cli.importnames, "import_names", lambda py, name: [name])
    monkeypatch.setattr(cli, "check_calls", check)
    return tmp_path


def test_outdated_summary(outdated_project):
    result = CliRunner().invoke(cli.app, ["outdated", "--json-report", "o.json"])
    assert result.exit_code == 1, result.output
    out = result.output
    assert "brokenpkg" in out and "1 breaking" in out
    assert "safepkg" in out and "✓ safe" in out
    assert "not used in your code" in out
    assert "current" not in out.split("Outdated dependencies")[1].split("already")[0]
    assert "1 dependency is already up to date" in out
    data = json.loads((outdated_project / "o.json").read_text())
    assert data["summary"]["errors"] == 1


def test_outdated_only(outdated_project):
    result = CliRunner().invoke(cli.app, ["outdated", "--only", "safepkg"])
    assert result.exit_code == 0, result.output
    assert "brokenpkg" not in result.output
