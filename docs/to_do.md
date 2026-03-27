# TODO — Agentic Traffic Testbed: Benchmark Integration & Metrics Roadmap

> **Purpose**: Guide the evolution of `agentic-traffic-testing` from a smoke-test MVP into a
> research-grade testbed that produces credible, reproducible measurements of agentic LLM
> traffic. The goal is to run industry-standard agentic workloads so the network-level
> telemetry (TCP/L3/L4 via tcpdump + Prometheus) we collect corresponds to **real cognitive work**, not toy prompts.
>
> **Key references**:
> - RLM paper & repo: https://arxiv.org/abs/2512.24601 / https://github.com/alexzhang13/rlm
> - MultiAgentBench (MARBLE): https://arxiv.org/abs/2503.01935 / https://github.com/ulab-uiuc/MARBLE
> - OOLONG benchmark: https://github.com/abertsch72/oolong
> - AgentBench: https://github.com/THUDM/AgentBench
> - MASEval (framework-agnostic multi-agent eval): https://arxiv.org/abs/2603.08835

---

## Phase 0 — Instrumentation & Metrics Collection Layer

Before integrating any benchmarks, the testbed needs a unified metrics layer that
captures everything we need to correlate network telemetry with LLM-level behavior.
This phase is about building the plumbing.

### 0.1 — LLM-Level Metrics (per-call granularity)

Every single LLM call (from any agent) must emit a structured log/event containing:

- [x] **`call_id`** — unique ID for this specific LLM invocation (uses `request_id` from LLM response meta, falls back to UUID)
- [x] **`task_id`** — links back to the top-level user request that spawned this call (propagated via `X-Task-ID` header; agent_b reuses parent task_id)
- [x] **`agent_id`** — which agent made the call (agent-a, agent-b, sub-agent, etc.)
- [ ] **`parent_call_id`** — if this call was spawned by a prior LLM call (for tracking recursion depth; needs ReAct loop from Phase 3.1)
- [x] **`call_type`** — `root` | `sub_call` | `tool_call` | `verification` (set at each call site)
- [x] **`prompt_tokens`** — number of input tokens sent to the LLM (from LLM server meta)
- [x] **`completion_tokens`** — number of output tokens received from the LLM (from LLM server meta)
- [x] **`total_tokens`** — prompt_tokens + completion_tokens (from LLM server meta)
- [x] **`latency_ms`** — wall-clock time from request sent to response fully received (from LLM server meta)
- [x] **`model_name`** — which model served this request (from `MODEL_NAME` env var)
- [x] **`timestamp_start`** — ISO 8601 timestamp when the request was sent
- [x] **`timestamp_end`** — ISO 8601 timestamp when the response was fully received
- [x] **`http_status`** — status code (200 for success; error path logs separately)
- [ ] **`error`** — null or error message if the call failed (error path logging to MetricsLogger not yet wired)

**Implementation note**: `MetricsLogger` class in `agents/common/metrics_logger.py` wraps
every outgoing LLM HTTP call in agent-a and agent-b. Output format is JSONL written to
`logs/llm_calls.jsonl`. Each experiment run shares a single file; use `task_id` to filter per-run records.

### 0.2 — Task-Level Metrics (per-task granularity)

After a full task completes (user request → final answer), emit a task-level summary:

- [x] **`task_id`** — same as above (in `/task` response)
- [x] **`scenario`** — `agentic_simple` | `agentic_multi_hop` | `agentic_parallel` | `baseline`
- [ ] **`benchmark_source`** — `oolong` | `multiagentbench` | `browsecomp` | `custom` | `none` (Phase 1)
- [x] **`task_query`** — the original query/prompt text
- [x] **`task_answer`** — the system's final answer (`output` field)
- [ ] **`ground_truth`** — expected correct answer (Phase 1, requires benchmark runner)
- [ ] **`score`** — task accuracy score (Phase 1, requires benchmark runner)
- [x] **`total_llm_calls`** — count of all LLM calls made for this task (fan-out)
- [ ] **`max_recursion_depth`** — deepest nesting of LLM sub-calls (needs ReAct loop, Phase 3.1)
- [x] **`total_agent_hops`** — number of agent-to-agent messages exchanged
- [ ] **`total_tool_calls`** — number of MCP tool invocations (Phase 3.2)
- [x] **`total_prompt_tokens`** — sum across all LLM calls
- [x] **`total_completion_tokens`** — sum across all LLM calls
- [x] **`total_tokens`** — grand total
- [x] **`total_latency_ms`** — wall-clock time from task received to final answer
- [x] **`llm_latency_ms`** — total time spent waiting on LLM responses (sum of all call latencies)
- [x] **`cost_estimate_usd`** — estimated cost using separate input/output rates (`COST_PER_INPUT_TOKEN_USD`, `COST_PER_OUTPUT_TOKEN_USD` in `infra/.env`; defaults to `null` when both are 0)
- [x] **`task_start`** — ISO 8601 timestamp
- [x] **`task_end`** — ISO 8601 timestamp

