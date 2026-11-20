import pytest

from drift_check.models import CallSite, SignatureDefinition
from drift_check.models import Finding
from drift_check.rules import RuleEngine, suppress_consequences


def call(kwargs=(), dynamic=False):
    return CallSite("app.py", 1, "sdk", "fn", list(kwargs), dynamic)


def sig(params=(), unknown=False, varkw=False, code="SUCCESS", **shape):
    return SignatureDefinition("sdk", "fn", list(params), unknown, varkw, code, **shape)


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


def imp(path="old_fn"):
    return CallSite("app.py", 1, "sdk", path, [], False, kind="import")


def test_missing_import_is_error():
    [f] = RuleEngine().evaluate(imp(), sig(unknown=True, code="METHOD_NOT_FOUND"))
    assert f.severity == "ERROR" and f.message.startswith("Import 'old_fn'")


@pytest.mark.parametrize("code", ["SUCCESS", "UNSUPPORTED_CALLABLE", "SIGNATURE_UNAVAILABLE", "UNRESOLVED"])
def test_existing_import_is_silent(code):
    assert RuleEngine().evaluate(imp(), sig(unknown=code != "SUCCESS", code=code)) == []


def test_import_failure_is_warning():
    assert severities(RuleEngine().evaluate(imp(), sig(unknown=True, code="IMPORT_FAILED"))) == ["WARNING"]


def test_calls_to_a_missing_import_are_reported_once():
    findings = [
        Finding("ERROR", "app.py", 1, "sdk", "old_fn", "Import 'old_fn' from 'sdk' no longer exists in the target version.",
                kind="import"),
        Finding("ERROR", "app.py", 5, "sdk", "old_fn", "Method 'old_fn' no longer exists in the target version."),
        Finding("ERROR", "app.py", 6, "sdk", "old_fn.sub", "Method 'old_fn.sub' no longer exists in the target version."),
        Finding("ERROR", "app.py", 7, "sdk", "other", "Method 'other' no longer exists in the target version."),
        Finding("ERROR", "b.py", 3, "sdk", "old_fn", "Method 'old_fn' no longer exists in the target version."),
        Finding("ERROR", "app.py", 8, "sdk", "old_fn", "Parameter 'x' is no longer supported."),
    ]
    kept = [(f.file_path, f.line_number) for f in suppress_consequences(findings)]
    assert kept == [("app.py", 1), ("app.py", 7), ("b.py", 3), ("app.py", 8)]


# ---------- required / positional arguments ----------

def pcall(n_positional=0, kwargs=(), star=False, kind="call"):
    return CallSite("app.py", 3, "sdk", "fn", list(kwargs), False,
                    positional_count=n_positional, has_star_args=star, kind=kind)


V1 = dict(params=["a", "b"], positional_names=["a", "b"], max_positional=2, required_params=["a"])
V2_REQUIRED = dict(params=["a", "b", "region"], positional_names=["a", "b"], max_positional=2,
                   required_params=["a", "region"])
V2_FEWER_POSITIONAL = dict(params=["a", "b"], positional_names=["a"], max_positional=1, required_params=["a"])


def test_new_required_parameter_is_error():
    [f] = RuleEngine().evaluate(pcall(1), sig(**V2_REQUIRED), baseline=sig(**V1))
    assert f.severity == "ERROR" and "'region'" in f.message


def test_required_parameter_passed_by_keyword_is_fine():
    assert RuleEngine().evaluate(pcall(1, ["region"]), sig(**V2_REQUIRED), baseline=sig(**V1)) == []


def test_too_many_positional_is_error():
    [f] = RuleEngine().evaluate(pcall(2), sig(**V2_FEWER_POSITIONAL), baseline=sig(**V1))
    assert f.severity == "ERROR" and "at most 1 positional" in f.message


def test_problem_that_already_exists_in_baseline_is_not_reported():
    # The call already misses 'a' today: not caused by the upgrade, so no noise.
    assert RuleEngine().evaluate(pcall(0), sig(**V1), baseline=sig(**V1)) == []


def test_star_args_skip_argument_checks():
    assert RuleEngine().evaluate(pcall(5, star=True), sig(**V2_FEWER_POSITIONAL), baseline=sig(**V1)) == []


def test_constructor_with_kwargs_model_is_quiet():
    model = dict(params=[], varkw=True, positional_names=[], max_positional=0, required_params=[])
    assert RuleEngine().evaluate(pcall(0, ["field_a"], kind="constructor"), sig(**model)) == []


def test_constructor_removed_kwarg_is_error():
    [f] = RuleEngine().evaluate(pcall(0, ["proxies"], kind="constructor"), sig(params=["api_key"]))
    assert f.severity == "ERROR" and "'proxies'" in f.message


# ---------- deprecations ----------

def test_deprecation_is_warning():
    [f] = RuleEngine().evaluate(pcall(), sig(deprecated="Use new_fn instead."))
    assert f.severity == "WARNING" and "Use new_fn instead." in f.message


def test_deprecation_notes_when_already_deprecated():
    [f] = RuleEngine().evaluate(pcall(), sig(deprecated="old"), baseline=sig(deprecated="old"))
    assert "already deprecated" in f.message


def test_deprecated_import_reported_once():
    findings = [
        Finding("WARNING", "a.py", 1, "sdk", "old", "Deprecated in the target version: x", kind="import"),
        Finding("WARNING", "a.py", 4, "sdk", "old", "Deprecated in the target version: x"),
        Finding("WARNING", "b.py", 4, "sdk", "old", "Deprecated in the target version: x"),
    ]
    assert [(f.file_path, f.line_number) for f in suppress_consequences(findings)] == [("a.py", 1), ("b.py", 4)]
