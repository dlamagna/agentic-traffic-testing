"""
MARBLE benchmark scorer.

Implements evaluation metrics aligned with MARBLE's milestone-based KPI
system.  Where MARBLE uses an LLM-as-judge (via ``model_prompting``) for
communication, planning, and task-level scoring, this module mirrors that
approach by routing judge calls through Agent A's ``/task`` endpoint so
all evaluation traffic is also captured in the testbed's telemetry.

Scoring dimensions
------------------
1. **Task quality** — LLM-as-judge rates the final output against the task.
2. **Communication quality** — rates inter-agent discussion quality (graph mode).
3. **Planning quality** — rates whether sub-task assignments were sensible.
4. **Collaboration** — rates whether agents leveraged each other effectively.
5. **Aggregate** — weighted mean of the above.

Domain-specific scoring (research novelty, code quality, bargaining outcome)
is layered on top when the MARBLE task includes domain-specific metric flags.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from benchmarks.marble.topology import TopologyResult


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_AGENT_A_URL = os.environ.get("AGENT_A_URL", "http://localhost:8101")
_TIMEOUT = float(os.environ.get("MARBLE_TIMEOUT_SECONDS", "300"))


def _httpx():
    try:
        import httpx
        return httpx
    except ImportError as exc:
        raise RuntimeError("httpx is required: pip install httpx") from exc


# ---------------------------------------------------------------------------
# Score container
# ---------------------------------------------------------------------------

@dataclass
class MarbleScore:
    """Result of evaluating one MARBLE task execution."""

    task_quality: float = 0.0
    communication_quality: float = 0.0
    planning_quality: float = 0.0
    collaboration_quality: float = 0.0
    domain_score: float = 0.0
    aggregate: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_quality": self.task_quality,
            "communication_quality": self.communication_quality,
            "planning_quality": self.planning_quality,
            "collaboration_quality": self.collaboration_quality,
            "domain_score": self.domain_score,
            "aggregate": self.aggregate,
            "details": self.details,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# LLM-as-judge prompts
# ---------------------------------------------------------------------------

_TASK_QUALITY_PROMPT = """\
You are evaluating the quality of a multi-agent system's output.

TASK:
{task}

SYSTEM OUTPUT:
{output}

Rate the output quality on a scale of 1-5:
1 = Completely wrong or irrelevant
2 = Partially addresses the task but has major issues
3 = Addresses the task but with notable gaps
4 = Good response with minor issues
5 = Excellent, comprehensive response

Respond with ONLY a JSON object: {{"score": <1-5>, "reasoning": "<brief explanation>"}}"""

_COMMUNICATION_PROMPT = """\
You are evaluating the quality of inter-agent communication in a collaborative task.

TASK:
{task}

COMMUNICATION LOG:
{communications}

Rate the communication quality on a scale of 1-5:
1 = No meaningful exchange
2 = Superficial exchange with little value
3 = Some useful information shared
4 = Good collaborative dialogue advancing the task
5 = Excellent, agents built on each other's contributions effectively

Respond with ONLY a JSON object: {{"score": <1-5>, "reasoning": "<brief explanation>"}}"""

_PLANNING_PROMPT = """\
You are evaluating how well tasks were decomposed and assigned in a multi-agent system.

ORIGINAL TASK:
{task}

AGENT PROFILES:
{profiles}

TASK ASSIGNMENTS:
{assignments}

Rate the planning quality on a scale of 1-5:
1 = Tasks were assigned randomly with no regard for agent capabilities
2 = Some alignment between agents and tasks
3 = Reasonable assignments but could be better
4 = Good task decomposition matching agent strengths
5 = Excellent strategic task allocation

Respond with ONLY a JSON object: {{"score": <1-5>, "reasoning": "<brief explanation>"}}"""

_COLLABORATION_PROMPT = """\
You are evaluating overall collaboration effectiveness in a multi-agent system.

TASK: {task}
TOPOLOGY: {topology}
NUMBER OF AGENTS: {num_agents}
TOTAL ITERATIONS: {iterations}
AGENT OUTPUTS: {agent_outputs}
FINAL OUTPUT: {final_output}

Rate collaboration effectiveness on a scale of 1-5:
1 = Agents worked in isolation, no synergy
2 = Minimal collaboration
3 = Some collaboration but not fully leveraged
4 = Good teamwork, agents complemented each other
5 = Excellent synergy, the whole exceeded the sum of parts

