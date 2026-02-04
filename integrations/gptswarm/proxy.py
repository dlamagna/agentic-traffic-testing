#!/usr/bin/env python3
"""
LM Studio-compatible Chat Completions proxy for the local vLLM backend.

GPTSwarm's LLM integration supports a special `model_name="lmstudio"` mode
that talks to an OpenAI-compatible Chat Completions API at:

    LM_STUDIO_URL = "http://localhost:1234/v1"

This small server implements that API surface and forwards requests to the
existing testbed LLM backend (`llm.serve_llm`), so GPTSwarm can run entirely
on your local model without OpenAI keys.

Endpoints:
  - POST /v1/chat/completions

Environment:
  - LLM_SERVER_URL (default: http://localhost:8000/chat)
      URL of the existing vLLM backend exposed by `llm.serve_llm`.
  - LMSTUDIO_PROXY_HOST (default: 0.0.0.0)
  - LMSTUDIO_PROXY_PORT (default: 1234)

Usage (host / dev):
  1) Start the vLLM backend (as you already do), e.g. via docker-compose.
  2) Run this proxy:

       python -m integrations.gptswarm.proxy

  3) In your GPTSwarm code, use:

       from swarm.graph.swarm import Swarm

       swarm = Swarm(
           ["IO", "IO", "IO"],
           "gaia",
           model_name="lmstudio",  # routed to this proxy
       )
       answer = swarm.run({"task": "What is the capital of Jordan?"})

The proxy will translate GPTSwarm's OpenAI-style Chat Completions calls into
simple {prompt, system_prompt, max_tokens} requests against the existing
`/chat` endpoint and wrap the response back into OpenAI's schema (including
`usage.*` fields) so GPTSwarm's pricing / token accounting continues to work.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
from aiohttp import web


LLM_SERVER_URL = os.environ.get("LLM_SERVER_URL", "http://localhost:8000/chat")
PROXY_HOST = os.environ.get("LMSTUDIO_PROXY_HOST", "0.0.0.0")
PROXY_PORT = int(os.environ.get("LMSTUDIO_PROXY_PORT", "1234"))


def _normalize_message_content(content: Any) -> str:
    """
    Normalize OpenAI-style `message.content` into a plain string.

    GPTSwarm currently sends simple string contents, but we handle the
    modern Chat Completions "content as list of parts" format as well
    for forward compatibility.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                # OpenAI chat parts format: {"type": "text", "text": "..."}
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def _build_llm_payload(
    model: str,
    messages: List[Dict[str, Any]],
    max_tokens: Optional[int],
    temperature: Optional[float],
) -> Dict[str, Any]:
    """
    Map OpenAI Chat Completions fields into the existing /chat payload.

    The local backend expects:
      - prompt: str
      - max_tokens: int (optional)
      - system_prompt: str (optional)
      - skip_chat_template: bool (optional)

    We:
      - Merge all system messages into `system_prompt`
      - Concatenate the remaining messages into a simple text transcript
    """
    system_parts: List[str] = []
    convo_parts: List[str] = []

    for msg in messages:
        role = str(msg.get("role", "user"))
        content = _normalize_message_content(msg.get("content"))
        if not content:
            continue

        if role == "system":
            system_parts.append(content)
        else:
            # Lightweight role prefixing; the Llama 3 chat template on the
            # backend will still apply its own proper formatting.
            prefix = role.upper()
            convo_parts.append(f"{prefix}: {content}")

    system_prompt = "\n\n".join(system_parts) if system_parts else None
    prompt = "\n\n".join(convo_parts) if convo_parts else ""

    payload: Dict[str, Any] = {"prompt": prompt}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    # Pass through a system prompt if present; the backend will inject it
    # into the Llama 3 chat template.
    if system_prompt:
        payload["system_prompt"] = system_prompt

    # Temperature is currently fixed in the backend's SamplingParams, so we
    # do not forward it. This can be wired through later if needed.

    return payload


async def handle_chat_completions(request: web.Request) -> web.Response:
    """
    Handle POST /v1/chat/completions.

    This is the only endpoint GPTSwarm's `GPTChat` backend hits when
    `model_name="lmstudio"` is used (see `swarm.llm.gpt_chat`).
    """
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    model = str(data.get("model", "lmstudio"))
    messages_raw = data.get("messages") or []
    if not isinstance(messages_raw, list) or not messages_raw:
        return web.json_response({"error": "Missing or invalid 'messages' field"}, status=400)

    max_tokens = data.get("max_tokens")
    if max_tokens is not None:
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = None

    # temperature is currently ignored by the backend but parsed for completeness
    temperature = data.get("temperature")
    try:
        if temperature is not None:
            temperature = float(temperature)
    except (TypeError, ValueError):
        temperature = None

    # Build payload for the existing /chat endpoint
    payload = _build_llm_payload(model, messages_raw, max_tokens, temperature)

    # Forward to local vLLM backend
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(LLM_SERVER_URL, json=payload)
            resp.raise_for_status()
        except httpx.TimeoutException:
            return web.json_response({"error": "Upstream LLM backend timeout"}, status=504)
        except httpx.HTTPError as exc:
            return web.json_response({"error": f"Upstream LLM backend error: {exc}"}, status=502)

    backend_data: Dict[str, Any] = resp.json()
    text = str(backend_data.get("output", ""))
    meta = backend_data.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}

    prompt_tokens = meta.get("prompt_tokens") or 0
    completion_tokens = meta.get("completion_tokens") or 0
    total_tokens = meta.get("total_tokens") or (prompt_tokens + completion_tokens)

    # Shape response like OpenAI Chat Completions so GPTSwarm's
    # `cost_count` and other utilities continue to work unchanged.
    result: Dict[str, Any] = {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }

    return web.json_response(result)


async def handle_health(request: web.Request) -> web.Response:
    """Simple health endpoint for docker-compose / monitoring."""
    return web.json_response({"status": "ok"})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/ready", handle_health)
    app.router.add_get("/live", handle_health)
    return app


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="LM Studio-compatible Chat Completions proxy for local vLLM backend"
    )
    parser.add_argument(
        "--host",
        default=PROXY_HOST,
        help=f"Bind host for proxy server (default: {PROXY_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=PROXY_PORT,
        help=f"HTTP port for proxy server (default: {PROXY_PORT})",
    )
    parser.add_argument(
        "--backend-url",
        default=LLM_SERVER_URL,
        help=f"URL of existing LLM backend /chat endpoint (default: {LLM_SERVER_URL})",
    )
    args = parser.parse_args(argv)

    # Allow overriding via CLI; keep globals in sync so logging is clear.
    global LLM_SERVER_URL  # noqa: PLW0603
    LLM_SERVER_URL = args.backend_url

    app = create_app()
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
