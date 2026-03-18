# RLM Workflow

**Recursive Language Models (RLM)** is an L8 workflow alongside AgentVerse.
Where AgentVerse orchestrates a fixed multi-stage protocol (recruitment →
decision → execution → evaluation), RLM lets the LLM itself decide, at
runtime, how many sub-calls to make, when to delegate to Agent B, and when
it has enough information to answer.

Reference: [arxiv 2512.24601](https://arxiv.org/abs/2512.24601) /
[github.com/alexzhang13/rlm](https://github.com/alexzhang13/rlm)

---

## How RLM works

1. The LLM receives a task and a system prompt instructing it to write Python.
2. The code runs in a local REPL.  The REPL exposes:
   - `rlm_subcall(prompt)` — spawn a recursive child LLM call.
   - `call_agent_b(subtask)` — delegate a sub-task to an Agent B instance
     (HTTP POST to `/subtask`).
   - `final_answer(text)` — signal that the answer is ready and exit the loop.
3. The LLM's code output is fed back into context; the loop repeats until
   `final_answer` is called or the iteration/token/timeout limit is hit.

This produces **task-adaptive** traffic: the number of LLM calls, Agent B
delegations, and TCP flows is determined by the task itself, not pre-wired by
the orchestrator.

---

## Architecture in this testbed

```
POST /rlm  (Agent A :8101)
  └─ RLMOrchestrator.run_workflow()
       └─ RLM(backend="vllm", base_url=LLM_BASE_URL)
            ├─ LMHandler — TCP socket server routing REPL→LLM requests
            ├─ LocalREPL — in-process Python REPL
            │   ├─ call_agent_b()    →  HTTP POST :8102/subtask  →  Agent B 0
            │   ├─ call_agent_b_1()  →  HTTP POST :8103/subtask  →  Agent B 1
            │   ├─ call_agent_b_N()  →  ...
            │   └─ rlm_subcall()     →  child RLM instance (recursive)
            └─ RLMChatCompletion  →  response dict (mirrors /task schema)
```

The vLLM backend is accessed via its **OpenAI-compatible `/v1` API** (not the
testbed's custom `/chat` endpoint).  All Agent B calls generate real HTTP
requests on the inter-agent network and are captured by the TCP metrics
collector.

---

## Endpoint

```
POST /rlm
```

### Request

| Field | Type | Default | Description |
|---|---|---|---|
| `task` | string | required | The user task or prompt |
| `scenario` | string | `rlm_recursive` | `rlm_simple` \| `rlm_recursive` \| `rlm_parallel` |
| `max_depth` | int | `1` | Recursion depth. `0` = plain LLM, no REPL |
| `max_iterations` | int | `30` | Max REPL loop iterations per completion |
| `agent_count` | int | `0` | Agent B workers to expose as REPL tools |
| `agent_b_workers` | list | `[]` | Per-worker `{endpoint, role, contract}` specs |
| `max_tokens` | int | null | Cumulative token budget (stops and returns best answer) |
| `max_timeout` | float | null | Wall-clock timeout in seconds |

### Response

The response follows the same top-level schema as `/task` and `/agentverse`
for uniform handling by benchmark runners and the correlate_metrics pipeline:

```json
{
  "task_id": "uuid",
  "scenario": "rlm_recursive",
  "output": "Final answer...",
  "rlm_iterations": 5,
  "rlm_subcalls": 2,
  "rlm_execution_time_s": 14.3,
  "total_llm_calls": 3,
  "total_agent_hops": 4,
  "total_prompt_tokens": 8200,
  "total_completion_tokens": 1100,
  "total_tokens": 9300,
  "total_latency_ms": 18500,
  "llm_latency_ms": 12000,
  "cost_estimate_usd": null,
  "task_start": "2026-03-17T...",
  "task_end": "2026-03-17T...",
  "llm_requests": [...]
}
```

---

## Scenarios

| Scenario | depth | REPL | Recursion | Agent B tools | Network signature |
|---|---|---|---|---|---|
| `rlm_simple` | 0 | no | no | no | 1 LLM flow — clean baseline |
| `rlm_recursive` | 1 | yes | yes | optional | Iterative burst of LLM calls; sequential Agent B |
| `rlm_parallel` | 1 | yes | yes | multiple | LLM fans out to N Agent B workers from REPL |

`rlm_simple` is a single-call baseline equivalent to `agentic_simple` but
routed through the RLM framework — useful for isolating framework overhead.

---

## Smoke tests

```bash
# Single call — no REPL
curl -X POST http://localhost:8101/rlm \
  -H "Content-Type: application/json" \
  -d '{"task": "What is 2+2?", "scenario": "rlm_simple"}'

# Recursive REPL with 2 Agent B workers available
curl -X POST http://localhost:8101/rlm \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Analyse the network topology of a distributed agent system.",
    "scenario": "rlm_recursive",
    "agent_count": 2
  }'

# Parallel fan-out to 3 Agent B instances
curl -X POST http://localhost:8101/rlm \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Research three perspectives on multi-agent coordination.",
    "scenario": "rlm_parallel",
    "agent_count": 3
  }'
```

---

## Configuration

Set in `infra/.env` or `infra/.env.experiment`:

| Variable | Default | Description |
|---|---|---|
| `RLM_ROOT` | `/home/dlamagna/projects/rlm` | Path to cloned RLM repo |
| `LLM_BASE_URL` | derived from `LLM_SERVER_URL` | vLLM `/v1` base URL |
| `RLM_MAX_DEPTH` | `1` | Default recursion depth |
| `RLM_MAX_ITERATIONS` | `30` | Default max REPL iterations |
| `RLM_MAX_TOKENS` | unset | Token budget (0 = no limit) |
| `RLM_MAX_TIMEOUT` | unset | Timeout in seconds (0 = no limit) |

**vLLM endpoint:** RLM uses the OpenAI-compatible `/v1` path, not the
testbed's custom `/chat` endpoint.  `LLM_BASE_URL` is auto-derived:

```
LLM_SERVER_URL=http://llm-backend:8000/chat
  → LLM_BASE_URL=http://llm-backend:8000/v1  (default)
```

Override explicitly if your vLLM server uses a non-standard path.

---

## Running benchmarks through RLM

Any benchmark task set (OOLONG, AgentBench, custom JSONL) can be routed
through the `/rlm` endpoint the same way tasks are routed through `/task`
or `/agentverse`:

```bash
# OOLONG trec_coarse through RLM recursive workflow
./scripts/experiment/run_rlm_benchmark.sh \
    --scenario rlm_recursive \
    --agent-count 3 \
    --max-tasks 50 \
    --output logs/benchmarks/rlm_recursive_oolong50.jsonl

# Parallel fan-out scenario
./scripts/experiment/run_rlm_benchmark.sh \
    --scenario rlm_parallel \
    --agent-count 5 \
    --max-tasks 50

# Custom task file
./scripts/experiment/run_rlm_benchmark.sh \
    --tasks-file data/my_tasks.jsonl \
    --no-oolong-scorer \
    --output logs/benchmarks/rlm_custom.jsonl
```

See [benchmarks/rlm/runner.py](../../benchmarks/rlm/runner.py) for CLI options
and [scripts/experiment/run_rlm_benchmark.sh](../../scripts/experiment/run_rlm_benchmark.sh)
for the shell wrapper.

---

## Telemetry

Every RLM workflow integrates with the testbed's standard logging pipeline.

**`logs/llm_calls.jsonl`** — one aggregated record per workflow
(`MetricsLogger`, same schema as AgentVerse and /task calls).

**`logs/telemetry.jsonl`** — per-iteration and per-sub-call events:

| Event | When |
|---|---|
| `rlm_request_received` | Workflow starts |
| `rlm_iteration_start/complete` | Each REPL loop iteration |
| `rlm_subcall_start/complete` | Each recursive LLM sub-call |
| `agent_b_request/response/error` | Each Agent B call from REPL |
| `rlm_complete` | Workflow finishes |
| `rlm_error` | Workflow failed |

Use `task_id` to join telemetry with TCP metrics in
`scripts/experiment/correlate_metrics.py`.

---

## Expected traffic signatures

| Scenario | Signature |
|---|---|
| `rlm_simple` | Identical to `agentic_simple`: 1 flow, minimal bytes |
| `rlm_recursive` | Burst of LLM flows (one per iteration); long total flow duration; SYN RTT accumulates with depth |
| `rlm_parallel` | Multiple Agent B TCP connections opening within milliseconds of each other; scatter-gather byte pattern |

The key distinguishing property of RLM traffic vs AgentVerse: **fan-out is
task-adaptive**.  Simple tasks may generate 1–2 REPL iterations; complex ones
can iterate 10–20+ times.  This produces the high cost variance and long-tail
iteration depth observed in the RLM paper (§4 Observation 4).

---

## Implementation files

| File | Role |
|---|---|
| [agents/agent_a/rlm_orchestrator.py](../../agents/agent_a/rlm_orchestrator.py) | `RLMOrchestrator` — wraps RLM, injects Agent B as REPL tools, bridges telemetry |
| [agents/agent_a/server.py](../../agents/agent_a/server.py) | `/rlm` HTTP endpoint handler |
| [benchmarks/rlm/runner.py](../../benchmarks/rlm/runner.py) | Runner for sending benchmark tasks through `/rlm` |
| [scripts/experiment/run_rlm_benchmark.sh](../../scripts/experiment/run_rlm_benchmark.sh) | Shell wrapper |
