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
            return ["x"]
    s = make("SIGNATURE_UNAVAILABLE", Stub()).resolve("sdk", "fn")
    assert not s.is_unknown and s.valid_parameters == ["x"]


def test_stub_provider_reads_target_interpreter(sdk_dir, monkeypatch):
    monkeypatch.chdir(sdk_dir)
    monkeypatch.syspath_prepend(str(sdk_dir))
    params = StubProvider().resolve(sys.executable, "fakesdk", "Client.ping")
    assert params == ["verbose"]
    assert StubProvider().resolve(sys.executable, "fakesdk", "Client.nope") is None