**Implementation note**: The task-level summary is computed by the entrypoint agent
(agent-a) when it produces a final answer. It aggregates all `call_id` events belonging
to the same `task_id`.

### 0.3 — Network Telemetry Correlation Metadata

The testbed uses **tcpdump-based TCP metrics**: `scripts/monitoring/tcp_metrics_collector.py`
captures traffic on the inter-agent bridge and exposes Prometheus metrics labelled by service pair.
To correlate these network metrics with task-level application behavior:

- [x] TCP-level monitoring implemented: `tcp_metrics_collector.py` exposes `tcp_bytes_total`,
  `tcp_packets_total`, `tcp_flow_duration_seconds_bucket`, `tcp_rtt_handshake_seconds_bucket`
  labelled by `src_service`/`dst_service`.
- [x] Docker mapping exporter (`docker_mapping_exporter.py`) translates bridge/cgroup IDs to
  human-readable service names for Grafana panels.
- [x] `X-Task-ID` header propagated on all inter-service calls (agent→agent, agent→LLM).
  Agent B reuses the parent task_id when this header is present, enabling application logs
  from all services to be joined on a single `task_id`.
- [x] `scripts/experiment/correlate_metrics.py` written: joins Prometheus TCP metrics
  (queried via Prometheus HTTP API over the task time window) with `logs/llm_calls.jsonl`.
  Output: one-row-per-task CSV with bytes, packets, SYN count, flow duration p50/p95,
  RTT p50/p95. Methodology and schema documented in `docs/monitoring.md`.

---

## Phase 0.5 — RLM Workflow Integration

RLM (Recursive Language Models) is a second L8 workflow alongside AgentVerse.
Where AgentVerse runs a fixed 4-stage protocol, RLM gives the LLM a Python REPL
and lets it decide — at runtime — how many sub-calls to make, when to delegate
to Agent B, and when to return a final answer.  This produces **task-adaptive**
multi-hop traffic patterns that complement AgentVerse's structured orchestration.

Reference: https://arxiv.org/abs/2512.24601 / https://github.com/alexzhang13/rlm
Local clone: `/home/dlamagna/projects/rlm`
Implementation docs: `docs/rlm/implementation.md`

### 0.5.1 — Core workflow implementation

- [x] Add `/rlm` POST endpoint to Agent A (`agents/agent_a/server.py`)
- [x] Write `agents/agent_a/rlm_orchestrator.py`:
  - `RLMOrchestrator.run_workflow()` — mirrors `AgentVerseOrchestrator.run_workflow()` API
  - Instantiates `RLM(backend="vllm", base_url=LLM_BASE_URL)` pointing at the local vLLM OpenAI-compat API
  - Injects Agent B instances as named REPL tools (`call_agent_b`, `call_agent_b_1`, …)
  - Bridges RLM callbacks (`on_iteration_start/complete`, `on_subcall_start/complete`) to `TelemetryLogger`
  - Logs aggregated usage to `MetricsLogger` (same JSONL schema as `/task`)
  - Returns response dict with same top-level keys as `/task` for uniform benchmark runner handling
- [x] Write `benchmarks/rlm/runner.py` — sends tasks through `/rlm`, scores via OOLONG scorer or exact-match fallback
- [x] Write `scripts/experiment/run_rlm_benchmark.sh` — shell wrapper (mirrors `run_oolong_benchmark.sh`)
- [x] Write `docs/rlm/implementation.md` — standalone doc (decoupled from AgentVerse docs)
- [x] Add `/v1/chat/completions` + `/v1/models` OpenAI-compat shim to `llm/serve_llm.py`
  — RLM's `OpenAIClient` expects OpenAI-format endpoints; our LLM backend only exposed `/chat`.
  — The shim converts the `messages` array to a formatted prompt via the tokenizer's `apply_chat_template`,
    calls the same vLLM generation path, and returns an OpenAI-format `chat.completion` object.
  — **No traffic leaves the local network** — `LLM_BASE_URL` is derived from `LLM_SERVER_URL` inside the container,
    always resolving to `http://llm-backend:8000/v1`.
- [x] Mount RLM repo and agent source into `agent-a` container via `docker-compose.yml` volume mounts
  — `RLM_ROOT=/rlm` env var wired; host path configurable via `RLM_REPO_PATH` (default: `/home/dlamagna/projects/rlm`)
  — Live-mount `../agents:/app/agents:ro` and `../llm:/app/llm:ro` so changes take effect on `docker compose restart` without a rebuild
- [x] Add RLM env vars to `infra/.env.experiment.example` (`RLM_REPO_PATH`, `RLM_SCENARIO`, `RLM_MAX_DEPTH`, …)

### 0.5.5 — Running and tracing RLM

**Prerequisites**: stack must be running (`docker compose up -d` from `infra/`).

**Step 1 — Restart affected services** (picks up the new `/v1` shim and agent source mounts):

```bash
cd infra
docker compose restart llm-backend agent-a
# Wait for llm-backend to pass its healthcheck (~60s on cold start)
docker compose logs -f llm-backend | grep "Application startup complete"
```

