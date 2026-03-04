## Interarrival Time Metrics for the LLM Backend

This document explains how **request interarrival time** is defined in the testbed, how it is visualised in Grafana, and how to interpret it together with existing LLM latency and queueing metrics.

---

## 1. What we mean by interarrival time

At the LLM backend, we see a stream of HTTP requests from:

- Agent A (orchestrator)
- Agent B workers (1–N)
- AgentVerse multi‑stage workflows
- Optional non‑agentic baselines

Each completed HTTP POST to `/chat`, `/completion`, or `/generate` increments the Prometheus counter:

- `llm_requests_total{status="success" | "error"}`

Conceptually, if requests arrive at times \( T_1, T_2, T_3, \dots \), then the **interarrival time** \( A_n \) is:

\[
A_n = T_n - T_{n-1}
\]

Smaller \( A_n \) means requests are coming closer together (higher instantaneous load); larger \( A_n \) means requests are more spaced out (lower instantaneous load).

Because this testbed focuses on *agentic* workloads with structured but dynamic workflows (recruitment → decision → execution → evaluation), the interarrival pattern encodes:

- Bursty phases (e.g. planning bursts, tool‑use bursts, summarisation phases)
- Differences between **agentic** and **non‑agentic** scenarios
- How orchestration choices translate into actual LLM traffic patterns

---

## 2. Closed-loop arrivals: what interarrival really measures

The agent workloads in this testbed are not a classic open-loop Poisson process where arrivals are independent of system state. Instead, they form a **closed-loop** system:

- An agent issues an LLM request.
- The LLM backend processes it and returns a response.
- The agent performs local work (reasoning, routing, tool calls).
- Only then does it decide whether and when to send the **next** LLM request.

For a single logical agent, the time between two successive LLM calls can be decomposed as:

\[
t_{\text{interarrival}} = t_{\text{LLM\_latency}} + t_{\text{agent\_processing}} + t_{\text{tool\_calls}}
\]

where:

- \( t_{\text{LLM\_latency}} \) is what the LLM backend already exposes as `llm_request_latency_seconds`.
- \( t_{\text{agent\_processing}} + t_{\text{tool\_calls}} \) is the **agent-side gap** between receiving a response and issuing the next LLM request.

This means the interarrival time we observe at the backend is **partly determined by the backend itself** (via LLM latency and batching config) and partly by agent behaviour. It is therefore:

- A valid **traffic characterisation** metric for a *specific system configuration* (LLM model, batching, number of agents, scenarios).
- Not an intrinsic, system-independent property of the abstract “agentic workflow”.

For the purposes of this testbed—comparing **agentic vs non-agentic** traffic under controlled server settings—that is acceptable and even desirable. It makes the **feedback loop** between LLM capacity and agent traffic explicitly visible.

---

## 3. How we derive interarrival time from existing metrics

We do **not** need a separate Prometheus metric to get a useful interarrival signal. From queueing theory:

- Let \( \lambda(t) \) be the instantaneous **arrival rate** (requests per second).
- The **mean interarrival time** over a small window is approximately:

\[
\mathbb{E}[A](t) \approx \frac{1}{\lambda(t)}
\]

We already export `llm_requests_total` from the LLM backend (`llm/serve_llm.py`). Grafana’s PromQL uses this to estimate arrival rate with:

```promql
sum(rate(llm_requests_total[30s]))
```

From there, we define **average interarrival time**:

```promql
1 / sum(rate(llm_requests_total[30s]))
```

- Units: **seconds**
- This is the quantity shown in the dedicated “LLM Interarrival Time (avg)” panel.
- The 30‑second window can be tuned, but 30s is a good compromise between noise and responsiveness.

If we ever need the **full distribution** of interarrival times (histogram), we could add a dedicated histogram in `llm/serve_llm.py`:

- New metric: `llm_interarrival_seconds_bucket`
- Implementation: track a global `last_arrival_ts`; on each accepted request, compute `now - last_arrival_ts` and `observe()` it into the histogram.

For now, the **derived mean** via PromQL is sufficient for high‑level analysis and comparisons between scenarios.

---

## 4. Where this appears in the Grafana dashboard

The main dashboard (`infra/monitoring/grafana/provisioning/dashboards/agentic-traffic.json`) is provisioned as **“Agentic Traffic Testbed”** and documented in `docs/monitoring.md`.

The new panel is added to the **AI Performance (LLM)** row.

