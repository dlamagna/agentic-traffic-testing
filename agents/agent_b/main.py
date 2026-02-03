import argparse
import json
import os
from typing import Any, Dict, Optional, Tuple

import httpx
from agents.common.telemetry import TelemetryLogger


DEFAULT_LLM_SERVER_URL = "http://localhost:8000/chat"
LLM_SERVER_URL = os.environ.get("LLM_SERVER_URL", DEFAULT_LLM_SERVER_URL)
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "120"))


def call_llm(prompt: str, headers: Optional[Dict[str, str]] = None) -> Tuple[str, Dict[str, Any]]:
    resp = httpx.post(
        LLM_SERVER_URL,
        json={"prompt": prompt},
        headers=headers,
        timeout=LLM_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    data: Dict[str, Any] = resp.json()
    return str(data.get("output", "")), (data.get("meta") if isinstance(data.get("meta"), dict) else {})


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent B MVP (standalone demo)")
    parser.add_argument("subtask", help="Subtask text for Agent B")
    parser.add_argument("--scenario", default=None, help="Scenario label")
    args = parser.parse_args()

    logger = TelemetryLogger(agent_id="AgentB", scenario=args.scenario)
    task_id = logger.new_task_id()

    logger.log(task_id=task_id, event_type="subtask_received", message=args.subtask)

    tool_call_id = logger.new_tool_call_id()
    logger.log(
        task_id=task_id,
        event_type="llm_request",
        message="Calling LLM server from AgentB",
        tool_call_id=tool_call_id,
        extra={"url": LLM_SERVER_URL},
    )

    output, _meta = call_llm(args.subtask)

    logger.log(
        task_id=task_id,
        event_type="llm_response",
        message="AgentB received LLM response",
        tool_call_id=tool_call_id,
        extra={"output_preview": output[:200]},
    )

    payload = {
        "task_id": task_id,
        "agent_id": "AgentB",
        "output": output,
    }
    print(json.dumps(payload))


if __name__ == "__main__":
    main()


