from mock_sdk import Client


client = Client()


def test_create():
    result = client.create(
        name="test",
        old_param="something"
    )

    print(result)


if __name__ == "__main__":
    test_create()