**Step 2 — Verify the OpenAI-compat shim is live**:

```bash
curl http://localhost:8000/v1/models
# Expected: {"object": "list", "data": [{"id": "meta-llama/Llama-3.1-8B-Instruct", ...}]}
```

**Step 3 — Send a single RLM request and trace it**:

```bash
# rlm_simple — plain LLM call (no REPL), useful as a sanity check
curl -s -X POST http://localhost:8101/rlm \
  -H "Content-Type: application/json" \
  -d '{"task": "What is the capital of France?", "scenario": "rlm_simple"}' | python3 -m json.tool

# rlm_recursive — REPL loop; LLM may issue sub-calls and call Agent B
curl -s -X POST http://localhost:8101/rlm \
  -H "Content-Type: application/json" \
  -d '{"task": "List the 3 longest rivers in Europe and their approximate lengths.", "scenario": "rlm_recursive", "max_depth": 1, "agent_count": 2}' | python3 -m json.tool
```

**Step 4 — Trace the workflow in Jaeger**:

Open `http://localhost:16686` → search for service `agent-a` → look for spans with operation `agent_a.rlm_workflow`.
Each REPL iteration and Agent B delegation appears as a child span.

**Step 5 — Run the full benchmark**:

```bash
cp infra/.env.experiment.example infra/.env.experiment  # first time only; edit paths as needed
./scripts/experiment/run_rlm_benchmark.sh --scenario rlm_recursive --max-tasks 10
# Results written to logs/benchmarks/rlm_<timestamp>.jsonl
```

**Key response fields to watch**:
- `rlm_iterations` — how many REPL loop turns the LLM took
- `rlm_subcalls` — how many recursive LLM sub-calls were issued
- `total_agent_hops` — how many Agent B delegations occurred
- `total_tokens` — cumulative token budget consumed

### 0.5.2 — Scenarios

Three scenarios exposed through `/rlm`:

- [x] **`rlm_simple`** (`max_depth=0`): plain LLM call through the RLM framework; no REPL.
  Single TCP flow — clean baseline for RLM overhead measurement.
- [x] **`rlm_recursive`** (`max_depth=1`): REPL loop + recursive sub-calls; Agent B available
  as optional tools.  Creates iterative, bursty LLM traffic.
- [x] **`rlm_parallel`** (`max_depth=1`): multiple Agent B workers exposed as individually-named
  REPL tools so the LLM can fan out to them concurrently from its own code.

### 0.5.3 — Establish baseline comparisons

- [ ] Run OOLONG `trec_coarse` (50 tasks) through each of the three RLM scenarios
- [ ] Run the same tasks through `agentic_simple` and `agentverse` for direct comparison
- [ ] Document in `benchmarks/rlm/RESULTS.md`:
  - Score per scenario (OOLONG exponential-decay metric)
  - `rlm_iterations`, `rlm_subcalls`, `total_tokens` distributions
  - TCP telemetry: flow count, total bytes, RTT p50/p95 per scenario
- [ ] Validate the task-adaptive fan-out hypothesis: `rlm_simple` ≈ 1 iteration,
  `rlm_recursive` on hard tasks ≥ 5 iterations

### 0.5.4 — Benchmark runner extension (optional)

- [ ] Support AgentBench task format in `benchmarks/rlm/runner.py` (add `--task-format agentbench`)
  so AgentBench tasks can be routed through both AgentVerse and RLM for a direct workflow comparison
- [ ] Add `--workflow` flag to `benchmarks/oolong/runner.py` that also accepts `/rlm`
  (currently only supports `/task` and `/agentverse`)

---

## Phase 1 — Benchmark Integration: AgentBench

AgentBench (Liu et al., 2024) evaluates LLMs across 5 interactive, multi-turn task
environments via function calling. Unlike OOLONG (single-shot), each task requires
iterative tool use — which directly generates the multi-step traffic patterns this
testbed is designed to study. The local clone lives at `/home/dlamagna/projects/AgentBench`.

See [docs/benchmarks/agentbench.md](docs/benchmarks/agentbench.md) for full integration design.

### 1.1 — Environment setup and task server startup

- [ ] Verify AgentBench clone is at `/home/dlamagna/projects/AgentBench` (or set `AGENTBENCH_ROOT`)
- [ ] Pull AgentBench Docker images: `docker compose -f extra/docker-compose.yml pull`
- [ ] Start task servers: `python -m src.start_task -a --config configs/start_task_lite.yaml`
- [ ] Confirm controller is reachable at `http://localhost:5000`
- [ ] Identify which task types run cleanly on local Llama (recommended subset: OS, DB, KG)
- [ ] Create `benchmarks/agentbench/` directory in the repo

### 1.2 — Loader and adapter implementation

- [ ] Write `benchmarks/agentbench/loader.py`:
  - `AgentBenchTask` dataclass: `task_id`, `task_type`, `description`, `tools`, `ground_truth`, `raw`
  - `load_tasks(task_type, split="standard", max_tasks=None)` — yields `AgentBenchTask`
  - Supports task types: `os`, `db`, `kg`, `af`, `ws`
