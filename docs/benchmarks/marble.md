# MultiAgentBench (MARBLE) Integration

**Paper**: [Zhu et al. 2025](https://arxiv.org/abs/2503.01935)
**Repo**: https://github.com/ulab-uiuc/MARBLE
**Local clone**: `../MARBLE` (set `MARBLE_ROOT` env var to override)
**Implementation**: `benchmarks/marble/`

---

## Overview

MARBLE is a benchmark for evaluating multi-agent collaboration and competition
across domains (coding, research, bargaining, database, minecraft).  Each task
specifies a set of agents with distinct profiles, a relationship graph defining
how agents connect, and a coordination topology (star, chain, tree, graph).

The testbed integration uses MARBLE's **task definitions and topology configs
verbatim** but **reimplements the coordination logic over Docker HTTP agents**
rather than running MARBLE's in-process engine.  This produces real inter-agent
network traffic that the testbed's TCP telemetry pipeline can capture and
analyze.

---

## Why reimplement instead of using MARBLE natively?

MARBLE's engine (`marble/engine/engine.py`) is designed to run **entirely
in-process**.  Agents are Python objects; all "thinking" goes through
`marble.llms.model_prompting.model_prompting` → `litellm.completion`.  There
is **no** built-in abstraction for plugging in external HTTP agent services.

| Aspect | Native MARBLE | What this means for us |
|---|---|---|
| Agent instances | Python objects in one process | No inter-agent TCP traffic at all |
| LLM calls | `litellm.completion(model=..., messages=...)` | Could point at vLLM via `OPENAI_API_BASE`, but traffic is only process→LLM, not agent→agent |
| Inter-agent comms | `send_message()`/`msg_box` (Python dicts, in-memory) | Zero network footprint |
| Environments | Domain-specific Python classes (`WebEnv`, `ResearchEnv`, `CodingEnv`) | Tool use via environment action handlers, not HTTP |
| Agent graph | `AgentGraph` holds Python objects, traversed in-process | No HTTP discovery or routing |

**Our core goal is measuring network traffic between cooperating agents.**
Running MARBLE natively would generate only process→LLM traffic (one TCP
flow pattern), while all agent-to-agent coordination would be invisible to
tcpdump and Prometheus.  By reimplementing the coordination over HTTP, every
agent interaction becomes a real TCP flow with measurable bytes, RTT, and
flow duration.

### Alternative: running native MARBLE with vLLM

It is **also** possible to run MARBLE's engine natively by pointing LiteLLM
at the testbed's vLLM server.  This is useful for **validating task accuracy**
(do the agents solve the task?) independently of traffic measurement.

```bash
# In the MARBLE repo — set env so LiteLLM routes to vLLM
export OPENAI_API_BASE="http://localhost:8000/v1"
export OPENAI_API_KEY="dummy"

# Use the OpenAI provider prefix so LiteLLM knows the endpoint format
# e.g., if vLLM serves meta-llama/Llama-3.1-8B-Instruct:
python marble/main.py --config_path marble/configs/test_config_2.yaml
# (edit the YAML to set llm: "openai/meta-llama/Llama-3.1-8B-Instruct")
```

The testbed adapter and native MARBLE can be run on the **same tasks** for a
direct comparison of output quality, with the adapter providing the traffic
data and native MARBLE providing the ground-truth coordination behaviour.

---

## Architecture

```
MARBLE JSONL tasks  (multiagentbench/<domain>/<domain>_main.jsonl)
       ↓
benchmarks/marble/loader.py       ← parse tasks → MarbleTask dataclass
       ↓
benchmarks/marble/topology.py     ← run star/chain/tree/graph coordination
       │                             over Agent A + Agent B Docker containers
       │ HTTP POST /task ──────→ Agent A (8101) ──→ LLM backend (8000)
       │ HTTP POST /subtask ───→ Agent B-1…B-5 (8102–8106) ──→ LLM backend
       ↓
benchmarks/marble/scorer.py       ← LLM-as-judge evaluation (also via HTTP)
       ↓
benchmarks/marble/runner.py       ← CLI: load → execute → score → JSONL
```

### Module responsibilities

| Module | Responsibility |
|---|---|
| `loader.py` | Parse MARBLE JSONL into `MarbleTask`/`MarbleAgent` dataclasses; discover repo; list/count tasks |
| `topology.py` | Map agents to endpoints; implement 4 coordination modes; prompt templates; HTTP calls; token accumulation |
| `scorer.py` | LLM-as-judge evaluation across 4 dimensions; weighted aggregate; skip-judge mode |
| `runner.py` | CLI arg parsing; task iteration loop; JSONL record serialisation; summary stats |

---

## Design decisions

### D1: Agent ↔ endpoint mapping

The first MARBLE agent in the task's `agents` list is mapped to **Agent A**
(the orchestrator on port 8101).  Remaining agents are distributed
**round-robin** across the available Agent B endpoints.

```
agents[0]  → Agent A (http://localhost:8101)      is_agent_a=True
agents[1]  → Agent B-1 (http://localhost:8102/subtask)
agents[2]  → Agent B-2 (http://localhost:8103/subtask)
agents[3]  → Agent B-3 (http://localhost:8104/subtask)
agents[4]  → Agent B-4 (http://localhost:8105/subtask)
agents[5+] → wraps around to Agent B-1 again (modulo)
```

**Why first agent = Agent A?**  In MARBLE configs, `agent1` is typically the
team leader, root node, or hub.  Star configs have `[agent2, agent1,
"reports_to"]`, tree configs have `[agent1, agent2, "parent"]` — agent1 is
almost always the coordinator.  The testbed's Agent A already serves this
orchestrator role.

**Why round-robin?**  The testbed has exactly 5 Agent B containers.  MARBLE
tasks have 2–5 agents (minus the orchestrator = 1–4 leaf agents).  Round-robin
ensures every container can be used and tasks with >5 leaf agents (rare) still
work by wrapping.

**Profile injection:** Each Agent B receives the MARBLE agent's full `profile`
string as `agent_b_role` in the HTTP payload.  This is prepended to Agent B's
LLM prompt as `"Role: {profile}"`, giving each container a distinct persona
matching the original MARBLE task.

**Endpoint configurability:** Agent A URL, Agent B URLs, and timeout are all
read from environment variables at module import time, so swapping the
deployment (e.g., using the distributed Docker Compose with static IPs) requires
no code changes:

| Variable | Default | Read by |
|---|---|---|
| `AGENT_A_URL` | `http://localhost:8101` | `topology.py`, `scorer.py` |
| `AGENT_B_URLS` | `http://localhost:8102-8106/subtask` (comma-sep) | `topology.py` |
| `MAX_PARALLEL_WORKERS` | `5` | `topology.py` (thread pool cap) |
| `MARBLE_TIMEOUT_SECONDS` | `300` | `topology.py`, `scorer.py` |

### D2: Topology-to-coordination mapping

Each MARBLE topology maps to a specific coordination function that mirrors the
corresponding method in MARBLE's `Engine`:

| MARBLE mode | Engine method | Testbed function | Key behaviour |
|---|---|---|---|
| `star` | `Engine.star_coordinate()` | `run_star()` | `EnginePlanner.assign_tasks()` → parallel agent execution → synthesis → `decide_next_step()` |
| `chain` | `Engine.chain_coordinate()` | `run_chain()` | Start with agent1 → `plan_next_agent()` handoff → sequential execution → termination |
| `tree` | `Engine.tree_coordinate()` + `_execute_agent_task_recursive()` | `run_tree()` + `_tree_execute_recursive()` | Root plans for children → recursive delegation → parent synthesizes |
| `graph` | `Engine.graph_coordinate()` | `run_graph()` | All agents act → `new_communication_session` peer dialogue → synthesis |

**Default topology:**  When MARBLE JSONL tasks have an empty `coordinate_mode`
(most do), the loader applies per-domain defaults matching MARBLE's
`jsonl2yaml.py`: all domains default to `"graph"` except `minecraft` which
defaults to `"star"`.  The `--topology` CLI flag overrides this for all tasks,
enabling topology sweep experiments.

### D3: How Agent A is used as both orchestrator and LLM proxy

Agent A serves double duty:

1. **As an orchestrator identity** — in chain mode, Agent A can be the "current
   agent" that acts on the task (when MARBLE's agent1 is mapped to Agent A).
2. **As an LLM proxy for coordination decisions** — planning, synthesis, handoff
   decisions, and continuation checks are all sent as tasks to Agent A's
   `/task` endpoint.  This is deliberate: it generates real HTTP traffic for
   every coordination decision, not just for agent work.

The `_call_agent_a_llm()` helper routes all planning/meta calls through
Agent A's `/task` with `scenario="marble_<mode>_<phase>"` labels (e.g.,
`"marble_star_plan"`, `"marble_chain_handoff"`, `"marble_graph_synth"`).
These scenario labels appear in telemetry, making it possible to distinguish
coordination overhead from task work in analysis.

