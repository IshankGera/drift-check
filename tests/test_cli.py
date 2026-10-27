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