- **Panel title**: `LLM Interarrival Time (avg)`
- **Row**: `AI Performance (LLM)`
- **Query**:

  ```promql
  1 / sum(rate(llm_requests_total[30s]))
  ```

- **Unit**: seconds (`s`)
- **Legend**: `avg interarrival (s)`

This panel lives alongside:

- `LLM End-to-end Latency (p50/p95)` – from `llm_request_latency_seconds_bucket`
- `LLM Time-to-First-Token (TTFT p50/p95)` – from `llm_queue_wait_seconds_bucket`
- `Prompt Tokens / s` – from `llm_prompt_tokens_total`
- `Completion Tokens / s` – from `llm_completion_tokens_total`
- `In-flight LLM Requests` – `llm_inflight_requests`

Together, these give a compact view of:

- How **often** requests arrive (interarrival)
- How **long** they take (latency)
- How much **time they spend queued** before the first token (TTFT)
- How many are **in flight** at once (concurrency)

---

## 5. Interpreting interarrival alongside request duration

This testbed usually runs **closed‑loop** workloads:

- Agents often wait for LLM responses before issuing follow‑up calls.
- The orchestration logic (number of agents, parallel vs sequential stages, tool usage) shapes when new LLM calls are generated.

Because of this, it is important to interpret **interarrival time** together with:

- **End‑to‑end request latency**: `llm_request_latency_seconds_bucket`
- **Queue wait / TTFT**: `llm_queue_wait_seconds_bucket`
- **In-flight requests**: `llm_inflight_requests`

Some common patterns:

- **Short interarrival + short latency + low queue wait**
  - High request rate, but the backend is keeping up.
  - vLLM batching is effective; little queueing.
  - Often corresponds to efficient, parallel agent phases (e.g. execution step with many short subtasks).

- **Short interarrival + long latency + high queue wait**
  - Backend is entering a **queueing regime**; requests arrive faster than they can be served.
  - `llm_inflight_requests` typically rises in the same timeframe.
  - Useful region to compare:
    - Agentic vs non‑agentic baseline
    - Different orchestration patterns (e.g. parallel vs sequential AgentVerse stages).

- **Long interarrival + long latency**
  - Arrival rate is low, but individual calls are expensive.
  - Latency is dominated by model behaviour (e.g. very long generations or heavy tool‑call chains), not by load.
  - Suggests optimisation targets at the **workflow or prompt level** rather than at the infrastructure level.

- **Long interarrival + short latency**
  - The system is mostly idle from the LLM’s perspective; any burstiness is driven entirely by agent logic rather than resource saturation.

From a queueing‑theory viewpoint, you can think in terms of:

- \( \lambda(t) \approx \sum \text{rate}(\text{llm\_requests\_total}[30s]) \) – arrival rate
- \( W(t) \) – observed end‑to‑end request latency
- \( L(t) \) – in‑flight requests (`llm_inflight_requests`)

Little’s Law suggests \( L \approx \lambda W \) (in steady state), so watching all three together lets you see when the LLM backend is being pushed into high‑load or overloaded regimes, and how agentic orchestration decisions translate into those regimes.

---

## 6. Relationship to TCP‑level metrics

Separately, the **TCP metrics collector** (`scripts/monitoring/tcp_metrics_collector.py`) provides:

- `tcp_packets_total{src_service,dst_service}`
- `tcp_bytes_total{src_service,dst_service}`
- `tcp_flow_duration_seconds_bucket{src_service,dst_service,le}`
- `tcp_rtt_handshake_seconds_bucket{src_service,dst_service,le}`

Those are visualised in the **Service-level Network (TCP)** row and are useful to reason about:

- Connection‑level burstiness and flow lifetimes
- SYN/SYN‑ACK RTTs between specific service pairs (e.g. Agent A → LLM backend)

However, TCP connection events are **not always 1:1 with LLM RPCs** (due to connection reuse/keep‑alive), so:

- Use **LLM interarrival time** (derived from `llm_requests_total`) as the primary signal for **application‑level arrivals**.
- Use **TCP‑level metrics** as a complementary view for **network‑layer behaviour** (RTT distributions, flow durations, retransmissions) associated with those agentic workloads.

Taken together, these metrics help characterise how **semantic workflows** (AgentID, TaskID, ToolCallID) translate into both:

- Application‑level load on the LLM backend (interarrival, latency, TTFT, tokens)
- Network‑level behaviour between agents, tools, and the LLM (TCP RTTs, flow durations, bytes/packets by service pair)

