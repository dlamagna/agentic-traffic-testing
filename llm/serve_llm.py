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
import os
import threading
import time
from typing import Any, Dict, List, Optional

from opentelemetry import propagate
from opentelemetry.trace import SpanKind

try:
    from vllm import LLM, SamplingParams  # type: ignore
except ImportError:  # pragma: no cover - only at runtime without vLLM
    LLM = None  # type: ignore
    SamplingParams = None  # type: ignore

try:
    from vllm.transformers_utils.tokenizer import get_tokenizer as vllm_get_tokenizer  # type: ignore
except ImportError:  # pragma: no cover - optional
    vllm_get_tokenizer = None  # type: ignore

try:
    from prometheus_client import Counter, Gauge, Histogram, generate_latest  # type: ignore
    from prometheus_client import CONTENT_TYPE_LATEST  # type: ignore
except ImportError:  # pragma: no cover - optional
    Counter = Gauge = Histogram = None  # type: ignore
    generate_latest = None  # type: ignore
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from llm.tracing import get_tracer


DEFAULT_MODEL_NAME = os.environ.get("LLM_MODEL", "meta-llama/Llama-3.1-8B-Instruct")
LOG_LLM_REQUESTS = os.environ.get("LOG_LLM_REQUESTS", "").lower() in ("1", "true", "yes", "on")
LOG_LLM_MAX_CHARS = int(os.environ.get("LLM_LOG_MAX_CHARS", "500"))
LLM_MAX_CONCURRENCY = int(os.environ.get("LLM_MAX_CONCURRENCY", "1"))
_LLM_SEMAPHORE = threading.Semaphore(max(1, LLM_MAX_CONCURRENCY))
LLM_DTYPE = os.environ.get("LLM_DTYPE")
LLM_MAX_NUM_SEQS = os.environ.get("LLM_MAX_NUM_SEQS")
LLM_MAX_NUM_BATCHED_TOKENS = os.environ.get("LLM_MAX_NUM_BATCHED_TOKENS")
LLM_GPU_MEMORY_UTILIZATION = os.environ.get("LLM_GPU_MEMORY_UTILIZATION")
LLM_METRICS_ENABLED = os.environ.get("LLM_METRICS_ENABLED", "1").lower() in ("1", "true", "yes", "on")
LLM_METRICS_INCLUDE_TOKENS = os.environ.get("LLM_METRICS_INCLUDE_TOKENS", "1").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
LLM_METRICS_PREFIX = os.environ.get("LLM_METRICS_PREFIX", "llm")

_METRICS_READY = LLM_METRICS_ENABLED and Counter is not None and Gauge is not None and Histogram is not None

if LLM_METRICS_ENABLED and not _METRICS_READY:
    print(
        "[llm-metrics] Disabled: prometheus_client not installed. "
        "Install via requirements.txt to enable /metrics."
    )

if _METRICS_READY:
    REQUESTS_TOTAL = Counter(
        f"{LLM_METRICS_PREFIX}_requests_total",
        "Total LLM requests",
        ["status"],
    )
    REQUEST_LATENCY = Histogram(
        f"{LLM_METRICS_PREFIX}_request_latency_seconds",
        "End-to-end LLM request latency",
    )
    QUEUE_WAIT = Histogram(
        f"{LLM_METRICS_PREFIX}_queue_wait_seconds",
        "Time spent waiting for LLM concurrency slot",
    )
    INFLIGHT = Gauge(
        f"{LLM_METRICS_PREFIX}_inflight_requests",
        "In-flight LLM requests",
    )
    PROMPT_TOKENS = Counter(
        f"{LLM_METRICS_PREFIX}_prompt_tokens_total",
        "Total prompt tokens",
    )
    COMPLETION_TOKENS = Counter(
        f"{LLM_METRICS_PREFIX}_completion_tokens_total",
        "Total completion tokens",
    )


def _log_prompt(source: str, prompt: str) -> None:
    if not LOG_LLM_REQUESTS:
        return
    max_chars = max(LOG_LLM_MAX_CHARS, 0)
    if max_chars == 0:
        preview = ""
        suffix = ""
    else:
        preview = prompt[:max_chars]
        suffix = "" if len(prompt) <= max_chars else f"... [truncated {len(prompt) - max_chars} chars]"
    print(f"[llm-request] source={source} prompt_len={len(prompt)} prompt={preview}{suffix}")


