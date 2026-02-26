# MCP-Universe Integration

This document describes how the [MCP-Universe](https://github.com/SalesforceAIResearch/MCP-Universe) benchmark framework is integrated into the agentic traffic testbed. MCP-Universe provides **recognized, measurable** MCP tool benchmarks across 6 domains (Location Navigation, Repository Management, Financial Analysis, 3D Design, Browser Automation, Web Search) and enables evaluation of LLMs and agents against real-world MCP servers.

## References

- **MCP-Universe GitHub**: https://github.com/SalesforceAIResearch/MCP-Universe  
- **Research Paper**: [MCP-Universe: Benchmarking Large Language Models with Real-World Model Context Protocol Servers](https://arxiv.org/abs/2508.14704)

## Overview

The integration adds:

1. **`tools/mcp-universe/`** – Wrapper package with an OpenAI-compatible proxy that forwards MCP-Universe’s LLM calls to your **local LLM backend**.
2. **`scripts/experiment/run_mcp_universe.py`** – Script to run MCP-Universe benchmarks from the testbed.
3. **Documentation** – This file and references in the main README.

MCP-Universe itself is **not vendored**. You must clone and install it separately, then point the testbed at it via `MCP_UNIVERSE_DIR`.

---

## Prerequisites

- **Python 3.10+**
- **Docker** (for Dockerized MCP servers in some MCP-Universe benchmarks)
- **MCP-Universe repo** cloned and dependencies installed
- **Local LLM backend** (vLLM or similar) running for local evaluation

---

## 1. Clone and Set Up MCP-Universe

```bash
git clone https://github.com/SalesforceAIResearch/MCP-Universe.git
cd MCP-Universe
python -m venv venv
source venv/bin/activate   # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt
pip install -r dev-requirements.txt
cd ..
```

Set the environment variable so the testbed can find it:

```bash
export MCP_UNIVERSE_DIR=/path/to/MCP-Universe
```

---

## 2. Run the Local LLM Backend

Start your testbed’s LLM backend as usual (see [README](../README.md) and `llm/`):

```bash
# From the testbed root
python -m llm.serve_llm --port 8000
```

Or via Docker Compose:

```bash
cd infra
docker compose up -d llm-backend
```

---

## 3. Start the OpenAI Proxy (for Local LLM)

MCP-Universe expects an OpenAI-style Chat Completions API. The testbed’s LLM serves `/chat` with a custom format. The **OpenAI proxy** in `tools/mcp-universe/` translates between the two.

```bash
# From the testbed root
python -m tools.mcp_universe.openai_proxy \
  --port 8110 \
  --backend-url http://localhost:8000/chat
```

Or, if the LLM runs in Docker:

```bash
python -m tools.mcp_universe.openai_proxy \
  --port 8110 \
  --backend-url http://llm-backend:8000/chat
```

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_SERVER_URL` | `http://llm-backend:8000/chat` | Local LLM `/chat` endpoint |

---

## 4. Configure MCP-Universe to Use the Local LLM

Point MCP-Universe’s OpenAI client at the proxy:

```bash
export OPENAI_API_KEY=dummy
export OPENAI_BASE_URL=http://localhost:8110/v1
```

If running MCP-Universe in Docker, use the host-visible URL (e.g. `http://host.docker.internal:8110/v1` on Mac/Windows).

---

## 5. Run MCP-Universe Benchmarks

From the **testbed** root:

```bash
# Ensure MCP-Universe path is set
export MCP_UNIVERSE_DIR=/path/to/MCP-Universe

# List available benchmark domains
python scripts/experiment/run_mcp_universe.py --list

# Run a single domain (e.g. dummy / minimal)
python scripts/experiment/run_mcp_universe.py dummy

# Run a specific domain (requires API keys for real MCP servers)
python scripts/experiment/run_mcp_universe.py location_navigation
python scripts/experiment/run_mcp_universe.py financial_analysis

# Run all discovered benchmarks
python scripts/experiment/run_mcp_universe.py --all
```

The script adds `MCP_UNIVERSE_DIR` to `PYTHONPATH` and invokes MCP-Universe’s test modules under `tests/benchmark/mcpuniverse/`.

---

## 6. Domain-Specific API Keys

Many MCP-Universe benchmarks use real external APIs. Configure as needed in MCP-Universe’s `.env`:

| Domain | Variables | Notes |
|--------|-----------|-------|
| Web Search | `SERP_API_KEY` | SerpAPI |
| Location Navigation | `GOOGLE_MAPS_API_KEY` | Google Maps |
| Repository Management | `GITHUB_PERSONAL_ACCESS_TOKEN`, `GITHUB_PERSONAL_ACCOUNT_NAME` | GitHub |
| Financial Analysis | Uses Yahoo Finance MCP (often no key) | |
| 3D Design | `BLENDER_APP_PATH` | Blender executable path |
| Browser Automation | Playwright MCP | Browser automation |

Copy MCP-Universe’s `.env.example` to `.env` and fill in values:

```bash
cp $MCP_UNIVERSE_DIR/.env.example $MCP_UNIVERSE_DIR/.env
# Edit .env with your API keys
```

---

## 7. Measuring and Observability

### Success Rate and Evaluators

MCP-Universe uses **execution-based evaluators** (format, static, dynamic) and reports:

- **Success rate (SR)** – Fraction of tasks fully passed
- **Average evaluator score (AE)** – Fraction of individual evaluators passed
- **Average steps (AS)** – Mean number of agent steps for successful tasks

### Testbed Telemetry

When running benchmarks via `run_mcp_universe.py`:

- LLM traffic goes through your local backend and the OpenAI proxy, so it appears in your testbed’s LLM metrics and traces.
- If you run agents (e.g. Agent A) that call MCP-Universe MCP servers, their tool calls can be correlated with `TaskID`, `AgentID`, `ToolCallID` in your telemetry.

### eBPF and Network Metrics

With the distributed setup (agents and tools on separate nodes), eBPF tools (`tcplife`, `tcpconnect`, `tcprtt`, `tcpretrans`) can capture L3/L4 behavior for:

- Agent ↔ LLM backend
- Agent ↔ MCP servers
- Agent ↔ Agent

---

## 8. Architecture Summary

```
┌─────────────────────────────────────────────────────────────────────┐
│                     MCP-Universe Benchmark Runner                    │
│               (scripts/experiment/run_mcp_universe.py)               │
└─────────────────────────────────────┬───────────────────────────────┘
                                      │ PYTHONPATH=$MCP_UNIVERSE_DIR
                                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        MCP-Universe Framework                        │
│  (BenchmarkRunner, ReAct Agent, MCP servers, evaluators)             │
└─────────────────────────────────────┬───────────────────────────────┘
                                      │ OpenAI client
                                      │ OPENAI_BASE_URL=http://...:8110/v1
                                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│              OpenAI Proxy (tools/mcp-universe/openai_proxy)          │
│              POST /v1/chat/completions → local LLM backend           │
└─────────────────────────────────────┬───────────────────────────────┘
                                      │ HTTP
                                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Local LLM Backend (llm/serve_llm)                 │
│                    POST /chat (vLLM, LLaMA, etc.)                    │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 9. File Layout

```
agentic-traffic-testing/
├── tools/
│   ├── mcp_universe/           # MCP-Universe integration
│   │   ├── __init__.py
│   │   └── openai_proxy.py     # OpenAI-compatible proxy for local LLM
│   ├── mcp_servers/            # Existing synthetic MCP servers
│   └── mcp_tool_db/
├── scripts/
│   └── experiment/
│       └── run_mcp_universe.py # Benchmark runner
└── docs/
    └── mcp_universe_integration.md  # This file
```

---

## 10. Troubleshooting

### "MCP-Universe directory not found"

- Ensure `MCP_UNIVERSE_DIR` is set and points to the repo root.
- Or pass `--mcp-universe-dir /path/to/MCP-Universe`.

### "Backend LLM request failed" (502 from proxy)

- Confirm the local LLM backend is running and reachable at `--backend-url`.
- Check firewall and Docker networking (e.g. `host.docker.internal`).

### Benchmark fails with missing API keys

- Configure `.env` in the MCP-Universe repo for domains that need external APIs.
- Use the `dummy` benchmark first to verify the proxy and local LLM setup.

### Import errors when running benchmarks

- Ensure MCP-Universe dependencies are installed in the same environment or that `PYTHONPATH` includes `$MCP_UNIVERSE_DIR` when invoking the benchmark (the runner sets this automatically).
