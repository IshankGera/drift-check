import mock_sdk

mock_sdk.old_method()
client = mock_sdk.Client()
client.create(name="test", old_param="hello")