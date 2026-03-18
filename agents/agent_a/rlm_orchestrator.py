"""
RLM Orchestrator

Integrates the RLM (Recursive Language Models) framework with the AgentVerse
testbed. RLM enables an LLM to write Python code in a REPL loop, recursively
call itself, and delegate sub-tasks to Agent B instances — producing the
multi-hop, iterative network traffic patterns this testbed is designed to
study.

Reference: https://github.com/alexzhang13/rlm
Paper:     https://arxiv.org/abs/2512.24601

Scenarios
---------
  rlm_simple    — max_depth=0; plain LLM call routed through the RLM framework
                  (no REPL). Baseline traffic: one TCP flow, one LLM request.
  rlm_recursive — max_depth=1; LLM operates in a REPL loop and may issue
                  recursive sub-calls via ``rlm_subcall()``. Agent B instances
                  are available as callable tools. Creates bursty, nested
                  traffic patterns.
  rlm_parallel  — like rlm_recursive but multiple Agent B workers are exposed
                  as individually-named tools so the LLM can fan out to them
                  concurrently from the REPL.

API contract (mirrors /task and /agentverse)
--------------------------------------------
The dict returned by ``RLMOrchestrator.run_workflow()`` follows the same
top-level schema as the /task endpoint so that benchmark runners can treat
all three workflow types uniformly.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import httpx

from agents.common.metrics_logger import MetricsLogger
from agents.common.telemetry import TelemetryLogger


# ---------------------------------------------------------------------------
# Telemetry bridge for RLM's VerbosePrinter
# ---------------------------------------------------------------------------
# VerbosePrinter.print_iteration() is called after every REPL iteration but
# only if enabled=True (which would also print rich output to the terminal).
# We subclass it to redirect iteration events into our TelemetryLogger without
# any console output, giving per-iteration structured JSON log entries.
#
# The iteration callbacks (on_iteration_start / on_iteration_complete) exist
# in the RLM constructor signature but are never invoked in the loop — this
# bridge is the only reliable way to get per-iteration telemetry.

def _make_telemetry_verbose_printer(
    logger: "TelemetryLogger",
    task_id: str,
    iteration_times: list,
) -> "object":
    """
    Return a VerbosePrinter subclass instance that writes per-iteration events
    to *logger* instead of the rich console.  Imported lazily because RLM must
    already be on sys.path when this is called.
    """
    from rlm.logger.verbose import VerbosePrinter  # type: ignore

    class _TelemetryVerbosePrinter(VerbosePrinter):
        def __init__(self) -> None:
            super().__init__(enabled=False)  # silence rich/console output
            self._tlogger = logger
            self._task_id = task_id
            self._iter_times = iteration_times
            self._iter_count = 0

        def print_iteration(self, iteration: "object", iteration_num: int) -> None:  # type: ignore[override]
            self._iter_count += 1
            duration = getattr(iteration, "iteration_time", None) or 0.0
            self._iter_times.append(duration)
            code_blocks = getattr(iteration, "code_blocks", []) or []
            subcalls = sum(len(getattr(getattr(b, "result", None), "rlm_calls", []) or []) for b in code_blocks)
            self._tlogger.log(
                task_id=self._task_id,
                event_type="rlm_iteration_complete",
                message=f"[RLM] Iteration {iteration_num} done ({duration:.2f}s)",
                extra={
                    "iteration": iteration_num,
                    "duration_s": round(duration, 3),
                    "subcalls": subcalls,
                    "response_preview": (getattr(iteration, "response", "") or "")[:200],
                },
            )

        def print_summary(self, iterations: int, duration: float, usage: dict) -> None:  # type: ignore[override]
            self._tlogger.log(
                task_id=self._task_id,
                event_type="rlm_summary",
                message=f"[RLM] REPL completed: {iterations} iterations in {duration:.2f}s",
                extra={"iterations": iterations, "duration_s": round(duration, 3), "usage": usage},
            )

        # Suppress all other rich output methods silently.
        def print_iteration_start(self, *a: object, **kw: object) -> None: pass  # type: ignore[override]
        def print_completion(self, *a: object, **kw: object) -> None: pass  # type: ignore[override]
        def print_code_execution(self, *a: object, **kw: object) -> None: pass  # type: ignore[override]
        def print_subcall(self, *a: object, **kw: object) -> None: pass  # type: ignore[override]
        def print_final_answer(self, *a: object, **kw: object) -> None: pass  # type: ignore[override]
        def print_limit_exceeded(self, *a: object, **kw: object) -> None: pass  # type: ignore[override]
        def print_metadata(self, *a: object, **kw: object) -> None: pass  # type: ignore[override]
        def print_compaction(self, *a: object, **kw: object) -> None: pass  # type: ignore[override]
        def print_compaction_status(self, *a: object, **kw: object) -> None: pass  # type: ignore[override]
        def print_budget_exceeded(self, *a: object, **kw: object) -> None: pass  # type: ignore[override]

    return _TelemetryVerbosePrinter()


# ---------------------------------------------------------------------------
# Environment config
# ---------------------------------------------------------------------------

RLM_ROOT = os.environ.get("RLM_ROOT", "/home/dlamagna/projects/rlm")

# vLLM exposes an OpenAI-compatible API at /v1 alongside the custom /chat
# endpoint used by the rest of the testbed.  Derive the base URL from the
# existing LLM_SERVER_URL env var, or allow an explicit override.
_llm_server_url = os.environ.get("LLM_SERVER_URL", "http://localhost:8000/chat")
_llm_base_default = _llm_server_url.rstrip("/chat").rstrip("/") + "/v1"
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", _llm_base_default)

MODEL_NAME = os.environ.get("MODEL_NAME", "meta-llama/Llama-3.1-8B-Instruct")
AGENT_B_TIMEOUT_SECONDS = float(os.environ.get("AGENT_B_TIMEOUT_SECONDS", "120"))

RLM_DEFAULT_MAX_DEPTH = int(os.environ.get("RLM_MAX_DEPTH", "1"))
RLM_DEFAULT_MAX_ITERATIONS = int(os.environ.get("RLM_MAX_ITERATIONS", "30"))
RLM_DEFAULT_MAX_TOKENS: Optional[int] = int(os.environ.get("RLM_MAX_TOKENS", "0")) or None
RLM_DEFAULT_MAX_TIMEOUT: Optional[float] = float(os.environ.get("RLM_MAX_TIMEOUT", "0")) or None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _inject_rlm() -> None:
    """Prepend the RLM repo to sys.path so it can be imported without pip-install."""
    if RLM_ROOT and RLM_ROOT not in sys.path:
        sys.path.insert(0, RLM_ROOT)


def _make_agent_b_caller(
    url: str,
    task_id: str,
    worker_index: int,
    logger: TelemetryLogger,
    llm_requests: List[Dict[str, Any]],
) -> Callable[[str], str]:
    """
    Return a synchronous callable that Agent B receives as a tool inside the
    RLM REPL.  The callable POSTs to Agent B's /subtask endpoint, captures the
    response, and appends a record to ``llm_requests`` for telemetry.

    The function signature visible to the LLM is simply::

        call_agent_b(subtask: str) -> str
    """
    def call_agent_b(subtask: str) -> str:
        tool_call_id = logger.new_tool_call_id()
        logger.log(
            task_id=task_id,
            event_type="agent_b_request",
            message=f"[RLM] Calling Agent B worker {worker_index}",
            tool_call_id=tool_call_id,
            extra={"url": url, "subtask_preview": subtask[:200]},
        )
        try:
            resp = httpx.post(
                url,
                json={"subtask": subtask, "scenario": "rlm_sub_call"},
                headers={"X-Task-ID": task_id},
                timeout=AGENT_B_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
            output = str(data.get("output", ""))
            llm_requests.append({
                "source": "agent_b",
                "label": f"rlm_worker_{worker_index}",
                "prompt": subtask,
                "response": output,
                "endpoint": url,
                "llm_meta": data.get("llm_meta") or {},
            })
            logger.log(
                task_id=task_id,
                event_type="agent_b_response",
                message=f"[RLM] Agent B worker {worker_index} responded",
                tool_call_id=tool_call_id,
                extra={"output_preview": output[:200]},
            )
            return output
        except Exception as exc:
            logger.log(
                task_id=task_id,
                event_type="agent_b_error",
                message=f"[RLM] Agent B worker {worker_index} failed: {exc}",
                tool_call_id=tool_call_id,
            )
            return f"[Agent B error: {exc}]"

    # Give the function a meaningful name so it appears clearly in REPL help text.
    call_agent_b.__name__ = "call_agent_b" if worker_index == 0 else f"call_agent_b_{worker_index}"
    return call_agent_b


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class RLMOrchestrator:
    """
    Manages an RLM execution against the local vLLM backend.

    Agent B instances are injected into the REPL as named Python functions,
    allowing the LLM to delegate sub-tasks via ordinary function calls.  All
    inter-service calls are logged to both the TelemetryLogger (event stream)
    and the MetricsLogger (JSONL per-call records), keeping RLM telemetry in
    the same format as the rest of the testbed.
    """

    def __init__(
        self,
        logger: TelemetryLogger,
        metrics_logger: MetricsLogger,
    ) -> None:
        self.logger = logger
        self.metrics_logger = metrics_logger

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_workflow(
        self,
        task: str,
        task_id: str,
        scenario: str = "rlm_recursive",
        max_depth: int = RLM_DEFAULT_MAX_DEPTH,
        max_iterations: int = RLM_DEFAULT_MAX_ITERATIONS,
        max_tokens: Optional[int] = RLM_DEFAULT_MAX_TOKENS,
        max_timeout: Optional[float] = RLM_DEFAULT_MAX_TIMEOUT,
        agent_b_workers: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Execute an RLM workflow and return a result dict that mirrors the /task
        response schema so benchmark runners can treat all three endpoint types
        uniformly.

        Parameters
        ----------
        task:
            The user task or prompt text.
        task_id:
            Correlation ID for telemetry (caller is responsible for creating it).
        scenario:
            One of ``rlm_simple``, ``rlm_recursive``, ``rlm_parallel``.
        max_depth:
            RLM recursion depth.  0 = plain LLM (no REPL).  1+ = REPL loop
            with optional recursive sub-calls.  Ignored for ``rlm_simple``.
        max_iterations:
            Maximum REPL loop iterations per completion.
        max_tokens:
            Optional cumulative token budget; RLM stops and returns the best
            answer found so far when exceeded.
        max_timeout:
            Optional wall-clock timeout in seconds.
        agent_b_workers:
            List of worker spec dicts, each with optional ``endpoint``,
            ``role``, and ``contract`` keys.  If omitted, no Agent B tools are
            registered in the REPL.
        """
        _inject_rlm()
        try:
            from rlm.core.rlm import RLM  # type: ignore[import-error]
        except ImportError as exc:
            raise RuntimeError(
                f"Cannot import RLM from '{RLM_ROOT}'. "
                "Set the RLM_ROOT environment variable to the cloned repo path. "
                f"Original error: {exc}"
            ) from exc

        task_start = datetime.now(timezone.utc).isoformat()
        self.logger.log(
            task_id=task_id,
            event_type="rlm_request_received",
            message=f"[RLM] Starting workflow (scenario={scenario}, depth={max_depth})",
            extra={"scenario": scenario, "max_depth": max_depth, "max_iterations": max_iterations},
        )

        # Mutable state shared between callbacks and the main body.
        llm_requests: List[Dict[str, Any]] = []
        rlm_subcall_count = 0
        rlm_iteration_times: List[float] = []

        # ------------------------------------------------------------------
        # Build custom_tools: expose Agent B workers as REPL-callable functions.
        # ------------------------------------------------------------------
        custom_tools: Dict[str, Any] = {}
        workers: List[Dict[str, Any]] = agent_b_workers or []
        for idx, worker in enumerate(workers):
            url = (worker.get("endpoint") or "").strip() or "http://agent-b:8102/subtask"
            caller = _make_agent_b_caller(
                url=url,
                task_id=task_id,
                worker_index=idx,
                logger=self.logger,
                llm_requests=llm_requests,
            )
            # First worker gets the canonical name; additional workers are
            # call_agent_b_1, call_agent_b_2, … so the LLM can address them
            # individually for parallel fan-out.
            tool_name = "call_agent_b" if idx == 0 else f"call_agent_b_{idx}"
            custom_tools[tool_name] = caller

        # rlm_simple forces depth=0 (no REPL, no recursion) regardless of
        # max_depth, giving a clean single-call baseline.
        effective_depth = 0 if scenario == "rlm_simple" else max_depth

        # ------------------------------------------------------------------
        # Telemetry callbacks.
        # ------------------------------------------------------------------
        def _on_subcall_start(depth: int, model: str, prompt_preview: str) -> None:
            nonlocal rlm_subcall_count
            rlm_subcall_count += 1
            self.logger.log(
                task_id=task_id,
                event_type="rlm_subcall_start",
                message=f"[RLM] Recursive sub-call at depth {depth}",
                extra={"depth": depth, "model": model, "prompt_preview": prompt_preview[:200]},
            )

        def _on_subcall_complete(depth: int, model: str, duration: float, error: Optional[str]) -> None:
            self.logger.log(
                task_id=task_id,
                event_type="rlm_subcall_complete",
                message=f"[RLM] Sub-call at depth {depth} completed ({duration:.2f}s)",
                extra={"depth": depth, "duration_s": round(duration, 3), "error": error},
            )

        # ------------------------------------------------------------------
        # Instantiate and run RLM.
        # ------------------------------------------------------------------
        rlm = RLM(
            backend="vllm",
            backend_kwargs={
                "model_name": MODEL_NAME,
                "base_url": LLM_BASE_URL,
                "api_key": "dummy",  # vLLM does not require a real key.
            },
            environment="local",
            max_depth=effective_depth,
            max_iterations=max_iterations,
            max_tokens=max_tokens,
            max_timeout=max_timeout,
            custom_tools=custom_tools if custom_tools else None,
            verbose=False,
            on_subcall_start=_on_subcall_start,
            on_subcall_complete=_on_subcall_complete,
            # on_iteration_start / on_iteration_complete are stored by RLM but
            # never called in the loop.  We inject a VerbosePrinter subclass
            # below to get reliable per-iteration telemetry instead.
        )
        # Replace RLM's no-op verbose printer with our telemetry bridge so
        # every REPL iteration produces a structured JSON log entry.
        rlm.verbose = _make_telemetry_verbose_printer(self.logger, task_id, rlm_iteration_times)

        ts_llm_start = datetime.now(timezone.utc).isoformat()
        try:
            result = rlm.completion(task)
        except Exception as exc:
            self.logger.log(
                task_id=task_id,
                event_type="rlm_error",
                message=f"[RLM] Workflow failed: {exc}",
            )
            raise
        ts_llm_end = datetime.now(timezone.utc).isoformat()

        # ------------------------------------------------------------------
        # Extract result fields.
        # rlm_simple (depth=0) hits _fallback_answer() which returns a plain
        # str; rlm_recursive returns an RLMChatCompletion object.
        # ------------------------------------------------------------------
        if isinstance(result, str):
            output: str = result
            usage = None
        else:
            output = result.response or ""
            usage = result.usage_summary
        total_input_tokens = int(usage.total_input_tokens) if usage else 0
        total_output_tokens = int(usage.total_output_tokens) if usage else 0
        total_tokens = total_input_tokens + total_output_tokens
        cost_raw = usage.total_cost if usage else None
        cost_estimate_usd = float(cost_raw) if cost_raw is not None else None

        llm_latency_ms = int(sum(rlm_iteration_times) * 1000)

        # Log a single aggregated record to MetricsLogger so the RLM workflow
        # appears in logs/llm_calls.jsonl alongside AgentVerse calls.
        self.metrics_logger.log_call(
            task_id=task_id,
            agent_id="AgentA-RLM",
            call_type="root",
            timestamp_start=ts_llm_start,
            timestamp_end=ts_llm_end,
            http_status=200,
            llm_meta={
                "prompt_tokens": total_input_tokens,
                "completion_tokens": total_output_tokens,
                "total_tokens": total_tokens,
                "latency_ms": llm_latency_ms,
            },
        )

        # Wall-clock latency for the full task (includes Agent B calls).
        try:
            _s = datetime.fromisoformat(task_start)
            _e = datetime.fromisoformat(ts_llm_end)
            total_latency_ms: Optional[int] = int((_e - _s).total_seconds() * 1000)
        except Exception:
            total_latency_ms = None

        total_agent_hops = sum(1 for r in llm_requests if r.get("source") == "agent_b")
        # Count root call + all LLM sub-calls issued by RLM recursion.
        total_llm_calls = 1 + rlm_subcall_count

        self.logger.log(
            task_id=task_id,
            event_type="rlm_complete",
            message="[RLM] Workflow complete",
            extra={
                "iterations": len(rlm_iteration_times),
                "subcalls": rlm_subcall_count,
                "total_tokens": total_tokens,
                "total_latency_ms": total_latency_ms,
            },
        )

        return {
            "task_id": task_id,
            "agent_id": "AgentA",
            "scenario": scenario,
            "task_query": task,
            "task_start": task_start,
            "task_end": ts_llm_end,
            "output": output,
            # RLM-specific trace fields.
            "rlm_iterations": len(rlm_iteration_times),
            "rlm_subcalls": rlm_subcall_count,
            "rlm_execution_time_s": round(result.execution_time, 3) if not isinstance(result, str) and result.execution_time else None,
            # Standard aggregates (same keys as /task).
            "total_llm_calls": total_llm_calls,
            "total_agent_hops": total_agent_hops,
            "total_prompt_tokens": total_input_tokens,
            "total_completion_tokens": total_output_tokens,
            "total_tokens": total_tokens,
            "total_latency_ms": total_latency_ms,
            "llm_latency_ms": llm_latency_ms,
            "cost_estimate_usd": cost_estimate_usd,
            "llm_requests": llm_requests,
        }
