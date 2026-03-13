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
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "512"))
LLM_MAX_MODEL_LEN = int(os.environ.get("LLM_MAX_MODEL_LEN") or "0")
# Safety margin (tokens) reserved beyond the completion budget to absorb
# chat-template overhead and minor tokenizer discrepancies.
LLM_PROMPT_SAFETY_MARGIN_TOKENS = int(os.environ.get("LLM_PROMPT_SAFETY_MARGIN_TOKENS", "128"))
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
    _LLM_LATENCY_BUCKETS = [0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0, 180.0]
    REQUEST_LATENCY = Histogram(
        f"{LLM_METRICS_PREFIX}_request_latency_seconds",
        "End-to-end LLM request latency",
        buckets=_LLM_LATENCY_BUCKETS,
    )
    QUEUE_WAIT = Histogram(
        f"{LLM_METRICS_PREFIX}_queue_wait_seconds",
        "Time spent waiting in vLLM queue",
        buckets=_LLM_LATENCY_BUCKETS,
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
    CONFIG_MAX_NUM_SEQS = Gauge(
        f"{LLM_METRICS_PREFIX}_config_max_num_seqs",
        "Configured max_num_seqs (vLLM scheduler concurrency); -1 means default",
    )
    CONFIG_MAX_NUM_BATCHED_TOKENS = Gauge(
        f"{LLM_METRICS_PREFIX}_config_max_num_batched_tokens",
        "Configured max_num_batched_tokens (vLLM scheduler); -1 means default",
    )
    CONFIG_GPU_MEMORY_UTILIZATION = Gauge(
        f"{LLM_METRICS_PREFIX}_config_gpu_memory_utilization",
        "Configured GPU memory utilization target (0-1); -1 means default",
    )
    CONFIG_MAX_TOKENS = Gauge(
        f"{LLM_METRICS_PREFIX}_config_max_tokens",
        "Configured max tokens per generation (LLM_MAX_TOKENS)",
    )
    CONFIG_COMPUTED_MAX_CONCURRENCY = Gauge(
        f"{LLM_METRICS_PREFIX}_computed_max_concurrency",
        "KV-cache-derived max concurrency: num_gpu_blocks * block_size / max_model_len "
        "(matches the 'Maximum concurrency for X tokens' line vLLM logs at startup)",
    )
    INTERARRIVAL_TIME = Histogram(
        f"{LLM_METRICS_PREFIX}_interarrival_seconds",
        "Time between consecutive LLM request arrivals",
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
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


async def _probe_engine_max_concurrency() -> None:
    """Background task: expose vLLM's KV-cache-derived max concurrency as a Prometheus gauge.

    vLLM logs this at startup:
        Maximum concurrency for <max_model_len> tokens per request: X.XXx

    The value is: num_gpu_blocks * block_size / max_model_len.
    We try multiple strategies in order of reliability:
      1. Pre-V1 (<0.6): access engine.cache_config via several possible sync-engine attributes.
      2. Prometheus registry scan: look for any vLLM metric whose name contains 'num_gpu_blocks'
         but not 'used' (total-blocks metrics), with dynamic block_size detection.
    The probe is retried up to 3 times with increasing delays to handle slow engine init.
    """
    if not _METRICS_READY:
        return

    for attempt, delay in enumerate([5, 15, 30]):
        await asyncio.sleep(delay)

        # Strategy 1: pre-V1 AsyncLLMEngine wraps a sync LLMEngine under various attribute names
        for attr in ("engine", "_engine", "llm_engine", "_llm_engine"):
            try:
                core = getattr(_backend._engine, attr)  # type: ignore[union-attr]
                num_gpu_blocks: int = core.cache_config.num_gpu_blocks
                block_size: int = core.cache_config.block_size
                max_model_len: int = core.model_config.max_model_len
                if num_gpu_blocks and max_model_len:
                    computed = (num_gpu_blocks * block_size) / max_model_len
                    CONFIG_COMPUTED_MAX_CONCURRENCY.set(computed)
                    print(
                        f"[llm-metrics] computed_max_concurrency={computed:.2f} "
                        f"(gpu_blocks={num_gpu_blocks} block_size={block_size} "
                        f"max_model_len={max_model_len}) [attempt {attempt + 1}]"
                    )
                    return
            except AttributeError:
                continue

        # Strategy 2: scan Prometheus registry for any vLLM gpu-blocks total metric.
        # Metric names vary by vLLM version; we match dynamically instead of hardcoding.
        try:
            from prometheus_client import REGISTRY  # type: ignore

            ref_max_model_len = LLM_MAX_MODEL_LEN

            # If LLM_MAX_MODEL_LEN was not configured, try to read it from the engine
            if ref_max_model_len == 0:
                for attr_chain in [
                    ("engine", "model_config", "max_model_len"),
                    ("_engine", "model_config", "max_model_len"),
                    ("model_config", "max_model_len"),
                ]:
                    try:
                        obj = _backend._engine  # type: ignore[union-attr]
                        for attr in attr_chain:
                            obj = getattr(obj, attr)
                        ref_max_model_len = int(obj)  # type: ignore[arg-type]
                        break
                    except AttributeError:
                        continue

            # Also try to read block_size from the registry
            detected_block_size: Optional[int] = None
            num_gpu_blocks_val: float = 0.0

            for metric_family in REGISTRY.collect():
                name_norm = metric_family.name.lower().replace(":", "_")
                # Match any metric that exposes total GPU blocks (exclude *_used variants)
                if "num_gpu_blocks" in name_norm and "used" not in name_norm:
                    for sample in metric_family.samples:
                        if sample.value > 0:
                            num_gpu_blocks_val = sample.value
                # Some vLLM versions expose block_size directly
                if "block_size" in name_norm and "gpu" in name_norm:
                    for sample in metric_family.samples:
                        if sample.value > 0:
                            detected_block_size = int(sample.value)

            if num_gpu_blocks_val > 0 and ref_max_model_len > 0:
                block_size = detected_block_size or int(os.environ.get("VLLM_BLOCK_SIZE", "16"))
                computed = (num_gpu_blocks_val * block_size) / ref_max_model_len
                CONFIG_COMPUTED_MAX_CONCURRENCY.set(computed)
                print(
                    f"[llm-metrics] computed_max_concurrency={computed:.2f} "
                    f"(gpu_blocks={num_gpu_blocks_val} block_size={block_size} "
                    f"max_model_len={ref_max_model_len}) [attempt {attempt + 1}]"
                )
                return
        except Exception:
            pass

    print(
        "[llm-metrics] Warning: could not determine computed_max_concurrency "
        "from vLLM internals after 3 attempts — gauge will remain unset."
    )


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
        self._default_sampling = SamplingParams(temperature=0.2, max_tokens=LLM_MAX_TOKENS)
        self._tokenizer = None
        self._tokenizer_ready = False

    async def generate(
        self,
        prompt: str,
        request_id: str | None = None,
        log_progress: bool = True,
        max_tokens: int | None = None,
    ) -> tuple[str, float]:
        """Generate text asynchronously. Concurrent calls are batched by vLLM.

        Returns a tuple of (generated_text, queue_wait_seconds), where
        queue_wait_seconds represents the time spent waiting in vLLM's
        internal scheduler before the first token is produced.
        """
        if request_id is None:
            request_id = str(uuid.uuid4())[:8]

        # Use per-request max_tokens when provided, otherwise fall back to default sampling.
        sampling = (
            SamplingParams(temperature=0.2, max_tokens=max_tokens)
            if max_tokens is not None
            else self._default_sampling
        )

        results_generator = self._engine.generate(
            prompt,
            sampling,
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
_last_arrival_time: Optional[float] = None
_arrival_lock = asyncio.Lock()


def _otel_span_metadata(span: Any) -> Dict[str, Any]:
    """Best-effort OpenTelemetry span metadata for JSON responses."""
    meta: Dict[str, Any] = {}
    try:
        ctx = span.get_span_context()
        meta["trace_id"] = f"{int(ctx.trace_id):032x}"
        meta["span_id"] = f"{int(ctx.span_id):016x}"
        meta["trace_flags"] = int(getattr(ctx, "trace_flags", 0))
        meta["is_remote"] = bool(getattr(ctx, "is_remote", False))
    except Exception:
        pass

    attrs: Dict[str, Any] = {}
    for attr_name in ("attributes", "_attributes"):
        try:
            raw = getattr(span, attr_name, None)
            if raw and isinstance(raw, dict):
                attrs.update(raw)
        except Exception:
            continue
    if attrs:
        meta["attributes"] = attrs
    return meta


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
    global _backend, _inflight_count, _last_arrival_time

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

        # Record interarrival time
        async with _arrival_lock:
            if _last_arrival_time is not None and _METRICS_READY:
                INTERARRIVAL_TIME.observe(start_time - _last_arrival_time)
            _last_arrival_time = start_time

        # Track in-flight requests
        async with _inflight_lock:
            _inflight_count += 1
            current_inflight = _inflight_count

        if _METRICS_READY:
            INFLIGHT.inc()

        span.set_attribute("app.path", request.path)
        
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

        max_tokens = data.get("max_tokens")
        if max_tokens is not None and not isinstance(max_tokens, int):
            try:
                max_tokens = int(max_tokens)
            except (TypeError, ValueError):
                max_tokens = None

        # Resolve or generate a stable request ID for this LLM call.
        # Prefer an upstream-provided ID so UI and Docker logs can be correlated.
        client_request_id = request.headers.get("X-Request-ID") or data.get("request_id")
        if client_request_id:
            request_id = str(client_request_id)
        else:
            request_id = str(uuid.uuid4())[:8]  # Short ID for logs

        span.set_attribute("app.request_id", request_id)

        # Apply chat template for Llama 3 Instruct format
        system_prompt = data.get("system_prompt")  # Optional override
        skip_template = data.get("skip_chat_template", False)
        original_prompt = prompt
        if not skip_template:
            prompt = _backend.apply_chat_template(prompt, system_prompt)

        # Truncate the formatted prompt if it would exceed the model's context
        # window.  We work at the token level so the cut is clean, then decode
        # back to a string for vLLM.  This is a last-resort safety net; the
        # orchestrator-side guardrails should trim prompts earlier in most cases.
        prompt_truncated = False
        prompt_truncated_tokens: Optional[int] = None
        if LLM_MAX_MODEL_LEN > 0:
            _backend._resolve_tokenizer()
            if _backend._tokenizer is not None:
                try:
                    effective_max_new_tokens = (
                        max_tokens if max_tokens is not None else LLM_MAX_TOKENS
                    )
                    max_input_tokens = max(
                        0,
                        LLM_MAX_MODEL_LEN
                        - effective_max_new_tokens
                        - LLM_PROMPT_SAFETY_MARGIN_TOKENS,
                    )
                    token_ids = _backend._tokenizer.encode(
                        prompt, add_special_tokens=False
                    )
                    if len(token_ids) > max_input_tokens:
                        prompt_truncated_tokens = len(token_ids) - max_input_tokens
                        token_ids = token_ids[:max_input_tokens]
                        prompt = _backend._tokenizer.decode(token_ids)
                        prompt_truncated = True
                        print(
                            f"[llm] req={request_id} PROMPT_TRUNCATED "
                            f"original_tokens={len(token_ids) + prompt_truncated_tokens} "
                            f"kept={max_input_tokens} "
                            f"dropped={prompt_truncated_tokens}",
                            flush=True,
                        )
                except Exception as _trunc_exc:
                    print(
                        f"[llm] req={request_id} WARNING: prompt truncation failed: {_trunc_exc}",
                        flush=True,
                    )

        span.set_attribute("app.prompt_length", len(original_prompt))
        span.set_attribute("app.formatted_prompt_length", len(prompt))
        span.set_attribute("app.chat_template_applied", not skip_template and LLM_APPLY_CHAT_TEMPLATE)
        span.set_attribute("app.prompt_truncated", prompt_truncated)
        if prompt_truncated_tokens is not None:
            span.set_attribute("app.prompt_truncated_tokens", int(prompt_truncated_tokens))
        if LOG_LLM_REQUESTS:
            span.set_attribute("app.prompt_preview", original_prompt[:200])
        _log_prompt("http", original_prompt)

        # Log request start
        template_info = " (templated)" if (not skip_template and LLM_APPLY_CHAT_TEMPLATE) else ""
        trunc_info = f" [TRUNCATED -{prompt_truncated_tokens}tok]" if prompt_truncated else ""
        print(
            f"[llm] req={request_id} START inflight={current_inflight} "
            f"prompt_len={len(original_prompt)}{template_info}{trunc_info}",
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
                max_tokens=max_tokens,
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

        # Include rich metadata for UI/raw JSON correlation. The UI doesn't need to
        # be updated for new keys; it will simply display the new JSON fields.
        meta: Dict[str, Any] = {
            "request_id": request_id,
            "latency_ms": latency_ms,
            "queue_wait_s": round(queue_wait_s, 4),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": (prompt_tokens + completion_tokens) if (prompt_tokens is not None and completion_tokens is not None) else None,
            "otel": _otel_span_metadata(span),
        }

        return web.json_response({"output": text, "meta": meta})


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

    # Export static configuration values as Prometheus gauges so Grafana can
    # put concurrency and latency into context.
    if _METRICS_READY:
        # Use -1 to indicate "default" / not explicitly set for optional values.
        CONFIG_MAX_NUM_SEQS.set(float(max_num_seqs) if max_num_seqs is not None else -1.0)
        CONFIG_MAX_NUM_BATCHED_TOKENS.set(
            float(max_num_batched_tokens) if max_num_batched_tokens is not None else -1.0
        )
        CONFIG_GPU_MEMORY_UTILIZATION.set(
            float(gpu_memory_utilization) if gpu_memory_utilization is not None else -1.0
        )
        CONFIG_MAX_TOKENS.set(float(LLM_MAX_TOKENS))

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

    # Probe vLLM's KV cache config once the engine workers have initialised
    # and expose the computed max concurrency as a Prometheus gauge.
    asyncio.create_task(_probe_engine_max_concurrency())

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
