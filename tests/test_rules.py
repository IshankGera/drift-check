import pytest

from drift_check.models import CallSite, SignatureDefinition
from drift_check.rules import RuleEngine


def call(kwargs=(), dynamic=False):
    return CallSite("app.py", 1, "sdk", "fn", list(kwargs), dynamic)


def sig(params=(), unknown=False, varkw=False, code="SUCCESS"):
    return SignatureDefinition("sdk", "fn", list(params), unknown, varkw, code)


def severities(findings):
    return [f.severity for f in findings]


def test_valid_call_is_silent():
    assert RuleEngine().evaluate(call(["a"]), sig(["a", "b"])) == []


def test_removed_parameter_is_error():
    assert severities(RuleEngine().evaluate(call(["old"]), sig(["new"]))) == ["ERROR"]


def test_unknown_param_with_varkw_is_warning():
    assert severities(RuleEngine().evaluate(call(["x"]), sig([], varkw=True))) == ["WARNING"]


def test_dynamic_call_site_is_warning():
    assert severities(RuleEngine().evaluate(call(dynamic=True), sig(["a"]))) == ["WARNING"]


def test_method_not_found_is_error():
    assert severities(RuleEngine().evaluate(call(), sig(unknown=True, code="METHOD_NOT_FOUND"))) == ["ERROR"]


@pytest.mark.parametrize("code", ["UNRESOLVED", "IMPORT_FAILED", "SIGNATURE_UNAVAILABLE", "UNSUPPORTED_CALLABLE"])
def test_uncertain_statuses_never_fail_the_build(code):
    assert severities(RuleEngine().evaluate(call(), sig(unknown=True, code=code))) == ["WARNING"]
