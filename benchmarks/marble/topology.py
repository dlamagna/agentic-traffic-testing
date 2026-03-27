"""
MARBLE topology coordinators.

Implements the four MARBLE coordination modes — **star**, **chain**, **tree**,
and **graph** — over the testbed's HTTP-based agents (Agent A + Agent B
containers).

Each coordinator receives a :class:`~benchmarks.marble.loader.MarbleTask`,
maps its MARBLE agents to Docker endpoints, runs the multi-iteration
coordination loop, and returns a structured result dict.

Mapping convention
------------------
- The **first** MARBLE agent (agent1 / root / hub) is mapped to **Agent A**
  which acts as the orchestrator and also has its own LLM identity.
- Remaining MARBLE agents are mapped round-robin to the available **Agent B**
  endpoints (up to 5, ports 8102–8106).
- Each Agent B receives the MARBLE agent's *profile* as its ``agent_b_role``
  and the task-specific instructions as ``agent_b_contract``.

All coordinators produce HTTP traffic between Agent A, Agent B, and the LLM
backend — the core requirement for network telemetry collection.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from benchmarks.marble.loader import MarbleAgent, MarbleTask

# Type alias for the optional streaming progress callback.
# Signature: callback(event_type: str, data: dict) -> None
ProgressCallback = Optional["Callable[[str, Dict[str, Any]], None]"]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_AGENT_A_URL = os.environ.get("AGENT_A_URL", "http://localhost:8101")
_AGENT_B_URLS_RAW = os.environ.get(
    "AGENT_B_URLS",
    "http://localhost:8102/subtask,"
    "http://localhost:8103/subtask,"
    "http://localhost:8104/subtask,"
    "http://localhost:8105/subtask,"
    "http://localhost:8106/subtask",
)
_AGENT_B_URLS: List[str] = [u.strip() for u in _AGENT_B_URLS_RAW.split(",") if u.strip()]
_TIMEOUT = float(os.environ.get("MARBLE_TIMEOUT_SECONDS", "300"))
_MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL_WORKERS", "5"))
_MARBLE_CALL_LOG = os.path.join(
    os.environ.get("MARBLE_METRICS_LOG_DIR", "logs"),
    "marble_llm_calls.jsonl",
)

# Set by run_topology() before each coordinator runs so that _call_agent_a /
# _call_agent_b can tag every logged record with the active topology/domain
# without threading extra parameters through all intermediate functions.
_ctx_coordinate_mode: str = ""
_ctx_domain: str = ""
_progress_cb: ProgressCallback = None
_trace_headers: Dict[str, str] = {}


def _emit(event: str, data: Dict[str, Any]) -> None:
    """Fire a progress event to the streaming callback (if set)."""
    cb = _progress_cb
    if cb is not None:
        try:
            cb(event, data)
        except Exception:
            pass


def _log_marble_call(
    *,
    call_id: str,
    task_id: Optional[str],
    agent_id: str,
    call_type: str,
    timestamp_start: str,
    timestamp_end: str,
    http_status: int,
    llm_meta: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    """Append one per-call record to logs/marble_llm_calls.jsonl."""
    meta = llm_meta or {}
    record: Dict[str, Any] = {
        "call_id": call_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "call_type": call_type,
        "coordinate_mode": _ctx_coordinate_mode,
        "domain": _ctx_domain,
        "prompt_tokens": meta.get("prompt_tokens"),
        "completion_tokens": meta.get("completion_tokens"),
        "total_tokens": meta.get("total_tokens"),
        "latency_ms": meta.get("latency_ms"),
        "llm_latency_ms": meta.get("llm_latency_ms"),
        "total_llm_calls": meta.get("total_llm_calls"),
        "total_agent_hops": meta.get("total_agent_hops"),
        "cost_estimate_usd": meta.get("cost_estimate_usd"),
        "queue_wait_s": meta.get("queue_wait_s"),
        "timestamp_start": timestamp_start,
        "timestamp_end": timestamp_end,
        "http_status": http_status,
        "error": error,
        "benchmark_source": "marble",
    }
    line = json.dumps(record, sort_keys=True)
    try:
        os.makedirs(os.path.dirname(_MARBLE_CALL_LOG) or ".", exist_ok=True)
        with open(_MARBLE_CALL_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        print(f"[marble-iat-logger] {exc}", file=sys.stderr)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _httpx():
    """Lazy import httpx."""
    try:
        import httpx
        return httpx
    except ImportError as exc:
        raise RuntimeError("httpx is required: pip install httpx") from exc


# ---------------------------------------------------------------------------
# Agent ↔ Endpoint mapping
# ---------------------------------------------------------------------------

@dataclass
class AgentMapping:
    """Maps a MARBLE agent to a physical endpoint."""
    marble_agent: MarbleAgent
    endpoint_url: str
    is_agent_a: bool = False


def build_agent_map(task: MarbleTask) -> Dict[str, AgentMapping]:
    """
    Map MARBLE agents to Docker endpoints.

    The first agent is mapped to Agent A (orchestrator); the rest are
    distributed across Agent B endpoints.
    """
    mapping: Dict[str, AgentMapping] = {}
    agents = task.agents

    if not agents:
        return mapping

    mapping[agents[0].agent_id] = AgentMapping(
        marble_agent=agents[0],
        endpoint_url=_AGENT_A_URL,
        is_agent_a=True,
    )

    for i, agent in enumerate(agents[1:]):
        b_url = _AGENT_B_URLS[i % len(_AGENT_B_URLS)]
        mapping[agent.agent_id] = AgentMapping(
            marble_agent=agent,
            endpoint_url=b_url,
            is_agent_a=False,
        )

    return mapping


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _call_agent_a(
    task_text: str,
    scenario: str = "marble_star",
    task_id: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """POST to Agent A's /task endpoint."""
    httpx = _httpx()
    payload: Dict[str, Any] = {
        "task": task_text,
        "scenario": scenario,
        "benchmark_source": "marble",
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    headers: Dict[str, str] = dict(_trace_headers)
    if task_id:
        headers["X-Task-ID"] = task_id
    ts_start = _utc_iso()
    resp = httpx.post(f"{_AGENT_A_URL}/task", json=payload, headers=headers, timeout=_TIMEOUT)
    ts_end = _utc_iso()
    resp.raise_for_status()
    body = resp.json()
    # Agent A returns tokens at the top level (not under a "meta" key)
    _log_marble_call(
        call_id=str(uuid.uuid4()),
        task_id=task_id,
        agent_id="agent_a",
        call_type="marble_a_call",
        timestamp_start=ts_start,
        timestamp_end=ts_end,
        http_status=resp.status_code,
        llm_meta={
            "prompt_tokens":      body.get("total_prompt_tokens"),
            "completion_tokens":  body.get("total_completion_tokens"),
            "total_tokens":       body.get("total_tokens"),
            "latency_ms":         body.get("total_latency_ms"),
            "llm_latency_ms":     body.get("llm_latency_ms"),
            "total_llm_calls":    body.get("total_llm_calls"),
            "total_agent_hops":   body.get("total_agent_hops"),
            "cost_estimate_usd":  body.get("cost_estimate_usd"),
        },
    )
    return body


def _call_agent_b(
    endpoint_url: str,
    subtask: str,
    role: str,
    contract: str = "",
    task_id: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """POST to an Agent B /subtask endpoint."""
    httpx = _httpx()
    payload: Dict[str, Any] = {
        "subtask": subtask,
        "scenario": "marble",
        "agent_b_role": role,
    }
    if contract:
        payload["agent_b_contract"] = contract
    if max_tokens:
        payload["max_tokens"] = max_tokens
    headers: Dict[str, str] = dict(_trace_headers)
    if task_id:
        headers["X-Task-ID"] = task_id
    ts_start = _utc_iso()
    resp = httpx.post(endpoint_url, json=payload, headers=headers, timeout=_TIMEOUT)
    ts_end = _utc_iso()
    resp.raise_for_status()
    body = resp.json()
    # Agent B returns per-call LLM metadata under the "llm_meta" key
    _log_marble_call(
        call_id=str(uuid.uuid4()),
        task_id=task_id,
        agent_id=f"agent_b_{endpoint_url.split('//')[1].split('/')[0]}",
        call_type="marble_b_call",
        timestamp_start=ts_start,
        timestamp_end=ts_end,
        http_status=resp.status_code,
        llm_meta=body.get("llm_meta", {}),
    )
    return body


def _call_agent_a_llm(
    prompt: str,
    task_id: Optional[str] = None,
    scenario: str = "marble",
) -> Dict[str, Any]:
    """
    Use Agent A's /task endpoint as a thin LLM proxy.

    This lets Agent A make planning/synthesis decisions while generating
    the same network traffic pattern as real orchestration.
    """
    return _call_agent_a(prompt, scenario=scenario, task_id=task_id)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class TopologyResult:
    """Aggregate result of running one MARBLE task through a topology."""

    task_id: str
    marble_task_id: int
    domain: str
    coordinate_mode: str
    iterations: List[Dict[str, Any]] = field(default_factory=list)
    final_output: str = ""
    agent_outputs: Dict[str, str] = field(default_factory=dict)
    communications: List[Dict[str, Any]] = field(default_factory=list)
    total_agent_calls: int = 0
    total_llm_calls: int = 0
    total_tokens: int = 0
    duration_s: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "marble_task_id": self.marble_task_id,
            "domain": self.domain,
            "coordinate_mode": self.coordinate_mode,
            "iterations": self.iterations,
            "final_output": self.final_output,
            "agent_outputs": self.agent_outputs,
            "communications": self.communications,
            "total_agent_calls": self.total_agent_calls,
            "total_llm_calls": self.total_llm_calls,
            "total_tokens": self.total_tokens,
            "duration_s": self.duration_s,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Prompt templates for coordination
# ---------------------------------------------------------------------------

_PLAN_TASKS_PROMPT = """\
You are a team coordinator. Given the following task and a team of agents, \
assign a specific sub-task to each agent based on their profile.

TASK:
{task}

AGENTS:
{agent_profiles}

Respond in JSON with agent_id as keys and sub-task descriptions as values.
Example: {{"agent2": "Research ...", "agent3": "Implement ..."}}

Only assign tasks to these agent IDs: {agent_ids}
"""

_SYNTHESIZE_PROMPT = """\
You are synthesizing results from a team of agents who worked on a task.

ORIGINAL TASK:
{task}

AGENT RESULTS:
{results}

Provide a comprehensive final answer that integrates all agent contributions."""

_CHAIN_HANDOFF_PROMPT = """\
You are deciding which agent should work next in a sequential workflow.

TASK: {task}

CURRENT AGENT ({current_agent}) PRODUCED:
{current_result}

AVAILABLE NEXT AGENTS:
{available_agents}

PREVIOUS CHAIN: {chain_history}

Respond with ONLY the agent_id of the next agent, and a brief instruction \
for what they should do. Format: NEXT_AGENT: <agent_id>
INSTRUCTION: <what to do>

If the task is complete, respond: NEXT_AGENT: DONE"""

_CONTINUE_PROMPT = """\
You are evaluating whether a multi-agent task is complete.

TASK: {task}

RESULTS SO FAR:
{results}

Should the team continue working? Respond with ONLY "CONTINUE" or "DONE".
If the results adequately address the task, say DONE. Otherwise say CONTINUE."""

_COMMUNICATION_PROMPT = """\
You are {agent_id} ({profile}).

You are communicating with {target_id} ({target_profile}) about a shared task.

TASK: {task}

{target_id} said: {incoming_message}

Respond to advance the collaborative work. Be concise and constructive."""


# ---------------------------------------------------------------------------
# Star Coordinator
# ---------------------------------------------------------------------------

def run_star(
    task: MarbleTask,
    max_iterations: int = 3,
) -> TopologyResult:
    """
    Star (centralized) coordination.

    Agent A acts as the central planner: assigns tasks to leaf agents (Agent B
    instances), collects results, synthesizes, and decides whether to iterate.

    Mirrors MARBLE's ``Engine.star_coordinate()``.
    """
    task_id = str(uuid.uuid4())
    agent_map = build_agent_map(task)
    start = time.monotonic()
    result = TopologyResult(
        task_id=task_id,
        marble_task_id=task.task_id,
        domain=task.domain,
        coordinate_mode="star",
    )

    leaf_agents = {aid: m for aid, m in agent_map.items() if not m.is_agent_a}
    if not leaf_agents:
        result.error = "No leaf agents to coordinate"
        result.duration_s = time.monotonic() - start
        return result

    try:
        for iteration in range(max_iterations):
            _emit("iteration_start", {"iteration": iteration + 1, "max_iterations": max_iterations})
            iter_data: Dict[str, Any] = {"iteration": iteration + 1, "assignments": {}, "results": {}}

            # --- Planning: Agent A assigns tasks ---
            _emit("phase_start", {"phase": "plan", "iteration": iteration + 1})
            agent_profiles_str = "\n".join(
                f"- {aid}: {m.marble_agent.profile[:300]}"
                for aid, m in leaf_agents.items()
            )
            plan_prompt = _PLAN_TASKS_PROMPT.format(
                task=task.task_content[:2000],
                agent_profiles=agent_profiles_str,
                agent_ids=", ".join(leaf_agents.keys()),
            )
            _cid = str(uuid.uuid4())[:8]
            _emit("agent_call_start", {"call_id": _cid, "from_id": "coordinator", "to_id": "agent1", "call_type": "plan",
                                       "prompt": plan_prompt[:3000]})
            plan_resp = _call_agent_a_llm(plan_prompt, task_id=task_id, scenario="marble_star_plan")
            result.total_llm_calls += 1
            result.total_agent_calls += 1
            _accumulate_tokens(result, plan_resp)
            _emit("agent_call_complete", {"call_id": _cid, "from_id": "coordinator", "to_id": "agent1", "call_type": "plan",
                                          "tokens": _resp_tokens(plan_resp), "output_preview": plan_resp.get("output", "")[:200],
                                          "response": plan_resp.get("output", "")[:3000]})

            assignments = _parse_assignments(
                plan_resp.get("output", ""),
                list(leaf_agents.keys()),
                task.task_content,
            )
            iter_data["assignments"] = assignments
            _emit("phase_complete", {"phase": "plan", "assignments": {k: v[:120] for k, v in assignments.items()}})

            # --- Execution: Fan out to Agent B instances ---
            _emit("phase_start", {"phase": "execute", "iteration": iteration + 1, "agent_count": len(assignments)})
            agent_results: Dict[str, str] = {}
            _exec_cids: Dict[str, str] = {}
            with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL, len(leaf_agents))) as pool:
                futures = {}
                for aid, subtask_text in assignments.items():
                    if aid not in leaf_agents:
                        continue
                    m = leaf_agents[aid]
                    _cid = str(uuid.uuid4())[:8]
                    _exec_cids[aid] = _cid
                    _emit("agent_call_start", {"call_id": _cid, "from_id": "agent1", "to_id": aid, "call_type": "execute",
                                               "prompt": subtask_text[:3000]})
                    fut = pool.submit(
                        _call_agent_b,
                        endpoint_url=m.endpoint_url,
                        subtask=subtask_text,
                        role=m.marble_agent.profile[:500],
                        task_id=task_id,
                    )
                    futures[fut] = aid

                for fut in as_completed(futures):
                    aid = futures[fut]
                    try:
                        resp = fut.result()
                        agent_results[aid] = resp.get("output", "")
                        result.total_llm_calls += 1
                        result.total_agent_calls += 1
                        _accumulate_tokens(result, resp)
                        _emit("agent_call_complete", {"call_id": _exec_cids.get(aid, ""), "from_id": aid, "to_id": "agent1", "call_type": "execute",
                                                      "agent_id": aid, "tokens": _resp_tokens(resp),
                                                      "output_preview": resp.get("output", "")[:200],
                                                      "response": resp.get("output", "")[:3000]})
                    except Exception as exc:
                        agent_results[aid] = f"[ERROR: {exc}]"
                        _emit("agent_call_error", {"call_id": _exec_cids.get(aid, ""), "agent_id": aid, "call_type": "execute", "error": str(exc)})

            iter_data["results"] = agent_results
            result.agent_outputs.update(agent_results)
            _emit("phase_complete", {"phase": "execute", "completed": len(agent_results)})

            # --- Synthesis: Agent A combines results ---
            _emit("phase_start", {"phase": "synthesize", "iteration": iteration + 1})
            results_str = "\n\n".join(
                f"[{aid}]: {text[:500]}" for aid, text in agent_results.items()
            )
            synth_prompt = _SYNTHESIZE_PROMPT.format(
                task=task.task_content[:2000],
                results=results_str,
            )
            _cid = str(uuid.uuid4())[:8]
            _emit("agent_call_start", {"call_id": _cid, "from_id": "coordinator", "to_id": "agent1", "call_type": "synthesize",
                                       "prompt": synth_prompt[:3000]})
            synth_resp = _call_agent_a_llm(synth_prompt, task_id=task_id, scenario="marble_star_synth")
            result.total_llm_calls += 1
            result.total_agent_calls += 1
            _accumulate_tokens(result, synth_resp)
            result.final_output = synth_resp.get("output", "")
            _emit("agent_call_complete", {"call_id": _cid, "from_id": "agent1", "to_id": "coordinator", "call_type": "synthesize",
                                          "tokens": _resp_tokens(synth_resp), "output_preview": result.final_output[:200],
                                          "response": result.final_output[:3000]})
            iter_data["summary"] = result.final_output[:1000]
            result.iterations.append(iter_data)
            _emit("phase_complete", {"phase": "synthesize"})

            # --- Decide: continue or stop ---
            if iteration < max_iterations - 1:
                _emit("phase_start", {"phase": "continue_check", "iteration": iteration + 1})
                should_stop = _should_stop(task.task_content, result.final_output, task_id, result)
                _emit("phase_complete", {"phase": "continue_check", "should_stop": should_stop})
                if should_stop:
                    break

            _emit("iteration_complete", {"iteration": iteration + 1, "total_calls": result.total_agent_calls,
                                         "total_tokens": result.total_tokens})

    except Exception as exc:
        result.error = str(exc)
        _emit("topology_error", {"error": str(exc)})

    result.duration_s = time.monotonic() - start
    return result


