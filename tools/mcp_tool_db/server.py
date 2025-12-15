import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict

from agents.common.telemetry import TelemetryLogger
from agents.common.tracing import get_tracer


HOST = "0.0.0.0"
PORT = int(os.environ.get("MCP_TOOL_DB_PORT", "8201"))


class DbToolRequestHandler(BaseHTTPRequestHandler):
    logger = TelemetryLogger(agent_id="ToolDB")
    tracer = get_tracer("mcp-tool-db")

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # type: ignore[override]
        if self.path != "/query":
            self._send_json(404, {"error": "Not found"})
            return

        with self.tracer.start_as_current_span("mcp_tool_db.query") as span:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b""

            try:
                data: Dict[str, Any] = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON"})
                return

            query = data.get("query")
            task_id = data.get("task_id") or "unknown-task"
            if not isinstance(query, str) or not query:
                self._send_json(400, {"error": "Missing 'query' field"})
                return

            span.set_attribute("app.query", query)
            span.set_attribute("app.task_id", task_id)

            logger = self.logger
            tool_call_id = logger.new_tool_call_id()
            logger.log(
                task_id=task_id,
                event_type="tool_request",
                message="DB tool query received",
                tool_call_id=tool_call_id,
                extra={"query_preview": query[:200]},
            )

            # Very simple deterministic "DB" response for now.
            result = {
                "records": [
                    {"id": 1, "value": f"Echo of '{query}'"},
                ]
            }

            logger.log(
                task_id=task_id,
                event_type="tool_response",
                message="DB tool response sent",
                tool_call_id=tool_call_id,
            )

            self._send_json(200, result)


def run() -> None:
    server = HTTPServer((HOST, PORT), DbToolRequestHandler)
    print(f"[*] MCP-style DB tool listening on http://{HOST}:{PORT}/query")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down DB tool server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()


