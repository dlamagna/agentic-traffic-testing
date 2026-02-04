## MCP implementation in the agentic traffic testbed

This document describes the **concrete MCP implementation** in this repository:

- how MCP fits into the **MVP architecture** from `README.md`
- which **local MCP servers** exist and what tools they expose
- how the **Python MCP client** is wired
- how to **run and test** the MCP setup
- how this ties back to **traffic analysis (eBPF, L3/L4)**.

It intentionally focuses on **local servers** and **simple, deterministic tools**, so you can
observe traffic patterns without depending on external SaaS MCP providers.

---

## 1. Architecture: where MCP fits in the MVP

The high-level MVP architecture from `README.md` has four logical nodes:

- **Node 1 – Agent A**
  - HTTP server (port 8101) that acts as the **orchestrator**.
  - Calls Agent B and the LLM backend.
  - Emits application-level telemetry with `TaskID`, `AgentID`, `ToolCallID`, etc.

- **Node 2 – Agent B**
  - Multiple HTTP servers (ports 8102–8106).
  - Handle subtasks on behalf of Agent A, calling the LLM backend.
  - Also emit telemetry.

- **Node 3 – LLM backend**
  - Local LLM server (vLLM) on port 8000.
  - Serves `/chat` for both Agent A and Agent B.

- **Node 4 – MCP tool servers**
  - A set of **separate services** that expose tools via Model Context Protocol.
  - Isolated on the `tools_network` and reachable via `inter_agent_network`.

MCP sits exclusively on **Node 4**:

- MCP servers are **independent processes/services**.
- Agents remain **plain HTTP + LLM clients**, _not_ MCP servers.
- Where desired, agents can act as **MCP clients** to those servers.

This matches the goal:

> “I don’t want to make my code an MCP server, I want demo MCP servers (coding, maps, finance, etc.) that my agents can use when needed.”

---

## 2. Existing MCP-related components in the repo

### 2.1 Dependencies

`requirements.txt` includes:

- `mcp` – official Python SDK for the Model Context Protocol.
- `fastmcp` – convenience framework for building MCP servers in Python.
- `pydantic` – used by the MCP ecosystem and for structured data models.

These are only used by:

- the **MCP servers** under `tools/mcp_servers/`
- the **MCP client** under `agents/common/mcp_client.py`

Your agents (`agent_a`, `agent_b`) and LLM backend remain valid even if MCP is not used.

---

### 2.2 MCP tool DB (HTTP tool, Node 4)

File: `tools/mcp_tool_db/server.py`

This is an **HTTP tool server** that behaves “MCP-style” for DB-like queries:

- Listens on `0.0.0.0:${MCP_TOOL_DB_PORT:-8201}` with the `/query` endpoint.
- Expects JSON:

  ```json
  {
    "query": "some text",
    "task_id": "optional-task-id"
  }
  ```

- Logs telemetry via `TelemetryLogger` with:
  - new `tool_call_id`
  - `tool_request` and `tool_response` events
  - span attributes `app.query`, `app.task_id`
- Returns a simple deterministic JSON “record” echoing the query.

In `infra/docker-compose.distributed.yml` this is wired as:

- Service `mcp-tool-db` on the **tools network** and **inter_agent_network**:
  - `tools_network` IP: `${MCP_TOOL_DB_IP:-172.24.0.10}`
  - `inter_agent_network` IP: `${MCP_TOOL_DB_INTER_IP:-172.23.0.40}`
  - Host port: `8201:8201`

Agent A is configured with:

- `MCP_DB_SERVER_URL=http://mcp-tool-db:8201/mcp` (environment variable)

This gives you a **baseline “DB tool”** with clear, observable HTTP traffic
between Agent A and Node 4.

---

### 2.3 New FastMCP demo servers (coding, finance, maps)

These are **local MCP servers** implemented with FastMCP and exposed as
stdio-based MCP servers. They live under:

- `tools/mcp_servers/__init__.py`
- `tools/mcp_servers/coding_server.py`
- `tools/mcp_servers/finance_server.py`
- `tools/mcp_servers/maps_server.py`

Each script is a standalone MCP server process.

#### 2.3.1 Coding tools MCP server

File: `tools/mcp_servers/coding_server.py`

- Server name: `"coding-tools-server"`:

  ```python
  from fastmcp import FastMCP

  server = FastMCP("coding-tools-server")
  ```