- [ ] Write `benchmarks/agentbench/adapter.py`:
  - Implements an AgentBench-compatible HTTP agent client
  - Translates each task into an Agent A `/task` POST with `scenario=agentic_multi_hop`
  - Serialises tool descriptions into the prompt (for LLMs that do not natively call structured tools)
  - Returns the agent's final answer to AgentBench's session API for environment-side evaluation
  - Passes `benchmark_source=agentbench` and `benchmark_task_type` as metadata

### 1.3 — Scoring implementation

- [ ] Write `benchmarks/agentbench/scorer.py`:
  - **Success Rate (OS, DB, ALF, WS)**: `1.0` if agent answer / action matches ground truth, else `0.0`
    - DB: exact match on result sets (order-insensitive, float tolerance 1e-2)
    - OS: environment-side check script determines pass/fail
  - **F1 score (KG)**: token-level F1 between predicted entity set and ground truth
  - `agentbench_score(task_type, y_true, y_pred) -> ScoreResult`
  - `agentbench_score_scalar(task_type, y_true, y_pred) -> float`

### 1.4 — Runner implementation

- [ ] Write `benchmarks/agentbench/runner.py`:
  - Loads tasks via `loader.py`, routes each through `adapter.py`, scores via `scorer.py`
  - Writes per-task JSONL: `benchmark_source`, `benchmark_task_type`, `benchmark_split`, `agentbench_task_id`, `task_id`, `scenario`, `ground_truth`, `model_answer`, `score`, `metric`, `score_details`, `agent_response`
  - On failure: writes `error`, `score=0.0`, `model_answer=null` and continues
  - Prints summary to stderr: tasks processed, mean score per task type
  - CLI: `--agent-url`, `--agentbench-url`, `--task-type`, `--scenario`, `--split`, `--max-tasks`, `--max-turns`, `--output`, `--timeout`
- [ ] Write `scripts/experiment/run_agentbench.sh`:
  - Sets `AGENTBENCH_ROOT`, checks task server health, invokes the runner

### 1.5 — Establish baseline scores

- [ ] Run OS interaction tasks (50 tasks, `standard` split, `agentic_multi_hop`). Record SR.
- [ ] Run DBBench (50 tasks, `standard` split). Record SR.
- [ ] Run KnowledgeGraph (50 tasks, `std` split). Record F1.
- [ ] Compare against AgentBench paper leaderboard (GPT-3.5 OS SR ≈ 32.5%, DB SR ≈ 33.3%, KG F1 ≈ 18.5%). Local Llama scores will be lower.
- [ ] Document results in `benchmarks/agentbench/RESULTS.md`

---

## Phase 2 — Benchmark Integration: OOLONG

OOLONG is a natural second benchmark: single-shot context aggregation tasks that map
directly onto multi-agent fan-out workflows. The RLM paper uses the `trec_coarse` split
(50 tasks over question datasets with semantic labels).

See [docs/benchmarks/oolong.md](docs/benchmarks/oolong.md) for full integration design.

### 2.1 — Download and prepare OOLONG data

- [ ] Clone or download OOLONG dataset from https://github.com/abertsch72/oolong
- [ ] Focus on the `trec_coarse` split as the RLM paper does
- [x] Create `benchmarks/oolong/` directory in the repo
- [x] Write a `benchmarks/oolong/loader.py` that loads the task set and provides an iterator of `(task_id, input_context, query, ground_truth)` tuples
- [x] Implement the OOLONG scoring function: `score(ŷ) = 0.75^|y - ŷ|` for numerical answers, exact match for other answers. Place in `benchmarks/oolong/scorer.py`

### 2.2 — Wire OOLONG tasks into Agent A

- [x] Create a `benchmarks/oolong/runner.py` script that:
  1. Iterates over OOLONG tasks
  2. For each task, sends the input context + query to Agent A's `/task` endpoint
  3. Captures the response
  4. Scores it using the OOLONG scoring function
  5. Writes per-task metrics to JSONL (using the schema from Phase 0.2)
- [x] Support running in different scenarios: `agentic_simple` (Agent A handles everything alone), `agentic_multi_hop` (Agent A decomposes and delegates to Agent B), `agentic_parallel` (Agent A fans out to multiple Agent B instances)
- [x] The runner should also accept a `--context-size` flag to test at different input lengths (e.g. 2^13 to 2^18 tokens), following the RLM paper's scaling methodology

### 2.3 — Establish baseline scores

- [ ] Run OOLONG `trec_coarse` with Llama-3.2-3B as a **direct single-call baseline** (no agent scaffolding). Record the score. This is your "base model" number.
- [ ] Run the same tasks through Agent A in `agentic_simple` mode. Record scores.
- [ ] Run through `agentic_multi_hop`. Record scores.
- [ ] Compare against RLM paper Table 1 numbers (Qwen3-Coder base = 36.00, RLM = 48.00; GPT-5 base = 44.00, RLM = 56.50). Your Llama-8B scores will be lower — that's expected and fine. The point is establishing a known quality floor.
- [ ] Document results in `benchmarks/oolong/RESULTS.md`

