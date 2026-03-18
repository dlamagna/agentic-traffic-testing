# Benchmark Integrations

This directory documents how each benchmark is wired into the Agentic Traffic Testbed. The purpose of integrating these benchmarks is to ensure the testbed runs **real cognitive workloads** — not toy prompts — so that the network-level telemetry (TCP flows, byte volumes, RTT distributions) collected at L2–L4 corresponds to meaningful agentic behaviour.

---

## Benchmarks at a glance

| Benchmark | Paper | Task type | Interaction mode | Metric | Traffic pattern | Status |
|-----------|-------|-----------|-----------------|--------|----------------|--------|
| [AgentBench](agentbench.md) | Liu et al. 2024 | OS / SQL / KG / Embodied / Shopping | Multi-turn tool-use (function calling) | SR, F1 | Iterative bursts (one LLM call + one TCP flow per turn) | Phase 1 — planned |
| [OOLONG](oolong.md) | Bertsch et al. 2025 | Long-context aggregation (classification) | Single-shot | Exponential decay, exact match | One large LLM call per task; fan-out with `agentic_parallel` | Phase 2 — loader + scorer done |
| [MCP-Universe](mcp_universe.md) | SalesforceAI 2025 | Real-world MCP tool execution (6 domains) | Multi-turn ReAct via MCP protocol | SR, AE, AS | Agent ↔ MCP server ↔ LLM round-trips | Integrated |
| MultiAgentBench (MARBLE) | Zhu et al. 2025 | Multi-agent collaboration + competition | Multi-agent (star / chain / graph) | Milestone-based KPIs | Agent-to-agent + LLM fan-out; topology-dependent | Phase 3 — planned |

---

## Why these benchmarks?

Each benchmark targets a distinct traffic pattern and cognitive workload type. Together they span the full range of agentic behaviour this testbed is designed to characterise:

### AgentBench — interactive tool use
Tasks require the model to explore an environment iteratively: run a bash command, observe stdout, decide the next action, repeat. The key property is **per-turn LLM calls**: each reasoning step is a separate HTTP request to the LLM backend, generating a distinct TCP flow. This makes AgentBench the best source of **depth-of-interaction** signal — the number of turns before task completion maps directly onto iteration depth (Phase 5 metric 5.3) and flow duration distributions.

Recommended task subset for local Llama (≤8B): **OS interaction**, **DBBench**, **KnowledgeGraph**.

### OOLONG — long-context aggregation
Tasks present a large input context (up to 2^18 tokens) and ask the model to classify or aggregate across it. The key property is **large single calls**: one task = one enormous LLM request. When run in `agentic_parallel` mode, a single task fans out to N Agent B instances simultaneously, producing a scatter-gather TCP pattern. OOLONG is the cleanest benchmark for studying how **context size** affects token volume per call (Phase 5 metric 5.4) and how fan-out count affects flow counts.

The benchmark also enables scaling experiments (Phase 7.2) by varying `--context-size` from 2^13 to 2^18.

### MCP-Universe — real-world tool chains
Tasks exercise actual MCP servers (GitHub, Maps, finance, browser) via a ReAct agent loop. All LLM calls are routed through the testbed's local vLLM backend via an OpenAI proxy, so every request is instrumented. The traffic pattern is **heterogeneous round-trips**: agent → LLM (large), agent → MCP server (small, fast), agent → LLM (large), repeat. This creates a mixed flow-size distribution that complements the more uniform patterns from AgentBench and OOLONG.

### MultiAgentBench (MARBLE) — multi-agent coordination
Tasks require explicit agent-to-agent communication across different coordination topologies (star, chain, graph). The traffic patterns produced by different topologies are directly measurable in the testbed's TCP telemetry — star topology produces hub-spoke flow patterns, chain topology produces sequential flow chains, graph topology produces concurrent parallel flows. MARBLE is the most direct validation that the testbed's multi-agent scenarios (`agentic_multi_hop`, `agentic_parallel`) produce realistic traffic.

---

## Integration pattern

All benchmarks share the same integration pattern:

```
Benchmark dataset / task server
         ↓
  benchmarks/<name>/loader.py      ← loads tasks into a common dataclass
  benchmarks/<name>/adapter.py     ← translates task format → Agent A /task request (where needed)
  benchmarks/<name>/scorer.py      ← benchmark-specific accuracy metric
  benchmarks/<name>/runner.py      ← CLI runner; writes per-task JSONL output
         ↓
  Agent A  POST /task
         ↓
  LLM Backend (vLLM)
         ↓
  Testbed telemetry pipeline:
    logs/llm_calls.jsonl            ← per-call token counts, latency, task_id
    Prometheus llm_* metrics        ← TTFT, throughput, in-flight
    tcp_metrics_collector.py        ← TCP bytes, flow duration, RTT by service pair
    correlate_metrics.py            ← joins application logs + Prometheus on task_id
```

Output JSONL per benchmark follows a common schema with `benchmark_source`, `benchmark_split`, `task_id`, `scenario`, `ground_truth`, `model_answer`, `score`, and `agent_response` fields.

---

## Comparing benchmark outputs

After running multiple benchmarks, use `scripts/experiment/correlate_metrics.py` to merge each benchmark's JSONL with the Prometheus TCP telemetry over the task time window. The resulting `data/correlated.csv` has one row per task and includes both accuracy (score) and network-level columns (bytes, flow count, RTT p50/p95, flow duration p50/p95).

This enables cross-benchmark comparisons such as:
- Does higher SR in AgentBench correlate with fewer turns (lower flow count)?
- How does OOLONG's token volume per call compare to AgentBench's per-turn volume?
- Does MARBLE's topology (star vs. graph) produce measurably different RTT distributions?

These comparisons are the core research contribution documented in Phase 5 (Four Key Traffic Metrics) of the [project roadmap](../to_do.md).

---

## Adding a new benchmark

1. Create `benchmarks/<name>/` with `loader.py`, `scorer.py`, `runner.py`.
2. Write `docs/benchmarks/<name>.md` following the structure of the existing docs.
3. Add an entry to the table above and update the [main README](../../README.md) benchmarks section.
4. Add a Phase entry to `docs/to_do.md`.
