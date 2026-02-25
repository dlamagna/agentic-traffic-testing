"""
Minimal OpenAI-compatible proxy in front of the local LLM backend.

MCP-Universe expects to talk to an OpenAI-style Chat Completions API.
This proxy translates those requests into calls to the existing
``llm/serve_llm.py`` backend and returns OpenAI-shaped JSON.

Usage (local dev):

    # 1) Start the local LLM backend as usual (see llm/README or infra compose)
    python -m llm.serve_llm --port 8000

    # 2) Start the OpenAI proxy (default: http://0.0.0.0:8110/v1/chat/completions)
    python -m tools.mcp_universe.openai_proxy

    # 3) In your MCP-Universe .env, point OpenAI at this proxy:
    #
    #   OPENAI_API_KEY=dummy-key
    #   OPENAI_BASE_URL=http://host-running-proxy:8110/v1
    #
    # Then configure benchmarks to use an OpenAI model name, e.g. gpt-4o.

The proxy intentionally implements only the subset of the OpenAI Chat
Completions API needed by MCP-Universe.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
import typing as t
import uuid

import aiohttp
from aiohttp import web

from tools.mcp_universe import DEFAULT_OPENAI_PROXY_HOST, DEFAULT_OPENAI_PROXY_PORT


BACKEND_URL_ENV = "LLM_SERVER_URL"
DEFAULT_BACKEND_URL = os.environ.get(BACKEND_URL_ENV, "http://llm-backend:8000/chat")


def _flatten_messages(messages: t.Sequence[dict[str, t.Any]]) -> str:
    """Turn OpenAI-style chat messages into a single prompt string."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_chunks = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        text_chunks.append(block["text"])
                elif isinstance(block, str):
                    text_chunks.append(block)
            content = "\n".join(text_chunks)
        if not isinstance(content, str):
            content = str(content)
        parts.append(f"[{role.upper()}]\n{content}\n")
    return "\n".join(parts).strip()


async def handle_chat_completions(request: web.Request) -> web.Response:
    """Handle POST /v1/chat/completions."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response(
            {"error": "Invalid JSON body"}, status=400
        )

    model = payload.get("model", "local-llm")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return web.json_response(
            {"error": "Field 'messages' must be a non-empty list"}, status=400
        )

    max_tokens = payload.get("max_tokens")
    if max_tokens is not None:
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = None

    prompt = _flatten_messages(messages)
    request_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    backend_url = request.app["backend_url"]

    async with aiohttp.ClientSession() as session:
        try:
            backend_payload: dict[str, t.Any] = {"prompt": prompt}
            if max_tokens is not None:
                backend_payload["max_tokens"] = max_tokens
            async with session.post(backend_url, json=backend_payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return web.json_response(
                        {
                            "error": "Backend LLM request failed",
                            "status": resp.status,
                            "backend_body": text[:500],
                        },
                        status=502,
                    )
                data = await resp.json()
        except Exception as exc:
            return web.json_response(
                {"error": f"Error calling local LLM backend: {exc}"},
                status=502,
            )

    output_text = ""
    if isinstance(data, dict):
        if isinstance(data.get("output"), str):
            output_text = data["output"]
        elif isinstance(data.get("choices"), list) and data["choices"]:
            choice = data["choices"][0]
            msg = choice.get("message", {}) if isinstance(choice, dict) else {}
            if isinstance(msg.get("content"), str):
                output_text = msg["content"]

    if not output_text:
        output_text = "[Proxy] Local LLM backend returned empty output."

    usage = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }

    response_body = {
        "id": request_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": output_text},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }
    return web.json_response(response_body, status=200)


def create_app(backend_url: str) -> web.Application:
    app = web.Application()
    app["backend_url"] = backend_url
    app.router.add_post("/v1/chat/completions", handle_chat_completions)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "backend_url": backend_url})

    app.router.add_get("/health", health)
    app.router.add_get("/ready", health)
    return app


async def _run_server(host: str, port: int, backend_url: str) -> None:
    app = create_app(backend_url)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    print("=" * 60)
    print(f"[*] OpenAI proxy for MCP-Universe ready")
    print(f"    Listening on http://{host}:{port}")
    print(f"    Forwarding to local LLM backend: {backend_url}")
    print("=" * 60)
    await site.start()

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="OpenAI-compatible proxy for running MCP-Universe against the local LLM backend"
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_OPENAI_PROXY_HOST,
        help=f"Bind host (default: {DEFAULT_OPENAI_PROXY_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_OPENAI_PROXY_PORT,
        help=f"Port for /v1/chat/completions (default: {DEFAULT_OPENAI_PROXY_PORT})",
    )
    parser.add_argument(
        "--backend-url",
        default=DEFAULT_BACKEND_URL,
        help=(
            "URL of the local LLM backend /chat endpoint. "
            f"Defaults to ${BACKEND_URL_ENV} or {DEFAULT_BACKEND_URL!r}."
        ),
    )
    args = parser.parse_args(argv)

    try:
        asyncio.run(_run_server(args.host, args.port, args.backend_url))
    except KeyboardInterrupt:
        print("\n[*] OpenAI proxy shutting down.")


if __name__ == "__main__":
    main()
