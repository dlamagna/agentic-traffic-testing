#!/usr/bin/env python3
"""

Endpoints:
  POST /chat   { "prompt": "text", "max_tokens": N } -> { "output": "text" }

Environment:
  LLM_MODEL (or MODEL_NAME) (default: meta-llama/Llama-3.1-8B-Instruct)
  LLM_MAX_TOKENS (default: 512) - max completion tokens per request
  HOST (default: 0.0.0.0)
  PORT (default: 8000)
  HF_TOKEN / HUGGINGFACE_HUB_TOKEN for gated models (optional)
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline


MODEL_NAME = os.environ.get("LLM_MODEL") or os.environ.get("MODEL_NAME", "meta-llama/Llama-3.1-8B-Instruct")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "512"))

_PIPELINE = None


def _load_pipeline():
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, token=token)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME,
        torch_dtype=torch.float32,
        token=token,
    )
    _PIPELINE = pipeline(
        "text-generation",
        model=model,
        tokenizer=tokenizer,
        device=-1,  # CPU
    )
    return _PIPELINE


class HFRequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # type: ignore[override]
        if self.path not in ("/chat", "/generate", "/completion"):
            self._send_json(404, {"error": "Not found"})
            return

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

        max_new_tokens = data.get("max_tokens") or data.get("max_new_tokens")
        if max_new_tokens is not None:
            try:
                max_new_tokens = int(max_new_tokens)
            except (TypeError, ValueError):
                max_new_tokens = LLM_MAX_TOKENS
        else:
            max_new_tokens = LLM_MAX_TOKENS

        generator = _load_pipeline()
        outputs = generator(prompt, max_new_tokens=max_new_tokens, temperature=0.7, do_sample=True)
        text = outputs[0]["generated_text"]

        self._send_json(200, {"output": text})


def run() -> None:
    server = HTTPServer((HOST, PORT), HFRequestHandler)
    print(f"[*] HF CPU server for {MODEL_NAME} on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down HF CPU server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()