### D4: Prompt templates

Six prompt templates drive coordination.  They are deliberately simpler than
MARBLE's native prompts (which use full system messages + tool schemas) because
our agents use a single-shot `/task` or `/subtask` endpoint, not OpenAI
function calling:

| Template | Used by | Purpose |
|---|---|---|
| `_PLAN_TASKS_PROMPT` | star, tree | Ask the LLM to decompose the task and assign sub-tasks to specific agent IDs.  Expects JSON output `{agent_id: subtask}`. |
| `_SYNTHESIZE_PROMPT` | star, chain, tree, graph | Combine multiple agent results into a final answer. |
| `_CHAIN_HANDOFF_PROMPT` | chain | Decide which agent acts next and what instruction to give them.  Format: `NEXT_AGENT: <id>` / `INSTRUCTION: <text>` / `NEXT_AGENT: DONE`. |
| `_CONTINUE_PROMPT` | star, tree, graph | Binary decision: should the team iterate ("CONTINUE") or stop ("DONE")? |
| `_COMMUNICATION_PROMPT` | graph | Multi-turn peer dialogue: agent responds to a message from another agent. |

**Fallback parsing:**  The plan-tasks prompt asks for JSON, but LLMs sometimes
produce malformed output.  `_parse_assignments()` tries JSON extraction
(finds `{…}` boundaries), and falls back to assigning the full task to every
agent.  `_parse_handoff()` looks for `NEXT_AGENT:` / `INSTRUCTION:` lines
and returns `None` if parsing fails (chain terminates).

