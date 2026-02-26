"""
Integration hooks for running the MCP-Universe benchmark framework
alongside the agentic traffic testbed.

This package does **not** vendor the MCP-Universe code. Instead, it
assumes you have cloned and installed:

    https://github.com/SalesforceAIResearch/MCP-Universe

and points at it via the ``MCP_UNIVERSE_DIR`` environment variable.

There are two main responsibilities here:

1. **LLM adapter (OpenAI proxy)**

   MCP-Universe expects an OpenAI/Anthropic/Gemini style LLM provider.
   The testbed already exposes a local LLM backend at ``/chat`` (see
   ``llm/serve_llm.py``).  The module
   :mod:`tools.mcp_universe.openai_proxy` provides a tiny
   OpenAI-compatible HTTP shim which:

   - listens on ``/v1/chat/completions``
   - forwards requests to the local LLM backend
   - returns an OpenAI-style JSON response that MCP-Universe can
     consume via the normal OpenAI client / API config.

2. **Benchmark runners (added under scripts/)**

   The experiment scripts in ``scripts/experiment/`` use this package
   purely as a namespaced home for MCP-Universe integration code.
   They:

   - locate the MCP-Universe checkout via ``MCP_UNIVERSE_DIR``
   - invoke its benchmark runners (e.g. location navigation,
     repository management, etc.)
   - optionally emit telemetry so that benchmark runs are visible in
     the same observability pipeline as the rest of the testbed.

The detailed user-facing documentation for this integration lives in
``docs/mcp_universe_integration.md``.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_OPENAI_PROXY_PORT",
    "DEFAULT_OPENAI_PROXY_HOST",
]

DEFAULT_OPENAI_PROXY_HOST = "0.0.0.0"
DEFAULT_OPENAI_PROXY_PORT = 8110