### 2.4 — OOLONG-Pairs (optional, more advanced)

- [ ] Implement the OOLONG-Pairs variant (20 pairwise aggregation queries)
- [ ] Scoring: F1 score
- [ ] This is quadratic complexity — specifically useful for stress-testing `agentic_parallel` fan-out patterns

---

## Phase 3 — Benchmark Integration: MultiAgentBench (MARBLE)

MultiAgentBench (MARBLE) is purpose-built for evaluating multi-agent collaboration
and competition. It provides milestone-based KPIs and supports multiple coordination
topologies (star, chain, tree, graph). This is the most relevant benchmark for
validating that your agent-to-agent traffic patterns are realistic.

Reference: https://arxiv.org/abs/2503.01935 / https://github.com/ulab-uiuc/MARBLE
Local clone: `/home/dlamagna/projects/MARBLE`
Implementation: `benchmarks/marble/`

### 3.1 — Evaluate MARBLE compatibility

- [x] Clone https://github.com/ulab-uiuc/MARBLE (at `../MARBLE`)
- [x] Review its task format, required agent interfaces, and coordination protocols
- [x] Determine which MARBLE scenarios can run on a local LLM
  - **Viable domains**: research (5 agents, discussion), coding (3 agents, collaboration),
    bargaining (4 agents, negotiation)
  - **Avoid**: minecraft (JS bridge + game server), database (needs Prometheus/Alertmanager)
  - MARBLE agents are in-process Python objects using LiteLLM; no HTTP agent framework
  - LLM layer is `litellm.completion` via `model_prompting()` — can point at vLLM via
    `OPENAI_API_BASE` but this skips Docker agent traffic
  - **Decision**: reimplement coordination over Docker HTTP agents for traffic generation,
    reuse MARBLE's task definitions and topology configs
- [ ] Write a compatibility assessment in `benchmarks/marble/COMPATIBILITY.md`

### 3.2 — Integrate MARBLE tasks (distributed adapter)

- [x] Create `benchmarks/marble/loader.py`:
  - `MarbleTask` / `MarbleAgent` dataclasses matching the JSONL schema
  - `load_marble_tasks(domain, max_tasks, task_ids, topology_override)` generator
  - Reads from `MARBLE_ROOT/multiagentbench/<domain>/<domain>_main.jsonl`
  - Handles empty `coordinate_mode` with per-domain defaults (matching `jsonl2yaml.py`)
- [x] Create `benchmarks/marble/topology.py` — four coordination modes over HTTP agents:
  - **Star** (`run_star`): Agent A as central planner → fan out to Agent B workers →
    synthesize. Maps to MARBLE's `Engine.star_coordinate()`.
  - **Chain** (`run_chain`): Sequential handoff between agents, Agent A mediating
    with LLM-driven `plan_next_agent` decisions. Maps to `Engine.chain_coordinate()`.
  - **Tree** (`run_tree`): Hierarchical delegation from root (Agent A) to children
    (Agent B), recursive with `plan_tasks_for_children`. Maps to
    `Engine.tree_coordinate()` + `_execute_agent_task_recursive()`.
  - **Graph** (`run_graph`): All agents act independently, then Agent A mediates
    communication sessions between connected pairs (3-turn dialogues). Maps to
    `Engine.graph_coordinate()` + `BaseAgent.new_communication_session`.
  - Agent mapping: first MARBLE agent → Agent A, remaining → Agent B endpoints
    (round-robin across 8102–8106). Agent profiles injected as `agent_b_role`.
- [x] Create `benchmarks/marble/scorer.py` — LLM-as-judge evaluation:
  - Task quality, communication quality, planning quality, collaboration scores
  - Weighted aggregate (task 40%, collaboration 25%, communication 20%, planning 15%)
  - Judge calls routed through Agent A `/task` for telemetry capture
- [x] Create `benchmarks/marble/runner.py` — CLI runner:
  - `python -m benchmarks.marble.runner --domain research --topology graph --max-tasks 5`
  - JSONL output following common schema (`benchmark_source=marble`)
  - Supports `--skip-judge` for topology-only runs (no scoring overhead)
- [x] Create `scripts/experiment/run_marble_benchmark.sh` — shell wrapper
- [ ] Wire into the same metrics pipeline from Phase 0 (MetricsLogger + task-level aggregation)

### 3.3 — Coordination topology experiments

MARBLE explicitly supports different multi-agent topologies. Map these to your scenarios:

- [x] **Star topology** → Agent A as hub, Agent B instances as spoke workers
- [x] **Chain topology** → Sequential handoff mediated by Agent A
- [x] **Tree topology** → Hierarchical delegation (Agent A root → Agent B children)
- [x] **Graph topology** → All agents act + peer communication sessions
- [ ] Run the same MARBLE tasks across all four topologies and record how network
  telemetry differs:
  ```bash
  for topo in star chain tree graph; do
      ./scripts/experiment/run_marble_benchmark.sh \
          --domain research --topology "$topo" --task-ids 1,2,3 \
          --output "logs/benchmarks/marble_research_${topo}.jsonl"
  done
  ```
- [ ] Correlate with TCP telemetry: star→hub-spoke flows, chain→sequential flows,
  tree→recursive flows, graph→concurrent parallel flows

---

## Phase 4 — Agentic Realism: Autonomous Multi-Step Workflows

This is about making the agent workflows themselves more realistic so the traffic
patterns they generate are representative of real-world agentic systems. This is the
phase where you move beyond "agent calls LLM once and responds" toward genuinely
autonomous multi-step behavior.

### 4.1 — Implement ReAct-style agent loop

The current agents seem to do single-shot LLM calls. Real agentic traffic comes from
iterative reasoning loops. Implement a ReAct (Reason + Act) loop in Agent A:

- [ ] Agent A receives a task
- [ ] Agent A enters a loop:
  1. **Think**: Call the LLM with the current context + history to decide the next action
  2. **Act**: Execute the chosen action (call a tool, query Agent B, search, etc.)
  3. **Observe**: Append the action result to context
  4. Repeat until the LLM decides to output a final answer, or a max-iteration limit is hit
- [ ] Set a configurable `MAX_ITERATIONS` (default: 10) to prevent runaway loops
- [ ] Each iteration in the loop generates its own LLM call → its own TCP flow → its own TCP telemetry data point. This is what creates the realistic bursty, multi-call traffic pattern.

### 4.2 — Implement tool-use via MCP

The `tools/mcp_tool_db/` directory exists but tool use isn't fully wired. Real agentic
workflows heavily involve tool calls, which create distinct network traffic patterns
(agent→tool is typically faster and smaller than agent→LLM).

- [ ] Implement at least 2 functioning MCP tools:
  - **DB query tool**: accepts a natural language question, translates to SQL, queries a local SQLite/Postgres, returns results
  - **Text retrieval tool**: accepts a query, does BM25 or simple keyword search over a document corpus, returns top-k chunks
- [ ] Wire these into the ReAct loop so the LLM can choose to call them as actions
- [ ] Track `tool_call_id`, `tool_name`, `tool_latency_ms`, `tool_result_size_bytes` in the metrics

### 4.3 — Implement sub-agent delegation (recursive calls)

This is the closest analog to RLM's recursive sub-calls. Agent A should be able to
spawn sub-tasks to Agent B, which then independently reasons and returns a result.

- [ ] Agent A's ReAct loop should include a `delegate_to_agent_b` action
- [ ] When delegating, Agent A constructs a sub-task prompt and sends it to Agent B
- [ ] Agent B runs its own independent ReAct loop to solve the sub-task
- [ ] Agent B's answer is returned to Agent A as an observation
- [ ] Track `parent_call_id` chains to measure recursion depth
- [ ] This creates the nested, bursty traffic pattern (A→LLM, A→B, B→LLM, B→A, A→LLM) that is characteristic of real agentic systems

### 4.4 — Parallel fan-out

- [x] For the `agentic_parallel` scenario, Agent A decomposes a task into N sub-tasks and sends them to Agent B concurrently (`ThreadPoolExecutor` in `agents/agent_a/server.py`)
- [x] Agent A waits for all Agent B responses, then synthesizes a final answer (scatter-gather pattern)
- [x] Creates a distinct "scatter-gather" network pattern: burst of outgoing requests, wait, burst of incoming responses
- [x] Fan-out count tracked as `total_agent_hops` + `total_llm_calls` in task-level response

---

## Phase 5 — The Four Key Traffic Metrics

These are the metrics that the RLM paper (and the broader agentic evaluation
literature) identify as critical for characterizing agentic workloads. Each one should
be computable from the logs collected in Phase 0.

### 5.1 — Fan-Out Pattern

**Definition**: How many LLM calls (and other sub-calls) does a single user request generate?

> **Data now available**: `total_llm_calls` and `total_agent_hops` are in every `/task` response; per-call records are in `logs/llm_calls.jsonl`; TCP flow count is in `data/correlated.csv` (`tcp_syn_count`). The analysis/plotting work below remains.

- [x] Compute from task-level metrics: `total_llm_calls` per task (in `/task` response and `llm_calls.jsonl`)
- [x] Also compute `total_agent_hops` per task (in `/task` response); `total_tool_calls` pending Phase 4.2
- [ ] Report as a **distribution** across all tasks in a benchmark run, not just a mean
- [ ] Produce a histogram: X-axis = number of sub-calls, Y-axis = number of tasks
- [ ] Compare across scenarios: baseline (1 call), agentic_simple (few calls), agentic_multi_hop (more calls), agentic_parallel (many concurrent calls)
- [ ] Compare with RLM paper: their trajectories range from single-digit to dozens of recursive calls depending on task complexity. Your fan-out should scale similarly with task difficulty.