- Tools:
  - `execute_python_code(code: str) -> dict`
    - Runs a short Python snippet in a subprocess.
    - Captures `stdout`, `stderr`, `return_code`, and `success`.
    - Enforces a 10s timeout to avoid runaway code.
  - `analyze_code_complexity(code: str) -> dict`
    - Counts `lines_of_code`, `non_empty_lines`, `function_count`, `class_count`.

- Resource:
  - `resource://code-snippets/python`:
    - Returns a small catalog of Python snippets (list and dict comprehensions,
      basic error handling, etc.).

This server is good for **synthetic “coding tool” traffic** (e.g., analysis plus
occasional subprocess runs) without exposing arbitrary remote execution.

#### 2.3.2 Finance MCP server

File: `tools/mcp_servers/finance_server.py`

- Server name: `"finance-server"`.
- Synthetic stock universe:
  - Symbols: `AAPL`, `GOOGL`, `MSFT`, `TSLA`.
  - Fixed base prices and percentage changes in `_STOCK_DATA`.

- Tools:
  - `get_stock_price(symbol: str) -> dict`
    - Returns:
      - `symbol`, `price` (with small random noise), `change_percent`, `timestamp`.
      - On unknown symbol: `error` and `available_symbols`.
  - `calculate_portfolio_value(holdings: dict[str, float]) -> dict`
    - Computes total portfolio value given `{symbol: shares}`.
    - Returns `total_value`, a list of `positions`, and `timestamp`.

- Resource:
  - `resource://market/indices`:
    - Returns synthetic values for `S&P 500`, `Dow Jones`, `NASDAQ`.

This is ideal for **finance-style tool calls** where you want deterministic,
local traffic instead of hitting real market APIs.

#### 2.3.3 Maps MCP server

File: `tools/mcp_servers/maps_server.py`

- Server name: `"maps-server"`.
- Local catalog `_LOCATIONS`:
  - `New York`, `London`, `Tokyo`, `Paris` with lat/lon + country.

- Tools:
  - `geocode_location(address: str) -> dict`
    - Case-insensitive substring match against `_LOCATIONS`.
    - Returns:
      - `coordinates` (`latitude`, `longitude`)
      - `city`, `country`, `found`.
    - On failure: `found=False` and an `error` message.
  - `calculate_distance(location1: str, location2: str) -> dict`
    - Resolves both locations using `geocode_location`.
    - Uses the Haversine formula to compute `distance_km` and `distance_miles`.

- Resource:
  - `resource://maps/known-locations`:
    - Returns the catalog of known locations and coordinates.

This gives you **maps-style traffic** (parameterized calls with moderate payloads)
without relying on Google Maps or other external services.

---

## 3. MCP client implementation for agents

File: `agents/common/mcp_client.py`

This module provides a generic **MCP client manager** that agents can
instantiate when they need to talk to MCP servers.

### 3.1 Configuration model

The client expects a per-server configuration like:

```python
server_configs = {
    "coding": {
        "command": "python",
        "args": ["tools/mcp_servers/coding_server.py"],
        "env": {},
    },
    "finance": {
        "command": "python",
        "args": ["tools/mcp_servers/finance_server.py"],
    },
    "maps": {
        "command": "python",
        "args": ["tools/mcp_servers/maps_server.py"],
    },
}
```

Each entry describes how to start a **stdio MCP server**:

- `command` – e.g. `python` or `python3`.
- `args` – script path plus any command-line parameters.
- `env` – any extra environment variables (optional).

### 3.2 MCPClientManager API

Key pieces of the implementation:

- **Creation**

  ```python
  from agents.common.mcp_client import MCPClientManager

  client = MCPClientManager(server_configs)
  ```

- **Connect to all servers**

  ```python
  await client.connect_all()
  ```

  Under the hood:

  - builds `StdioServerParameters` for each config
  - calls `stdio_client()` from `mcp.client.stdio`
  - creates a `ClientSession(read, write)`
  - runs `session.initialize()`
  - calls `session.list_tools()` and caches the tools

- **List tools**

  ```python
  client.list_tools()
  client.list_tools("coding")  # single server
  ```

- **Call a tool**

  ```python
  result = await client.call_tool(
      "coding",
      "execute_python_code",
      {"code": "print('Hello from MCP')"},
  )
  ```

- **Close all sessions**

  ```python
  await client.close()
  ```

- **Synchronous helper**

  The module also provides:

  ```python
  from agents.common.mcp_client import run_sync
  ```

  This lets you invoke an async MCP call from synchronous code by wrapping
  an `async` coroutine and either:

  - using the current running loop (if one exists), or
  - starting a fresh event loop.

