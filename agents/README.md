# Agents

This directory contains the multi-agent system (MAS) used by the agentic traffic testing testbed. It defines two agent roles — **Agent A** (orchestrator) and **Agent B** (worker) — along with shared utilities and workflow templates. The same agent infrastructure supports multiple distinct workflow types.

## Directory Structure

```
agents/
├── agent_a/                  # Orchestrator agent
│   ├── server.py             # HTTP server — exposes /task, /agentverse, /rlm
│   ├── main.py               # Shared LLM/Agent B call utilities + CLI entrypoint
│   ├── orchestrator.py       # AgentVerse 4-stage workflow implementation
│   ├── rlm_orchestrator.py   # RLM (Recursive Language Models) workflow
│   └── prompts.py            # LLM prompt templates used by the AgentVerse workflow
├── agent_b/                  # Worker agent
│   ├── server.py             # HTTP server — exposes /subtask and /discuss
│   └── main.py               # LLM call logic
├── common/                   # Shared utilities (used by both agents)
│   ├── telemetry.py          # Structured JSON event logging
│   ├── tracing.py            # OpenTelemetry tracing helpers
│   ├── metrics_logger.py     # Per-call metrics logging
│   └── mcp_client.py         # MCP tool client
└── templates/                # Workflow and contract configuration
    ├── agentverse_workflow.json         # Task definitions and config for AgentVerse
    ├── mas_agent_contracts_simple.json  # Contract template: planner/researcher/executor/critic
    ├── mas_agent_contracts_enhanced.json
    ├── mas_agent_contracts_debate.json  # Contract template: moderator/pro/con/judge
    └── mas_agent_contracts_auction.json # Contract template: coordinator/bidders/auctioneer
```

## Agent Roles

Both agents share a single **Dockerfile** and the same base Python image. Their behaviour is determined by which server module is started and what environment variables are set.

### Agent A — Orchestrator

Runs on port **8101**. Receives tasks from external clients (experiment scripts, the chat UI) and orchestrates the full workflow. Depending on the endpoint called, it either decomposes the task itself, delegates to a workflow-specific orchestrator (`orchestrator.py` or `rlm_orchestrator.py`), or fans out subtasks directly to Agent B.

Endpoints:
- `POST /task` — classic parallel fan-out (see below)
- `POST /agentverse` — AgentVerse 4-stage workflow
- `GET /agentverse?task_id=<id>` — retrieve a persisted AgentVerse run
- `POST /rlm` — RLM recursive workflow

### Agent B — Worker

Five instances run in parallel on ports **8102–8106**. Each listens on `/subtask` (and `/discuss` as an alias) and executes a single LLM call for the subtask it receives, optionally prefixed with a role and contract. Agent B is workflow-agnostic — it executes whatever prompt Agent A sends.

## Network Layout

```
                    ┌─────────────────────────────────────────────┐
                    │             inter_agent_network              │
                    │               172.23.0.0/24                 │
                    └─────────────────────────────────────────────┘
                         │              │              │
              ┌──────────┴──────┐   ┌──┴──────────┐  │
              │    Agent A      │   │  Agent B ×5  │  │
              │  172.23.0.10    │──▶│ .20–.24      │  │
              │  port 8101      │   │ ports 8102–6 │  │
              └─────────────────┘   └─────────────-┘  │
                                                       │
                                          ┌────────────┴────────┐
                                          │    LLM Backend       │
                                          │  172.23.0.30:8000   │
                                          │  (vLLM / Llama 3.1) │
                                          └─────────────────────┘
```

| Service   | Host port | Inter-agent IP |
|-----------|-----------|----------------|
| agent-a   | 8101      | 172.23.0.10    |
| agent-b   | 8102      | 172.23.0.20    |
| agent-b-2 | 8103      | 172.23.0.21    |
| agent-b-3 | 8104      | 172.23.0.22    |
| agent-b-4 | 8105      | 172.23.0.23    |
| agent-b-5 | 8106      | 172.23.0.24    |

## Supported Workflows

### 1. Classic Fan-out (`POST /task`)

Agent A calls the LLM to decompose the task into subtasks, then fans them out to Agent B instances in parallel. Agent roles and contracts are defined in the MAS contract templates under `templates/`. Several contract styles are provided:

| Template | Scenario |
|----------|----------|
| `mas_agent_contracts_simple.json` | Planner → researcher + executors → critic → summariser |
| `mas_agent_contracts_debate.json` | Moderator assigns pro/con roles, judge decides |
| `mas_agent_contracts_auction.json` | Coordinator defines rules, bidders submit, auctioneer allocates |
| `mas_agent_contracts_enhanced.json` | Extended simple workflow with richer contracts |

### 2. AgentVerse (`POST /agentverse`)

Implemented in `orchestrator.py`. Runs a 4-stage iterative workflow (expert recruitment → collaborative decision-making → action execution → evaluation) based on the [AgentVerse paper](https://arxiv.org/pdf/2308.10848). Task definitions and default config live in `templates/agentverse_workflow.json`. Completed runs are persisted to `logs/agentverse/<task_id>.json`.

See [docs/agentverse/implementation.md](../docs/agentverse/implementation.md) for the full stage breakdown, API reference, known issues, and configuration details.

### 3. RLM — Recursive Language Models (`POST /rlm`)

Implemented in `rlm_orchestrator.py`. Integrates the [RLM framework](https://github.com/alexzhang13/rlm) to produce multi-hop, iterative traffic patterns. Three scenarios:

| Scenario | Description |
|----------|-------------|
| `rlm_simple` | `max_depth=0` — plain LLM call through RLM (no REPL). Baseline: one TCP flow. |
| `rlm_recursive` | `max_depth=1` — LLM runs in a REPL loop, may issue recursive sub-calls via `rlm_subcall()`. Creates bursty, nested traffic. |
| `rlm_parallel` | Like recursive, but multiple Agent B workers exposed as named tools for concurrent fan-out from the REPL. |

## Configuration

### Key Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_SERVER_URL` | `http://localhost:8000/chat` | vLLM backend endpoint |
| `LLM_TIMEOUT_SECONDS` | `120` | Per-request LLM timeout |
| `LLM_MAX_MODEL_LEN` | `4096` | Model context window (must match vLLM config) |
| `LLM_MAX_TOKENS` | `512` | Default max output tokens per LLM call |
| `LLM_EVAL_MAX_TOKENS` | `= LLM_MAX_TOKENS` | Max tokens for AgentVerse evaluation calls |
| `AGENT_B_URLS` | `http://agent-b:8102/subtask` | Comma-separated Agent B endpoints |
| `AGENT_B_TIMEOUT_SECONDS` | `120` | Per-request Agent B timeout |
| `MAX_PARALLEL_WORKERS` | `5` | Max concurrent Agent B calls |
| `DISCUSSION_HISTORY_MAX_CHARS` | `6000` | Max chars of AgentVerse discussion history per round (see AgentVerse docs) |
| `EVAL_MAX_PROMPT_CHARS` | `20000` | Char guard on AgentVerse evaluation prompt |
| `LOG_LLM_REQUESTS` | `0` | Set to `1` to log full prompts/responses |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://jaeger:4318/v1/traces` | Jaeger trace endpoint |

## Known Issues

For AgentVerse-specific issues (context explosion in horizontal discussion, consensus detection, duplicate contract injection) see [docs/agentverse/implementation.md § Known Issues](../docs/agentverse/implementation.md#known-issues).