**From TCP telemetry (Prometheus)**: Fan-out also manifests as **flow count per task** — how many distinct TCP connections are opened during a single task's lifetime. Correlate `tcp_flows_active` spikes and `tcp_syn_total` increments over the task time window with the application-level fan-out count.

### 5.2 — Cost Variance

**Definition**: Agentic costs are high-variance — cheap median, expensive tail. (RLM Observation 4.)

- [ ] For each benchmark run, compute per-task cost (or cost proxy):
  - If using API models: actual dollar cost = (prompt_tokens × input_price) + (completion_tokens × output_price)
  - If using local vLLM: use **GPU-seconds per task** as the cost proxy (total_latency_ms of LLM calls, not wall clock). Alternatively, apply a configurable $/token rate to enable apples-to-apples comparison with RLM paper costs.
- [ ] Report cost at **percentiles**: 25th, 50th, 75th, 95th — matching Figure 3 in the RLM paper
- [ ] Produce a quartile box plot per method/scenario
- [ ] The same analysis should be done on TCP telemetry (Prometheus): total bytes transferred per task (`tcp_bytes_total` over task window), flow count per task (`tcp_syn_total` delta), total connection time per task (`tcp_flow_duration_seconds_bucket`) — all should show the same high-variance pattern for agentic scenarios vs. tight variance for the baseline.

**Key insight to validate**: The baseline scenario should have nearly constant cost per task. Agentic scenarios should have similar or lower **median** cost but much higher **tail** cost. If you see this pattern in both application-level token counts AND network-level byte volumes, that's a strong signal that your testbed is producing realistic agentic traffic.

### 5.3 — Iteration Depth

**Definition**: How many agent-to-agent or agent-to-LLM round trips occur before a task completes.

- [ ] Compute `max_recursion_depth` per task from the `parent_call_id` chain in call-level logs
- [ ] Compute `total_llm_rounds` — number of ReAct loop iterations in the root agent
- [ ] Compute `total_agent_hops` — how many times messages cross agent boundaries
- [ ] Report as distributions (histogram) across tasks in a benchmark run

**From TCP telemetry (Prometheus)**: Iteration depth directly drives:
- **TCP flow duration distributions** (`tcp_flow_duration_seconds_bucket`): deeper iteration = longer-lived connections (if using keep-alive) or more connections (if not)
- **RTT accumulation** (`tcp_rtt_handshake_seconds_bucket`): more round trips = more RTT samples per task. Compare the RTT distribution for high-depth vs low-depth tasks.
- [ ] Produce a scatter plot: X-axis = iteration depth, Y-axis = median SYN RTT for that task (from `tcp_rtt_handshake_seconds_bucket`)
- [ ] Produce a scatter plot: X-axis = iteration depth, Y-axis = total flow duration (from `tcp_flow_duration_seconds_bucket`) for that task

### 5.4 — Token Volume Per Call

**Definition**: The distribution of prompt and completion token counts across individual LLM calls within a task. Root calls tend to be large (full context); sub-calls tend to be smaller (focused sub-tasks).

> **Data now available**: `logs/llm_calls.jsonl` has one record per LLM call with `prompt_tokens`, `completion_tokens`, `call_type`, `agent_id`, and `task_id`. The analysis/plotting work below remains.

- [x] Per-call `prompt_tokens` and `completion_tokens` logged to `logs/llm_calls.jsonl` by `MetricsLogger`
- [x] `call_type` set at each call site (`root` for final agent_a call, `sub_call` for planning/progress/agent_b calls)
- [ ] Report as distributions per call type:
  - Root calls: expect large prompt_tokens (full context)
  - Sub-calls: expect smaller prompt_tokens (decomposed sub-task)
  - Tool calls: expect small prompt and small completion (structured input/output)
  - Verification calls: expect moderate (re-checking a result)
- [ ] The RLM paper uses GPT-5-mini for sub-calls and GPT-5 for root calls. If you ever move to a two-model setup (large root model, small sub-task model), track which model served each call.

**From TCP telemetry (Prometheus)**: Token volume directly correlates with **bytes transferred per flow** (`tcp_bytes_total` by service pair).
- [ ] Compare the application-level token distribution with the network-level bytes-per-service-pair distribution. They should have the same shape (bimodal: large root calls + many small sub-calls).
- [ ] Produce an overlay chart: token count distribution vs. bytes-per-flow distribution, normalized.

---

## Phase 6 — Reporting & Reproducibility

### 6.1 — Experiment runner

- [ ] Create a `scripts/run_experiment.py` that:
  1. Takes a config file specifying: benchmark, scenario, number of tasks, context size, model
  2. Ensures the TCP metrics collector (`tcp_metrics_collector.py`) is running and recording
  3. Runs the benchmark tasks through the agent pipeline
  4. Scrapes Prometheus metrics at the end of the run
  5. Runs `correlate_metrics.py` to merge application JSONL logs with Prometheus TCP telemetry
  6. Produces a summary report
