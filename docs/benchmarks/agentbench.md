# AgentBench Benchmark Integration

This document describes how AgentBench is wired into the Agentic Traffic Testbed and how to run it end-to-end.

---

### 1. Data source and layout

- **Upstream repo**: `https://github.com/THUDM/AgentBench`
- **Paper**: Liu et al., 2024 — "AgentBench: Evaluating LLMs as Agents" (arxiv:2308.03688)
- **Local clone**: `/home/dlamagna/projects/AgentBench` (or set `AGENTBENCH_ROOT` env var)

AgentBench provides **5 task environments** running as Docker-based servers. The testbed expects the clone at:

- `<parent>/agentic-traffic-testing`
- `<parent>/AgentBench`

Override with `AGENTBENCH_ROOT=<path>`.

**Task environments and data locations inside the AgentBench clone:**

| Task | Abbrev | Data path | Metric |
|------|--------|-----------|--------|
| OS Interaction | OS | `data/os_interaction/data/` | Success Rate (SR) |
| DBBench | DB | `data/dbbench/standard.jsonl` | Success Rate (SR) |
| KnowledgeGraph | KG | `data/knowledgegraph/std.json` | F1 score |
| ALFWorld | AF | `data/alfworld/standard.json` | Success Rate (SR) |
| WebShop | WS | API-based (~16 GB env) | SR / completion rate |

**Recommended subset for local Llama (≤8B) on a single GPU server:** OS, DB, KG. These are the most tractable and have the cleanest function-calling interfaces. ALFWorld can be added if GPU memory and eval time allow. WebShop requires a large environment download and is deferred to Phase 6.

---

### 2. Architecture and integration approach

The runner supports **two modes**:

**Standalone mode** (default — no AgentBench servers needed):

```
AgentBench data files (JSONL / JSON)
         ↓  load_tasks()
   benchmarks/agentbench/runner.py
         ↓  POST /task (single-shot prompt)
    Agent A (localhost:8101)
         ↓  offline scoring
   benchmarks/agentbench/scorer.py
```

**Controller mode** (`AGENTBENCH_URL` set):

```
AgentBench Task Server (Docker)
  HTTP /api/start_sample → /api/interact → /api/calculate_overall
         ↕
  benchmarks/agentbench/runner.py  (multi-turn agent loop)
         ↕  POST /task (one call per turn, full history as prompt)
    Agent A (localhost:8101)
         ↕  LLM calls / Agent B delegation
    LLM Backend (vLLM)
```

In controller mode each tool-use turn generates a distinct LLM call and TCP flow —
producing the iterative bursty traffic patterns this testbed is designed to study.

---

### 3. Environment setup

#### 3.1 — Standalone mode (no servers needed)

Standalone mode reads data files directly from `AGENTBENCH_ROOT`:

```bash
# Confirm AGENTBENCH_ROOT is set in infra/.env.experiment:
#   AGENTBENCH_ROOT=/home/dlamagna/projects/AgentBench

./scripts/experiment/run_agentbench.sh --task-type db --max-tasks 50 -v
```

#### 3.2 — Controller mode (full multi-turn evaluation)

Requires AgentBench task servers to be running.

```bash
cd /home/dlamagna/projects/AgentBench

# Pull Docker images
docker compose -f extra/docker-compose.yml pull

# Start task servers
python -m src.start_task -a --config configs/start_task_lite.yaml
```

Task servers register on port **5000** (controller). Enable controller mode by
setting `AGENTBENCH_URL` in `infra/.env.experiment`:

```bash
AGENTBENCH_URL=http://localhost:5000
```

OS interaction and DB tasks also need Docker-in-Docker access — the Docker socket
must be available to the runner process.

---

### 4. Adapter

File: `benchmarks/agentbench/adapter.py`

The adapter bridges Agent A (text-generation) with the AgentBench controller protocol.

**`build_agent_prompt(task, history)`** — constructs the prompt sent to Agent A at
each turn, serialising the conversation history (including previous tool calls and
results) as readable text, with format instructions asking for a `Think / Act /
Arguments` structured response.

**`parse_agent_response(task_type, tools, agent_text)`** — parses Agent A's
free-text output into a tool call.  For OS tasks it matches the `Think / Act: bash
/ ```bash ... ``` ` format.  For DB/KG tasks it extracts JSON arguments.  Returns
`(tool_name, formatted_content, parsed_args)`.

**`make_interact_request(session_id, content)`** — builds the `InteractRequest`
dict for the `/api/interact` controller endpoint.

---

### 5. Loader

File: `benchmarks/agentbench/loader.py`

**`AgentBenchTask`** dataclass:

| Field          | Type              | Description                                  |
|----------------|-------------------|----------------------------------------------|
| `task_id`      | `str`             | Unique identifier                            |
| `task_type`    | `str`             | `"os"`, `"db"`, `"kg"`                       |
| `description`  | `str`             | Natural language task prompt                 |
| `tools`        | `list[dict]`      | OpenAI function-calling tool definitions     |
| `ground_truth` | `str`             | Serialised expected answer                   |
| `raw`          | `dict`            | Full original record                         |

**`load_tasks(task_type, split="standard", max_tasks=None, agentbench_root=None)`**
yields `AgentBenchTask` items from the local data files.

**`controller_task_name(task_type)`** — returns the controller task name for a
given type (e.g. `"db"` → `"dbbench-std"`).