### D5: Truncation limits

All prompts truncate inputs to prevent context window overflow on the 4096-token
vLLM configuration:

| Field | Max chars | Rationale |
|---|---|---|
| Task content | 2000 | Preserves most MARBLE task descriptions while leaving room for agent profiles and instructions |
| Agent profile | 200–500 | Profiles can be very long; 500 chars keeps the key info |
| Agent results | 500 per agent | Prevents context explosion when synthesizing 5 agents' outputs |
| Communication messages | 500 per message, 3000 total | Communication logs can grow large; cap preserves recent/relevant content |
| Final outputs in JSONL | 5000 | Keeps JSONL files manageable |

### D6: Parallel execution via ThreadPoolExecutor

Star, tree (siblings), and graph modes use `ThreadPoolExecutor` for parallel
agent calls.  The pool size is `min(MAX_PARALLEL_WORKERS, num_agents)`,
defaulting to 5 (matching the 5 Agent B containers).

This is a deliberate deviation from MARBLE's native engine, which executes
agents **sequentially** in a for-loop (except tree siblings which could be
parallelized).  We parallelize because:
- It generates a realistic "scatter-gather" TCP traffic pattern
- It better utilizes the 5 Agent B containers
- It's closer to how production multi-agent systems actually operate

### D7: Communication sessions (graph mode)

In MARBLE's native engine, `BaseAgent.act()` may trigger a
`new_communication_session` tool call, initiating a multi-turn (5-turn) dialogue
between two agents via `send_message`/`receive_message` on in-memory `msg_box`es.

We replicate this as explicit HTTP exchanges:

1. After all agents act in parallel, the graph coordinator iterates over
   relationship edges from the MARBLE config
2. For each **unique pair** of connected agents (deduped via sorted tuple), a
   communication session is created
3. The session runs **3 turns** of alternating messages (agent2 responds to
   agent1, agent1 responds back, agent2 responds again)
4. Each message is a separate HTTP call to the appropriate Agent B endpoint
   (or Agent A if that agent is agent1)
5. The full session log is stored in `TopologyResult.communications`

**Why 3 turns instead of MARBLE's 5?**  The native MARBLE sessions are often
wasteful on smaller models — later turns tend to repeat.  3 turns captures the
essential collaborative exchange while keeping token usage manageable on
Llama-8B.  This is configurable via the `turns` parameter in
`_run_communication_session()`.

**Deduplication:** Research tasks have 10 bidirectional relationships for 5
agents (full mesh).  Without deduplication, each pair would get two sessions.
The `comm_pairs_done` set tracks `tuple(sorted([a1, a2]))` to ensure exactly
one session per pair.

### D8: Iteration control and termination

