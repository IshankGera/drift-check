"""Target version resolution (independent review, bug 5)."""
import pytest
import typer

from drift_check.pypi import PyPIResolver


def resolver(monkeypatch, releases):
    r = PyPIResolver("pkg")
    monkeypatch.setattr(r, "fetch_releases", lambda: list(releases))
    return r


@pytest.mark.parametrize("requested,releases,expected", [
    ("5.1", ["5.0", "5.1", "5.1.1"], "5.1"),            # two-part release, as published (Django)
    ("2024.1", ["2023.4", "2024.1"], "2024.1"),          # calendar versions (pytz)
    ("5.1.0", ["5.1", "5.1.1"], "5.1"),                  # same version written differently
    ("1.50", ["1.50.0", "1.50.2"], "1.50.0"),
    ("2", ["2.0.0", "2.1.0"], "2.0.0"),
])
def test_published_versions_resolve(monkeypatch, requested, releases, expected):
    assert resolver(monkeypatch, releases).resolve(requested) == expected


def test_unpublished_version_suggests_closest_release_string(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(typer.Exit) as e:
        resolver(monkeypatch, ["1.50.0", "1.50.2"]).resolve("1.50.99")
    assert e.value.exit_code == 2
    assert "--to 1.50.2" in capsys.readouterr().out
