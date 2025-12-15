import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional

from agents.agent_a.main import AGENT_B_URL, call_agent_b, call_llm
from agents.common.telemetry import TelemetryLogger
from agents.common.tracing import get_tracer


HOST = "0.0.0.0"
PORT = int(os.environ.get("AGENT_A_PORT", "8101"))


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
            if not isinstance(task, str) or not task:
                self._send_json(400, {"error": "Missing 'task' field"})
                return

            span.set_attribute("app.task", task)
            if scenario:
                span.set_attribute("app.scenario", scenario)

            # New task / telemetry
            logger = self.logger
            logger.scenario = scenario  # type: ignore[assignment]
            task_id = logger.new_task_id()
            span.set_attribute("app.task_id", task_id)
            logger.log(task_id=task_id, event_type="task_received", message=task)

            final_prompt: str
            agent_b_output: Optional[str] = None

            if scenario == "agentic_multi_hop":
                tool_call_id_b = logger.new_tool_call_id()
                logger.log(
                    task_id=task_id,
                    event_type="agent_b_request",
                    message="Calling Agent B (multi-hop)",
                    tool_call_id=tool_call_id_b,
                    extra={"url": AGENT_B_URL},
                )
                try:
                    with self.tracer.start_as_current_span("agent_a.call_agent_b") as span_b:
                        span_b.set_attribute("app.agent_b.url", AGENT_B_URL)
                        span_b.set_attribute("app.agent_b.scenario", scenario or "")
                        agent_b_output = call_agent_b(task, scenario=scenario)
                except Exception as exc:
                    logger.log(
                        task_id=task_id,
                        event_type="agent_b_error",
                        message=f"Agent B call failed: {exc}",
                        tool_call_id=tool_call_id_b,
                    )
                    self._send_json(502, {"error": f"Agent B failed: {exc}"})
                    return

                logger.log(
                    task_id=task_id,
                    event_type="agent_b_response",
                    message="Received Agent B response (multi-hop)",
                    tool_call_id=tool_call_id_b,
                    extra={"output_preview": (agent_b_output or "")[:200]},
                )

                final_prompt = (
                    "You are Agent A. The user task is:\n"
                    f"{task}\n\n"
                    "Agent B responded with:\n"
                    f"{agent_b_output}\n\n"
                    "Produce the final answer for the user."
                )
            else:
                final_prompt = task

            tool_call_id_llm = logger.new_tool_call_id()
            logger.log(
                task_id=task_id,
                event_type="llm_request",
                message="Calling LLM server (HTTP AgentA)",
                tool_call_id=tool_call_id_llm,
            )

            output = call_llm(final_prompt)

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
                },
            )


def run() -> None:
    server = HTTPServer((HOST, PORT), AgentARequestHandler)
    print(f"[*] Agent A HTTP server listening on http://{HOST}:{PORT}/task")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down Agent A server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()


