import pytest
import typer

from drift_check.cli import validate_local_wheel


@pytest.fixture
def wheel(tmp_path):
    p = tmp_path / "mock_sdk-2.0.0-py3-none-any.whl"
    p.write_bytes(b"")
    return str(p)


def test_valid_wheel(wheel):
    assert validate_local_wheel(wheel, "mock-sdk", "2.0.0") == "2.0.0"
    assert validate_local_wheel(wheel, "mock_sdk", "2.0") == "2.0.0"


@pytest.mark.parametrize("pkg,ver", [("mock-sdk", "9.9.9"), ("other", "2.0.0")])
def test_mismatched_wheel_is_rejected(wheel, pkg, ver):
    with pytest.raises(typer.Exit) as e:
        validate_local_wheel(wheel, pkg, ver)
    assert e.value.exit_code == 2


def test_missing_wheel(tmp_path):
    with pytest.raises(typer.Exit) as e:
        validate_local_wheel(str(tmp_path / "nope.whl"), "x", "1.0")
    assert e.value.exit_code == 2


# ---------- v0.4.1: non-UTF-8 consoles and crash exit code (independent review, bug 7) ----------
import os
import subprocess
import sys


def run_cli(tmp_path, *args, **env):
    return subprocess.run([sys.executable, "-m", "drift_check.cli", *args], cwd=tmp_path,
                          capture_output=True, text=True, env={**os.environ, **env})


def test_cp1252_console_does_not_crash(tmp_path):
    (tmp_path / "requirements.txt").write_text("other==1.0\n")
    result = run_cli(tmp_path, "upgrade", "notdeclared", "--to", "2.0.0", PYTHONIOENCODING="cp1252")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "not declared" in result.stdout


def test_unexpected_crash_exits_2(monkeypatch):
    from drift_check import cli

    def boom():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as e:
        cli.run()
    assert e.value.code == 2


def test_drift_check_python_override(tmp_path, monkeypatch):
    from drift_check.env import EnvironmentDetector
    fake = tmp_path / "python-custom"
    fake.write_text("")
    monkeypatch.setenv("DRIFT_CHECK_PYTHON", str(fake))
    assert EnvironmentDetector(str(tmp_path)).find_python_executable() == str(fake)
