"""Runtime signature worker: the core of the upgrade check."""


def test_top_level_function(worker):
    r = worker("fakesdk", "top_level")
    assert r["status"] == "SUCCESS"
    assert set(r["valid_parameters"]) == {"name", "timeout"}
    assert r["accepts_kwargs"] is False


def test_removed_function_is_method_not_found(worker):
    assert worker("fakesdk", "gone")["status"] == "METHOD_NOT_FOUND"


def test_dynamic_kwargs(worker):
    r = worker("fakesdk", "dynamic")
    assert r["status"] == "SUCCESS" and r["accepts_kwargs"] is True


def test_client_method_excludes_self(worker):
    r = worker("fakesdk", "Client.ping")
    assert r["status"] == "SUCCESS"
    assert r["valid_parameters"] == ["verbose"]


def test_classmethod(worker):
    r = worker("fakesdk", "Client.from_env")
    assert r["status"] == "SUCCESS" and r["valid_parameters"] == ["prefix"]


# Regression: client.chat.completions.create used to be a false ERROR
def test_cached_property_chain_is_followed(worker):
    r = worker("fakesdk", "Client.chat.completions.create")
    assert r["status"] == "SUCCESS"
    assert set(r["valid_parameters"]) == {"model", "messages", "temperature"}


def test_optional_property_type_is_unwrapped(worker):
    assert worker("fakesdk", "Client.maybe.completions.create")["status"] == "SUCCESS"


def test_annotated_instance_attribute(worker):
    assert worker("fakesdk", "Client.typed.create")["status"] == "SUCCESS"


def test_untyped_property_is_unresolved_not_error(worker):
    assert worker("fakesdk", "Client.mystery.completions.create")["status"] == "UNRESOLVED"


def test_constructor_assignment_is_followed(worker):
    # self.untyped = Untyped()  ->  Untyped
    r = worker("fakesdk", "Client.untyped.go")
    assert r["status"] == "SUCCESS" and r["valid_parameters"] == ["fast"]


def test_untyped_instance_attribute_is_unresolved_not_error(worker):
    assert worker("fakesdk", "Client.untyped.thing.create")["status"] == "UNRESOLVED"


def test_removed_client_method_is_still_an_error(worker):
    assert worker("fakesdk", "Client.old_method")["status"] == "METHOD_NOT_FOUND"


def test_removed_method_behind_property_is_an_error(worker):
    assert worker("fakesdk", "Client.chat.completions.old_create")["status"] == "METHOD_NOT_FOUND"


def test_import_failure_is_not_method_not_found(worker):
    r = worker("brokensdk", "anything")
    assert r["status"] == "IMPORT_FAILED"
    assert "missing optional dependency" in r["detail"]


# Regression: openai 2.x imports return types lazily inside cached_property bodies
def test_lazily_imported_return_type_is_followed(worker):
    r = worker("lazysdk", "Client.chat.completions.create")
    assert r["status"] == "SUCCESS"
    assert set(r["valid_parameters"]) == {"model", "messages", "max_completion_tokens"}


def test_type_checking_import_is_followed(worker):
    r = worker("lazysdk", "Client.embeddings.create")
    assert r["status"] == "SUCCESS" and set(r["valid_parameters"]) == {"model", "input"}


def test_removed_param_detectable_through_lazy_chain(worker):
    r = worker("lazysdk", "Client.chat.completions.create")
    assert "max_tokens" not in r["valid_parameters"]


def test_unimported_submodule_is_resolved(worker):
    r = worker("fakesdk2", "tools.helper")
    assert r["status"] == "SUCCESS" and set(r["valid_parameters"]) == {"path", "strict"}


def test_missing_submodule_is_method_not_found(worker):
    assert worker("fakesdk2", "gone.helper")["status"] == "METHOD_NOT_FOUND"


# ---------- argument shape and deprecations ----------

def test_required_and_positional_shape(worker):
    r = worker("argsdk", "needs_region")
    assert r["positional_names"] == ["name", "region"]
    assert r["max_positional"] == 2
    assert r["required_params"] == ["name", "region"]


def test_varargs_means_unlimited_positional(worker):
    assert worker("argsdk", "flexible")["max_positional"] is None


def test_constructor_signature_excludes_self(worker):
    r = worker("argsdk", "Client")
    assert r["required_params"] == ["api_key"] and r["max_positional"] == 1


def test_method_with_positional_only(worker):
    r = worker("argsdk", "Client.send")
    assert r["positional_names"] == ["payload", "retries"] and r["required_params"] == ["payload"]


def test_warnings_warn_deprecation_detected(worker):
    assert "new_style" in worker("argsdk", "old_style")["deprecated"]


def test_conditional_warning_is_not_deprecation(worker):
    assert worker("argsdk", "conditional")["deprecated"] is None


def test_pep702_deprecated_detected(worker):
    r = worker("argsdk", "make_client")
    if r["status"] == "METHOD_NOT_FOUND":
        import pytest
        pytest.skip("typing_extensions not installed")
    assert r["deprecated"] == "Use Client instead"
