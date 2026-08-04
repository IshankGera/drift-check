import requests
from openai import OpenAI
import datetime

# V1 failed this because it's a C-extension. V2 will parse the .pyi stub!
now = datetime.datetime.now(tz=None)

requests.post(url="https://api.example.com", json={"key": "value"})
# The runtime provider handles this flawlessly
requests.post(
    url="https://api.example.com/data",
    json={"key": "value"},
    timeout=10,
    fake_parameter=True # This should trigger an error!
)

# The runtime provider safely defers this to V2
client = OpenAI()
client.chat.completions.create(model="gpt-4")