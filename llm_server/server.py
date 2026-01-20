import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict

from opentelemetry import propagate
from opentelemetry.trace import SpanKind

from agents.common.tracing import get_tracer


def generate_response(prompt: str) -> str:
    """
    Minimal placeholder LLM/SLM response generator.
    In a real deployment, this would call vLLM or another local model.
    """
    # For now, just echo the prompt with a prefix to keep things simple.
    return f"[LLM placeholder response] {prompt}"


class LLMRequestHandler(BaseHTTPRequestHandler):
    tracer = get_tracer("llm-server")

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # type: ignore[override]
        if self.path not in ("/chat", "/completion"):
            self._send_json(404, {"error": "Not found"})
            return

        carrier = {key: value for key, value in self.headers.items()}
        ctx = propagate.extract(carrier)
        with self.tracer.start_as_current_span(
            "llm.handle_request",
            context=ctx,
            kind=SpanKind.SERVER,
        ) as span:
            span.set_attribute("app.path", self.path)

            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b""

            try:
                data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON"})
                return

            prompt = data.get("prompt") or data.get("input")
            if not isinstance(prompt, str) or not prompt:
                self._send_json(400, {"error": "Missing 'prompt' field"})
                return

            span.set_attribute("app.prompt_length", len(prompt))
            text = generate_response(prompt)
            self._send_json(200, {"output": text})


def run(host: str = "0.0.0.0", port: int = 8000) -> None:
    server = HTTPServer((host, port), LLMRequestHandler)
    print(f"[*] LLM server listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down LLM server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()



