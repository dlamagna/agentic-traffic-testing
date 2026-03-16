import json
import os
import sys
import uuid
from typing import Any, Dict, Optional


MODEL_NAME = os.environ.get("MODEL_NAME", "unknown")
_DEFAULT_LOG_DIR = os.environ.get("METRICS_LOG_DIR", "logs")


class MetricsLogger:
    """
    Per-call LLM metrics logger. Writes one JSONL record per LLM invocation to
    logs/llm_calls.jsonl (Phase 0.1 schema).

    Fields logged per call:
        call_id, task_id, agent_id, parent_call_id, call_type,
        prompt_tokens, completion_tokens, total_tokens, latency_ms,
        model_name, timestamp_start, timestamp_end, http_status, error
    """

    def __init__(self, log_dir: str = _DEFAULT_LOG_DIR) -> None:
        self.log_file = os.path.join(log_dir, "llm_calls.jsonl")
        os.makedirs(log_dir, exist_ok=True)

    def log_call(
        self,
        *,
        task_id: str,
        agent_id: str,
        call_type: str,
        timestamp_start: str,
        timestamp_end: str,
        http_status: int,
        llm_meta: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        parent_call_id: Optional[str] = None,
        call_id: Optional[str] = None,
    ) -> str:
        """
        Log a single LLM call. Returns the call_id (useful as parent_call_id for sub-calls).

        call_type: 'root' | 'sub_call' | 'tool_call' | 'verification'
        llm_meta: the 'meta' dict returned by the LLM server response
                  (contains request_id, latency_ms, prompt_tokens, etc.)
        """
        meta = llm_meta or {}
        cid = call_id or meta.get("request_id") or str(uuid.uuid4())
        record: Dict[str, Any] = {
            "call_id": cid,
            "task_id": task_id,
            "agent_id": agent_id,
            "parent_call_id": parent_call_id,
            "call_type": call_type,
            "prompt_tokens": meta.get("prompt_tokens"),
            "completion_tokens": meta.get("completion_tokens"),
            "total_tokens": meta.get("total_tokens"),
            "latency_ms": meta.get("latency_ms"),
            "model_name": MODEL_NAME,
            "timestamp_start": timestamp_start,
            "timestamp_end": timestamp_end,
            "http_status": http_status,
            "error": error,
        }
        line = json.dumps(record, sort_keys=True)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            print(f"[metrics-logger-error] {exc}: {line}", file=sys.stderr)
        return cid
