# AgentVerse Implementation

This document describes how the [AgentVerse](https://arxiv.org/pdf/2308.10848) multi-agent workflow was recreated and adapted for our agentic traffic testing testbed.

## References

- **Paper**: [AgentVerse: Facilitating Multi-Agent Collaboration and Exploring Emergent Behaviors](https://arxiv.org/pdf/2308.10848) (Chen et al., ICLR 2024)
- **Original Code**: [OpenBMB/AgentVerse](https://github.com/OpenBMB/AgentVerse)

## Overview

AgentVerse proposes a four-stage framework that mirrors human group problem-solving:

1. **Expert Recruitment** – Determine and adjust agent group composition based on the task
2. **Collaborative Decision-Making** – Agents discuss and decide on the approach (horizontal or vertical structure)
3. **Action Execution** – Execute collaboratively-decided actions
4. **Evaluation** – Assess results and provide feedback for iteration

Our implementation embeds this workflow into the existing testbed: **Agent A** acts as the orchestrator, and **Agent B** instances serve as the pool of expert workers that can be assigned different roles dynamically.

---

## Architecture Mapping

| AgentVerse Concept | Our Testbed Implementation |
|--------------------|----------------------------|
| Orchestrator / Recruiter | **Agent A** (`agents/agent_a/orchestrator.py`) |
| Expert agents | **Agent B instances** (agent-b, agent-b-2, agent-b-3, agent-b-4, agent-b-5) |
| LLM backbone | **llm-backend** (vLLM serving LLaMA 3.1 8B) |
| Task input | HTTP `POST /agentverse` on Agent A (port 8101) |

Agent B instances are homogeneous at deploy time: they all run the same `/subtask` (and `/discuss`) endpoint. Roles (planner, executor, critic, etc.) are assigned dynamically at runtime via prompts and metadata.

---

## The Four Stages

### Stage 1: Expert Recruitment

**Purpose**: Analyze the task and decide which experts are needed.

**Implementation**:
- Agent A calls the LLM with a recruitment prompt
- The LLM returns JSON specifying: roles, counts, contracts, communication structure (horizontal/vertical), and reasoning
- Roles are chosen from: `planner`, `researcher`, `executor`, `critic`, `summarizer`
- Up to 5 experts (limited by `MAX_PARALLEL_WORKERS` and `AGENT_B_URLS`)
- If the LLM omits reasoning, we generate a fallback from the chosen structure

**Adaptation**: The original paper uses a dedicated "recruiter" agent; we use the orchestrator (Agent A) to perform recruitment via a single LLM call.

### Stage 2: Collaborative Decision-Making

**Purpose**: Agents discuss and agree on an approach before executing.

**Implementation** supports two structures:

| Structure | Description | Best For |
|-----------|-------------|----------|
| **Horizontal** | All experts contribute in rounds; consensus via `[CONSENSUS]` signal | Consulting, brainstorming, tool-using |
| **Vertical** | Solver proposes; reviewers critique; solver refines | Math, coding, software development |

- **Horizontal**: Each expert receives the accumulated discussion history; up to 3 rounds. Agents signal consensus by ending their response with `[CONSENSUS]` (or common markdown variants such as `**CONSENSUS**` and `**CONSENSUS:**`). The orchestrator uses a regex to detect any of these forms — all remaining rounds are skipped once every agent has signalled. Discussion history is truncated from the front when it exceeds `DISCUSSION_HISTORY_MAX_CHARS` to prevent context explosion (see [Known Issues](#known-issues)).
- **Vertical**: Solver (first expert) proposes; reviewers (remaining experts) critique; up to 3 iterations; reviewers may signal `[APPROVED]`
- After discussion, the orchestrator synthesizes a final decision via another LLM call

**Adaptation**: All experts are implemented as Agent B instances. The orchestrator sends HTTP requests to `AGENT_B_URLS` in sequence (horizontal) or solver-then-reviewers (vertical). Each request embeds the full prompt template (including role and contract), so Agent B does not prepend a duplicate contract header.

### Stage 3: Action Execution

**Purpose**: Execute the plan produced in Stage 2.

**Implementation**:
- Each expert receives a subtask derived from the synthesized decision
- Subtasks are executed in parallel via `ThreadPoolExecutor`
- Each Agent B call is an HTTP POST to `/subtask` with role, contract, and subtask text
- Agent B instances call the shared LLM and return results

**Adaptation**: Execution is parallelized across Agent B instances; the orchestrator aggregates outputs and passes them to the evaluation stage.

### Stage 4: Evaluation

**Purpose**: Check if the goal is met and decide whether to iterate.

**Implementation**:
- Orchestrator calls the LLM with an evaluation prompt
- The LLM returns JSON: `goal_achieved`, `score`, `feedback`, `missing_aspects`, `should_iterate`
- If `should_iterate` is true and iterations remain, the workflow restarts from Stage 1 with the feedback
- Maximum iterations are configurable (default 3, max 5)

**Adaptation**: Evaluation is done by the orchestrator via a single LLM call, rather than a dedicated evaluator agent.

#### Prompt length guardrails

Because the evaluation prompt includes the full text of all execution outputs from previous
iterations, it can grow large. To avoid exceeding the LLM's context window (which would
cause a `500` from the LLM backend), the orchestrator now enforces a **token‑aware**
budget before calling the Evaluator LLM:

- It uses the same tokenizer family as the vLLM backend (driven by `LLM_MODEL`) to count
  tokens on the orchestrator side.
- It computes a **prompt token budget** from the backend settings:
  - `LLM_MAX_MODEL_LEN` – max total tokens per sequence (prompt + completion)
  - `LLM_EVAL_MAX_TOKENS` (or `LLM_MAX_TOKENS` by default) – desired max completion tokens
  - `LLM_PROMPT_SAFETY_MARGIN_TOKENS` – safety margin for chat templates/overhead
- It builds a "base" evaluation prompt with an empty `results` block and tokenizes it.
- The remaining token budget is allocated to `results`. If the tokenized `results` text
  exceeds this budget, the orchestrator trims the **oldest tokens** from `results`,
  keeping the most recent execution outputs.
- When trimming occurs, the orchestrator:
  - sets span attributes such as `app.evaluation_prompt_truncated`,
    `app.evaluation_prompt_tokens`, and
    `app.evaluation_prompt_results_tokens_trimmed` on the
    `orchestrator.evaluate_results` span (visible in Jaeger), and
  - appends a short `[System] Evaluation input was truncated…` note (including approximate
    token counts) to the `feedback` field shown in the iteration details panel.

A legacy character‑based cap (`EVAL_MAX_PROMPT_CHARS`) is still used as a fallback if
tokenization is unavailable. In normal operation, the token‑aware guardrail keeps the
evaluation call within the model's configured context length while making any loss of
earlier execution detail transparent in both telemetry and the UI.

---

## Iteration and Final Synthesis

When evaluation indicates more work is needed:
- Feedback is passed back to Stage 1 (Expert Recruitment)
- The next iteration may recruit different experts or adjust the plan
- When done (goal achieved or max iterations), the orchestrator generates the final answer via a synthesis LLM call

---

## API

### Endpoint

```
POST http://agent-a:8101/agentverse
```

### Request

```json
{
  "task": "Your task description here",
  "max_iterations": 3,
  "success_threshold": 70,
  "force_structure": "horizontal"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task` | string | Yes | The user's task or question |
| `max_iterations` | int | No | Max evaluation loops (1–5, default 3) |
| `success_threshold` | int | No | Score (0–100) at which the workflow accepts the result and stops iterating (default 70) |
| `force_structure` | string | No | Override LLM-chosen communication structure: `"horizontal"` or `"vertical"`. Omit to let the recruiter decide. |

### Response

```json
{
  "task_id": "uuid",
  "original_task": "...",
  "completed": true,
  "iterations": 2,
  "final_output": "Synthesized answer...",
  "stages": {
    "recruitment": { "experts": [...], "communication_structure": "horizontal", "reasoning": "..." },
    "decision": { "final_decision": "...", "consensus_reached": true, "discussion_rounds": [...] },
    "execution": { "outputs": [...], "success_count": 3, "failure_count": 0 },
    "evaluation": { "goal_achieved": true, "score": 85, "feedback": "..." }
  },
  "iteration_history": [...],
  "llm_requests": [...]
}
```

`llm_requests` contains every LLM request and response in execution order, used by the UI "See detailed agentverse flow" table.

---

## UI

A dedicated **AgentVerse** page is available at:

```
http://localhost:3000/agentverse/
```

Features:
- Task input with example tasks (math, research, consulting, coding)
- Max iterations selector
- Four-stage workflow visualization
- Iteration history
- Final output
- **See detailed agentverse flow** – table of all LLM requests with expandable prompt/response

The main chat UI at `/chat/` is unchanged; AgentVerse is a separate flow.

---

## Running bulk experiments

The **experiment runner** (`scripts/experiment/run_experiment.sh`) is purpose-built for AgentVerse: it orchestrates repeated `/agentverse` calls across all example tasks, scrapes the full Prometheus metric set after each run, and produces matching matplotlib plots.

```bash
# 5 iterations of every example task
./scripts/experiment/run_experiment.sh -n 5
```

See [experiment_runner.md](experiment_runner.md) for the full reference: CLI flags, output directory layout, metrics CSV schema, interarrival plot interpretation, and how to extend tasks or metrics.

---

## Configuration

### Environment Variables (Agent A)

See [agents/README.md § Configuration](../../agents/README.md#configuration) for the full variable reference. AgentVerse-specific variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_MAX_MODEL_LEN` | `4096` | Model context window — must match the vLLM `--max-model-len` setting |
| `LLM_MAX_TOKENS` | `512` | Default max output tokens per LLM call |
| `LLM_EVAL_MAX_TOKENS` | `= LLM_MAX_TOKENS` | Max tokens for evaluation-stage LLM calls |
| `LLM_PROMPT_SAFETY_MARGIN_TOKENS` | `128` | Headroom reserved for chat templates and backend metadata when computing prompt budgets |
| `MAX_PARALLEL_WORKERS` | `5` | Max experts per iteration (limited by `AGENT_B_URLS` count) |
| `DISCUSSION_HISTORY_MAX_CHARS` | `6000` | Max chars of discussion history injected per round; older rounds are trimmed from the front |
| `EVAL_MAX_PROMPT_CHARS` | `20000` | Character-based fallback cap on evaluation prompt when token counting is unavailable |

### Docker Compose

`AGENT_B_URLS` in `infra/docker-compose.yml`:

```
http://agent-b:8102/subtask,http://agent-b-2:8103/subtask,http://agent-b-3:8104/subtask,http://agent-b-4:8105/subtask,http://agent-b-5:8106/subtask
```

Expert index maps to Agent B instance: index 0 → agent-b-1, index 1 → agent-b-2, etc.

---

## Known Issues

### Context Explosion in Horizontal Discussion (observed 2026-03-23)

**Root causes (three compounding bugs):**

1. **Unbounded discussion history** — agent responses were appended verbatim to `discussion_history` and re-injected into every subsequent prompt. With 5 agents and verbose responses, the prompt for round 3 reached ~16,000 tokens against a 4,096-token context window, causing vLLM to reject or stall requests.

2. **Fragile consensus detection** — agents were instructed to emit `[CONSENSUS]` but frequently produced markdown variants (`**CONSENSUS:**`, `**CONSENSUS**`). The original exact-string check missed these, so `all_consensus` stayed `False` and all 3 rounds ran unconditionally, maximising history growth.

3. **Duplicate contract injection** — `HORIZONTAL_DISCUSSION_PROMPT` (and `EXECUTION_PROMPT`) already embed the agent contract in the template body. The agent_b server was also prepending `"Contract: ..."` to the LLM prompt, sending it twice per call and wasting context tokens.

**Fixes applied:**

1. **History truncation** (`DISCUSSION_HISTORY_MAX_CHARS=6000`): before each round, `discussion_history` is trimmed from the front when it exceeds the character budget, keeping the most recent rounds and prefixing with `...[earlier rounds truncated]...`.

2. **Broadened consensus detection**: the check now uses `_CONSENSUS_SIGNAL_RE`, a compiled regex that matches `[CONSENSUS]`, `**CONSENSUS**`, `**CONSENSUS:**`, and other common forms (case-insensitive). Rounds now short-circuit as intended.

3. **Single contract injection**: `agent_b/server.py` checks `"Your Contract:" in subtask` before prepending the contract to the role-context prefix. Fully-templated subtasks skip the duplicate; raw short subtasks are unaffected.

**Note on the per-turn output cap:** an earlier version of the mitigation imposed a hard `max_tokens=512` on every discussion turn (`DISCUSSION_MAX_TOKENS`). This was removed because it blocked legitimate long outputs (e.g. code generation tasks). The history truncation and early consensus exit together provide sufficient protection.

**Worst-case context budget after fixes (5 agents, 3 rounds):**

| Component | Tokens (approx.) |
|-----------|-----------------|
| Base prompt + task | ~300 |
| Discussion history cap (6,000 chars ÷ 4) | ~1,500 |
| Agent output (uncapped) | up to remaining budget |
| **Prompt tokens per call** | **≤ ~1,800** |

---

## File Structure

```
agents/
  agent_a/
    orchestrator.py    # 4-stage AgentVerse logic
    server.py          # HTTP handler; /agentverse endpoint
  agent_b/
    server.py          # /subtask, /discuss; role from request payload
  templates/
    agentverse_workflow.json   # Workflow config, example tasks

ui/
  agentverse/
    index.html         # AgentVerse UI
  chat/
    index.html         # Classic chat UI (unchanged)
```

---

## Adaptations for the Testbed

1. **Agent pool**: We use a fixed set of 5 Agent B containers; roles are assigned at runtime via prompts. The paper sometimes assumes dynamically spawned agents.

2. **Single LLM**: All agents (orchestrator and experts) share one LLM backend. The paper mentions multiple model options.

3. **HTTP-based coordination**: Orchestrator and experts communicate over HTTP. No shared memory or message bus.

4. **Tracing**: OpenTelemetry spans are created for orchestrator and Agent B calls; Jaeger can trace the full workflow.

5. **Telemetry**: `TelemetryLogger` records events (recruitment, decision rounds, execution, evaluation) to log files.

6. **Distributed deployment**: With `docker-compose.distributed.yml`, Agent A, Agent B, and LLM can run on different networks to study traffic patterns.

---

## Redeployment

To pick up changes to the AgentVerse implementation:

```bash
cd infra
docker compose build agent-a chat-ui
docker compose up -d agent-a chat-ui
```

Only `agent-a` (orchestrator) and `chat-ui` (AgentVerse page) need to be rebuilt; `agent-b`, `llm-backend`, and others stay as-is.

---

## Example Tasks

The UI and `agentverse_workflow.json` include example tasks:

- **Math problem** – Amusement park ride optimization (vertical structure)
- **Research task** – Renewable energy adoption (horizontal)
- **Consulting** – AI customer service recommendations (horizontal)
- **Coding** – Task management system (vertical)

These are useful for validating the workflow and comparing horizontal vs vertical behavior.
