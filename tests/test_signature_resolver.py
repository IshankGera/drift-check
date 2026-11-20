"""Regression: the stub fallback must never overrule METHOD_NOT_FOUND."""
import sys

from drift_check.signatures import SignatureResolver, StubProvider


class FakeRuntime:
    def __init__(self, status):
        self.status = status

    def resolve(self, *args):
        return {"status": self.status}


class ExplodingStub:
    def resolve(self, *args):
        raise AssertionError("stub fallback must not run")


def make(status, stub):
    r = SignatureResolver(project_python_exec=sys.executable)
    r.runtime_provider = FakeRuntime(status)
    r.stub_provider = stub
    return r


def test_method_not_found_skips_stub_fallback():
    s = make("METHOD_NOT_FOUND", ExplodingStub()).resolve("sdk", "old_method")
    assert s.is_unknown and s.error_code == "METHOD_NOT_FOUND"


def test_unresolved_skips_stub_fallback():
    assert make("UNRESOLVED", ExplodingStub()).resolve("sdk", "a.b").error_code == "UNRESOLVED"


def test_stub_used_when_signature_unavailable():
    class Stub:
        def resolve(self, *args):
            return ["x"], False
    s = make("SIGNATURE_UNAVAILABLE", Stub()).resolve("sdk", "fn")
    assert not s.is_unknown and s.valid_parameters == ["x"]


def test_stub_provider_reads_target_interpreter(sdk_dir, monkeypatch):
    monkeypatch.chdir(sdk_dir)
    monkeypatch.syspath_prepend(str(sdk_dir))
    params, accepts_kwargs = StubProvider().resolve(sys.executable, "fakesdk", "Client.ping")
    assert params == ["verbose"] and accepts_kwargs is False
    assert StubProvider().resolve(sys.executable, "fakesdk", "Client.nope") is None


def test_stub_overloads_and_kwargs(tmp_path, monkeypatch):
    # Independent review, bug 10: stubs ignored **kwargs and used only the first @overload
    pkg = tmp_path / "stubsdk"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "__init__.pyi").write_text(
        "from typing import overload\n"
        "def request(url, **kwargs): ...\n"
        "@overload\n"
        "def configure(*, level: int) -> None: ...\n"
        "@overload\n"
        "def configure(*, mode: str) -> None: ...\n"
        "class Box:\n"
        "    def put(self, item, *, force=False): ...\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    stub = StubProvider()
    assert stub.resolve(sys.executable, "stubsdk", "request") == (["url"], True)
    assert stub.resolve(sys.executable, "stubsdk", "configure") == (["level", "mode"], False)
    assert stub.resolve(sys.executable, "stubsdk", "Box.put") == (["force", "item"], False)
