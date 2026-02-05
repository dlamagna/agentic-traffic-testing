"""
MCP client integration for agents.

This module provides a small wrapper around the official MCP Python SDK
so agents can connect to local MCP servers (coding, maps, finance, etc.)
without becoming MCP servers themselves.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any, Dict, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


logger = logging.getLogger(__name__)


class MCPClientManager:
    """Manage stdio-based MCP server connections for an agent."""

    def __init__(self, server_configs: Dict[str, Dict[str, Any]]) -> None:
        """
        Args:
            server_configs: mapping from logical server name to config, e.g.:

                {
                    "coding": {
                        "command": "python",
                        "args": ["tools/mcp_servers/coding_server.py"],
                        "env": {},
                    },
                    ...
                }
        """
        self._server_configs = server_configs
        self._sessions: Dict[str, ClientSession] = {}
        self._tools: Dict[str, Any] = {}
        # AsyncExitStack keeps the stdio transports and sessions alive
        # for the lifetime of this manager.
        self._exit_stack = AsyncExitStack()

    async def connect_all(self) -> None:
        """Connect to all configured MCP servers and cache their tool lists."""
        await self._exit_stack.__aenter__()

        for name, cfg in self._server_configs.items():
            if name in self._sessions:
                continue

            params = StdioServerParameters(
                command=cfg.get("command", "python"),
                args=cfg.get("args", []),
                env=cfg.get("env", {}),
            )

            try:
                logger.info(
                    "Connecting to MCP server '%s' using %s %s",
                    name,
                    params.command,
                    params.args,
                )

                # stdio_client is an async context manager; we use the exit stack
                # so the transport stays open while the manager is alive.
                read, write = await self._exit_stack.enter_async_context(stdio_client(params))
                session = await self._exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()

                tools_result = await session.list_tools()
                self._sessions[name] = session
                self._tools[name] = tools_result.tools

                logger.info(
                    "Connected to MCP server '%s' with %d tools",
                    name,
                    len(tools_result.tools),
                )
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.exception("Failed to connect to MCP server '%s': %s", name, exc)

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Any:
        """Call a named tool on one of the connected MCP servers."""
        session = self._sessions.get(server_name)
        if not session:
            raise RuntimeError(f"MCP server '{server_name}' is not connected")

        try:
            result = await session.call_tool(tool_name, arguments)
            return result.content
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Error calling tool %s on MCP server '%s': %s", tool_name, server_name, exc)
            raise

    def list_tools(self, server_name: Optional[str] = None) -> Dict[str, Any]:
        """Return cached tool metadata."""
        if server_name is not None:
            return {server_name: self._tools.get(server_name, [])}
        return dict(self._tools)

    async def close(self) -> None:
        """Close all MCP sessions and transports."""
        self._sessions.clear()
        self._tools.clear()
        try:
            await self._exit_stack.aclose()
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("Error while closing MCP exit stack")


def run_sync(coro: Any) -> Any:
    """
    Helper to run an async MCP call from sync code.

    This is useful if you want to experiment with MCP integration
    from the existing synchronous HTTP handlers before moving to
    a fully async stack.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    return asyncio.run(coro)

