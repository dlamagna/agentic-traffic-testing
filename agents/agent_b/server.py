import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

from opentelemetry import propagate
from opentelemetry.trace import SpanKind

from agents.agent_b.main import LLM_SERVER_URL, call_llm
from agents.common.telemetry import TelemetryLogger
from agents.common.tracing import get_tracer


HOST = "0.0.0.0"
PORT = int(os.environ.get("AGENT_B_PORT", "8102"))
LOG_LLM_REQUESTS = os.environ.get("LOG_LLM_REQUESTS", "").lower() in ("1", "true", "yes", "on")
LLM_LOG_MAX_CHARS = int(os.environ.get("LLM_LOG_MAX_CHARS", "500"))


def _log_llm_prompt(label: str, prompt: str) -> None:
    if not LOG_LLM_REQUESTS:
        return
    max_chars = max(LLM_LOG_MAX_CHARS, 0)
    if max_chars == 0:
        preview = ""
        suffix = ""
    else:
        preview = prompt[:max_chars]
        suffix = "" if len(prompt) <= max_chars else f"... [truncated {len(prompt) - max_chars} chars]"
    print(f"[agent-b][llm] {label} prompt_len={len(prompt)} prompt={preview}{suffix}")


class AgentBRequestHandler(BaseHTTPRequestHandler):
    logger = TelemetryLogger(agent_id="AgentB")
    tracer = get_tracer("agent-b")

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
        if self.path != "/subtask":
            self.send_response(404)
        else:
            self.send_response(204)
        self._set_cors()
        self.end_headers()

    def do_POST(self) -> None:  # type: ignore[override]
        if self.path != "/subtask":
            self._send_json(404, {"error": "Not found"})
            return

        carrier = {key: value for key, value in self.headers.items()}
        agent_index = self.headers.get("x-agent-index")
        ctx = propagate.extract(carrier)
        with self.tracer.start_as_current_span(
            "agent_b.handle_subtask",
            context=ctx,
            kind=SpanKind.SERVER,
        ) as span:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b""

            try:
                data: Dict[str, Any] = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON"})
                return

            subtask = data.get("subtask")
            scenario = data.get("scenario")
            agent_b_role = data.get("agent_b_role") if isinstance(data.get("agent_b_role"), str) else None
            agent_b_contract = (
                data.get("agent_b_contract") if isinstance(data.get("agent_b_contract"), str) else None
            )
            if not isinstance(subtask, str) or not subtask:
                self._send_json(400, {"error": "Missing 'subtask' field"})
                return

            span.set_attribute("app.subtask", subtask)
            if scenario:
                span.set_attribute("app.scenario", scenario)
            if agent_index:
                span.set_attribute("app.agent_index", agent_index)
            if agent_b_role:
                span.set_attribute("app.agent_role", agent_b_role)
                span.set_attribute(
                    "app.role_service",
                    f"{os.environ.get('OTEL_SERVICE_NAME', 'agent-b')}:{agent_b_role}",
                )

            logger = self.logger
            logger.scenario = scenario  # type: ignore[assignment]
            task_id = logger.new_task_id()
            span.set_attribute("app.task_id", task_id)
            logger.log(
                task_id=task_id,
                event_type="subtask_received",
                message=subtask,
                extra={
                    "agent_role": agent_b_role,
                    "agent_index": agent_index,
                }
                if agent_b_role or agent_index
                else None,
            )

            tool_call_id = logger.new_tool_call_id()
            logger.log(
                task_id=task_id,
                event_type="llm_request",
                message="Calling LLM server (HTTP AgentB)",
                tool_call_id=tool_call_id,
            )

            role_context_parts = []
            if agent_b_role:
                role_context_parts.append(f"Role: {agent_b_role}")
            if agent_b_contract:
                role_context_parts.append(f"Contract: {agent_b_contract}")
            role_context = "\n".join(role_context_parts)
            prompt = (
                f"You are Agent B.\n{role_context}\n\n{subtask}"
                if role_context
                else f"You are Agent B.\n\n{subtask}"
            )
            _log_llm_prompt(f"subtask_{agent_index or 'unknown'}", prompt)

            with self.tracer.start_as_current_span(
                "agent_b.call_llm",
                kind=SpanKind.CLIENT,
            ) as span_llm:
                span_llm.set_attribute("app.llm.url", LLM_SERVER_URL)
                headers: Dict[str, str] = {}
                propagate.inject(headers)
                output = call_llm(prompt, headers=headers)

            llm_request = {
                "source": "agent_b",
                "label": f"subtask_{agent_index or 'unknown'}",
                "prompt": prompt,
                "response": output,
                "agent_index": agent_index,
                "endpoint": LLM_SERVER_URL,
            }

            logger.log(
                task_id=task_id,
                event_type="llm_response",
                message="AgentB received LLM response (HTTP)",
                tool_call_id=tool_call_id,
                extra={"output_preview": output[:200]},
            )

            self._send_json(
                200,
                {
                    "task_id": task_id,
                    "agent_id": "AgentB",
                    "output": output,
                    "llm_prompt": prompt,
                    "llm_response": output,
                    "llm_endpoint": LLM_SERVER_URL,
                    "llm_requests": [llm_request],
                },
            )


def run() -> None:
    server = ThreadingHTTPServer((HOST, PORT), AgentBRequestHandler)
    print(f"[*] Agent B HTTP server listening on http://{HOST}:{PORT}/subtask")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down Agent B server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()


