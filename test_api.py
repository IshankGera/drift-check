import mock_sdk

# Test 4: The target v2 adds `new_param`, but we don't use it. (Should be SILENT)
mock_sdk.unchanged_method(name="test")

# Test 5: The target v2 uses **kwargs, so the engine can't statically guarantee safety. (Should WARN)
mock_sdk.dynamic_call(random_key="value")