# ---------------------------------------------------------------------------
# Chain Coordinator
# ---------------------------------------------------------------------------

def run_chain(
    task: MarbleTask,
    max_iterations: int = 3,
) -> TopologyResult:
    """
    Chain (sequential) coordination.

    Agents process the task one after another. Agent A mediates the handoff,
    deciding which agent acts next based on the current result and the
    relationship graph.

    Mirrors MARBLE's ``Engine.chain_coordinate()``.
    """
    task_id = str(uuid.uuid4())
    agent_map = build_agent_map(task)
    start = time.monotonic()
    result = TopologyResult(
        task_id=task_id,
        marble_task_id=task.task_id,
        domain=task.domain,
        coordinate_mode="chain",
    )

    all_agents = list(agent_map.values())
    if not all_agents:
        result.error = "No agents in task"
        result.duration_s = time.monotonic() - start
        return result

    max_chain_steps = max_iterations * len(all_agents)

    try:
        current_agent_id = all_agents[0].marble_agent.agent_id
        current_task = task.task_content
        chain_history: List[str] = []

        for step in range(max_chain_steps):
            _emit("iteration_start", {"iteration": step + 1, "max_iterations": max_chain_steps, "chain_step": True})
            iter_data: Dict[str, Any] = {
                "chain_step": step + 1,
                "agent": current_agent_id,
            }

            m = agent_map.get(current_agent_id)
            if m is None:
                break

            # --- Act: current agent processes the task ---
            _emit("phase_start", {"phase": "act", "iteration": step + 1})
            _cid = str(uuid.uuid4())[:8]
            if m.is_agent_a:
                _act_prompt = f"You are {m.marble_agent.agent_id}: {m.marble_agent.profile[:300]}\n\nTask: {current_task[:2000]}"
            else:
                _act_prompt = current_task[:2000]
            _emit("agent_call_start", {"call_id": _cid, "from_id": "coordinator", "to_id": current_agent_id, "call_type": "act",
                                       "prompt": _act_prompt[:3000]})
            if m.is_agent_a:
                resp = _call_agent_a_llm(
                    _act_prompt,
                    task_id=task_id,
                    scenario="marble_chain_act",
                )
            else:
                resp = _call_agent_b(
                    endpoint_url=m.endpoint_url,
                    subtask=_act_prompt,
                    role=m.marble_agent.profile[:500],
                    task_id=task_id,
                )

            current_result = resp.get("output", "")
            result.total_llm_calls += 1
            result.total_agent_calls += 1
            _accumulate_tokens(result, resp)
            result.agent_outputs[current_agent_id] = current_result
            chain_history.append(f"{current_agent_id}: {current_result[:200]}")
            iter_data["result"] = current_result[:500]
            _emit("agent_call_complete", {"call_id": _cid, "from_id": current_agent_id, "to_id": "coordinator", "call_type": "act",
                                          "agent_id": current_agent_id, "tokens": _resp_tokens(resp),
                                          "output_preview": current_result[:200],
                                          "response": current_result[:3000]})
            _emit("phase_complete", {"phase": "act", "agent_id": current_agent_id})

            # --- Handoff: Agent A decides the next agent ---
            _emit("phase_start", {"phase": "handoff", "iteration": step + 1})
            non_current = [
                m2 for aid, m2 in agent_map.items()
                if aid != current_agent_id
            ]
            if not non_current:
                result.iterations.append(iter_data)
                break

            available_str = "\n".join(
                f"- {m2.marble_agent.agent_id}: {m2.marble_agent.profile[:200]}"
                for m2 in non_current
            )
            handoff_prompt = _CHAIN_HANDOFF_PROMPT.format(
                task=task.task_content[:1000],
                current_agent=current_agent_id,
                current_result=current_result[:500],
                available_agents=available_str,
                chain_history=" → ".join(h[:80] for h in chain_history[-5:]),
            )
            _cid = str(uuid.uuid4())[:8]
            _emit("agent_call_start", {"call_id": _cid, "from_id": "coordinator", "to_id": "agent1", "call_type": "handoff",
                                       "prompt": handoff_prompt[:3000]})
            handoff_resp = _call_agent_a_llm(
                handoff_prompt, task_id=task_id, scenario="marble_chain_handoff",
            )
            result.total_llm_calls += 1
            result.total_agent_calls += 1
            _accumulate_tokens(result, handoff_resp)

            handoff_text = handoff_resp.get("output", "")
            next_agent_id, instruction = _parse_handoff(handoff_text, agent_map)
            iter_data["next_agent"] = next_agent_id
            result.iterations.append(iter_data)
            _emit("agent_call_complete", {"call_id": _cid, "from_id": "agent1", "to_id": "coordinator", "call_type": "handoff",
                                          "next_agent": next_agent_id,
                                          "response": handoff_text[:3000]})
            _emit("phase_complete", {"phase": "handoff", "next_agent": next_agent_id})

            if next_agent_id == "DONE" or next_agent_id is None:
                result.final_output = current_result
                _emit("iteration_complete", {"iteration": step + 1, "done": True})
                break

            current_agent_id = next_agent_id
            current_task = (
                f"Previous agent's result:\n{current_result[:1000]}\n\n"
                f"Instruction: {instruction}\n\n"
                f"Original task: {task.task_content[:1000]}"
            )
            _emit("iteration_complete", {"iteration": step + 1, "total_calls": result.total_agent_calls})
        else:
            result.final_output = current_result  # type: ignore[possibly-undefined]

        if not result.final_output and result.agent_outputs:
            _emit("phase_start", {"phase": "synthesize"})
            results_str = "\n\n".join(
                f"[{aid}]: {txt[:500]}" for aid, txt in result.agent_outputs.items()
            )
            _cid = str(uuid.uuid4())[:8]
            _synth_prompt = _SYNTHESIZE_PROMPT.format(
                task=task.task_content[:2000], results=results_str,
            )
            _emit("agent_call_start", {"call_id": _cid, "from_id": "coordinator", "to_id": "agent1",
                                       "call_type": "synthesize", "prompt": _synth_prompt[:3000]})
            synth_resp = _call_agent_a_llm(
                _synth_prompt,
                task_id=task_id,
                scenario="marble_chain_synth",
            )
            result.total_llm_calls += 1
            result.total_agent_calls += 1
            _accumulate_tokens(result, synth_resp)
            result.final_output = synth_resp.get("output", "")
            _emit("agent_call_complete", {"call_id": _cid, "from_id": "agent1", "to_id": "coordinator",
                                          "call_type": "synthesize", "tokens": _resp_tokens(synth_resp),
                                          "output_preview": result.final_output[:200],
                                          "response": result.final_output[:3000]})
            _emit("phase_complete", {"phase": "synthesize"})

    except Exception as exc:
        result.error = str(exc)
        _emit("topology_error", {"error": str(exc)})

    result.duration_s = time.monotonic() - start
    return result