Every topology runs a loop up to `max_iterations` (default: 3, matching
MARBLE's config convention).  Between iterations, Agent A is asked via
`_should_stop()` whether the task is complete.  This mirrors MARBLE's
`EnginePlanner.decide_next_step()`.

The continuation check uses `_CONTINUE_PROMPT`, which asks the LLM to respond
with "CONTINUE" or "DONE".  The parser looks for "DONE" anywhere in the
uppercased output.

**Chain mode** has a different termination model: the handoff prompt can return
`NEXT_AGENT: DONE`, and the chain has a hard cap of `max_iterations × num_agents`
steps (matching MARBLE's `max_chain_length`).

### D9: Token accumulation

Every HTTP response from Agent A or Agent B includes `llm_meta` (or `meta`)
with `total_tokens` from the vLLM backend.  `_accumulate_tokens()` extracts
and sums these into `TopologyResult.total_tokens`.

This is an approximation — it counts tokens consumed by the LLM backend, not
the full prompt including agent scaffolding.  For precise per-call token
tracking, use the existing `logs/llm_calls.jsonl` written by Agent A/B's
`MetricsLogger`, filtered by `X-Task-ID`.

### D10: Task ID propagation

Every HTTP call within a topology run carries the same `X-Task-ID` header (a
UUID generated at the start of each task).  This enables:
- Correlating all `llm_calls.jsonl` entries for a single MARBLE task
- Joining Prometheus TCP metrics with application-level records via
  `scripts/experiment/correlate_metrics.py`
- Tracing in Jaeger (all spans share the same task_id attribute)

### D11: Scoring approach

MARBLE's native evaluator uses `model_prompting` (LiteLLM) to score
communication quality, planning quality, milestone KPIs, and domain-specific
task quality.  We mirror this with LLM-as-judge prompts routed through
Agent A's `/task` endpoint.

| Dimension | Weight | Native MARBLE equivalent | Our implementation |
|---|---|---|---|
| Task quality | 40% | `evaluate_task_research()`, `evaluate_task_world()`, `evaluate_code_quality()` | Single-prompt 1-5 scale via `score_task_quality()` |
| Collaboration | 25% | Implicit in milestone KPIs | `score_collaboration()` — rates agent synergy |
| Communication | 20% | `evaluate_communication()` | `score_communication()` — rates dialogue quality |
| Planning | 15% | `evaluate_planning()` | `score_planning()` — rates task decomposition |

**Scores are normalised to [0, 1]** by dividing the 1-5 LLM rating by 5.

**Judge calls generate telemetry:** routing judge prompts through Agent A
means scoring also produces HTTP traffic.  Use `--skip-judge` to skip scoring
when you only care about topology traffic patterns.

**Communication scoring is conditional:** if the topology produced no
communication sessions (star and chain modes typically don't), the
communication score stays at 0 and doesn't penalise the aggregate.

### D12: MARBLE JSONL format consumption

MARBLE's task JSONL files are consumed verbatim from the cloned repo.  Each
line contains:

```
{
  "scenario": "research",
  "task_id": 1,
  "coordinate_mode": "",              ← often empty; filled by defaults
  "relationships": [["agent1","agent2","collaborate with"], ...],
  "llm": "",                          ← ignored (we use testbed's vLLM)
  "environment": {"type":"","name":"","max_iterations":""},
  "task": {"content": "..."},         ← the actual task text
  "agents": [{"type":"BaseAgent","agent_id":"agent1","profile":"..."}, ...],
  "memory": {"type": ""},             ← ignored (agents are stateless)
  "metrics": {"evaluate_llm":"", ...},
  "engine_planner": {"initial_progress": "..."},
  "output": {"format":"jsonl","file_path":""}
}
```

**Fields we use:** `task_id`, `task.content`, `agents` (id + profile),
`relationships`, `coordinate_mode`, `scenario` (as domain), `metrics`
(for future domain-specific scoring flags).

**Fields we ignore:** `llm` (we use the testbed's vLLM), `memory` (agents
are HTTP-stateless), `environment` (no domain-specific env), `output`
(we write our own JSONL).  `engine_planner.initial_progress` is logged
but not used in coordination.

---

## Coordination topologies — detailed flow

### Star (centralized)

Mirrors `Engine.star_coordinate()`:

```
for each iteration (up to max_iterations):
  1. PLAN: Agent A → LLM → JSON {agent_id: subtask}       [1 HTTP call]
  2. EXECUTE: parallel fan-out to Agent B instances         [N HTTP calls]
  3. SYNTHESIZE: Agent A → LLM → combined answer           [1 HTTP call]
  4. DECIDE: Agent A → LLM → CONTINUE or DONE              [1 HTTP call]
```

Per iteration: **3 + N** HTTP calls (where N = leaf agent count).
For research tasks (4 leaf agents): **7 calls/iteration**, **21 calls/3
iterations** maximum.

### Chain (sequential)

Mirrors `Engine.chain_coordinate()`:

```
current_agent = agent1 (mapped to Agent A)
for each step (up to max_iterations × num_agents):
  1. ACT: current agent processes the task                  [1 HTTP call]
  2. HANDOFF: Agent A → LLM → NEXT_AGENT + INSTRUCTION     [1 HTTP call]
  3. If NEXT_AGENT is DONE → break
  4. Else → forward result + instruction to next agent
```

Per step: **2 HTTP calls**.  Chain terminates when the LLM says DONE or the
step limit is reached.

### Tree (hierarchical)

Mirrors `Engine.tree_coordinate()` + `_execute_agent_task_recursive()`:

```
for each iteration (up to max_iterations):
  _tree_execute_recursive(root, task):
    if has_children and depth < max_depth:
      1. PLAN: parent → LLM → sub-tasks for children       [1 HTTP call]
      2. RECURSE: parallel call to each child               [N recursive calls]
      3. SYNTHESIZE: parent combines children + own work    [1 HTTP call]
    else (leaf):
      1. ACT: leaf agent processes the task                 [1 HTTP call]
  DECIDE: continue or stop                                  [1 HTTP call]
```

**Root detection:** scans relationships for `"parent"` edges, finds agents
with no parent.  If none found, falls back to `agents[0]`.

**Max recursion depth:** capped at 3 levels (hardcoded in
`_tree_execute_recursive`'s `max_depth` parameter) to prevent runaway
delegation on deeply nested trees.

### Graph (decentralized)

Mirrors `Engine.graph_coordinate()` + `BaseAgent.act()` +
`new_communication_session`:

```
for each iteration (up to max_iterations):
  PHASE 1 — ACT:
    All agents act on global task in parallel                [N HTTP calls]
    (iteration >0: each agent sees previous round's results)

  PHASE 2 — COMMUNICATE:
    For each unique pair in relationship graph:
      Run 3-turn dialogue session                            [~6 HTTP calls/pair]

  PHASE 3 — SYNTHESIZE:
    Agent A combines all agent outputs                       [1 HTTP call]

  DECIDE: continue or stop                                   [1 HTTP call]
```

For research tasks (5 agents, 10 relationships = 10 unique pairs):
**5 + 60 + 1 + 1 = 67 calls/iteration**.  This is the most traffic-intensive
topology, which is correct — graph mode is meant to simulate fully decentralized
collaboration.

---

## Domains

| Domain | Tasks | Agents/task | Relationships | Default topology | Viability on Llama-8B |
|---|---|---|---|---|---|
| research | 100 | 5 | Full mesh ("collaborate with") | graph | **Good** — discussion tasks, no tool deps |
| coding | 100 | 3 | Bidirectional ("collaborates with") | graph | **Fair** — needs code generation quality |
| bargaining | 100 | 4 | Hierarchical ("parent") | graph | **Good** — negotiation, good for tree/graph |
| database | 100 | varies | varies | graph | **Poor** — needs Prometheus/Alertmanager stack |
| minecraft | 100 | varies | varies | star | **Not viable** — needs JS bridge + game server |

**Recommended for experiments:** research and bargaining (rich agent interaction,
no external dependencies).  Coding is viable but output quality on Llama-8B
may be low.

---

## Usage

```bash
# Run 5 research tasks with graph topology (default)
./scripts/experiment/run_marble_benchmark.sh \
    --domain research --max-tasks 5 --verbose

# Run coding tasks with star topology, skip scoring
./scripts/experiment/run_marble_benchmark.sh \
    --domain coding --topology star --skip-judge --max-tasks 3

# Run bargaining tasks with tree topology
./scripts/experiment/run_marble_benchmark.sh \
    --domain bargaining --topology tree --max-tasks 3

# Topology sweep: same tasks across all four topologies
for topo in star chain tree graph; do
    ./scripts/experiment/run_marble_benchmark.sh \
        --domain research --topology "$topo" --task-ids 1,2,3 \
        --output "logs/benchmarks/marble_research_${topo}.jsonl"
done

# Direct Python invocation
python -m benchmarks.marble.runner \
    --domain bargaining --topology tree --max-tasks 5 \
    --max-iterations 2 --output logs/benchmarks/marble_bargaining.jsonl
```

## CLI options

| Flag | Default | Description |
|---|---|---|
| `--domain` | `research` | MARBLE domain (coding, research, bargaining, database, minecraft) |
| `--topology` | from task | Override topology (star, chain, tree, graph) |
| `--max-tasks` | all | Limit number of tasks |
| `--task-ids` | all | Comma-separated specific task IDs (e.g. `1,2,5`) |
| `--max-iterations` | 3 | Max coordination iterations per task |
| `--skip-judge` | false | Skip LLM-as-judge scoring |
| `--output` | `logs/benchmarks/marble_results.jsonl` | Output JSONL path |
| `--timeout` | 300 | Per-request timeout in seconds |
| `--verbose` | false | Print per-task details to stderr |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MARBLE_ROOT` | `../MARBLE` | Path to cloned MARBLE repo |
| `AGENT_A_URL` | `http://localhost:8101` | Agent A base URL |
| `AGENT_B_URLS` | `http://localhost:8102-8106/subtask` (comma-separated) | Agent B endpoints |
| `MAX_PARALLEL_WORKERS` | `5` | Thread pool cap for parallel agent calls |
| `MARBLE_DOMAIN` | `research` | Default domain |
| `MARBLE_TOPOLOGY` | (from task) | Force topology for all tasks |
| `MARBLE_MAX_ITERATIONS` | `3` | Max iterations per task |
| `MARBLE_TIMEOUT_SECONDS` | `300` | Per-request HTTP timeout |

---

## Output schema

Each JSONL record contains:

```json
{
  "benchmark_source": "marble",
  "benchmark_domain": "research",
  "marble_task_id": 1,
  "task_id": "a1b2c3d4-...",
  "coordinate_mode": "graph",
  "topology_override": "graph",
  "agent_count": 5,
  "agent_ids": ["agent1", "agent2", "agent3", "agent4", "agent5"],

  "final_output": "...",
  "total_agent_calls": 67,
  "total_llm_calls": 67,
  "total_tokens": 34000,
  "duration_s": 120.5,
  "iterations": 1,
  "communication_sessions": 10,
  "topology_error": null,

  "score": 0.72,
  "task_quality": 0.80,
  "communication_quality": 0.60,
  "planning_quality": 0.70,
  "collaboration_quality": 0.75,
  "domain_score": 0.0,
  "score_error": null,
  "score_details": { "task_quality": {"score": 4, "reasoning": "..."}, ... },

  "agent_outputs": {
    "agent1": "...",
    "agent2": "...",
    "agent3": "...",
    "agent4": "...",
    "agent5": "..."
  }
}
```

---

## Differences from native MARBLE

| Aspect | Native MARBLE | Testbed integration |
|---|---|---|
| Agents | In-process Python objects (`BaseAgent`) | Docker containers (Agent A + Agent B) over HTTP |
| LLM calls | `litellm.completion` via `model_prompting()` | Agent A/B → vLLM backend `/chat` (or `/v1/chat/completions`) |
| Agent communication | In-process `send_message`/`msg_box` (Python dicts) | HTTP POST between containers (real TCP flows) |
| Communication turns | 5 turns per session | 3 turns per session (configurable; shorter to suit smaller models) |
| Agent execution | Sequential for-loop (star, graph), recursive (tree) | Parallel via `ThreadPoolExecutor` (star, graph, tree siblings) |
| Environments | Domain-specific (`WebEnv`, `ResearchEnv`, `CodingEnv`, `DBEnv`) with action handlers | Generic HTTP task/subtask — no environment-specific tool use |
| Shared memory | `SharedMemory` / `BaseMemory` with cross-agent read | Agents are HTTP-stateless; context is injected per-prompt |
| Agent strategies | `cot`, `react`, `reflexion`, `default` (via prompt prefixes) | Single strategy — task description only |
| Planning method | `EnginePlanner` with `naive`/`cot`/`group_discuss`/`cognitive_evolve` | Single planning prompt requesting JSON task assignments |
| Evaluation | `Evaluator` class with `model_prompting` for each dimension | LLM-as-judge through Agent A `/task` endpoint |
| Network traffic | Single-process (no inter-agent TCP traffic) | Real TCP flows between Docker containers, captured by `tcp_metrics_collector.py` |
| Tool use | Environment `action_handler_descriptions` exposed as OpenAI function tools | Not yet implemented |
| Milestone KPIs | `evaluate_kpi()` with task-specific milestone extraction | Not yet implemented (planned as domain-specific scorer extension) |

---

## Extensibility

### Adding a new domain

1. Add the JSONL path to `_DOMAIN_JSONL` in `loader.py`
2. Add a default topology to `_DEFAULT_TOPOLOGY` in `loader.py`
3. Add domain-specific scoring logic in `scorer.py` if needed (e.g., code
   quality for coding tasks, agreement detection for bargaining)
4. Update `SUPPORTED_DOMAINS` tuple

### Swapping agent endpoints

Set `AGENT_A_URL` and `AGENT_B_URLS` environment variables.  The topology
module reads these at import time.  To use the distributed Docker Compose
(with static IPs on the inter-agent network):

```bash
export AGENT_A_URL="http://172.23.0.10:8101"
export AGENT_B_URLS="http://172.23.0.20:8102/subtask,http://172.23.0.21:8103/subtask,http://172.23.0.22:8104/subtask,http://172.23.0.23:8105/subtask,http://172.23.0.24:8106/subtask"
```

### Changing scoring weights

Edit the `weights` dict in `score_marble_task()` in `scorer.py`:

```python
weights = {
    "task": 0.40,
    "collaboration": 0.25,
    "communication": 0.20,
    "planning": 0.15,
}
```

### Adding domain-specific scoring

The `MarbleScore.domain_score` field is reserved for this.  To add e.g.
bargaining outcome detection:

1. Check `topology_result.domain == "bargaining"` in `score_marble_task()`
2. Parse agent outputs for agreement/price signals
3. Set `score.domain_score` accordingly
4. Include `domain_score` in the weighted aggregate

### Changing communication turn count

Pass `turns=N` to `_run_communication_session()` in `run_graph()`.  Currently
hardcoded to 3; can be made configurable via env var or CLI flag.

---

## Known limitations

1. **No environment-specific tool use** — MARBLE's `CodingAgent` uses
   `create_code`/`edit_code` actions; `WebEnv` provides `bing_search`/
   `google_search`.  These are not available through the testbed's generic
   HTTP endpoints.  Agent work is purely LLM text generation.

2. **No shared memory** — MARBLE agents can read each other's past outputs
   via `SharedMemory`.  Our agents are stateless; inter-iteration context is
   injected into prompts (graph mode includes previous round results).

3. **No agent strategies** — MARBLE supports `cot`, `react`, `reflexion`
   reasoning prompt prefixes.  Our agents use a single direct prompt.

4. **No milestone KPI extraction** — MARBLE's evaluator extracts
   domain-specific milestones (e.g., "correctly identified sub-task").  Our
   scorer uses general-purpose LLM-as-judge ratings.

5. **Agent A serialises orchestration calls** — planning, synthesis, and
   termination checks go through Agent A's `/task` sequentially.  If Agent A
   is slow (e.g., LLM queue pressure), this becomes a bottleneck.

6. **JSON parsing fragility** — the plan-tasks prompt expects JSON, but
   Llama-8B sometimes produces invalid JSON.  The fallback (assign full task
   to every agent) ensures progress but reduces coordination quality.

7. **Thread-safety of TopologyResult** — `total_llm_calls` and `total_tokens`
   are incremented from multiple threads without locking.  In practice, Python's
   GIL prevents corruption of int increments, but counts may be slightly off
   under extreme concurrency.

---

## Web UI

A dedicated browser-based interface at `ui/marble/index.html` provides
interactive access to the MARBLE benchmark without requiring the CLI.

### Accessing the UI

| Method | URL |
|--------|-----|
| Docker landing page | `http://<host>:3000/marble/` (card on the main Agent Testbed page) |
| Direct | `http://<host>:3000/marble/index.html` |
| Local dev (file) | Open `ui/marble/index.html` directly in a browser |

The Dockerfile at `ui/Dockerfile` copies the `ui/marble/` directory into the
static file server image and generates a landing page with a MARBLE card
linking to `/marble/`.

### UI controls

| Control | Description |
|---------|-------------|
| **Domain** | Select `research`, `coding`, or `bargaining`. Updates the agent preview panel. |
| **Topology** | Visual card selector for `star`, `chain`, `tree`, `graph`. Each card shows an SVG diagram of the coordination pattern. |
| **Max Iterations** | 1–3 iterations per task (capped at 5 server-side). |
| **Task ID** | Optional integer; leave empty for the first task in the domain's JSONL. |
| **Coordination Prompt Style** | `default` (structured JSON assignments), `detailed` (verbose agent instructions), or `minimal` (brief, fast). Displayed in the task preview panel. |
| **Agent A Endpoint** | Defaults to `http://<current-host>:8101`. Overridable for remote deployments. |
| **Skip LLM-as-Judge** | When "Yes", skips the scoring phase (topology execution only, faster). |

### Agent preview

When a domain is selected, the left panel shows the MARBLE agents mapped to
Docker endpoints:

- **agent1** → Agent A (orchestrator, blue badge)
- **agent2–agentN** → Agent B instances (green badge, round-robin)

Each chip shows the agent ID, human-readable role label, and endpoint mapping.

### Backend endpoint: `POST /marble`

The UI calls Agent A's `/marble` endpoint (added to `agents/agent_a/server.py`).
This endpoint runs a **single MARBLE task** and returns the full result as JSON.

**Request body:**

```json
{
  "domain": "research",
  "topology": "graph",
  "max_iterations": 3,
  "task_id": 1,
  "skip_judge": false,
  "prompt_style": "default"
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `domain` | string | `"research"` | MARBLE domain |
| `topology` | string | `"graph"` | Coordination mode: `star`, `chain`, `tree`, `graph` |
| `max_iterations` | int | `3` | Max coordination iterations (capped at 5) |
| `task_id` | int\|null | `null` | Specific MARBLE task ID, or null for first available |
| `skip_judge` | bool | `false` | Skip LLM-as-judge scoring |
| `prompt_style` | string | `"default"` | Prompt template style |

**Response** (200 OK):

```json
{
  "benchmark_source": "marble",
  "benchmark_domain": "research",
  "marble_task_id": 1,
  "task_id": "abc-123",
  "task_content": "...",
  "coordinate_mode": "graph",
  "agent_count": 5,
  "agent_ids": ["agent1", "agent2", "agent3", "agent4", "agent5"],
  "final_output": "...",
  "total_agent_calls": 12,
  "total_llm_calls": 8,
  "total_tokens": 4200,
  "duration_s": 45.2,
  "iterations": 2,
  "communication_sessions": 3,
  "topology_error": null,
  "agent_outputs": { "agent1": "...", "agent2": "..." },
  "iteration_details": [{ "iteration": 1, "summary": "..." }],
  "score": 0.72,
  "task_quality": 0.8,
  "communication_quality": 0.65,
  "planning_quality": 0.7,
  "collaboration_quality": 0.75,
  "domain_score": 0.0,
  "score_error": null
}
```

Score fields are omitted when `skip_judge` is true.

### Results display

The right panel renders:

1. **Status bar** — ready / running (animated pulse) / done / error with duration.
2. **Progress bar** — approximate progress during the request.
3. **Evaluation scores** — five score cards (aggregate, task, collaboration,
   communication, planning) plus a metrics table with topology, agent count,
   LLM calls, tokens, duration, etc.
4. **Iteration timeline** — chronological list of iteration summaries.
5. **Agent outputs** — per-agent output boxes, colour-coded by the domain's
   agent palette.
6. **Final output** — the synthesised final answer.
7. **Raw JSON** — collapsible full JSON response.

### Architecture flow

```
Browser (ui/marble/index.html)
    │
    │  POST /marble  { domain, topology, ... }
    ▼
Agent A server (agents/agent_a/server.py)
    │  _handle_marble()
    │    ├── benchmarks.marble.loader.load_marble_tasks()
    │    ├── benchmarks.marble.topology.run_topology()
    │    │     ├── Agent A /task  (planning, synthesis)
    │    │     └── Agent B /subtask  (worker execution)
    │    └── benchmarks.marble.scorer.score_marble_task()
    │
    ▼  JSON response
Browser renders scores, timeline, agent outputs
```

### Differences from CLI runner

| Aspect | CLI (`python -m benchmarks.marble.runner`) | UI (`POST /marble`) |
|--------|---------------------------------------------|---------------------|
| Tasks per run | Multiple (batch via `--max-tasks`) | Single task |
| Output | JSONL file | JSON HTTP response |
| Invocation | Shell script / command line | Browser button |
| Task selection | `--task-ids` list | Single `task_id` or first available |
| Prompt style | Not configurable (default only) | Selectable via dropdown |
| Scoring | Always runs unless `--skip-judge` | Configurable per request |

The underlying topology execution (`run_topology`) and scoring (`score_marble_task`)
modules are shared between the CLI and UI code paths.
