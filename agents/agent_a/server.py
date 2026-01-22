import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, Optional

from opentelemetry import context as otel_context
from opentelemetry import propagate
from opentelemetry.trace import SpanKind

from agents.agent_a.main import AGENT_B_URLS, LLM_SERVER_URL, call_agent_b, call_llm
from agents.common.telemetry import TelemetryLogger
from agents.common.tracing import get_tracer


HOST = "0.0.0.0"
PORT = int(os.environ.get("AGENT_A_PORT", "8101"))
MAX_AGENT_B_TURNS = int(os.environ.get("MAX_AGENT_B_TURNS", "3"))
CONTEXT_PREVIEW_LEN = 300
MAX_PARALLEL_WORKERS = int(os.environ.get("MAX_PARALLEL_WORKERS", "5"))


def _clean_string(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalize_workers(
    requested_count: Optional[int],
    worker_payloads: Any,
    fallback_urls: Iterable[str],
) -> list[Dict[str, Optional[str]]]:
    urls = [url for url in fallback_urls if url]
    if not urls:
        urls = ["http://agent-b:8102/subtask"]

    count = requested_count if isinstance(requested_count, int) and requested_count > 0 else len(urls)
    count = min(count, MAX_PARALLEL_WORKERS)

    payloads: list[Dict[str, Any]] = worker_payloads if isinstance(worker_payloads, list) else []
    normalized: list[Dict[str, Optional[str]]] = []

    for idx in range(count):
        payload = payloads[idx] if idx < len(payloads) and isinstance(payloads[idx], dict) else {}
        endpoint = _clean_string(payload.get("endpoint"))
        role = _clean_string(payload.get("role"))
        contract = _clean_string(payload.get("contract"))
        if not endpoint:
            endpoint = urls[idx % len(urls)]
        normalized.append({"endpoint": endpoint, "role": role, "contract": contract})

    return normalized


def _parse_subtasks(raw: str, desired_count: int, fallback_task: str) -> list[str]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None

    subtasks: list[str] = []
    if isinstance(parsed, list):
        subtasks = [str(item).strip() for item in parsed if str(item).strip()]
    elif isinstance(parsed, dict):
        items = parsed.get("subtasks")
        if isinstance(items, list):
            subtasks = [str(item).strip() for item in items if str(item).strip()]

    if not subtasks:
        subtasks = [f"Subtask {idx + 1}: {fallback_task}" for idx in range(desired_count)]

    if len(subtasks) < desired_count:
        subtasks += [
            f"Subtask {idx + 1}: {fallback_task}"
            for idx in range(len(subtasks), desired_count)
        ]
    return subtasks[:desired_count]


def handle_tool_call(task: str, logger: TelemetryLogger, task_id: str) -> str:
    pass


class AgentARequestHandler(BaseHTTPRequestHandler):
    logger = TelemetryLogger(agent_id="AgentA")
    tracer = get_tracer("agent-a")

    def _set_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._set_cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # type: ignore[override]
        if self.path != "/task":
            self.send_response(404)
        else:
            self.send_response(204)
        self._set_cors()
        self.end_headers()

    def do_POST(self) -> None:  # type: ignore[override]
        if self.path != "/task":
            self._send_json(404, {"error": "Not found"})
            return

        with self.tracer.start_as_current_span("agent_a.handle_task") as span:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b""

            try:
                data: Dict[str, Any] = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON"})
                return

            task = data.get("task")
            scenario = data.get("scenario")
            agent_a_role = data.get("agent_a_role") if isinstance(data.get("agent_a_role"), str) else None
            agent_a_contract = (
                data.get("agent_a_contract") if isinstance(data.get("agent_a_contract"), str) else None
            )
            agent_b_role = data.get("agent_b_role") if isinstance(data.get("agent_b_role"), str) else None
            agent_b_contract = (
                data.get("agent_b_contract") if isinstance(data.get("agent_b_contract"), str) else None
            )
            agent_count = data.get("agent_count")
            agent_b_workers = data.get("agent_b_workers")
            if not isinstance(task, str) or not task:
                self._send_json(400, {"error": "Missing 'task' field"})
                return

            span.set_attribute("app.task", task)
            if scenario:
                span.set_attribute("app.scenario", scenario)
            if agent_a_role:
                span.set_attribute("app.agent_role", agent_a_role)
                span.set_attribute(
                    "app.role_service",
                    f"{os.environ.get('OTEL_SERVICE_NAME', 'agent-a')}:{agent_a_role}",
                )

            # New task / telemetry
            logger = self.logger
            logger.scenario = scenario  # type: ignore[assignment]
            task_id = logger.new_task_id()
            span.set_attribute("app.task_id", task_id)
            logger.log(
                task_id=task_id,
                event_type="task_received",
                message=task,
                extra={"agent_role": agent_a_role} if agent_a_role else None,
            )

            final_prompt: str
            agent_b_output: Optional[str] = None
            agent_b_outputs: list[str] = []
            agent_a_progress_notes: list[str] = []
            max_turns = MAX_AGENT_B_TURNS
            requested_turns = data.get("max_agent_turns")
            if isinstance(requested_turns, int) and requested_turns > 0:
                max_turns = min(requested_turns, MAX_AGENT_B_TURNS)

            role_context_parts = []
            if agent_a_role:
                role_context_parts.append(f"Role: {agent_a_role}")
            if agent_a_contract:
                role_context_parts.append(f"Contract: {agent_a_contract}")
            role_context = "\n".join(role_context_parts)
            role_context_block = f"{role_context}\n" if role_context else ""

            if scenario == "agentic_parallel":
                workers = _normalize_workers(agent_count, agent_b_workers, AGENT_B_URLS)
                logger.log(
                    task_id=task_id,
                    event_type="agent_a_parallel_setup",
                    message="Planning parallel subtasks",
                    extra={"worker_count": len(workers)},
                )

                planning_prompt = (
                    "You are Agent A, acting as the planner. Break the user task into "
                    f"{len(workers)} concrete, independent subtasks. Return ONLY valid JSON "
                    'as an array of strings, e.g. ["subtask 1", "subtask 2"].\n\n'
                    f"{role_context_block}"
                    f"User task:\n{task}"
                )
                try:
                    with self.tracer.start_as_current_span(
                        "agent_a.plan_subtasks",
                        kind=SpanKind.CLIENT,
                    ) as span_plan:
                        span_plan.set_attribute("app.llm.url", LLM_SERVER_URL)
                        headers: Dict[str, str] = {}
                        propagate.inject(headers)
                        planned_raw = call_llm(planning_prompt, headers=headers)
                except Exception as exc:
                    logger.log(
                        task_id=task_id,
                        event_type="agent_a_planning_error",
                        message=f"Planning failed: {exc}",
                    )
                    planned_raw = "[]"

                subtasks = _parse_subtasks(planned_raw, len(workers), task)
                logger.log(
                    task_id=task_id,
                    event_type="agent_a_planning_complete",
                    message="Subtasks planned",
                    extra={"subtasks": subtasks},
                )

                agent_b_outputs = []
                def _run_worker_call(
                    parent_ctx: otel_context.Context,
                    worker_index: int,
                    worker: Dict[str, Optional[str]],
                    subtask: str,
                ) -> str:
                    token = otel_context.attach(parent_ctx)
                    role = worker["role"] or agent_b_role
                    try:
                        with self.tracer.start_as_current_span(
                            "agent_a.call_agent_b_parallel",
                            kind=SpanKind.CLIENT,
                        ) as span_b:
                            span_b.set_attribute("app.agent_b.url", worker["endpoint"] or "")
                            span_b.set_attribute("app.agent_index", worker_index)
                            if role:
                                span_b.set_attribute("app.agent_role", role)
                            headers: Dict[str, str] = {"x-agent-index": str(worker_index)}
                            propagate.inject(headers)
                            return call_agent_b(
                                subtask,
                                scenario=scenario,
                                headers=headers,
                                agent_b_role=role,
                                agent_b_contract=worker["contract"] or agent_b_contract,
                                agent_b_url=worker["endpoint"],
                            )
                    finally:
                        otel_context.detach(token)

                request_ctx = otel_context.get_current()
                with ThreadPoolExecutor(max_workers=len(workers)) as executor:
                    future_map = {}
                    for idx, (worker, subtask) in enumerate(zip(workers, subtasks), start=1):
                        tool_call_id_b = logger.new_tool_call_id()
                        logger.log(
                            task_id=task_id,
                            event_type="agent_b_request",
                            message=f"Calling worker {idx} for parallel subtask",
                            tool_call_id=tool_call_id_b,
                            extra={
                                "url": worker["endpoint"],
                                "agent_index": idx,
                                "agent_role": worker["role"] or agent_b_role,
                                "subtask_preview": subtask[:CONTEXT_PREVIEW_LEN],
                            },
                        )
                        future = executor.submit(
                            _run_worker_call,
                            request_ctx,
                            idx,
                            worker,
                            subtask,
                        )
                        future_map[future] = (idx, worker, subtask)

                    for future in as_completed(future_map):
                        idx, worker, subtask = future_map[future]
                        try:
                            output = future.result()
                            agent_b_outputs.append(
                                {
                                    "agent_index": idx,
                                    "endpoint": worker["endpoint"],
                                    "subtask": subtask,
                                    "output": output,
                                }
                            )
                            logger.log(
                                task_id=task_id,
                                event_type="agent_b_response",
                                message=f"Worker {idx} completed",
                                extra={
                                    "output_preview": output[:200],
                                    "agent_role": worker["role"] or agent_b_role,
                                },
                            )
                        except Exception as exc:
                            logger.log(
                                task_id=task_id,
                                event_type="agent_b_error",
                                message=f"Worker {idx} failed: {exc}",
                                extra={"agent_role": worker["role"] or agent_b_role},
                            )
                            agent_b_outputs.append(
                                {
                                    "agent_index": idx,
                                    "endpoint": worker["endpoint"],
                                    "subtask": subtask,
                                    "output": f"Worker failed: {exc}",
                                }
                            )

                worker_summary_lines = []
                for idx, worker in enumerate(workers, start=1):
                    output = next(
                        (item["output"] for item in agent_b_outputs if item["agent_index"] == idx),
                        "",
                    )
                    subtask = next(
                        (item["subtask"] for item in agent_b_outputs if item["agent_index"] == idx),
                        subtasks[idx - 1] if idx - 1 < len(subtasks) else "",
                    )
                    worker_summary_lines.append(
                        f"Worker {idx} ({worker['endpoint']}):\nSubtask: {subtask}\n{output}"
                    )
                worker_summary = "\n\n".join(worker_summary_lines)

                final_prompt = (
                    "You are Agent A acting as planner/critic. Review the worker reports, "
                    "note inconsistencies or gaps, then produce the best final response to the user.\n\n"
                    f"{role_context_block}"
                    f"User task:\n{task}\n\n"
                    "Worker reports:\n"
                    f"{worker_summary}"
                )
                agent_b_output = worker_summary
            elif scenario == "agentic_multi_hop":
                context_summary = ""
                for turn in range(1, max_turns + 1):
                    subtask = (
                        f"[Turn {turn}] Help solve the user task. "
                        "Provide concrete steps or intermediate results.\n"
                        f"User task:\n{task}\n\n"
                        f"Context so far:\n{context_summary or '(none yet)'}"
                    )

                    tool_call_id_b = logger.new_tool_call_id()
                    logger.log(
                        task_id=task_id,
                        event_type="agent_b_request",
                        message=f"Calling Agent B (multi-hop, turn {turn})",
                        tool_call_id=tool_call_id_b,
                        extra={
                            "url": AGENT_B_URL,
                            "turn": turn,
                            "context_preview": context_summary[:CONTEXT_PREVIEW_LEN],
                        },
                    )
                    try:
                        with self.tracer.start_as_current_span(
                            "agent_a.call_agent_b",
                            kind=SpanKind.CLIENT,
                        ) as span_b:
                            span_b.set_attribute("app.agent_b.url", AGENT_B_URL)
                            span_b.set_attribute("app.agent_b.scenario", scenario or "")
                            span_b.set_attribute("app.turn", turn)
                            headers: Dict[str, str] = {}
                            propagate.inject(headers)
                            agent_b_output = call_agent_b(
                                subtask,
                                scenario=scenario,
                                headers=headers,
                                agent_b_role=agent_b_role,
                                agent_b_contract=agent_b_contract,
                            )
                    except Exception as exc:
                        logger.log(
                            task_id=task_id,
                            event_type="agent_b_error",
                            message=f"Agent B call failed (turn {turn}): {exc}",
                            tool_call_id=tool_call_id_b,
                        )
                        self._send_json(502, {"error": f"Agent B failed: {exc}"})
                        return

                    agent_b_outputs.append(agent_b_output or "")
                    logger.log(
                        task_id=task_id,
                        event_type="agent_b_response",
                        message=f"Received Agent B response (turn {turn})",
                        tool_call_id=tool_call_id_b,
                        extra={
                            "turn": turn,
                            "output_preview": (agent_b_output or "")[:200],
                        },
                    )

                    progress_prompt = (
                        "You are Agent A. Provide a short progress check after this turn. "
                        "Summarize what's done, what's unclear, and one next step.\n\n"
                        f"{role_context_block}\n"
                        f"User task:\n{task}\n\n"
                        f"Agent B notes (turn {turn}):\n{agent_b_output or ''}\n\n"
                        f"Context so far:\n{context_summary or '(none yet)'}"
                    )
                    try:
                        with self.tracer.start_as_current_span(
                            "agent_a.progress_check",
                            kind=SpanKind.CLIENT,
                        ) as span_progress:
                            span_progress.set_attribute("app.llm.url", LLM_SERVER_URL)
                            span_progress.set_attribute("app.turn", turn)
                            headers = {}
                            propagate.inject(headers)
                            progress_note = call_llm(progress_prompt, headers=headers)
                            agent_a_progress_notes.append(progress_note)
                            logger.log(
                                task_id=task_id,
                                event_type="agent_a_progress_check",
                                message=f"Progress check completed (turn {turn})",
                                extra={"output_preview": progress_note[:200]},
                            )
                    except Exception as exc:
                        logger.log(
                            task_id=task_id,
                            event_type="agent_a_progress_check_error",
                            message=f"Progress check failed (turn {turn}): {exc}",
                        )

                    # Update context summary with the latest B output (keep short).
                    context_summary = (context_summary + "\n" + (agent_b_output or "")).strip()
                    if len(context_summary) > 2000:
                        context_summary = context_summary[-2000:]

                final_prompt = (
                    "You are Agent A. The user task is:\n"
                    f"{role_context_block}\n"
                    f"{task}\n\n"
                    "Agent B provided these iterative notes:\n"
                    f"{context_summary}\n\n"
                    "Use the notes to produce the final concise answer. "
                    "Ignore any progress check notes unless useful. "
                    "Now produce the final concise answer for the user task."
                )
                agent_b_output = "\n---\n".join(agent_b_outputs)
            else:
                final_prompt = task

            tool_call_id_llm = logger.new_tool_call_id()
            logger.log(
                task_id=task_id,
                event_type="llm_request",
                message="Calling LLM server (HTTP AgentA)",
                tool_call_id=tool_call_id_llm,
            )

            try:
                with self.tracer.start_as_current_span(
                    "agent_a.call_llm",
                    kind=SpanKind.CLIENT,
                ) as span_llm:
                    span_llm.set_attribute("app.llm.url", LLM_SERVER_URL)
                    headers: Dict[str, str] = {}
                    propagate.inject(headers)
                    output = call_llm(final_prompt, headers=headers)
            except Exception as exc:
                logger.log(
                    task_id=task_id,
                    event_type="llm_error",
                    message=f"LLM call failed (HTTP AgentA): {exc}",
                    tool_call_id=tool_call_id_llm,
                )
                self._send_json(502, {"error": f"LLM failed: {exc}"})
                return

            logger.log(
                task_id=task_id,
                event_type="llm_response",
                message="Received LLM response (HTTP AgentA)",
                tool_call_id=tool_call_id_llm,
                extra={"output_preview": output[:200]},
            )

            self._send_json(
                200,
                {
                    "task_id": task_id,
                    "agent_id": "AgentA",
                    "scenario": scenario,
                    "output": output,
                    "agent_b_output": agent_b_output,
                    "agent_b_outputs": agent_b_outputs,
                    "agent_a_progress_notes": agent_a_progress_notes,
                },
            )


def run() -> None:
    server = ThreadingHTTPServer((HOST, PORT), AgentARequestHandler)
    print(f"[*] Agent A HTTP server listening on http://{HOST}:{PORT}/task")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down Agent A server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()


