"""
Async vLLM-based LLM backend for the testbed.

Targets the model:
  meta-llama/Llama-3.1-8B-Instruct

Uses AsyncLLMEngine for true request batching - concurrent requests are
automatically batched together by vLLM's scheduler for GPU efficiency.
"""

import argparse
import asyncio
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from opentelemetry import propagate
from opentelemetry.trace import SpanKind

try:
    from vllm import SamplingParams  # type: ignore
    from vllm.engine.arg_utils import AsyncEngineArgs  # type: ignore
    from vllm.engine.async_llm_engine import AsyncLLMEngine  # type: ignore
except ImportError:  # pragma: no cover - only at runtime without vLLM
    AsyncLLMEngine = None  # type: ignore
    AsyncEngineArgs = None  # type: ignore
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

try:
    from aiohttp import web  # type: ignore
except ImportError:  # pragma: no cover
    web = None  # type: ignore

from llm.tracing import get_tracer


DEFAULT_MODEL_NAME = os.environ.get("LLM_MODEL", "meta-llama/Llama-3.1-8B-Instruct")
LOG_LLM_REQUESTS = os.environ.get("LOG_LLM_REQUESTS", "").lower() in ("1", "true", "yes", "on")
LOG_LLM_MAX_CHARS = int(os.environ.get("LLM_LOG_MAX_CHARS", "500"))
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
# Whether to apply Llama 3 chat template to raw prompts
LLM_APPLY_CHAT_TEMPLATE = os.environ.get("LLM_APPLY_CHAT_TEMPLATE", "1").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
LLM_DEFAULT_SYSTEM_PROMPT = os.environ.get(
    "LLM_DEFAULT_SYSTEM_PROMPT",
    "You are a helpful AI assistant. Provide clear, concise, and accurate responses.",
)

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
        "Time spent waiting in vLLM queue",
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
    BATCH_SIZE = Histogram(
        f"{LLM_METRICS_PREFIX}_batch_size",
        "Number of requests batched together",
        buckets=[1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 32],
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


class AsyncVLLMBackend:
    """Async vLLM backend using AsyncLLMEngine for true batching."""

    def __init__(
        self,
        model: str,
        max_model_len: int | None,
        dtype: str | None,
        max_num_seqs: int | None,
        max_num_batched_tokens: int | None,
        gpu_memory_utilization: float | None,
    ) -> None:
        if AsyncLLMEngine is None or AsyncEngineArgs is None:
            raise RuntimeError(
                "vLLM is not installed. Please `pip install vllm` or "
                "run the official vLLM server container instead."
            )
        self._model_name = model

        engine_kwargs: Dict[str, Any] = {"model": model}
        if max_model_len is not None:
            engine_kwargs["max_model_len"] = max_model_len
        if dtype:
            engine_kwargs["dtype"] = dtype
        if max_num_seqs is not None:
            engine_kwargs["max_num_seqs"] = max_num_seqs
        if max_num_batched_tokens is not None:
            engine_kwargs["max_num_batched_tokens"] = max_num_batched_tokens
        if gpu_memory_utilization is not None:
            engine_kwargs["gpu_memory_utilization"] = gpu_memory_utilization

        engine_args = AsyncEngineArgs(**engine_kwargs)
        self._engine = AsyncLLMEngine.from_engine_args(engine_args)
        self._default_sampling = SamplingParams(temperature=0.2, max_tokens=512)
        self._tokenizer = None
        self._tokenizer_ready = False

    async def generate(
        self,
        prompt: str,
        request_id: str | None = None,
        log_progress: bool = True,
    ) -> tuple[str, float]:
        """Generate text asynchronously. Concurrent calls are batched by vLLM.

        Returns a tuple of (generated_text, queue_wait_seconds), where
        queue_wait_seconds represents the time spent waiting in vLLM's
        internal scheduler before the first token is produced.
        """
        if request_id is None:
            request_id = str(uuid.uuid4())[:8]

        results_generator = self._engine.generate(
            prompt,
            self._default_sampling,
            request_id,
        )

        final_output = None
        queue_start = time.monotonic()
        last_log_time = queue_start
        last_token_count = 0
        queue_wait_s: float = 0.0

        # Span for time-to-first-token (TTFT): from queuing until the first token.
        wait_span = _tracer.start_span("llm.time_to_first_token")
        gen_span = None
        gen_start = None

        try:
            first = True
            async for request_output in results_generator:
                # First token: end wait span and start generation span.
                if first:
                    first = False
                    first_time = time.monotonic()
                    queue_wait_s = first_time - queue_start
                    # Record TTFT (time to first token)
                    wait_span.set_attribute("llm_ttft_seconds", queue_wait_s)
                    wait_span.end()
                    wait_span = None

                    gen_span = _tracer.start_span("llm.generate")
                    gen_span.set_attribute("app.request_id", request_id)
                    gen_start = first_time

                final_output = request_output

                # Log progress periodically (every ~2 seconds)
                if log_progress and final_output.outputs:
                    now = time.monotonic()
                    try:
                        current_tokens = len(final_output.outputs[0].token_ids)
                    except AttributeError:
                        # Fallback: estimate from text length
                        current_tokens = len(final_output.outputs[0].text.split())
                    if now - last_log_time >= 2.0:
                        elapsed = now - queue_start
                        tokens_per_sec = current_tokens / elapsed if elapsed > 0 else 0
                        print(
                            f"[llm] req={request_id} PROGRESS tokens={current_tokens} "
                            f"speed={tokens_per_sec:.1f} tok/s",
                            flush=True,
                        )
                        last_log_time = now
                        last_token_count = current_tokens

            if final_output is None or not final_output.outputs:
                return "", queue_wait_s

            # Log final token count
            if log_progress:
                try:
                    total_tokens = len(final_output.outputs[0].token_ids)
                except AttributeError:
                    total_tokens = len(final_output.outputs[0].text.split())
                end_time = time.monotonic()
                elapsed = end_time - queue_start
                tokens_per_sec = total_tokens / elapsed if elapsed > 0 else 0
                print(
                    f"[llm] req={request_id} GENERATED tokens={total_tokens} "
                    f"time={elapsed:.2f}s speed={tokens_per_sec:.1f} tok/s",
                    flush=True,
                )

            # Attach pure generation time to the llm.generate span.
            if gen_span is not None and gen_start is not None:
                gen_ms = int((time.monotonic() - gen_start) * 1000)
                gen_span.set_attribute("llm.generate_ms", gen_ms)

            return final_output.outputs[0].text, queue_wait_s

        finally:
            # Make sure spans are closed even if errors/edge cases occur.
            if wait_span is not None:
                wait_span.end()
            if gen_span is not None:
                gen_span.end()

    def _resolve_tokenizer(self) -> None:
        if self._tokenizer_ready:
            return
        self._tokenizer_ready = True
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

    def apply_chat_template(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Apply Llama 3 chat template to a raw prompt.
        
        Converts a plain text prompt into the proper Llama 3 Instruct format
        with special tokens for system/user/assistant roles.
        """
        if not LLM_APPLY_CHAT_TEMPLATE:
            return prompt
        
        self._resolve_tokenizer()
        
        # Build messages in chat format
        messages = []
        sys_prompt = system_prompt or LLM_DEFAULT_SYSTEM_PROMPT
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})
        messages.append({"role": "user", "content": prompt})
        
        # Try to use the tokenizer's built-in chat template
        if self._tokenizer is not None and hasattr(self._tokenizer, "apply_chat_template"):
            try:
                formatted = self._tokenizer.apply_chat_template(
                    messages,
                    tokenize=False,
                    add_generation_prompt=True,
                )
                return formatted
            except Exception as e:
                print(f"[llm] Warning: apply_chat_template failed: {e}, using fallback")
        
        # Fallback: manually construct Llama 3 format
        parts = ["<|begin_of_text|>"]
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            parts.append(f"<|start_header_id|>{role}<|end_header_id|>\n\n{content}<|eot_id|>")
        parts.append("<|start_header_id|>assistant<|end_header_id|>\n\n")
        return "".join(parts)


# Global backend instance (set during server startup)
_backend: Optional[AsyncVLLMBackend] = None
_tracer = get_tracer("llm-backend")
_inflight_count = 0
_inflight_lock = asyncio.Lock()


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({"status": "ok"})


async def handle_metrics(request: web.Request) -> web.Response:
    """Prometheus metrics endpoint."""
    if not _METRICS_READY or generate_latest is None:
        return web.json_response({"error": "Metrics disabled"}, status=503)
    output = generate_latest()
    # aiohttp forbids passing a content_type value that already includes a
    # charset parameter. Prometheus's CONTENT_TYPE_LATEST includes charset,
    # so we set the header directly instead of using the content_type kwarg.
    return web.Response(body=output, headers={"Content-Type": CONTENT_TYPE_LATEST})


async def handle_chat(request: web.Request) -> web.Response:
    """Handle chat/completion requests with async batching."""
    global _backend, _inflight_count

    if _backend is None:
        return web.json_response({"error": "Backend not initialized"}, status=503)

    # Extract tracing context from headers
    carrier = dict(request.headers)
    ctx = propagate.extract(carrier)

    with _tracer.start_as_current_span(
        "llm.handle_request",
        context=ctx,
        kind=SpanKind.SERVER,
    ) as span:
        start_time = time.monotonic()
        request_id = str(uuid.uuid4())[:8]  # Short ID for logs

        # Track in-flight requests
        async with _inflight_lock:
            _inflight_count += 1
            current_inflight = _inflight_count

        if _METRICS_READY:
            INFLIGHT.inc()

        span.set_attribute("app.path", request.path)
        span.set_attribute("app.request_id", request_id)

        try:
            data: Dict[str, Any] = await request.json()
        except json.JSONDecodeError:
            async with _inflight_lock:
                _inflight_count -= 1
            if _METRICS_READY:
                INFLIGHT.dec()
            return web.json_response({"error": "Invalid JSON"}, status=400)

        prompt = data.get("prompt") or data.get("input")
        if not isinstance(prompt, str) or not prompt:
            async with _inflight_lock:
                _inflight_count -= 1
            if _METRICS_READY:
                INFLIGHT.dec()
            return web.json_response({"error": "Missing 'prompt' field"}, status=400)

        # Apply chat template for Llama 3 Instruct format
        system_prompt = data.get("system_prompt")  # Optional override
        skip_template = data.get("skip_chat_template", False)
        original_prompt = prompt
        if not skip_template:
            prompt = _backend.apply_chat_template(prompt, system_prompt)

        span.set_attribute("app.prompt_length", len(original_prompt))
        span.set_attribute("app.formatted_prompt_length", len(prompt))
        span.set_attribute("app.chat_template_applied", not skip_template and LLM_APPLY_CHAT_TEMPLATE)
        if LOG_LLM_REQUESTS:
            span.set_attribute("app.prompt_preview", original_prompt[:200])
        _log_prompt("http", original_prompt)

        # Log request start
        template_info = " (templated)" if (not skip_template and LLM_APPLY_CHAT_TEMPLATE) else ""
        print(
            f"[llm] req={request_id} START inflight={current_inflight} "
            f"prompt_len={len(original_prompt)}{template_info}",
            flush=True,
        )

        status = "success"
        prompt_tokens: Optional[int] = None
        completion_tokens: Optional[int] = None
        text = ""
        queue_wait_s: float = 0.0

        try:
            # Delegate detailed timing to the backend: it returns the
            # generated text and the time spent waiting for a vLLM slot.
            text, queue_wait_s = await _backend.generate(
                prompt,
                request_id=request_id,
                log_progress=True,
            )

            prompt_tokens = _backend.count_tokens(prompt)
            completion_tokens = _backend.count_tokens(text)

            if prompt_tokens is not None:
                span.set_attribute("llm.prompt_tokens", prompt_tokens)
            if completion_tokens is not None:
                span.set_attribute("llm.completion_tokens", completion_tokens)
            if prompt_tokens is not None and completion_tokens is not None:
                span.set_attribute("llm.total_tokens", prompt_tokens + completion_tokens)

        except Exception as exc:  # pragma: no cover
            status = "error"
            async with _inflight_lock:
                _inflight_count -= 1
            if _METRICS_READY:
                INFLIGHT.dec()
            latency_s = time.monotonic() - start_time
            print(f"[llm] req={request_id} ERROR after {int(latency_s * 1000)}ms: {exc}", flush=True)
            _record_metrics(status, latency_s, queue_wait_s, prompt_tokens, completion_tokens)
            return web.json_response({"error": f"Generation failed: {exc}"}, status=500)

        async with _inflight_lock:
            _inflight_count -= 1
            remaining_inflight = _inflight_count

        if _METRICS_READY:
            INFLIGHT.dec()

        latency_s = time.monotonic() - start_time
        latency_ms = int(latency_s * 1000)

        # Log request completion
        print(
            f"[llm] req={request_id} DONE latency={latency_ms}ms "
            f"prompt={prompt_tokens} completion={completion_tokens} "
            f"remaining={remaining_inflight}",
            flush=True,
        )

        _record_metrics(status, latency_s, queue_wait_s, prompt_tokens, completion_tokens)
        _log_request_stats(
            {
                "status": status,
                "latency_ms": latency_ms,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        )

        return web.json_response({"output": text})


def create_app() -> web.Application:
    """Create the aiohttp application with routes."""
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/ready", handle_health)
    app.router.add_get("/live", handle_health)
    app.router.add_get("/metrics", handle_metrics)
    app.router.add_post("/chat", handle_chat)
    app.router.add_post("/completion", handle_chat)
    app.router.add_post("/generate", handle_chat)
    return app


async def run_async_server(
    host: str,
    port: int,
    model_name: str,
    max_model_len: int | None,
    dtype: str | None,
    max_num_seqs: int | None,
    max_num_batched_tokens: int | None,
    gpu_memory_utilization: float | None,
) -> None:
    """Run the async HTTP server with vLLM backend."""
    global _backend

    if web is None:
        raise RuntimeError("aiohttp is not installed. Please `pip install aiohttp`.")

    print(f"[*] Initializing AsyncLLMEngine for {model_name}...")
    _backend = AsyncVLLMBackend(
        model=model_name,
        max_model_len=max_model_len,
        dtype=dtype,
        max_num_seqs=max_num_seqs,
        max_num_batched_tokens=max_num_batched_tokens,
        gpu_memory_utilization=gpu_memory_utilization,
    )

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host, port)
    print("=" * 60)
    print(f"[*] vLLM async backend ready")
    print(f"    Model: {model_name}")
    print(f"    URL: http://{host}:{port}")
    print(f"    max_num_seqs: {max_num_seqs or 'default'}")
    print(f"    max_model_len: {max_model_len or 'default'}")
    print(f"    Batching: ENABLED (concurrent requests batched automatically)")
    print("=" * 60)

    await site.start()

    # Keep running until interrupted
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


def main(argv: List[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Async LLM backend (vLLM) for agentic traffic testbed")
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

    try:
        asyncio.run(
            run_async_server(
                args.host,
                args.port,
                args.model,
                args.max_model_len,
                dtype,
                max_num_seqs,
                max_num_batched_tokens,
                gpu_memory_utilization,
            )
        )
    except KeyboardInterrupt:
        print("\n[*] Shutting down vLLM async backend.")


if __name__ == "__main__":
    main()