- [ ] Each experiment run gets a unique ID and a timestamped directory under `logs/experiments/`

### 6.2 — Summary report generator

- [ ] Write a `scripts/generate_report.py` that reads an experiment directory and produces:
  - Task accuracy summary (mean, std dev, per-task scores)
  - Fan-out distribution (histogram)
  - Cost quartiles (box plot data)
  - Iteration depth distribution (histogram)
  - Token volume by call type (grouped box plot data)
  - TCP telemetry correlation charts (scatter plots described in 5.3 and 5.4, using `data/correlated.csv` from `correlate_metrics.py`)
- [ ] Output as both JSON (for programmatic consumption) and markdown (for human review)
- [ ] Comparison mode: given two experiment directories, produce a side-by-side comparison (e.g. agentic vs. baseline, or Llama-8B vs. Llama-3B)

### 6.3 — Reproducibility checklist

- [ ] Pin all model versions, vLLM config, and benchmark dataset versions in experiment configs
- [ ] Record GPU model, VRAM, and driver version in experiment metadata
- [ ] Record vLLM scheduling parameters (max_num_seqs, max_num_batched_tokens, gpu_memory_utilization) in experiment metadata
- [ ] Publish experiment configs alongside results so others can reproduce

---

## Phase 7 — Stretch Goals

### 7.1 — BrowseComp-Plus integration

- [ ] Download BrowseComp-Plus 100K document corpus
- [ ] Set up a document retrieval tool (BM25 index over the corpus)
- [ ] Run 150 randomly sampled tasks through the agent pipeline
- [ ] Score as percentage of correct answers
- [ ] Compare against RLM paper: GPT-5 base = 0%, RLM(GPT-5) = 91.33%

### 7.2 — Scaling experiments

- [ ] For OOLONG, run at multiple context sizes (2^13, 2^14, 2^15, 2^16, 2^17, 2^18 tokens)
- [ ] Plot accuracy vs. context size curves (like RLM paper Figure 1)
- [ ] Plot cost vs. context size curves
- [ ] Plot TCP telemetry metrics (flow count, total bytes, RTT distribution from `correlate_metrics.py`) vs. context size
- [ ] This shows how network telemetry scales with input complexity

### 7.3 — MASEval integration

- [ ] Evaluate MASEval (https://arxiv.org/abs/2603.08835) as a framework-agnostic evaluation layer
- [ ] MASEval provides per-agent tracing and cross-framework benchmarking — it could replace custom metrics code
- [ ] If viable, adapt the testbed to expose a MASEval-compatible interface

### 7.4 — LLM-as-Judge scoring

- [ ] For tasks where exact-match or F1 scoring is too rigid, implement LLM-as-Judge evaluation
- [ ] Use a stronger model (or API model like Claude/GPT) to score the local model's outputs
- [ ] This is standard practice when local model quality makes exact-match scoring too harsh
- [ ] Track judge-model cost separately from testbed cost

---

## Quick Reference: What to Cite

When writing up results from this testbed, cite these for credibility:

| Benchmark / Method | Paper | What it validates |
|---|---|---|
| OOLONG | Bertsch et al. (2025) | Linear-complexity long-context aggregation tasks |
| OOLONG-Pairs | Zhang et al. (2025, RLM paper) | Quadratic-complexity pairwise reasoning |
| BrowseComp-Plus | Chen et al. (2025) | Multi-hop QA over large document corpora |
| MultiAgentBench (MARBLE) | Zhu et al. (2025) | Multi-agent collaboration & coordination |
| AgentBench | Liu et al. (2024) | General LLM-as-agent across 8 environments |
| RLM | Zhang, Kraska, Khattab (2025) | Recursive inference scaling + cost methodology |
| MASEval | (2025) | Framework-agnostic multi-agent evaluation |
| ReAct | Yao et al. (2023) | Reasoning + acting loop for tool-use agents |

---

## Priority Order

If time/resources are limited, work through phases in this order:

1. **Phase 0** (instrumentation) — nothing else works without this
2. **Phase 0.5** (RLM workflow) — second L8 workflow alongside AgentVerse; broadens traffic diversity immediately
3. **Phase 1.1–1.5** (AgentBench integration) — multi-turn tool use gives the richest traffic signal first
4. **Phase 4.1** (ReAct loop) — the single biggest upgrade for traffic realism
5. **Phase 2.1–2.3** (OOLONG integration) — gives a quality floor and comparable numbers
6. **Phase 5** (the four key metrics) — this is the actual research contribution
7. **Phase 4.2–4.3** (tools + delegation) — enriches traffic patterns
8. **Phase 3** (MARBLE) — adds multi-agent validation
9. **Phase 6** (reporting) — makes it publishable
10. **Phase 7** (stretch) — deepens the results



Ok thanks ! Can you also create a UI page, as a sub page of my landing page for the whole UI, which shows the MultiAgentBench workflow, and gives me options for topologies etc that I can run, as well as different type of agents, and a drop down for the different prompts I can select ?