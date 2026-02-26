"""Local demo MCP servers for the agentic traffic testbed.

This package contains small, self-contained MCP servers that expose
simple tools the agents can call when needed (coding, maps, finance, etc.).

Each server is started as a standalone process (stdio MCP server)
and is *not* integrated into the main HTTP agents, keeping the agents
as regular HTTP services while still allowing MCP-style tool usage.
"""