# ---------------------------------------------------------------------------
# Tree Coordinator
# ---------------------------------------------------------------------------

def run_tree(
    task: MarbleTask,
    max_iterations: int = 3,
) -> TopologyResult:
    """
    Tree (hierarchical) coordination.

    Agent A is the root. It plans sub-tasks for its children (Agent B
    instances), children execute (potentially delegating further), and the
    root synthesizes.

    Mirrors MARBLE's ``Engine.tree_coordinate()`` +
    ``_execute_agent_task_recursive()``.
    """
    task_id = str(uuid.uuid4())
    agent_map = build_agent_map(task)
    start = time.monotonic()
    result = TopologyResult(
        task_id=task_id,
        marble_task_id=task.task_id,
        domain=task.domain,
        coordinate_mode="tree",
    )

    # Build parent→children from relationships
    children_map: Dict[str, List[str]] = {}
    for a1, a2, rel in task.relationships:
        if rel.lower() == "parent":
            children_map.setdefault(a1, []).append(a2)

    # Find root (first agent, or agent with no parent)
    parented = {child for _, child, rel in task.relationships if rel.lower() == "parent"}
    root_candidates = [a.agent_id for a in task.agents if a.agent_id not in parented]
    root_id = root_candidates[0] if root_candidates else task.agents[0].agent_id

    try:
        for iteration in range(max_iterations):
            _emit("iteration_start", {"iteration": iteration + 1, "max_iterations": max_iterations})
            iter_data: Dict[str, Any] = {"iteration": iteration + 1, "root": root_id}

            _emit("phase_start", {"phase": "execute", "iteration": iteration + 1})
            tree_result, comms = _tree_execute_recursive(
                agent_id=root_id,
                task_text=task.task_content,
                agent_map=agent_map,
                children_map=children_map,
                task_id=task_id,
                result=result,
                depth=0,
            )
            result.final_output = tree_result
            iter_data["summary"] = tree_result[:1000]
            iter_data["communications"] = comms
            result.iterations.append(iter_data)
            _emit("phase_complete", {"phase": "execute"})

            if iteration < max_iterations - 1:
                _emit("phase_start", {"phase": "continue_check", "iteration": iteration + 1})
                should_stop = _should_stop(task.task_content, result.final_output, task_id, result)
                _emit("phase_complete", {"phase": "continue_check", "should_stop": should_stop})
                if should_stop:
                    break

            _emit("iteration_complete", {"iteration": iteration + 1, "total_calls": result.total_agent_calls,
                                         "total_tokens": result.total_tokens})

    except Exception as exc:
        result.error = str(exc)
        _emit("topology_error", {"error": str(exc)})

    result.duration_s = time.monotonic() - start
    return result