def _log_request_stats(stats: Dict[str, Any]) -> None:
    if not LOG_LLM_REQUESTS:
        return
    pairs = " ".join(f"{key}={value}" for key, value in stats.items() if value is not None)
    if pairs:
        print(f"[llm-metrics] {pairs}")


def _record_metrics(
    status: str,
    latency_s: float,
    queue_wait_s: float,
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
) -> None:
    if not _METRICS_READY:
        return
    REQUESTS_TOTAL.labels(status=status).inc()
    REQUEST_LATENCY.observe(latency_s)
    QUEUE_WAIT.observe(queue_wait_s)
    if prompt_tokens is not None:
        PROMPT_TOKENS.inc(prompt_tokens)
    if completion_tokens is not None:
        COMPLETION_TOKENS.inc(completion_tokens)


class VLLMBackend:
    def __init__(
        self,
        model: str,
        max_model_len: int | None,
        dtype: str | None,
        max_num_seqs: int | None,
        max_num_batched_tokens: int | None,
        gpu_memory_utilization: float | None,
    ) -> None:
        if LLM is None:
            raise RuntimeError(
                "vLLM is not installed. Please `pip install vllm` or "
                "run the official vLLM server container instead."
            )
        self._model_name = model
        llm_kwargs = {"model": model}
        if max_model_len is not None:
            llm_kwargs["max_model_len"] = max_model_len
        if dtype:
            llm_kwargs["dtype"] = dtype
        if max_num_seqs is not None:
            llm_kwargs["max_num_seqs"] = max_num_seqs
        if max_num_batched_tokens is not None:
            llm_kwargs["max_num_batched_tokens"] = max_num_batched_tokens
        if gpu_memory_utilization is not None:
            llm_kwargs["gpu_memory_utilization"] = gpu_memory_utilization
        self._llm = LLM(**llm_kwargs)
        self._default_sampling = SamplingParams(temperature=0.2, max_tokens=512)
        self._tokenizer = None
        self._tokenizer_ready = False

    def generate(self, prompt: str) -> str:
        outputs = self._llm.generate(
            [prompt],
            sampling_params=self._default_sampling,
        )
        # vLLM returns a list of RequestOutput objects
        text: str = outputs[0].outputs[0].text
        return text

    def _resolve_tokenizer(self) -> None:
        if self._tokenizer_ready:
            return
        self._tokenizer_ready = True
        get_tok = getattr(self._llm, "get_tokenizer", None)
        if callable(get_tok):
            try:
                self._tokenizer = get_tok()
                return
            except Exception:
                self._tokenizer = None
                return
        if vllm_get_tokenizer is not None:
            try:
                self._tokenizer = vllm_get_tokenizer(self._model_name)
            except Exception:
                self._tokenizer = None

    def count_tokens(self, text: str) -> Optional[int]:
        if not LLM_METRICS_INCLUDE_TOKENS:
            return None
        if not text:
            return 0
        self._resolve_tokenizer()
        if self._tokenizer is None:
            return None
        try:
            return len(self._tokenizer.encode(text, add_special_tokens=False))
        except Exception:
            return None


