"""
Minimal vLLM-based LLM backend for the testbed.

Targets the model:
  meta-llama/Llama-3.1-8B-Instruct

This module assumes you have vLLM installed and a GPU available.
You can also run vLLM via its provided CLI container instead of this script,
but this gives you a clear declaration of the model we intend to use.
"""

import argparse
import json
from typing import Any, Dict, List

from opentelemetry import propagate
from opentelemetry.trace import SpanKind

try:
    from vllm import LLM, SamplingParams  # type: ignore
except ImportError:  # pragma: no cover - only at runtime without vLLM
    LLM = None  # type: ignore
    SamplingParams = None  # type: ignore

from http.server import BaseHTTPRequestHandler, HTTPServer

from llm.tracing import get_tracer


DEFAULT_MODEL_NAME = "meta-llama/Llama-3.1-8B-Instruct"


class VLLMBackend:
    def __init__(self, model: str, max_model_len: int | None) -> None:
        if LLM is None:
            raise RuntimeError(
                "vLLM is not installed. Please `pip install vllm` or "
                "run the official vLLM server container instead."
            )
        llm_kwargs = {"model": model}
        if max_model_len is not None:
            llm_kwargs["max_model_len"] = max_model_len
        self._llm = LLM(**llm_kwargs)
        self._default_sampling = SamplingParams(temperature=0.2, max_tokens=512)

    def generate(self, prompt: str) -> str:
        outputs = self._llm.generate(
            [prompt],
            sampling_params=self._default_sampling,
        )
        # vLLM returns a list of RequestOutput objects
        text: str = outputs[0].outputs[0].text
        return text


class LLMRequestHandler(BaseHTTPRequestHandler):
    backend: VLLMBackend  # type: ignore[assignment]
    tracer = get_tracer("llm-backend")

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # type: ignore[override]
        if self.path not in ("/chat", "/completion", "/generate"):
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
                data: Dict[str, Any] = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON"})
                return

            prompt = data.get("prompt") or data.get("input")
            if not isinstance(prompt, str) or not prompt:
                self._send_json(400, {"error": "Missing 'prompt' field"})
                return

            span.set_attribute("app.prompt_length", len(prompt))
            try:
                text = self.backend.generate(prompt)
            except Exception as exc:  # pragma: no cover - runtime safety
                self._send_json(500, {"error": f"Generation failed: {exc}"})
                return

            self._send_json(200, {"output": text})


def run_http_server(host: str, port: int, model_name: str, max_model_len: int | None) -> None:
    backend = VLLMBackend(model=model_name, max_model_len=max_model_len)
    LLMRequestHandler.backend = backend  # type: ignore[assignment]

    server = HTTPServer((host, port), LLMRequestHandler)
    print(f"[*] vLLM backend serving {model_name} on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down vLLM backend.")
    finally:
        server.server_close()


def main(argv: List[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="LLM backend (vLLM) for agentic traffic testbed")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL_NAME,
        help="HuggingFace model name (default: %(default)s)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: %(default)d)")
    parser.add_argument(
        "--max-model-len",
        type=int,
        default=None,
        help="Override model max sequence length for KV cache sizing.",
    )
    args = parser.parse_args(argv)

    run_http_server(args.host, args.port, args.model, args.max_model_len)


if __name__ == "__main__":
    main()