**`tools_for(task_type)`** — returns OpenAI tool definitions for that type.

---

### 6. Scoring

File: `benchmarks/agentbench/scorer.py`

**Success Rate (OS, DB):**
- `score = 1.0` if the answer matches ground truth, else `0.0`.
- Controller mode: task server determines pass/fail via environment execution.
- Standalone mode:
  - DB: order-insensitive, case-insensitive match against `label`.
  - OS: exact match against `evaluation.match.answer`; check-script tasks score 0 (need environment).

**F1 (KG):**
- Entity-set F1: `precision = |pred ∩ gold| / |pred|`, `recall = |pred ∩ gold| / |gold|`.
- Controller mode: controller returns F1 from SPARQL execution.
- Standalone mode: entities parsed from Agent A's text; approximate.

**Public API:**
- `agentbench_score(task_type, task, model_answer, controller_result=None) -> ScoreResult`
- `agentbench_score_scalar(...) -> float`
- `compute_aggregate(task_type, results) -> dict`

---

### 7. Runner

File: `benchmarks/agentbench/runner.py`

#### CLI

```bash
python -m benchmarks.agentbench.runner \
  --task-type db \
  --scenario agentic_multi_hop \
  --max-tasks 50 \
  --output logs/benchmarks/agentbench_db.jsonl \
  --verbose
```

| Flag | Default | Description |
|------|---------|-------------|
| `--agent-url` | `http://localhost:8101/task` | Agent A `/task` endpoint |
| `--agentbench-url` | *(unset)* | Controller URL — enables controller mode |
| `--agentbench-root` | `../AgentBench` | Path to AgentBench repo (standalone only) |
| `--task-type` | `db` | `os` \| `db` \| `kg` |
| `--scenario` | `agentic_multi_hop` | Label for `/task` calls |
| `--split` | `standard` | Dataset split |
| `--max-tasks` | *(all)* | Cap on task count |
| `--max-turns` | `10` | Max turns per task (controller mode) |
| `--output` | `logs/benchmarks/agentbench_db.jsonl` | JSONL output file |
| `--timeout` | `300` | Per-request timeout (seconds) |
| `--verbose` / `-v` | off | Print per-task results to stderr |

Environment variables (via `infra/.env.experiment`):

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTBENCH_ROOT` | `../AgentBench` | Path to repo |
| `AGENTBENCH_URL` | *(unset)* | Controller URL |
| `AGENTBENCH_MAX_TURNS` | `10` | Max turns (controller mode) |
| `AGENT_A_URL` | `http://localhost:8101/task` | Agent A endpoint |

#### Output schema (JSONL)

```json
{
  "benchmark_source": "agentbench",
  "benchmark_task_type": "db",
  "benchmark_split": "standard",
  "agentbench_task_id": "db-standard-0",
  "task_id": "<Agent A task_id>",
  "scenario": "agentic_multi_hop",
  "ground_truth": "[\"Women +60kg Bronze\"]",
  "model_answer": "Women +60kg Bronze",
  "score": 1.0,
  "metric": "success_rate",
  "score_details": {"source": "offline", "predicted": ["Women +60kg Bronze"], ...},
  "agent_response": {"output": "...", "task_id": "...", ...},
  "error": null
}
```

Failed tasks: `score=0.0`, `model_answer=null`, non-null `error`.

---

### 8. Shell wrapper

File: `scripts/experiment/run_agentbench.sh`

```bash
./scripts/experiment/run_agentbench.sh --task-type db --max-tasks 50 -v
```

Behaviour:
- Sources `infra/.env.experiment` (sets `AGENTBENCH_ROOT`, `AGENTBENCH_URL`, etc.)
- Clones AgentBench from GitHub if `AGENTBENCH_ROOT` does not exist
- In controller mode: checks controller reachability; falls back to standalone if unreachable
- Invokes `python -m benchmarks.agentbench.runner "$@"`

---

### 9. Running the recommended task types

```bash
# DBBench (SQL reasoning — fastest to get offline scores)
./scripts/experiment/run_agentbench.sh --task-type db --max-tasks 50 -v

# OS interaction (bash tool use)
./scripts/experiment/run_agentbench.sh --task-type os --max-tasks 50 -v

# KnowledgeGraph (F1-scored entity retrieval)
./scripts/experiment/run_agentbench.sh --task-type kg --max-tasks 50 -v
```

---

### 10. Metrics captured per run

In addition to AgentBench's own SR/F1 scores, every run captures the full testbed telemetry:

| Layer | Metric |
|-------|--------|
| L7 — Agent | `logs/llm_calls.jsonl`: per-call token counts, latency, `call_type`, `task_id` |
| L7 — Task | `/task` response: `total_llm_calls`, `total_agent_hops`, `total_tokens`, `total_latency_ms` |
| L5 — LLM | Prometheus `llm_*`: TTFT, token throughput, in-flight requests |
| L3/4 — TCP | `tcp_bytes_total`, `tcp_flow_duration_seconds_bucket`, `tcp_rtt_handshake_seconds_bucket` by service pair |
| Correlation | `scripts/experiment/correlate_metrics.py` joins all layers on `task_id` + time window |

Each tool-use turn generates a distinct LLM call and TCP flow. The multi-turn interaction structure means a single AgentBench task produces a sequence of correlated flows — richer traffic signal than single-shot workloads.
