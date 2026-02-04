#!/usr/bin/env python3
"""
demo.py
-------

Minimal CLI demo for running a GPTSwarm graph against the local LLM backend.

This script assumes:
  - The vLLM backend is running (e.g. via infra/docker-compose.yml) and
    reachable at LLM_SERVER_URL (default: http://localhost:8000/chat).
  - The LM Studio-compatible proxy is running:

        python -m integrations.gptswarm.proxy

  - GPTSwarm is installed (optional, via `pip install -r integrations/gptswarm/requirements.txt`).

By default it uses a simple Swarm with three IO agents in the "gaia" domain,
routed through `model_name="lmstudio"` so that all LLM calls hit the local
proxy instead of OpenAI.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict

from swarm.graph.swarm import Swarm


def run_swarm(task: str) -> Dict[str, Any] | Any:
    """
    Run a minimal GPTSwarm example on the local LLM.

    The exact structure of the returned object depends on the domain and
    final node configuration; for the default "gaia" setup this is usually
    a dict containing at least the final answer under a key like "answer"
    or "final_answer".
    """
    # Use the LM Studio integration path in GPTSwarm (model_name="lmstudio"),
    # which we have wired to the local proxy implemented in integrations.gptswarm.proxy.
    swarm = Swarm(
        ["IO", "IO", "IO"],
        "gaia",
        model_name="lmstudio",
    )
    inputs = {"task": task}
    return swarm.run(inputs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a minimal GPTSwarm demo on the local LLM.")
    parser.add_argument(
        "task",
        nargs="?",
        default="Compare two strategies for shaping network traffic in a microservices environment.",
        help="Task to pass into the GPTSwarm graph.",
    )
    args = parser.parse_args()

    result = run_swarm(args.task)
    print(result)


if __name__ == "__main__":
    main()
