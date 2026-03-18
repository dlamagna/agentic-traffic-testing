"""
AgentBench benchmark runner.

Two modes
---------
Standalone mode (no ``--agentbench-url``)
    Tasks are loaded directly from the AgentBench data files.  Each task is
    sent as a single POST to Agent A.  Agent A's response is scored
    *offline* against the ground truth.

    Pro:  no AgentBench task servers needed.
    Con:  DB/OS tasks cannot execute tools, so offline scores are lower-
          bound estimates.  KG F1 is approximate.

Controller mode (``--agentbench-url`` set)
    The runner connects to a running AgentBench controller
    (``http://localhost:5000/api``).  Tasks are run via the full multi-turn
    interaction protocol: the runner acts as an agent, calling Agent A for
    inference at each turn and forwarding the response to the controller.
    The controller executes tools (bash / SQL / SPARQL) and returns results.
    Final scores are environment-side accurate.

    Pro:  accurate scores via environment-side evaluation.
    Requires: AgentBench task servers must be running.

Output
------
Per-task JSONL records written to ``--output``.  Fields:

    benchmark_source, benchmark_task_type, benchmark_split,
    agentbench_task_id, task_id, scenario, ground_truth, model_answer,
    score, metric, score_details, agent_response, error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from benchmarks.agentbench.adapter import (
    build_agent_prompt,
    make_interact_request,
    parse_agent_response,
)
from benchmarks.agentbench.loader import (
    TASK_TYPE_DB,
    TASK_TYPE_KG,
    TASK_TYPE_OS,
    AgentBenchTask,
    controller_task_name,
    load_tasks,
)
from benchmarks.agentbench.scorer import ScoreResult, agentbench_score, compute_aggregate


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _http_client():
    try:
        import httpx
        return httpx
    except ImportError as exc:
        raise RuntimeError(
            "httpx is required to run the AgentBench runner. "
            "Install it with: pip install httpx"
        ) from exc


def _send_task(
    agent_url: str,
    task_text: str,
    scenario: str,
    timeout: float,
) -> Dict[str, Any]:
    """POST a single task to Agent A's /task endpoint."""
    httpx = _http_client()
    payload: Dict[str, Any] = {
        "task": task_text,
        "scenario": scenario,
        "benchmark_source": "agentbench",
    }
    resp = httpx.post(agent_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _extract_agent_answer(response: Dict[str, Any]) -> str:
    """Extract the text answer from Agent A's response."""
    answer = response.get("output") or response.get("final_output")
    if answer is None:
        answer = json.dumps(response, ensure_ascii=False)
    return str(answer)


# ---------------------------------------------------------------------------
# Standalone mode
# ---------------------------------------------------------------------------


def _build_standalone_prompt(task: AgentBenchTask) -> str:
    """
    Build a single-shot prompt for standalone mode (no multi-turn loop).
    """
    if task.task_type == TASK_TYPE_DB:
        table_text = task.raw.get("_table_text", "")
        return (
            f"You are a database expert.\n\n"
            f"{table_text}\n\n"
            f"Question: {task.description}\n\n"
            f"Provide ONLY the answer value(s) as a JSON array, e.g. [\"value\"]."
        )
    if task.task_type == TASK_TYPE_OS:
        return (
            f"You are a Linux system expert.\n\n"
            f"Task: {task.description}\n\n"
            f"Provide the bash command to solve this and the exact final answer. "
            f"State the answer on a line starting with 'Answer: '."
        )
    if task.task_type == TASK_TYPE_KG:
        return (
            f"You are a knowledge graph expert.\n\n"
            f"Question: {task.description}\n\n"
            f"List the answer entity values as a JSON array, e.g. [\"entity1\", \"entity2\"]."
        )
    return task.description


def _run_standalone_task(
    task: AgentBenchTask,
    agent_url: str,
    scenario: str,
    timeout: float,
) -> Tuple[str, Dict[str, Any], Optional[str]]:
    """Run a single task in standalone mode. Returns (model_answer, response, error)."""
    prompt = _build_standalone_prompt(task)
    try:
        response = _send_task(agent_url, prompt, scenario, timeout)
        model_answer = _extract_agent_answer(response)
        return model_answer, response, None
    except Exception as exc:
        return "", {}, str(exc)


# ---------------------------------------------------------------------------
# Controller mode
# ---------------------------------------------------------------------------


def _controller_get_indices(controller_url: str, task_name: str, timeout: float) -> List[Any]:
    """Fetch task indices from the AgentBench controller."""
    httpx = _http_client()
    resp = httpx.get(
        f"{controller_url}/get_indices",
        params={"name": task_name},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _controller_start_sample(
    controller_url: str, task_name: str, index: Any, timeout: float
) -> Dict[str, Any]:
    """Start a task sample and return the initial session state."""
    httpx = _http_client()
    resp = httpx.post(
        f"{controller_url}/start_sample",
        json={"name": task_name, "index": index},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _controller_interact(
    controller_url: str, session_id: int, content: str, timeout: float
) -> Dict[str, Any]:
    """Send an agent response to the controller and get the updated state."""
    httpx = _http_client()
    resp = httpx.post(
        f"{controller_url}/interact",
        json=make_interact_request(session_id, content),
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _controller_cancel(controller_url: str, session_id: int, timeout: float) -> None:
    httpx = _http_client()
    try:
        httpx.post(
            f"{controller_url}/cancel",
            json={"session_id": session_id},
            timeout=timeout,
        )
    except Exception:
        pass


def _controller_calculate_overall(
    controller_url: str, task_name: str, results: List[Dict[str, Any]], timeout: float
) -> Dict[str, Any]:
    httpx = _http_client()
    resp = httpx.post(
        f"{controller_url}/calculate_overall",
        json={"name": task_name, "results": results},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _run_controller_task(
    task: AgentBenchTask,
    index: Any,
    controller_url: str,
    agent_url: str,
    scenario: str,
    max_turns: int,
    timeout: float,
    verbose: bool,
) -> Tuple[str, Dict[str, Any], Optional[str], Optional[Dict[str, Any]]]:
    """
    Run a single task via the AgentBench controller.

    Returns ``(model_answer, agent_final_response, error, controller_result)``.
    """
    task_name = controller_task_name(task.task_type)

    # Start sample
    try:
        state = _controller_start_sample(controller_url, task_name, index, timeout)
    except Exception as exc:
        return "", {}, f"start_sample failed: {exc}", None

    session_id: int = state.get("session_id", -1)
    output: Dict[str, Any] = state.get("output", {})
    history: List[Dict[str, Any]] = output.get("history") or []
    status: str = output.get("status", "running")

    last_agent_response: Dict[str, Any] = {}
    model_answer = ""
    turns = 0

    while status == "running" and turns < max_turns:
        # Build prompt for Agent A
        prompt = build_agent_prompt(task, history)

        # Call Agent A
        try:
            agent_resp = _send_task(agent_url, prompt, scenario, timeout)
            last_agent_response = agent_resp
            raw_answer = _extract_agent_answer(agent_resp)
        except Exception as exc:
            _controller_cancel(controller_url, session_id, timeout)
            return model_answer, last_agent_response, f"agent call failed: {exc}", None

        # Parse into tool call
        tool_name, formatted_content, parsed_args = parse_agent_response(
            task.task_type, task.tools, raw_answer
        )

        if verbose:
            print(
                f"  [turn {turns}] tool={tool_name} "
                f"args={json.dumps(parsed_args or {})[:80]}",
                file=sys.stderr,
            )

        # Track final answer
        if "commit_final_answer" in tool_name or "answer" in tool_name:
            if parsed_args:
                answers = parsed_args.get("answers") or [parsed_args.get("answer", "")]
                model_answer = "; ".join(str(a) for a in answers if a)
            else:
                model_answer = raw_answer[:200]

        # Send to controller
        try:
            new_state = _controller_interact(
                controller_url, session_id, formatted_content, timeout
            )
        except Exception as exc:
            _controller_cancel(controller_url, session_id, timeout)
            return model_answer, last_agent_response, f"interact failed: {exc}", None

        output = new_state.get("output", {})
        history = output.get("history") or []
        status = output.get("status", "unknown")
        turns += 1

    controller_result = output.get("result")
    if not model_answer:
        model_answer = str(controller_result) if controller_result else ""

    return model_answer, last_agent_response, None, controller_result


# ---------------------------------------------------------------------------
# Main benchmark loop
# ---------------------------------------------------------------------------


def _verbose_print(
    idx: int,
    task: AgentBenchTask,
    model_answer: str,
    score_result: ScoreResult,
) -> None:
    sep = "-" * 60
    print(sep, file=sys.stderr)
    print(f"[task {idx}] id={task.task_id} type={task.task_type}", file=sys.stderr)
    print(f"  ground_truth    : {task.ground_truth[:80]}", file=sys.stderr)
    print(f"  score           : {score_result.score:.4f} ({score_result.metric})", file=sys.stderr)
    print(f"  parse_confidence: {score_result.parse_confidence}", file=sys.stderr)
    print(f"  model_answer    : {model_answer[:200]!r}", file=sys.stderr)


def run_agentbench_benchmark(
    agent_url: str,
    task_type: str,
    scenario: str,
    split: str,
    max_tasks: Optional[int],
    output_path: Path,
    timeout: float,
    agentbench_url: Optional[str] = None,
    max_turns: int = 10,
    verbose: bool = False,
    agentbench_root: Optional[Path] = None,
) -> None:
    """
    Run the AgentBench benchmark and write per-task JSONL results.

    If ``agentbench_url`` is set, controller mode is used (full multi-turn
    protocol with environment-side evaluation).  Otherwise standalone mode
    is used (single-shot, offline scoring).
    """
    controller_mode = bool(agentbench_url)
    controller_url = (agentbench_url.rstrip("/") + "/api") if agentbench_url else None

    output_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    num_errors = 0
    score_sum = 0.0
    score_results: List[ScoreResult] = []

    if controller_mode:
        # In controller mode, get the indices from the server and iterate those
        task_name = controller_task_name(task_type)
        try:
            indices = _controller_get_indices(controller_url, task_name, timeout)
        except Exception as exc:
            print(
                f"[run_agentbench] ERROR: Could not get task indices from {controller_url}: {exc}",
                file=sys.stderr,
            )
            return
        if max_tasks is not None:
            indices = indices[:max_tasks]
        # Build a minimal AgentBenchTask for prompt construction
        # (real data comes from the controller; we only need type/tools)
        from benchmarks.agentbench.loader import tools_for
        dummy_task = AgentBenchTask(
            task_id=task_name,
            task_type=task_type,
            description="",
            tools=tools_for(task_type),
            ground_truth="",
        )
        task_iterator = [(idx, dummy_task) for idx in indices]
    else:
        tasks_gen = load_tasks(
            task_type=task_type,
            split=split,
            max_tasks=max_tasks,
            agentbench_root=agentbench_root,
        )
        task_iterator = [(task.task_id, task) for task in tasks_gen]

    collected_controller_outputs: List[Dict[str, Any]] = []

    with output_path.open("w", encoding="utf-8") as out_f:
        for idx, (task_index, task) in enumerate(task_iterator):
            record: Dict[str, Any] = {
                "benchmark_source": "agentbench",
                "benchmark_task_type": task_type,
                "benchmark_split": split,
                "agentbench_task_id": task_index,
                "task_id": None,
                "scenario": scenario,
                "ground_truth": task.ground_truth,
                "model_answer": None,
                "score": 0.0,
                "metric": "success_rate" if task_type in (TASK_TYPE_OS, TASK_TYPE_DB) else "f1",
                "score_details": {},
                "agent_response": None,
                "error": None,
            }

            if controller_mode:
                model_answer, agent_resp, error, ctrl_result = _run_controller_task(
                    task=task,
                    index=task_index,
                    controller_url=controller_url,
                    agent_url=agent_url,
                    scenario=scenario,
                    max_turns=max_turns,
                    timeout=timeout,
                    verbose=verbose,
                )
                record["task_id"] = agent_resp.get("task_id") if agent_resp else None
                if error:
                    record["error"] = error
                    record["model_answer"] = model_answer or None
                    out_f.write(json.dumps(record) + "\n")
                    out_f.flush()
                    total += 1
                    num_errors += 1
                    if verbose:
                        print(f"[task {idx}] id={task_index}  ERROR: {error}", file=sys.stderr)
                    continue

                score_result = agentbench_score(task_type, task, model_answer, ctrl_result)
                # Collect for calculate_overall
                if ctrl_result is not None:
                    collected_controller_outputs.append({
                        "index": task_index,
                        "status": "completed",
                        "result": ctrl_result,
                        "history": [],
                    })

            else:
                model_answer, agent_resp, error = _run_standalone_task(
                    task, agent_url, scenario, timeout
                )
                record["task_id"] = agent_resp.get("task_id") if agent_resp else None
                if error:
                    record["error"] = error
                    out_f.write(json.dumps(record) + "\n")
                    out_f.flush()
                    total += 1
                    num_errors += 1
                    if verbose:
                        print(f"[task {idx}] id={task.task_id}  ERROR: {error}", file=sys.stderr)
                    continue

                score_result = agentbench_score(task_type, task, model_answer)

            record.update(
                {
                    "ground_truth": task.ground_truth,
                    "model_answer": model_answer,
                    "score": score_result.score,
                    "metric": score_result.metric,
                    "score_details": score_result.score_details,
                    "agent_response": agent_resp,
                }
            )

            out_f.write(json.dumps(record) + "\n")
            out_f.flush()

            total += 1
            score_sum += score_result.score
            score_results.append(score_result)

            if verbose:
                _verbose_print(idx, task, model_answer, score_result)

    # Aggregate summary
    if total > 0:
        agg = compute_aggregate(task_type, score_results)
        mean_score = score_sum / total
        mode_label = "controller" if controller_mode else "standalone"
        print(
            f"AgentBench {task_type} [{scenario}] [{mode_label}] run complete: "
            f"{total} tasks (errors={num_errors}), "
            f"mean score={mean_score:.4f}",
            file=sys.stderr,
        )
        if "success_rate" in agg:
            print(f"  success_rate={agg['success_rate']:.4f}", file=sys.stderr)
        if "mean_f1" in agg:
            print(f"  mean_f1={agg['mean_f1']:.4f}", file=sys.stderr)
    else:
        print("No AgentBench tasks were processed (max_tasks=0?).", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an AgentBench task type against Agent A."
    )
    parser.add_argument(
        "--agent-url",
        default=os.environ.get("AGENT_A_URL", "http://localhost:8101/task"),
        help="Agent A /task endpoint URL (default: %(default)s).",
    )
    parser.add_argument(
        "--agentbench-url",
        default=os.environ.get("AGENTBENCH_URL"),
        help=(
            "AgentBench controller base URL (e.g. http://localhost:5000). "
            "When set, controller mode is used with full multi-turn evaluation. "
            "Set via AGENTBENCH_URL in infra/.env.experiment."
        ),
    )
    parser.add_argument(
        "--agentbench-root",
        type=Path,
        default=None,
        help=(
            "Path to the cloned AgentBench repo. "
            "Used only in standalone mode to load task data. "
            "Defaults to AGENTBENCH_ROOT env var or ../AgentBench."
        ),
    )
    parser.add_argument(
        "--task-type",
        default="db",
        choices=["os", "db", "kg"],
        help="AgentBench task type (default: db).",
    )
    parser.add_argument(
        "--scenario",
        default="agentic_multi_hop",
        choices=["agentic_simple", "agentic_multi_hop", "agentic_parallel"],
        help="Agentic scenario label for /task runs (default: agentic_multi_hop).",
    )
    parser.add_argument(
        "--split",
        default="standard",
        help="Dataset split: 'standard' (test) or 'train' (default: standard).",
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Maximum number of tasks to run (default: all).",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=int(os.environ.get("AGENTBENCH_MAX_TURNS", "10")),
        help=(
            "Maximum interaction turns per task in controller mode (default: 10). "
            "Set via AGENTBENCH_MAX_TURNS."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("logs/benchmarks/agentbench_db.jsonl"),
        help="Path to the JSONL file for per-task results.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Per-request timeout in seconds (default: 300).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Print per-task results to stderr as they complete.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    run_agentbench_benchmark(
        agent_url=args.agent_url,
        task_type=args.task_type,
        scenario=args.scenario,
        split=args.split,
        max_tasks=args.max_tasks,
        output_path=args.output,
        timeout=args.timeout,
        agentbench_url=args.agentbench_url,
        max_turns=args.max_turns,
        verbose=args.verbose,
        agentbench_root=args.agentbench_root,
    )


if __name__ == "__main__":
    main()
