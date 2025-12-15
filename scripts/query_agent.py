#!/usr/bin/env python
"""
Quick helper to send a single request to Agent A or Agent B and print the reply.

Examples
--------
  python scripts/query_agent.py a "Summarise the latest metrics"
  python scripts/query_agent.py b "Generate a test plan" --scenario agentic_multi_hop
  python scripts/query_agent.py a "Hello" --url http://localhost:8101/task
"""

import argparse
import json
from typing import Any, Dict

import httpx


DEFAULT_ENDPOINTS = {
    "a": "http://localhost:8101/task",
    "b": "http://localhost:8102/subtask",
}


def send(agent: str, text: str, scenario: str | None, url: str) -> Dict[str, Any]:
    payload_field = "task" if agent == "a" else "subtask"
    payload: Dict[str, Any] = {payload_field: text}
    if scenario:
        payload["scenario"] = scenario

    resp = httpx.post(url, json=payload, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Send one request to an agent HTTP endpoint")
    parser.add_argument("agent", choices=("a", "b"), help="Target agent (a=Agent A, b=Agent B)")
    parser.add_argument("text", help="Task/subtask text to send")
    parser.add_argument("--scenario", default=None, help="Scenario label to include in telemetry")
    parser.add_argument(
        "--url",
        default=None,
        help="Override the agent endpoint URL (defaults to localhost ports 8101/8102)",
    )
    args = parser.parse_args()

    url = args.url or DEFAULT_ENDPOINTS[args.agent]
    result = send(agent=args.agent, text=args.text, scenario=args.scenario, url=url)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