---

## 7. What is missing today and where it would live

The external analysis highlights several **additional metrics** that would make the interarrival story more interpretable. This section summarises which ones exist today, which are missing, and where they would belong in the dashboard.

### 7.1 Agent-side gap and interarrival decomposition

**Goal:** separate:

- \( t_{\text{LLM\_latency}} \) – LLM backend time (already measured), from
- \( t_{\text{agent\_gap}} = t_{\text{agent\_processing}} + t_{\text{tool\_calls}} \) – agent-side time between response and next request.

**Currently available:**

- `llm_request_latency_seconds_bucket` (LLM latency)
- `llm_queue_wait_seconds_bucket` (queue/TTFT)
- Derived average interarrival time (this document)

**Missing metrics:**

- An explicit **agent-side gap histogram**, e.g.:
  - `agent_llm_gap_seconds_bucket{agent_id,scenario,...}`
  - Defined per agent as: time from “LLM response received” → “next LLM request sent”.

**Candidate dashboard location (once implemented):**

- New row: **“Agent Workflow Metrics”** or a new panel row under **AI Performance (LLM)**:
  - Panel: `Agent-side Gap (p50/p95)` – `histogram_quantile` over `agent_llm_gap_seconds_bucket`.
  - This would sit next to LLM latency and TTFT, completing the decomposition:
    - Queue wait (server-side scheduling)
    - LLM compute time
    - Agent-side gap

### 7.2 Task-level workflow metrics

The external notes recommend several task-centric metrics:

- **LLM calls per completed task**
- **End-to-end task latency**
- **Burst size** (concurrent LLM calls within a short window)
- **Request fan-out ratio** (Agent A vs Agent B calls, or more generally orchestrator vs worker)

**Current coverage:**

- We can approximate **burst size** indirectly from:
  - `rate(llm_requests_total[window])` (spikes) and
  - `llm_inflight_requests` (number of concurrent in-flight calls).
- We can distinguish some fan-out patterns manually via:
  - TCP metrics with `src_service`/`dst_service` labels (e.g. Agent A vs Agent B → LLM).

**Missing metrics:**

To make these first-class and scenario-comparable, we would need new application-level Prometheus metrics from the agent/http layers, for example:

- `agent_task_total{scenario,status}` – number of completed tasks.
- `agent_task_latency_seconds_bucket{scenario}` – end-to-end task latency.
- `agent_llm_calls_total{scenario,role}` – number of LLM calls made per scenario and agent role (or per service).
- Optional derived metrics in PromQL:
  - “LLM calls per task” = `sum(rate(agent_llm_calls_total[window])) / sum(rate(agent_task_total[window]))`.
  - “Fan-out ratio (Agent A vs Agent B)” = `rate(agent_llm_calls_total{role="orchestrator"}[window]) / rate(agent_llm_calls_total{role!="orchestrator"}[window])`.

**Candidate dashboard layout (once metrics exist):**

- New row: **“Agent Workflow Metrics”**, with panels such as:
  - `Tasks Completed / s` – from `agent_task_total`.
  - `Task End-to-end Latency (p50/p95)` – from `agent_task_latency_seconds_bucket`.
  - `LLM Calls per Task` – derived ratio panel.
  - `LLM Calls by Role` – stacked series by `role` label from `agent_llm_calls_total`.

### 7.3 Using the existing dashboard with closed-loop awareness

Even without the additional metrics wired up yet, the current dashboard already supports the core closed-loop interpretation if you keep the following in mind:

- **Interarrival time** in the `LLM Interarrival Time (avg)` panel is *not* purely exogenous; it reflects:
  - LLM latency (from `llm_request_latency_seconds_bucket`)
  - Agent orchestration and tool calls (not yet separately measured)
  - vLLM batching and capacity settings (`LLM_MAX_NUM_SEQS`, `LLM_MAX_NUM_BATCHED_TOKENS`, etc.)
- When comparing **agentic scenarios** (`agentic_simple`, `agentic_multi_hop`, `agentic_parallel`, AgentVerse) or **agentic vs non-agentic baseline**, you should:
  - Hold LLM config constant.
  - Read interarrival curves alongside the latency/TTFT and inflight panels.

Future work can add the agent-side gap and task-level metrics described above, making the decomposition explicit and turning those interpretations into directly observable time series.


