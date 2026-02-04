"""
MCP server providing simple coding-related tools.

This is a *demo* server intended for local experiments in the
agentic traffic testbed. It exposes lightweight, deterministic tools
that are safe to call from agents.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastmcp import FastMCP


server = FastMCP("coding-tools-server")


@server.tool()
def execute_python_code(code: str) -> dict:
    """
    Safely execute short Python snippets and return stdout/stderr.

    Intended for toy experiments only – not for untrusted multi-tenant use.
    """
    try:
        # Run code in a fresh Python subprocess with a hard timeout.
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.returncode,
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {
            "error": "Code execution timed out after 10 seconds.",
            "success": False,
        }


@server.tool()
def analyze_code_complexity(code: str) -> dict:
    """Return basic structural statistics about a Python code snippet."""
    lines = code.splitlines()
    non_empty = [line for line in lines if line.strip()]
    return {
        "lines_of_code": len(lines),
        "non_empty_lines": len(non_empty),
        "function_count": sum(1 for line in non_empty if line.lstrip().startswith("def ")),
        "class_count": sum(1 for line in non_empty if line.lstrip().startswith("class ")),
    }


@server.resource("resource://code-snippets/python")
def get_python_snippets() -> str:
    """Return a small catalog of common Python patterns for testing."""
    return (
        "Common Python Snippets:\n"
        "- List comprehension: [x * 2 for x in range(10)]\n"
        "- Dict comprehension: {k: v for k, v in items}\n"
        "- Error handling: try:\n"
        "      ...\n"
        "  except Exception as e:\n"
        "      ...\n"
    )


if __name__ == "__main__":
    # Run as an MCP stdio server
    server.run()

