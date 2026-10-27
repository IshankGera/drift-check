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
