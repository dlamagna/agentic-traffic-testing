import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional

from agents.agent_a.main import AGENT_B_URL, call_agent_b, call_llm
from agents.common.telemetry import TelemetryLogger
from agents.common.tracing import get_tracer


HOST = "0.0.0.0"
PORT = int(os.environ.get("AGENT_A_PORT", "8101"))
MAX_AGENT_B_TURNS = int(os.environ.get("MAX_AGENT_B_TURNS", "3"))
CONTEXT_PREVIEW_LEN = 300


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
            agent_b_outputs: list[str] = []

            if scenario == "agentic_multi_hop":
                context_summary = ""
                for turn in range(1, MAX_AGENT_B_TURNS + 1):
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
                        with self.tracer.start_as_current_span("agent_a.call_agent_b") as span_b:
                            span_b.set_attribute("app.agent_b.url", AGENT_B_URL)
                            span_b.set_attribute("app.agent_b.scenario", scenario or "")
                            span_b.set_attribute("app.turn", turn)
                            agent_b_output = call_agent_b(subtask, scenario=scenario)
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

                    # Update context summary with the latest B output (keep short).
                    context_summary = (context_summary + "\n" + (agent_b_output or "")).strip()
                    if len(context_summary) > 2000:
                        context_summary = context_summary[-2000:]

                final_prompt = (
                    "You are Agent A. The user task is:\n"
                    f"{task}\n\n"
                    "Agent B provided these iterative notes:\n"
                    f"{context_summary}\n\n"
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
                output = call_llm(final_prompt)
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


