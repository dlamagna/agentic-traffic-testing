import json
import os
import socket
import sys
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any


def _now_ms() -> int:
    return int(time.time() * 1000)


def _node_id() -> str:
    return os.environ.get("NODE_NAME", socket.gethostname())


@dataclass
class TelemetryEvent:
    task_id: str
    agent_id: str
    tool_call_id: Optional[str]
    event_type: str
    message: str
    timestamp_ms: int
    scenario: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


class TelemetryLogger:
    def __init__(self, agent_id: str, log_file: Optional[str] = None, scenario: Optional[str] = None) -> None:
        self.agent_id = agent_id
        self.scenario = scenario
        self.node_id = _node_id()
        self.log_file = log_file or f"logs/{self.node_id}_{agent_id}.log"
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

    def new_task_id(self) -> str:
        return str(uuid.uuid4())

    def new_tool_call_id(self) -> str:
        return str(uuid.uuid4())

    def log(
        self,
        task_id: str,
        event_type: str,
        message: str,
        tool_call_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        event = TelemetryEvent(
            task_id=task_id,
            agent_id=self.agent_id,
            tool_call_id=tool_call_id,
            event_type=event_type,
            message=message,
            timestamp_ms=_now_ms(),
            scenario=self.scenario,
            extra=extra or {},
        )
        record: Dict[str, Any] = asdict(event)
        record["node_id"] = self.node_id

        line = json.dumps(record, sort_keys=True)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            # Best-effort logging; fall back to stderr.
            print(f"[telemetry-error] {exc}: {line}", file=sys.stderr)



