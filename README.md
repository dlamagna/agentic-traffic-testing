# Agentic Traffic Testbed

A testbed to study how **agentic software** (LLM-powered agents with tools) generates traffic patterns that differ from non-agentic workloads.

The goal is to characterise, across multiple layers (L2–L8), how agentic workflows behave: request interarrival distributions, burstiness, RTT, flow durations, and the relationship between semantic workflow decisions (orchestration mode, tool use, agent fan-out) and packet/flow-level network behaviour.

Runs entirely on a **single GPU server** using **Docker containers** to simulate a multi-node setup.

---

## Table of contents

- [1. Architecture](#1-architecture)
- [2. Repository layout](#2-repository-layout)
- [3. Quickstart](#3-quickstart)
- [4. Agent endpoints](#4-agent-endpoints)
- [5. Agent config and roles](#5-agent-config-and-roles)
- [6. Monitoring](#6-monitoring)
- [7. Experiment runner](#7-experiment-runner)
- [8. MCP-Universe benchmark integration](#8-mcp-universe-benchmark-integration)
- [9. Shared GPU usage](#9-shared-gpu-usage)

---

## 1. Architecture

```mermaid
flowchart TB

%% LAYERS
L8["L8 Workflow (AgentVerse)"]
L7["L7 Agents + MCP (python)"]
L6["L6 Docker Containers"]
L5["L5 LLM Backend (vLLM + Llama)"]
L34["L3/4 Docker Networks"]
L2["L2 Traffic Capture (tcpdump)"]

%% MAIN STACK
L8 --> L7
L7 --> L6
L6 --> L5
L34 --> L2

%% NETWORK PATHS
L6 --> L34
L5 --> L34

%% MONITORING
PROM["Prometheus"]
GRAF["Grafana"]

PROM --> GRAF

%% METRICS
L8 -. workflow latency .-> PROM
L7 -. token usage .-> PROM
L6 -. cpu / memory .-> PROM
L5 -. model latency / TTFT .-> PROM
L34 -. connection rates .-> PROM
L2 -. packet timing .-> PROM

%% STYLING
style L8 fill:#e3f2fd,stroke:#1e88e5,stroke-width:2px
style L7 fill:#e8f5e9,stroke:#43a047,stroke-width:2px
style L6 fill:#fff8e1,stroke:#f9a825,stroke-width:2px
style L5 fill:#fce4ec,stroke:#d81b60,stroke-width:2px
style L34 fill:#ede7f6,stroke:#5e35b1,stroke-width:2px
style L2 fill:#eceff1,stroke:#546e7a,stroke-width:2px
```

The testbed runs in **distributed mode** (default): each logical service gets an isolated Docker network plus every service joins a shared `inter_agent_network` (`172.23.0.0/24`) that carries all cross-service traffic.

| Service | agent_a_network | agent_b_network | llm_network | tools_network | inter_agent_network |
|---|---|---|---|---|---|
| agent-a | 172.20.0.10 | — | — | — | 172.23.0.10 |
| agent-b | — | 172.21.0.10 | — | — | 172.23.0.20 |
| agent-b-2 | — | 172.21.0.11 | — | — | 172.23.0.21 |
| agent-b-3 | — | 172.21.0.12 | — | — | 172.23.0.22 |
| agent-b-4 | — | 172.21.0.13 | — | — | 172.23.0.23 |
| agent-b-5 | — | 172.21.0.14 | — | — | 172.23.0.24 |
| llm-backend | — | — | 172.22.0.10 | — | 172.23.0.30 |
| mcp-tool-db | — | — | — | 172.24.0.10 | 172.23.0.40 |
| prometheus | — | — | — | — | 172.23.0.70 |
| grafana | — | — | — | — | 172.23.0.71 |
| cadvisor | — | — | — | — | 172.23.0.72 |
| docker-mapping-exporter | — | — | — | — | 172.23.0.73 |

All IPs are overridable via environment variables in `infra/.env`. The conditions of each network can be manipulated to introduce delay, jitter, and packet loss. See [docs/networking.md](docs/networking.md) for full network topology details.

### Services

| Service | Role |
|---------|------|
| **agent-a** | Orchestrator — recruits Agent B experts, calls LLM, uses MCP tools |
| **agent-b** (×1–5 replicas) | Worker — receives subtasks from Agent A, calls LLM |
| **llm-backend** | vLLM server (Llama) — serves inference to all agents |
| **mcp-tool-db** | MCP tool server (database / synthetic tools) |
| **prometheus / grafana / cadvisor** | Metrics collection and visualisation |
| **docker-mapping-exporter** | Translates raw bridge/cgroup IDs → human-readable service names |

### Metrics layers

| Layer | What is measured |
|-------|-----------------|
| **L8 — Workflow** | Workflow latency, inter-agent timing |
| **L7 — Agents + MCP** | Token usage, request latency, tool invocation rate |
| **L6 — Containers** | CPU, memory, network I/O (cAdvisor) |
| **L5 — LLM Backend** | Token throughput, model latency, TTFT, in-flight requests |
| **L3/4 — Networking** | TCP bytes/packets, flow durations, SYN RTTs by service pair |
| **L2 — Traffic Capture** | Raw packet timestamps via `tcpdump` on the inter-agent bridge |

See [docs/architecture_diagrams/layers.md](docs/architecture_diagrams/layers.md) and [docs/networking.md](docs/networking.md) for full details.

---

## 2. Repository layout

```text
.
├── agents/
│   ├── agent_a/          # Orchestrator (AgentVerse, multi-hop, parallel scenarios)
│   ├── agent_b/          # Worker agent
│   ├── common/           # Shared MCP client, telemetry, tracing
│   ├── templates/        # AgentVerse workflow and contract templates
│   └── Dockerfile
├── llm/
│   ├── serve_llm.py      # vLLM server with Prometheus metrics
│   ├── config/
│   └── Dockerfile
├── infra/
│   ├── docker-compose.yml                        # Simple (single-network) mode
│   ├── docker-compose.distributed.yml            # Distributed (multi-network) mode
│   ├── docker-compose.monitoring*.yml            # Prometheus / Grafana / cAdvisor
│   └── monitoring/
│       ├── prometheus.yml
│       └── grafana/provisioning/                 # Datasources + auto-provisioned dashboard
├── scripts/
│   ├── deploy/           # Deployment and lifecycle scripts
│   ├── experiment/       # Experiment runner, query scripts, plotting
│   ├── monitoring/       # tcp_metrics_collector.py, docker_mapping_exporter.py, health_check.py
│   ├── setup/            # Prerequisites (Docker)
│   └── traffic/          # Traffic collection helpers
├── data/
│   └── runs/             # Experiment output (JSON responses, scraped metrics, plots)
├── docs/
│   ├── monitoring.md
│   ├── networking.md
│   ├── interarrival_metrics.md
│   ├── experiment_runner.md
│   ├── mcp_universe_integration.md
│   └── architecture_diagrams/
└── logs/
```

---

## 3. Quickstart

```bash
# 1. Configure environment
cd infra
cp .env.example .env
# Edit .env: set MODEL_NAME, ENABLE_MONITORING=1, etc.

# 2. Deploy (distributed mode with monitoring)
cd ..
./scripts/deploy/deploy.sh

# 3. Check health
python scripts/monitoring/health_check.py
```

- **Grafana**: `http://localhost:3001` (admin/admin) — *Agentic Traffic Testbed* dashboard
- **Prometheus**: `http://localhost:9090`
- **Agent A**: `http://localhost:8101`

See [`infra/README.md`](infra/README.md) for full deployment options, LLM config, and environment variables.

---

## 4. Agent endpoints

**Agent A** (port 8101):

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/task` | POST | Main task endpoint. Scenario controls whether Agent A calls Agent B and/or LLM. |
| `/agentverse` | POST | Full AgentVerse 4-stage workflow (recruitment → decision → execution → evaluation). |

**Agent B** (ports 8102–8106):

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/subtask` | POST | Receives subtasks from Agent A; calls LLM and returns result. |
| `/discuss` | POST | Alias for `/subtask`; used in AgentVerse collaborative discussions. |

### Quick smoke tests

```bash
# Agent A — single hop (Agent A → LLM)
curl -X POST http://localhost:8101/task \
  -H "Content-Type: application/json" \
  -d '{"task":"Summarise what this testbed is for."}'

# Agent A — multi-hop (Agent A → Agent B → LLM)
curl -X POST http://localhost:8101/task \
  -H "Content-Type: application/json" \
  -d '{"task":"Produce a 3-step plan for RTT metrics.","scenario":"agentic_multi_hop"}'

# Agent A — full AgentVerse workflow (4 stages)
curl -X POST http://localhost:8101/agentverse \
  -H "Content-Type: application/json" \
  -d '{"task":"Design a traffic analysis experiment."}'

# Agent B directly
curl -X POST http://localhost:8102/subtask \
  -H "Content-Type: application/json" \
  -d '{"subtask":"List two example MCP tool calls."}'
```

---

## 5. Agent config and roles

### Payload fields

**`/task`** (Agent A):
- `task` (required): User task text.
- `scenario`: `agentic_simple` (default), `agentic_multi_hop`, or `agentic_parallel`.
- `agent_a_role`, `agent_a_contract`: Optional role and contract for Agent A.
- `agent_b_role`, `agent_b_contract`: Optional role and contract for downstream Agent B.
- `agent_count`, `agent_b_workers`: For `agentic_parallel`; control worker count and per-worker endpoints/roles.

**`/agentverse`** (Agent A):
- `task` (required): Triggers full 4-stage workflow.

**`/subtask`** and **`/discuss`** (Agent B):
- `subtask` (required): Subtask text.
- `scenario`: Optional label for telemetry.
- `agent_b_role`, `agent_b_contract`: Optional per-request role and contract.

### Agent roles

Agent A acts as the **orchestrator** and recruits Agent B instances dynamically:

| Role | Description |
|------|-------------|
| **orchestrator** | Agent A; recruits experts, coordinates decision-making, synthesises results. |
| **planner** | Plans approach; acts as solver in vertical (solver + reviewers) mode. |
| **researcher** | Gathers information. |
| **executor** | Executes specific subtasks. |
| **critic** | Critiques proposals; reviewer in vertical mode. |
| **summarizer** | Summarises discussion or results. |

All Agent B instances run the same code; roles are passed per-request. The **AgentVerse orchestrator** (`agents/agent_a/orchestrator.py`) implements:

1. **Expert recruitment** – LLM decides which roles and how many agents.
2. **Collaborative decision** – Horizontal (democratic discussion) or vertical (solver + reviewers).
3. **Action execution** – Experts execute subtasks in parallel.
4. **Evaluation** – Assess results; optionally iterate with feedback.

---

## 6. Monitoring

When `ENABLE_MONITORING=1`, the stack deploys **Prometheus**, **Grafana**, and **cAdvisor**, and starts the **TCP metrics collector** on the host.

### Components

| Component | What it does |
|-----------|-------------|
| **cAdvisor** | Container-level CPU, memory, network I/O |
| **llm-backend `/metrics`** | `llm_*` latency, TTFT, token throughput, in-flight requests |
| **TCP metrics collector** (`scripts/monitoring/tcp_metrics_collector.py`) | Captures traffic on the `inter_agent_network` bridge via `tcpdump`; exports `tcp_bytes_total`, `tcp_packets_total`, `tcp_flow_duration_seconds_bucket`, `tcp_rtt_handshake_seconds_bucket` labelled by `src_service`/`dst_service` |
| **Docker mapping exporter** (`scripts/monitoring/docker_mapping_exporter.py`) | Translates `br-xxxx` bridge IDs and cgroup scope paths to human-readable service names for Grafana panels |

### Dashboard

The **Agentic Traffic Testbed** dashboard (`http://localhost:3001`) is auto-provisioned and covers:

- Overview (active containers, network TX/RX, LLM request rate)
- Network Traffic (bytes/packets by Docker network)
- Resource Usage (CPU and memory per container)
- Service-level Network / TCP (bytes, flow durations, SYN RTTs by service pair)
- AI Performance / LLM (latency p50/p95, TTFT, tokens/s, in-flight)
- Interarrival Interpretation (mean interarrival time, burstiness coefficient)
- Traffic Characterisation (interarrival jitter, queue wait distribution)
- LLM Configuration (vLLM settings, KV-cache concurrency, error counts)

See [docs/monitoring.md](docs/monitoring.md) for the full PromQL reference, how to start the TCP collector manually, and tips on enabling per-container cAdvisor metrics.

See [docs/interarrival_metrics.md](docs/interarrival_metrics.md) for how interarrival time is derived and interpreted alongside latency and queue metrics.

---

## 7. Experiment runner

Repeatable bulk-collection pipeline for interarrival time and related metrics.

```bash
# Run 5 iterations of each task (math-problem + coding-task)
./scripts/experiment/run_experiment.sh -n 5
```

Each run saves the AgentVerse JSON response, scrapes all Prometheus metrics, and generates matplotlib plots and an interarrival time distribution analysis.

See [docs/experiment_runner.md](docs/experiment_runner.md) for full usage, output layout, CSV schema, and how to extend tasks or metrics.

---

## 8. MCP-Universe benchmark integration

Integrates the [MCP-Universe](https://github.com/SalesforceAIResearch/MCP-Universe) benchmark framework for execution-based MCP tool evaluation across 6 domains. Runs against the local LLM backend via an OpenAI-compatible proxy.

See [docs/mcp_universe_integration.md](docs/mcp_universe_integration.md) for setup and usage.

---

## 9. Shared GPU usage

```bash
nvidia-smi
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
```

Avoid killing processes you don't own. If GPU memory is tight, lower model size or run off-peak.
