import argparse
import json
from typing import Any, Dict

import httpx
import os

from agents.common.telemetry import TelemetryLogger


DEFAULT_LLM_SERVER_URL = "http://localhost:8000/chat"
LLM_SERVER_URL = os.environ.get("LLM_SERVER_URL", DEFAULT_LLM_SERVER_URL)


def call_llm(prompt: str) -> str:
    resp = httpx.post(LLM_SERVER_URL, json={"prompt": prompt}, timeout=30.0)
    resp.raise_for_status()
    data: Dict[str, Any] = resp.json()
    return str(data.get("output", ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent A MVP")
    parser.add_argument("task", help="User task / intent text")
    parser.add_argument("--scenario", default=None, help="Scenario label (baseline/agentic_simple/agentic_multi_hop)")
    args = parser.parse_args()

    logger = TelemetryLogger(agent_id="AgentA", scenario=args.scenario)
    task_id = logger.new_task_id()

    logger.log(task_id=task_id, event_type="task_received", message=args.task)

    tool_call_id = logger.new_tool_call_id()
    logger.log(
        task_id=task_id,
        event_type="llm_request",
        message="Calling LLM server",
        tool_call_id=tool_call_id,
        extra={"url": LLM_SERVER_URL},
    )

    output = call_llm(args.task)

    logger.log(
        task_id=task_id,
        event_type="llm_response",
        message="Received LLM response",
        tool_call_id=tool_call_id,
        extra={"output_preview": output[:200]},
    )

    payload = {
        "task_id": task_id,
        "agent_id": "AgentA",
        "output": output,
    }
    print(json.dumps(payload))


if __name__ == "__main__":
    main()