This client is currently **not hard-wired** into Agent A/B – you can decide where
and when to use it (for example, within `handle_tool_call` in `agents/agent_a/server.py`)
and behind which `scenario` flag.

---

## 4. MCP experiment script

File: `scripts/experiment/test_mcp_servers.py`

This script acts as a **smoke test and traffic generator** for the new MCP servers.

### 4.1 What it does

- Creates an `MCPClientManager` with server configs that point to:

  - `tools/mcp_servers/coding_server.py`
  - `tools/mcp_servers/finance_server.py`
  - `tools/mcp_servers/maps_server.py`

- Calls:
  - `client.connect_all()` and prints the tool metadata.
  - `coding.execute_python_code` with a simple snippet.
  - `finance.get_stock_price("AAPL")`.
  - `maps.calculate_distance("New York", "London")`.
- Closes all MCP sessions.

### 4.2 How to run it

From the repo root:

```bash
python scripts/experiment/test_mcp_servers.py
```

This will:

- spawn three local MCP server processes (via stdio)
- perform a small sequence of tool calls
- print the results to stdout

You can run your usual **eBPF tools** in parallel (e.g., `tcpconnect`, `tcplife`)
to observe any additional network or process activity caused by MCP usage.

---

## 5. How to integrate MCP into agents (optional, incremental)

Right now the MCP stack is **standalone**:

- Agents A/B continue to operate exactly as documented in `README.md`.
- MCP servers can be driven manually or via experiment scripts.

To let agents use MCP tools **without turning them into MCP servers**:

1. **Choose a scenario flag** (e.g. `scenario: "agentic_with_mcp_tools"`).
2. **Initialize an MCP client** inside Agent A’s server code:

   - Import `MCPClientManager`.
   - Build the `server_configs` mapping (possibly from environment variables).
   - Call `connect_all()` on startup, or lazily on first use.

3. **Route tool-like tasks to MCP** in `handle_tool_call` or similar logic:

   - Detect when the LLM (or payload) indicates a need for a tool
     (coding, finance, maps).
   - Use `client.call_tool(...)` to invoke the right server/tool.
   - Log `mcp_server`, `mcp_tool`, `mcp_call_id` in telemetry.

4. **Keep HTTP and MCP paths separate**:

   - Your existing **HTTP tools** (like the DB tool over `/query`) remain as-is.
   - MCP tools provide a second, standardized channel (stdio-based MCP).

This preserves your current **microservice-style HTTP topology** while adding
an **MCP-based tool plane** that can be turned on per-scenario for experiments.

---

## 6. Observability and traffic analysis

With MCP enabled, you have three main traffic classes to compare:

- **Agent ↔ LLM backend (Node 1/2 ↔ Node 3)** – classic LLM API traffic.
- **Agent ↔ HTTP tools (e.g., DB tool)** – standard HTTP calls to Node 4.
- **Agent ↔ MCP servers (stdio-based MCP)** – local process traffic, primarily
  visible as:
  - process-level metrics (spawned Python processes)
  - any additional logging you add around MCP calls

For network-level analysis:

- The `mcp-tool-db` container already runs on `tools_network` +
  `inter_agent_network`, so L3/L4 metrics can be collected via eBPF on Node 4.
- If you later containerize the FastMCP servers, you can place them on the same
  `tools_network` to make their traffic **topologically similar** to the DB tool.

To correlate application-level semantics with low-level behavior, include MCP
fields in your telemetry (log structure such as):

```python
{
    "task_id": "...",
    "agent_id": "AgentA",
    "mcp_server": "coding|finance|maps|db",
    "mcp_tool": "execute_python_code|get_stock_price|...",
    "mcp_call_id": "unique-id",
    "timestamp": "...",
}
```

This will let you map:

- **MCP tool invocations** ⇔ **flows and packets** in your traffic traces.
- Compare:
  - “Agentic with tools (MCP + HTTP tools)” vs
  - “Agentic without tools” vs
  - “Non-agentic baseline service chain”.

---

## 7. Summary

- MCP in this testbed is implemented as a set of **local, synthetic tool servers**:
  - HTTP DB tool (`mcp-tool-db`).
  - FastMCP servers for **coding**, **finance**, and **maps**.
- Agents remain **HTTP-based clients** and can optionally act as **MCP clients**
  via `MCPClientManager`.
- You can exercise the MCP servers using `scripts/experiment/test_mcp_servers.py`
  and your usual eBPF toolchain to study how **agentic + tools** workloads
  behave at the network and systems level.