class LLMRequestHandler(BaseHTTPRequestHandler):
    backend: VLLMBackend  # type: ignore[assignment]
    tracer = get_tracer("llm-backend")

    def do_GET(self) -> None:  # type: ignore[override]
        if self.path in ("/health", "/ready", "/live"):
            self._send_json(200, {"status": "ok"})
            return
        if self.path == "/metrics":
            if not _METRICS_READY or generate_latest is None:
                self._send_json(503, {"error": "Metrics disabled"})
                return
            output = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)
            return
        self._send_json(404, {"error": "Not found"})

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
            start_time = time.monotonic()
            if _METRICS_READY:
                INFLIGHT.inc()
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
            if LOG_LLM_REQUESTS:
                span.set_attribute("app.prompt_preview", prompt[:200])
            _log_prompt("http", prompt)
            status = "success"
            prompt_tokens: Optional[int] = None
            completion_tokens: Optional[int] = None
            error_payload: Optional[Dict[str, str]] = None
            wait_ms = 0
            try:
                with self.tracer.start_as_current_span("llm.wait_for_slot") as wait_span:
                    queue_start = time.monotonic()
                    _LLM_SEMAPHORE.acquire()
                    wait_ms = int((time.monotonic() - queue_start) * 1000)
                    wait_span.set_attribute("llm.queue_wait_ms", wait_ms)
                    span.set_attribute("app.queue_wait_ms", wait_ms)
                    if wait_ms > 0:
                        print(f"[llm-queue] waited_ms={wait_ms} concurrency={LLM_MAX_CONCURRENCY}")

                with self.tracer.start_as_current_span("llm.generate") as gen_span:
                    gen_start = time.monotonic()
                    text = self.backend.generate(prompt)
                    gen_ms = int((time.monotonic() - gen_start) * 1000)
                    gen_span.set_attribute("llm.generate_ms", gen_ms)

                prompt_tokens = self.backend.count_tokens(prompt)
                completion_tokens = self.backend.count_tokens(text)
                if prompt_tokens is not None:
                    span.set_attribute("llm.prompt_tokens", prompt_tokens)
                if completion_tokens is not None:
                    span.set_attribute("llm.completion_tokens", completion_tokens)
                if prompt_tokens is not None and completion_tokens is not None:
                    span.set_attribute("llm.total_tokens", prompt_tokens + completion_tokens)
            except Exception as exc:  # pragma: no cover - runtime safety
                status = "error"
                error_payload = {"error": f"Generation failed: {exc}"}
            finally:
                _LLM_SEMAPHORE.release()
                if _METRICS_READY:
                    INFLIGHT.dec()

            latency_s = time.monotonic() - start_time
            queue_wait_s = wait_ms / 1000.0
            _record_metrics(status, latency_s, queue_wait_s, prompt_tokens, completion_tokens)
            _log_request_stats(
                {
                    "status": status,
                    "latency_ms": int(latency_s * 1000),
                    "queue_wait_ms": wait_ms,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                }
            )
            if status == "error":
                self._send_json(500, error_payload or {"error": "Generation failed"})
                return
            self._send_json(200, {"output": text})


def run_http_server(
    host: str,
    port: int,
    model_name: str,
    max_model_len: int | None,
    dtype: str | None,
    max_num_seqs: int | None,
    max_num_batched_tokens: int | None,
    gpu_memory_utilization: float | None,
) -> None:
    backend = VLLMBackend(
        model=model_name,
        max_model_len=max_model_len,
        dtype=dtype,
        max_num_seqs=max_num_seqs,
        max_num_batched_tokens=max_num_batched_tokens,
        gpu_memory_utilization=gpu_memory_utilization,
    )
    LLMRequestHandler.backend = backend  # type: ignore[assignment]

    server = ThreadingHTTPServer((host, port), LLMRequestHandler)
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
    parser.add_argument("--dtype", default=None, help="vLLM dtype override (e.g., float16).")
    parser.add_argument(
        "--max-num-seqs",
        type=int,
        default=None,
        help="Max sequences per iteration (vLLM scheduler).",
    )
    parser.add_argument(
        "--max-num-batched-tokens",
        type=int,
        default=None,
        help="Max batched tokens per iteration (vLLM scheduler).",
    )
    parser.add_argument(
        "--gpu-memory-utilization",
        type=float,
        default=None,
        help="Target GPU memory utilization fraction (0-1).",
    )
    args = parser.parse_args(argv)

    def _env_int(value: str | None) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _env_float(value: str | None) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except ValueError:
            return None

    dtype = args.dtype or LLM_DTYPE
    max_num_seqs = args.max_num_seqs or _env_int(LLM_MAX_NUM_SEQS)
    max_num_batched_tokens = args.max_num_batched_tokens or _env_int(LLM_MAX_NUM_BATCHED_TOKENS)
    gpu_memory_utilization = args.gpu_memory_utilization or _env_float(LLM_GPU_MEMORY_UTILIZATION)

    run_http_server(
        args.host,
        args.port,
        args.model,
        args.max_model_len,
        dtype,
        max_num_seqs,
        max_num_batched_tokens,
        gpu_memory_utilization,
    )


if __name__ == "__main__":
    main()


