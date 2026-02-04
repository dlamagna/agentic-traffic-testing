#!/usr/bin/env python3
"""
docker.py
---------

GPTSwarm demo script for Docker environments.

This script patches GPTSwarm's hardcoded LM_STUDIO_URL to work with the
lmstudio-proxy container. When running in Docker, GPTSwarm needs to connect
to `http://lmstudio-proxy:1234/v1` instead of `http://localhost:1234/v1`.

Usage:
  docker compose run --rm gptswarm python -m integrations.gptswarm.scripts.docker "Your task"
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict

# Patch GPTSwarm's LM_STUDIO_URL before importing Swarm
# GPTSwarm hardcodes "http://localhost:1234/v1", but in Docker we need
# to connect to the lmstudio-proxy container.
LMSTUDIO_PROXY_URL = os.environ.get(
    "LMSTUDIO_PROXY_URL",
    "http://lmstudio-proxy:1234/v1"  # Docker container name
)

# Monkey-patch the constant in swarm.llm.gpt_chat before any imports
import swarm.llm.gpt_chat as gpt_chat_module
gpt_chat_module.LM_STUDIO_URL = LMSTUDIO_PROXY_URL

# Now import Swarm (it will use the patched URL)
from swarm.graph.swarm import Swarm


def run_swarm(task: str) -> Dict[str, Any] | Any:
    """
    Run a minimal GPTSwarm example on the local LLM via Docker.

    The exact structure of the returned object depends on the domain and
    final node configuration; for the default "gaia" setup this is usually
    a dict containing at least the final answer under a key like "answer"
    or "final_answer".
    """
    # Use the LM Studio integration path in GPTSwarm (model_name="lmstudio"),
    # which we have wired to the lmstudio-proxy container.
    swarm = Swarm(
        ["IO", "IO", "IO"],
        "gaia",
        model_name="lmstudio",
    )
    inputs = {"task": task}
    return swarm.run(inputs)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a minimal GPTSwarm demo on the local LLM (Docker version)."
    )
    parser.add_argument(
        "task",
        nargs="?",
        default="Compare two strategies for shaping network traffic in a microservices environment.",
        help="Task to pass into the GPTSwarm graph.",
    )
    args = parser.parse_args()

    print(f"[*] GPTSwarm Docker demo")
    print(f"    LM Studio proxy URL: {LMSTUDIO_PROXY_URL}")
    print(f"    Task: {args.task}")
    print()

    result = run_swarm(args.task)
    print()
    print("[*] Result:")
    print(result)


if __name__ == "__main__":
    main()
