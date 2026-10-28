"""A tiny local server that behaves like an OpenAI-compatible API for tests."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

API_KEY = "test-key"


def _error(status, message, param=None, code=None):
    return status, {"error": {"message": message, "type": "invalid_request_error", "param": param, "code": code}}


def decide(body):
    model = body.get("model")
    if model == "retired-model":
        return _error(404, f"The model `{model}` does not exist", "model", "model_not_found")
    if model.startswith("o3"):
        if "max_tokens" in body:
            return _error(400, "Unsupported parameter: 'max_tokens' is not supported with this model. "
                               "Use 'max_completion_tokens' instead.", "max_tokens", "unsupported_parameter")
        if body.get("temperature", 1) != 1:
            return _error(400, "Unsupported value: 'temperature' does not support 0.2 with this model.",
                          "temperature", "unsupported_value")
        if "top_p" in body:
            return _error(400, "Unsupported parameter: 'top_p' is not supported with this model.",
                          "top_p", "unsupported_parameter")
    if model == "busy-model":
        return _error(429, "Rate limit reached")
    return 200, {"id": "x", "choices": [{"message": {"role": "assistant", "content": "ok"}}]}


class Handler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.requests.append((self.path, self.headers.get("Authorization"), body))
        if self.headers.get("Authorization") != f"Bearer {API_KEY}":
            status, payload = _error(401, "Incorrect API key provided", code="invalid_api_key")
        else:
            status, payload = decide(body)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/v1"
