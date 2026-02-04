"""
Small experiment script to exercise the local MCP servers.

Usage:
    python scripts/experiment/test_mcp_servers.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agents.common.mcp_client import MCPClientManager


PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def main() -> None:
    config = {
        "coding": {
            "command": "python",
            "args": [str(PROJECT_ROOT / "tools" / "mcp_servers" / "coding_server.py")],
        },
        "finance": {
            "command": "python",
            "args": [str(PROJECT_ROOT / "tools" / "mcp_servers" / "finance_server.py")],
        },
        "maps": {
            "command": "python",
            "args": [str(PROJECT_ROOT / "tools" / "mcp_servers" / "maps_server.py")],
        },
    }

    client = MCPClientManager(config)
    await client.connect_all()

    print("=== Available MCP tools ===")
    print(client.list_tools())

    print("\n=== Coding: execute_python_code ===")
    coding_result = await client.call_tool(
        "coding",
        "execute_python_code",
        {"code": "print('Hello from MCP coding server'); x = 2 + 2"},
    )
    print(coding_result)

    print("\n=== Finance: get_stock_price(AAPL) ===")
    finance_result = await client.call_tool(
        "finance",
        "get_stock_price",
        {"symbol": "AAPL"},
    )
    print(finance_result)

    print("\n=== Maps: calculate_distance(New York, London) ===")
    maps_result = await client.call_tool(
        "maps",
        "calculate_distance",
        {"location1": "New York", "location2": "London"},
    )
    print(maps_result)

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