def _tree_execute_recursive(
    agent_id: str,
    task_text: str,
    agent_map: Dict[str, AgentMapping],
    children_map: Dict[str, List[str]],
    task_id: str,
    result: TopologyResult,
    depth: int,
    max_depth: int = 3,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Recursively execute a tree node and its children."""
    m = agent_map.get(agent_id)
    if m is None:
        return "", []

    children_ids = children_map.get(agent_id, [])
    communications: List[Dict[str, Any]] = []

    if children_ids and depth < max_depth:
        # Plan tasks for children
        child_profiles = "\n".join(
            f"- {cid}: {agent_map[cid].marble_agent.profile[:200]}"
            for cid in children_ids if cid in agent_map
        )
        plan_prompt = _PLAN_TASKS_PROMPT.format(
            task=task_text[:2000],
            agent_profiles=child_profiles,
            agent_ids=", ".join(children_ids),
        )

        if m.is_agent_a:
            plan_resp = _call_agent_a_llm(plan_prompt, task_id=task_id, scenario="marble_tree_plan")
        else:
            plan_resp = _call_agent_b(
                m.endpoint_url, plan_prompt, role=m.marble_agent.profile[:500], task_id=task_id,
            )
        result.total_llm_calls += 1
        result.total_agent_calls += 1
        _accumulate_tokens(result, plan_resp)

        child_assignments = _parse_assignments(
            plan_resp.get("output", ""), children_ids, task_text,
        )

        # Execute children (can be parallel for siblings)
        children_results: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL, len(children_ids))) as pool:
            futures = {}
            for cid in children_ids:
                child_task = child_assignments.get(cid, task_text[:1000])
                fut = pool.submit(
                    _tree_execute_recursive,
                    agent_id=cid,
                    task_text=child_task,
                    agent_map=agent_map,
                    children_map=children_map,
                    task_id=task_id,
                    result=result,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
                futures[fut] = cid

            for fut in as_completed(futures):
                cid = futures[fut]
                try:
                    child_output, child_comms = fut.result()
                    children_results[cid] = child_output
                    communications.extend(child_comms)
                except Exception as exc:
                    children_results[cid] = f"[ERROR: {exc}]"

        # Synthesize: parent agent processes children's results
        children_str = "\n".join(
            f"[{cid}]: {txt[:500]}" for cid, txt in children_results.items()
        )
        synth_task = (
            f"{task_text[:1000]}\n\n"
            f"Your team members produced these results:\n{children_str}\n\n"
            f"Synthesize and continue working on the original task."
        )

        if m.is_agent_a:
            own_resp = _call_agent_a_llm(synth_task, task_id=task_id, scenario="marble_tree_synth")
        else:
            own_resp = _call_agent_b(
                m.endpoint_url, synth_task, role=m.marble_agent.profile[:500], task_id=task_id,
            )
        result.total_llm_calls += 1
        result.total_agent_calls += 1
        _accumulate_tokens(result, own_resp)
        own_output = own_resp.get("output", "")
        result.agent_outputs[agent_id] = own_output
        return own_output, communications

    else:
        # Leaf node: just act
        if m.is_agent_a:
            resp = _call_agent_a_llm(
                f"You are {agent_id}: {m.marble_agent.profile[:300]}\n\nTask: {task_text[:2000]}",
                task_id=task_id,
                scenario="marble_tree_leaf",
            )
        else:
            resp = _call_agent_b(
                m.endpoint_url, task_text[:2000],
                role=m.marble_agent.profile[:500], task_id=task_id,
            )
        result.total_llm_calls += 1
        result.total_agent_calls += 1
        _accumulate_tokens(result, resp)
        output = resp.get("output", "")
        result.agent_outputs[agent_id] = output
        return output, communications


# ---------------------------------------------------------------------------
# Graph Coordinator
# ---------------------------------------------------------------------------

def run_graph(
    task: MarbleTask,
    max_iterations: int = 3,
) -> TopologyResult:
    """
    Graph (decentralized) coordination.

    All agents receive the global task and act independently. Agent A then
    mediates communication sessions between connected agents (based on
    MARBLE relationships), and synthesizes the final result.

    Mirrors MARBLE's ``Engine.graph_coordinate()`` + ``BaseAgent.act()``
    with ``new_communication_session`` tool calls.
    """
    task_id = str(uuid.uuid4())
    agent_map = build_agent_map(task)
    start = time.monotonic()
    result = TopologyResult(
        task_id=task_id,
        marble_task_id=task.task_id,
        domain=task.domain,
        coordinate_mode="graph",
    )

    # Build adjacency from relationships
    adjacency: Dict[str, List[str]] = {}
    for a1, a2, _ in task.relationships:
        adjacency.setdefault(a1, []).append(a2)

    try:
        for iteration in range(max_iterations):
            _emit("iteration_start", {"iteration": iteration + 1, "max_iterations": max_iterations})
            iter_data: Dict[str, Any] = {"iteration": iteration + 1, "results": {}}

            # --- Phase 1: All agents act on the global task independently ---
            _emit("phase_start", {"phase": "act", "iteration": iteration + 1, "agent_count": len(agent_map)})
            agent_results: Dict[str, str] = {}
            _act_cids: Dict[str, str] = {}
            _act_prompts: Dict[str, str] = {}
            with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL, len(agent_map))) as pool:
                futures = {}
                for aid, m in agent_map.items():
                    neighbors = adjacency.get(aid, [])
                    neighbor_info = ", ".join(
                        f"{nid} ({agent_map[nid].marble_agent.profile[:100]})"
                        for nid in neighbors if nid in agent_map
                    )

                    act_prompt = (
                        f"You are {aid}: {m.marble_agent.profile[:300]}\n\n"
                        f"Task: {task.task_content[:2000]}\n\n"
                        f"Your collaborators: {neighbor_info or 'none'}\n"
                    )

                    if iteration > 0 and result.agent_outputs:
                        prev_results = "\n".join(
                            f"[{k}]: {v[:200]}" for k, v in result.agent_outputs.items()
                            if k != aid
                        )
                        act_prompt += f"\nPrevious round results from others:\n{prev_results}\n"

                    _cid = str(uuid.uuid4())[:8]
                    _act_cids[aid] = _cid
                    _act_prompts[aid] = act_prompt
                    _emit("agent_call_start", {"call_id": _cid, "from_id": "coordinator", "to_id": aid, "call_type": "act",
                                               "prompt": act_prompt[:3000]})
                    if m.is_agent_a:
                        fut = pool.submit(
                            _call_agent_a_llm, act_prompt,
                            task_id=task_id, scenario="marble_graph_act",
                        )
                    else:
                        fut = pool.submit(
                            _call_agent_b, m.endpoint_url, act_prompt,
                            role=m.marble_agent.profile[:500], task_id=task_id,
                        )
                    futures[fut] = aid

                for fut in as_completed(futures):
                    aid = futures[fut]
                    try:
                        resp = fut.result()
                        agent_results[aid] = resp.get("output", "")
                        result.total_llm_calls += 1
                        result.total_agent_calls += 1
                        _accumulate_tokens(result, resp)
                        _emit("agent_call_complete", {"call_id": _act_cids.get(aid, ""), "from_id": aid, "to_id": "coordinator", "call_type": "act",
                                                      "agent_id": aid, "tokens": _resp_tokens(resp),
                                                      "output_preview": resp.get("output", "")[:200],
                                                      "response": resp.get("output", "")[:3000]})
                    except Exception as exc:
                        agent_results[aid] = f"[ERROR: {exc}]"
                        _emit("agent_call_error", {"call_id": _act_cids.get(aid, ""), "agent_id": aid, "call_type": "act", "error": str(exc)})

            result.agent_outputs.update(agent_results)
            iter_data["results"] = {k: v[:500] for k, v in agent_results.items()}
            _emit("phase_complete", {"phase": "act", "completed": len(agent_results)})

            # --- Phase 2: Communication sessions between connected agents ---
            comm_pairs_done: set = set()
            comm_pair_list = []
            for a1 in adjacency:
                for a2 in adjacency[a1]:
                    pair_key = tuple(sorted([a1, a2]))
                    if pair_key in comm_pairs_done:
                        continue
                    comm_pairs_done.add(pair_key)
                    if a1 in agent_map and a2 in agent_map:
                        comm_pair_list.append((a1, a2))

            if comm_pair_list:
                _emit("phase_start", {"phase": "communicate", "iteration": iteration + 1,
                                      "pair_count": len(comm_pair_list)})
                for a1, a2 in comm_pair_list:
                    _emit("comm_session_start", {"agent1_id": a1, "agent2_id": a2})
                    comm = _run_communication_session(
                        a1, a2, agent_map, agent_results,
                        task.task_content, task_id, result,
                    )
                    if comm:
                        result.communications.append(comm)
                _emit("phase_complete", {"phase": "communicate", "sessions": len(result.communications)})

            # --- Phase 3: Synthesis ---
            _emit("phase_start", {"phase": "synthesize", "iteration": iteration + 1})
            all_results_str = "\n\n".join(
                f"[{aid}]: {txt[:500]}" for aid, txt in agent_results.items()
            )
            _cid = str(uuid.uuid4())[:8]
            _synth_prompt = _SYNTHESIZE_PROMPT.format(
                task=task.task_content[:2000], results=all_results_str,
            )
            _emit("agent_call_start", {"call_id": _cid, "from_id": "coordinator", "to_id": "agent1", "call_type": "synthesize",
                                       "prompt": _synth_prompt[:3000]})
            synth_resp = _call_agent_a_llm(
                _synth_prompt,
                task_id=task_id,
                scenario="marble_graph_synth",
            )
            result.total_llm_calls += 1
            result.total_agent_calls += 1
            _accumulate_tokens(result, synth_resp)
            result.final_output = synth_resp.get("output", "")
            _emit("agent_call_complete", {"call_id": _cid, "from_id": "agent1", "to_id": "coordinator", "call_type": "synthesize",
                                          "tokens": _resp_tokens(synth_resp), "output_preview": result.final_output[:200],
                                          "response": result.final_output[:3000]})
            iter_data["summary"] = result.final_output[:1000]
            result.iterations.append(iter_data)
            _emit("phase_complete", {"phase": "synthesize"})

            if iteration < max_iterations - 1:
                _emit("phase_start", {"phase": "continue_check", "iteration": iteration + 1})
                should_stop = _should_stop(task.task_content, result.final_output, task_id, result)
                _emit("phase_complete", {"phase": "continue_check", "should_stop": should_stop})
                if should_stop:
                    break

            _emit("iteration_complete", {"iteration": iteration + 1, "total_calls": result.total_agent_calls,
                                         "total_tokens": result.total_tokens})

    except Exception as exc:
        result.error = str(exc)
        _emit("topology_error", {"error": str(exc)})

    result.duration_s = time.monotonic() - start
    return result


# ---------------------------------------------------------------------------
# Communication session (graph mode)
# ---------------------------------------------------------------------------

def _run_communication_session(
    agent1_id: str,
    agent2_id: str,
    agent_map: Dict[str, AgentMapping],
    current_results: Dict[str, str],
    task_content: str,
    task_id: str,
    result: TopologyResult,
    turns: int = 3,
) -> Optional[Dict[str, Any]]:
    """
    Simulate a MARBLE ``new_communication_session`` between two agents.

    Each agent sends a message, the other responds, for *turns* exchanges.
    Returns the communication log.
    """
    m1 = agent_map[agent1_id]
    m2 = agent_map[agent2_id]

    messages: List[Dict[str, str]] = []
    current_message = (
        f"I've been working on this task and here's my progress: "
        f"{current_results.get(agent1_id, 'No results yet')[:300]}"
    )

    for turn in range(turns):
        # Agent 2 responds to Agent 1
        resp_prompt = _COMMUNICATION_PROMPT.format(
            agent_id=agent2_id,
            profile=m2.marble_agent.profile[:200],
            target_id=agent1_id,
            target_profile=m1.marble_agent.profile[:200],
            task=task_content[:500],
            incoming_message=current_message[:500],
        )
        _cid = str(uuid.uuid4())[:8]
        _emit("agent_call_start", {"call_id": _cid, "from_id": agent1_id, "to_id": agent2_id,
                                   "call_type": "communicate", "prompt": resp_prompt[:3000]})
        if m2.is_agent_a:
            resp = _call_agent_a_llm(resp_prompt, task_id=task_id, scenario="marble_graph_comm")
        else:
            resp = _call_agent_b(
                m2.endpoint_url, resp_prompt,
                role=m2.marble_agent.profile[:300], task_id=task_id,
            )
        result.total_llm_calls += 1
        result.total_agent_calls += 1
        _accumulate_tokens(result, resp)
        reply = resp.get("output", "")
        _emit("agent_call_complete", {"call_id": _cid, "from_id": agent2_id, "to_id": agent1_id,
                                      "agent_id": agent2_id, "call_type": "communicate",
                                      "tokens": _resp_tokens(resp), "output_preview": reply[:200],
                                      "response": reply[:3000]})
        messages.append({"from": agent2_id, "to": agent1_id, "message": reply[:500]})
        _emit("comm_message", {"from_id": agent2_id, "to_id": agent1_id, "turn": turn + 1,
                                "message_preview": reply[:120]})

        if turn < turns - 1:
            # Agent 1 responds back
            resp_prompt_2 = _COMMUNICATION_PROMPT.format(
                agent_id=agent1_id,
                profile=m1.marble_agent.profile[:200],
                target_id=agent2_id,
                target_profile=m2.marble_agent.profile[:200],
                task=task_content[:500],
                incoming_message=reply[:500],
            )
            _cid2 = str(uuid.uuid4())[:8]
            _emit("agent_call_start", {"call_id": _cid2, "from_id": agent2_id, "to_id": agent1_id,
                                       "call_type": "communicate", "prompt": resp_prompt_2[:3000]})
            if m1.is_agent_a:
                resp2 = _call_agent_a_llm(resp_prompt_2, task_id=task_id, scenario="marble_graph_comm")
            else:
                resp2 = _call_agent_b(
                    m1.endpoint_url, resp_prompt_2,
                    role=m1.marble_agent.profile[:300], task_id=task_id,
                )
            result.total_llm_calls += 1
            result.total_agent_calls += 1
            _accumulate_tokens(result, resp2)
            current_message = resp2.get("output", "")
            _emit("agent_call_complete", {"call_id": _cid2, "from_id": agent1_id, "to_id": agent2_id,
                                          "agent_id": agent1_id, "call_type": "communicate",
                                          "tokens": _resp_tokens(resp2), "output_preview": current_message[:200],
                                          "response": current_message[:3000]})
            messages.append({"from": agent1_id, "to": agent2_id, "message": current_message[:500]})
            _emit("comm_message", {"from_id": agent1_id, "to_id": agent2_id, "turn": turn + 1,
                                    "message_preview": current_message[:120]})

    return {
        "session_id": str(uuid.uuid4()),
        "participants": [agent1_id, agent2_id],
        "turns": len(messages),
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resp_tokens(resp: Dict[str, Any]) -> int:
    """Extract total_tokens from an agent response."""
    meta = resp.get("llm_meta") or resp.get("meta") or {}
    return int(meta.get("total_tokens", 0)) if isinstance(meta, dict) else 0


def _accumulate_tokens(result: TopologyResult, resp: Dict[str, Any]) -> None:
    """Add token counts from an agent response to the running total."""
    meta = resp.get("llm_meta") or resp.get("meta") or {}
    if isinstance(meta, dict):
        result.total_tokens += int(meta.get("total_tokens", 0))


def _parse_assignments(
    raw_text: str,
    agent_ids: List[str],
    fallback_task: str,
) -> Dict[str, str]:
    """
    Try to parse a JSON dict of agent_id → subtask from LLM output.
    Falls back to assigning the full task to every agent.
    """
    try:
        start = raw_text.find("{")
        end = raw_text.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(raw_text[start:end])
            if isinstance(parsed, dict):
                return {k: str(v) for k, v in parsed.items() if k in agent_ids}
    except (json.JSONDecodeError, ValueError):
        pass

    return {aid: fallback_task[:1000] for aid in agent_ids}


def _parse_handoff(
    raw_text: str,
    agent_map: Dict[str, AgentMapping],
) -> Tuple[Optional[str], str]:
    """Parse chain handoff response for NEXT_AGENT and INSTRUCTION."""
    next_agent = None
    instruction = ""

    for line in raw_text.split("\n"):
        line = line.strip()
        if line.upper().startswith("NEXT_AGENT:"):
            val = line.split(":", 1)[1].strip()
            if val.upper() == "DONE":
                return "DONE", ""
            if val in agent_map:
                next_agent = val
        elif line.upper().startswith("INSTRUCTION:"):
            instruction = line.split(":", 1)[1].strip()

    return next_agent, instruction


def _should_stop(
    task_content: str,
    current_output: str,
    task_id: str,
    result: TopologyResult,
) -> bool:
    """Ask Agent A whether the task is complete."""
    _cid = str(uuid.uuid4())[:8]
    _check_prompt = _CONTINUE_PROMPT.format(
        task=task_content[:1000],
        results=current_output[:1000],
    )
    _emit("agent_call_start", {"call_id": _cid, "from_id": "coordinator", "to_id": "agent1",
                               "call_type": "continue_check", "prompt": _check_prompt[:3000]})
    resp = _call_agent_a_llm(
        _check_prompt,
        task_id=task_id,
        scenario="marble_continue_check",
    )
    result.total_llm_calls += 1
    result.total_agent_calls += 1
    _accumulate_tokens(result, resp)
    output = resp.get("output", "").strip().upper()
    _emit("agent_call_complete", {"call_id": _cid, "from_id": "agent1", "to_id": "coordinator",
                                  "call_type": "continue_check", "tokens": _resp_tokens(resp),
                                  "output_preview": output[:200],
                                  "response": resp.get("output", "")[:3000]})
    return "DONE" in output


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_COORDINATORS = {
    "star": run_star,
    "chain": run_chain,
    "tree": run_tree,
    "graph": run_graph,
}


def run_topology(
    task: MarbleTask,
    max_iterations: int = 3,
    topology_override: Optional[str] = None,
    progress_callback: ProgressCallback = None,
) -> TopologyResult:
    """
    Run a MARBLE task through the appropriate topology coordinator.

    Uses the task's ``coordinate_mode`` unless *topology_override* is set.

    If *progress_callback* is provided it will be called with
    ``(event_type: str, data: dict)`` at each significant step, enabling
    SSE streaming from the HTTP layer.
    """
    global _ctx_coordinate_mode, _ctx_domain, _progress_cb, _trace_headers
    mode = topology_override or task.coordinate_mode
    coordinator = _COORDINATORS.get(mode)
    if coordinator is None:
        raise ValueError(
            f"Unsupported coordinate_mode '{mode}'. "
            f"Supported: {list(_COORDINATORS.keys())}"
        )
    _ctx_coordinate_mode = mode
    _ctx_domain = task.domain
    _progress_cb = progress_callback

    agent_map = build_agent_map(task)
    _emit("topology_start", {
        "topology": mode,
        "domain": task.domain,
        "task_id": task.task_id,
        "task_preview": task.task_content[:300],
        "agents": [
            {"id": aid, "role": m.marble_agent.profile[:100], "is_agent_a": m.is_agent_a,
             "endpoint": m.endpoint_url}
            for aid, m in agent_map.items()
        ],
        "relationships": [list(r) for r in task.relationships[:20]],
    })

    # Start a root OTel span for this marble task and inject the traceparent so
    # all downstream HTTP calls (Agent A, Agent B, LLM backend) share the same
    # Jaeger trace ID.  Without a root span the inject() call has no active
    # context and _trace_headers stays empty, causing each agent to create an
    # independent trace.
    _trace_headers = {}
    _root_span = None
    _ctx_token = None
    try:
        from agents.common.tracing import get_tracer
        from opentelemetry import context as otel_context, propagate as otel_propagate
        from opentelemetry import trace as otel_trace
        from opentelemetry.trace import SpanKind

        _tracer = get_tracer("marble-runner")
        _root_span = _tracer.start_span(
            "marble.run_topology",
            kind=SpanKind.INTERNAL,
            attributes={
                "marble.domain": task.domain,
                "marble.topology": mode,
                "marble.marble_task_id": str(task.task_id),
            },
        )
        _ctx_token = otel_context.attach(otel_trace.set_span_in_context(_root_span))
        otel_propagate.inject(_trace_headers)
    except Exception:
        pass

    try:
        return coordinator(task, max_iterations=max_iterations)
    finally:
        _progress_cb = None
        if _root_span is not None:
            try:
                _root_span.end()
            except Exception:
                pass
        if _ctx_token is not None:
            try:
                from opentelemetry import context as otel_context
                otel_context.detach(_ctx_token)
            except Exception:
                pass
