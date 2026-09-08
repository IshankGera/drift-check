import openai

# This was the standard way to call the API in v0.28.0
# In v1.0.0+, this entire class/method structure was deleted and replaced by the instantiated Client.
openai.ChatCompletion.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello"}]
)