Respond with ONLY a JSON object: {{"score": <1-5>, "reasoning": "<brief explanation>"}}"""


# ---------------------------------------------------------------------------
# Judge helper
# ---------------------------------------------------------------------------

def _llm_judge(prompt: str, task_id: Optional[str] = None) -> Dict[str, Any]:
    """Send a judge prompt through Agent A and parse the score."""
    httpx = _httpx()
    payload = {
        "task": prompt,
        "scenario": "marble_judge",
        "benchmark_source": "marble",
    }
    headers: Dict[str, str] = {}
    if task_id:
        headers["X-Task-ID"] = task_id

    try:
        resp = httpx.post(
            f"{_AGENT_A_URL}/task", json=payload, headers=headers, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        output = resp.json().get("output", "")
        return _parse_judge_response(output)
    except Exception as exc:
        return {"score": 0, "reasoning": f"Judge call failed: {exc}"}


def _parse_judge_response(text: str) -> Dict[str, Any]:
    """Extract score JSON from judge LLM output."""
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(text[start:end])
            if isinstance(parsed, dict) and "score" in parsed:
                return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: look for a bare number
    for token in text.split():
        try:
            val = int(token)
            if 1 <= val <= 5:
                return {"score": val, "reasoning": "Parsed from raw text"}
        except ValueError:
            continue
    return {"score": 0, "reasoning": "Could not parse judge output"}


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------

def score_task_quality(
    task_content: str,
    final_output: str,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    prompt = _TASK_QUALITY_PROMPT.format(
        task=task_content[:2000],
        output=final_output[:2000],
    )
    return _llm_judge(prompt, task_id)


def score_communication(
    task_content: str,
    communications: List[Dict[str, Any]],
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not communications:
        return {"score": 0, "reasoning": "No communication occurred"}

    comm_text = ""
    for comm in communications[:5]:
        for msg in comm.get("messages", [])[:6]:
            comm_text += f"[{msg.get('from', '?')} → {msg.get('to', '?')}]: {msg.get('message', '')[:200]}\n"
    comm_text = comm_text[:3000]

    prompt = _COMMUNICATION_PROMPT.format(
        task=task_content[:1000],
        communications=comm_text,
    )
    return _llm_judge(prompt, task_id)


def score_planning(
    task_content: str,
    agent_profiles: str,
    assignments: str,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    prompt = _PLANNING_PROMPT.format(
        task=task_content[:1000],
        profiles=agent_profiles[:1000],
        assignments=assignments[:1000],
    )
    return _llm_judge(prompt, task_id)


def score_collaboration(
    task_content: str,
    topology_result: TopologyResult,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    agent_outputs = "\n".join(
        f"[{k}]: {v[:200]}" for k, v in topology_result.agent_outputs.items()
    )[:2000]

    prompt = _COLLABORATION_PROMPT.format(
        task=task_content[:1000],
        topology=topology_result.coordinate_mode,
        num_agents=len(topology_result.agent_outputs),
        iterations=len(topology_result.iterations),
        agent_outputs=agent_outputs,
        final_output=topology_result.final_output[:1000],
    )
    return _llm_judge(prompt, task_id)


# ---------------------------------------------------------------------------
# Aggregate scorer
# ---------------------------------------------------------------------------

def score_marble_task(
    task_content: str,
    topology_result: TopologyResult,
    skip_judge: bool = False,
) -> MarbleScore:
    """
    Compute a full MARBLE-style evaluation for one task execution.

    When *skip_judge* is ``True``, only structural metrics are collected
    (no LLM judge calls).  Useful for dry runs or when the LLM backend
    is unavailable.
    """
    score = MarbleScore()
    task_id = topology_result.task_id

    if topology_result.error:
        score.error = topology_result.error
        return score

    if skip_judge:
        score.details["skipped_judge"] = True
        score.aggregate = 0.0
        return score

    try:
        tq = score_task_quality(task_content, topology_result.final_output, task_id)
        score.task_quality = float(tq.get("score", 0)) / 5.0
        score.details["task_quality"] = tq
    except Exception as exc:
        score.details["task_quality_error"] = str(exc)

    try:
        collab = score_collaboration(task_content, topology_result, task_id)
        score.collaboration_quality = float(collab.get("score", 0)) / 5.0
        score.details["collaboration"] = collab
    except Exception as exc:
        score.details["collaboration_error"] = str(exc)

    if topology_result.communications:
        try:
            cq = score_communication(
                task_content, topology_result.communications, task_id,
            )
            score.communication_quality = float(cq.get("score", 0)) / 5.0
            score.details["communication"] = cq
        except Exception as exc:
            score.details["communication_error"] = str(exc)

    if topology_result.iterations:
        first_iter = topology_result.iterations[0]
        assignments = first_iter.get("assignments") or first_iter.get("task_assignments", {})
        if assignments:
            try:
                profiles_str = "\n".join(
                    f"- {k}: {v[:200]}"
                    for k, v in topology_result.agent_outputs.items()
                )[:1000]
                assignments_str = json.dumps(assignments, ensure_ascii=False)[:1000]
                pq = score_planning(
                    task_content, profiles_str, assignments_str, task_id,
                )
                score.planning_quality = float(pq.get("score", 0)) / 5.0
                score.details["planning"] = pq
            except Exception as exc:
                score.details["planning_error"] = str(exc)

    # Weighted aggregate (task quality weighted highest)
    weights = {
        "task": 0.40,
        "collaboration": 0.25,
        "communication": 0.20,
        "planning": 0.15,
    }
    raw = (
        weights["task"] * score.task_quality
        + weights["collaboration"] * score.collaboration_quality
        + weights["communication"] * score.communication_quality
        + weights["planning"] * score.planning_quality
    )
    score.aggregate = round(raw, 4)

    return score
