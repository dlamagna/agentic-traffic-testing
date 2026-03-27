"""
MARBLE (MultiAgentBench) benchmark runner.

Loads tasks from the MARBLE repository, executes them through the testbed's
Docker-deployed agents using the appropriate coordination topology, scores
the results, and writes per-task JSONL records.

Usage
-----
    python -m benchmarks.marble.runner \\
        --domain research \\
        --topology graph \\
        --max-tasks 5 \\
        --output logs/benchmarks/marble_research.jsonl

    python -m benchmarks.marble.runner \\
        --domain coding \\
        --max-tasks 3 \\
        --max-iterations 2 \\
        --verbose

See ``--help`` for full options.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from benchmarks.marble.loader import (
    MarbleTask,
    available_domains,
    load_marble_tasks,
    marble_root,
    task_count,
)
from benchmarks.marble.scorer import MarbleScore, score_marble_task
from benchmarks.marble.topology import TopologyResult, run_topology


# ---------------------------------------------------------------------------
# Verbose output helpers
# ---------------------------------------------------------------------------

def _verbose_header(task: MarbleTask) -> None:
    sep = "=" * 70
    print(sep, file=sys.stderr)
    print(
        f"[MARBLE] domain={task.domain}  task_id={task.task_id}  "
        f"topology={task.coordinate_mode}  agents={task.agent_count}",
        file=sys.stderr,
    )
    print(f"  task: {task.task_content[:200]}...", file=sys.stderr)
    for a in task.agents:
        print(f"  agent {a.agent_id}: {a.profile[:120]}...", file=sys.stderr)


def _verbose_result(topo_result: TopologyResult, score: MarbleScore) -> None:
    print(f"  duration: {topo_result.duration_s:.1f}s", file=sys.stderr)
    print(
        f"  calls: agent={topo_result.total_agent_calls}  "
        f"llm={topo_result.total_llm_calls}  "
        f"tokens={topo_result.total_tokens}",
        file=sys.stderr,
    )
    print(f"  score: {score.aggregate:.4f}", file=sys.stderr)
    if score.error:
        print(f"  score_error: {score.error}", file=sys.stderr)
    print(f"  output: {topo_result.final_output[:300]}...", file=sys.stderr)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_marble_benchmark(
    domain: str,
    max_tasks: Optional[int],
    task_ids: Optional[List[int]],
    topology_override: Optional[str],
    max_iterations: int,
    output_path: Path,
    timeout: float,
    skip_judge: bool,
    verbose: bool,
    tasks_dir: Optional[Path] = None,
) -> None:
    """
    Main benchmark loop: load → execute → score → write JSONL.

    Per-task artifacts (meta.json, response.json, calls.csv) are written to
    timestamped subdirectories under ``tasks_dir`` (defaults to
    ``<output_path.parent>/tasks/``).  An experiment-level ``runs.jsonl`` is
    appended alongside the output JSONL.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Resolve per-task artifact directory
    _tasks_dir: Path = tasks_dir if tasks_dir is not None else output_path.parent / "tasks"
    _tasks_dir.mkdir(parents=True, exist_ok=True)

    # Experiment-level runs index sits next to the output JSONL
    runs_jsonl = output_path.parent / "runs.jsonl"

    # Call log path (set by topology module env var)
    call_log_path = os.path.join(
        os.environ.get("MARBLE_METRICS_LOG_DIR", "logs"),
        "marble_llm_calls.jsonl",
    )

    total = 0
    num_errors = 0
    score_sum = 0.0

    # Pre-flight checks
    root = marble_root()
    if not root.is_dir():
        print(
            f"[MARBLE] ERROR: MARBLE repo not found at {root}. "
            "Set MARBLE_ROOT env var or clone to ../MARBLE.",
            file=sys.stderr,
        )
        sys.exit(1)

    avail = available_domains()
    if domain not in avail:
        print(
            f"[MARBLE] ERROR: Domain '{domain}' not available. "
            f"Found JSONL files for: {avail}",
            file=sys.stderr,
        )
        sys.exit(1)

    total_available = task_count(domain)
    effective_max = max_tasks if max_tasks else total_available
    print(
        f"[MARBLE] domain={domain}  available={total_available}  "
        f"max_tasks={effective_max}  topology={topology_override or 'from-task'}  "
        f"max_iterations={max_iterations}  skip_judge={skip_judge}",
        file=sys.stderr,
    )

    with output_path.open("w", encoding="utf-8") as out_f:
        for task in load_marble_tasks(
            domain=domain,
            max_tasks=max_tasks,
            task_ids=task_ids,
            topology_override=topology_override,
        ):
            if verbose:
                _verbose_header(task)

            topo_result: Optional[TopologyResult] = None
            score: Optional[MarbleScore] = None
            run_start_ms = int(time.time() * 1000)
            run_ts = datetime.fromtimestamp(run_start_ms / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d_%H-%M-%S"
            )

            try:
                topo_result = run_topology(
                    task,
                    max_iterations=max_iterations,
                    topology_override=topology_override,
                )
            except Exception as exc:
                run_end_ms = int(time.time() * 1000)
                record = _error_record(task, topology_override, str(exc))
                out_f.write(json.dumps(record) + "\n")
                out_f.flush()
                # Still save artifacts for error cases so runs are traceable
                task_dir = _save_task_artifacts(
                    _tasks_dir, task, None, None,
                    run_ts, run_start_ms, run_end_ms, call_log_path,
                )
                _append_runs_jsonl(
                    runs_jsonl, task, None, None,
                    task_dir, run_ts, run_start_ms, run_end_ms,
                )
                total += 1
                num_errors += 1
                if verbose:
                    print(f"  TOPOLOGY ERROR: {exc}", file=sys.stderr)
                continue

            run_end_ms = int(time.time() * 1000)

            # Score
            try:
                score = score_marble_task(
                    task_content=task.task_content,
                    topology_result=topo_result,
                    skip_judge=skip_judge,
                )
            except Exception as exc:
                score = MarbleScore(error=str(exc))

            if verbose and topo_result and score:
                _verbose_result(topo_result, score)

            record = _build_record(task, topology_override, topo_result, score)
            out_f.write(json.dumps(record) + "\n")
            out_f.flush()

            # Save per-task artifacts
            task_dir = _save_task_artifacts(
                _tasks_dir, task, topo_result, score,
                run_ts, run_start_ms, run_end_ms, call_log_path,
            )
            _append_runs_jsonl(
                runs_jsonl, task, topo_result, score,
                task_dir, run_ts, run_start_ms, run_end_ms,
            )

            total += 1
            score_sum += score.aggregate if score else 0.0
            if topo_result and topo_result.error:
                num_errors += 1

    # Summary
    if total > 0:
        mean_score = score_sum / total
        print(
            f"\n[MARBLE] {domain} [{topology_override or 'mixed'}] run complete: "
            f"{total} tasks (errors={num_errors}), "
            f"mean score={mean_score:.4f}",
            file=sys.stderr,
        )
    else:
        print("[MARBLE] No tasks were processed.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------

def _build_record(
    task: MarbleTask,
    topology_override: Optional[str],
    topo_result: TopologyResult,
    score: MarbleScore,
) -> Dict[str, Any]:
    return {
        "benchmark_source": "marble",
        "benchmark_domain": task.domain,
        "marble_task_id": task.task_id,
        "task_id": topo_result.task_id,
        "coordinate_mode": topo_result.coordinate_mode,
        "topology_override": topology_override,
        "agent_count": task.agent_count,
        "agent_ids": task.agent_ids,
        # Topology execution
        "final_output": topo_result.final_output[:5000],
        "total_agent_calls": topo_result.total_agent_calls,
        "total_llm_calls": topo_result.total_llm_calls,
        "total_tokens": topo_result.total_tokens,
        "duration_s": topo_result.duration_s,
        "iterations": len(topo_result.iterations),
        "communication_sessions": len(topo_result.communications),
        "topology_error": topo_result.error,
        # Scoring
        "score": score.aggregate,
        "task_quality": score.task_quality,
        "communication_quality": score.communication_quality,
        "planning_quality": score.planning_quality,
        "collaboration_quality": score.collaboration_quality,
        "domain_score": score.domain_score,
        "score_error": score.error,
        "score_details": score.details,
        # Raw results for downstream analysis
        "agent_outputs": {
            k: v[:2000] for k, v in topo_result.agent_outputs.items()
        },
    }


def _error_record(
    task: MarbleTask,
    topology_override: Optional[str],
    error_msg: str,
) -> Dict[str, Any]:
    return {
        "benchmark_source": "marble",
        "benchmark_domain": task.domain,
        "marble_task_id": task.task_id,
        "task_id": None,
        "coordinate_mode": topology_override or task.coordinate_mode,
        "topology_override": topology_override,
        "agent_count": task.agent_count,
        "agent_ids": task.agent_ids,
        "final_output": None,
        "total_agent_calls": 0,
        "total_llm_calls": 0,
        "total_tokens": 0,
        "duration_s": 0.0,
        "iterations": 0,
        "communication_sessions": 0,
        "topology_error": error_msg,
        "score": 0.0,
        "task_quality": 0.0,
        "communication_quality": 0.0,
        "planning_quality": 0.0,
        "collaboration_quality": 0.0,
        "domain_score": 0.0,
        "score_error": error_msg,
        "score_details": {},
        "agent_outputs": {},
    }


# ---------------------------------------------------------------------------
# Per-task artifact saving
# ---------------------------------------------------------------------------

_CALLS_CSV_FIELDS = [
    "call_id", "task_id", "agent_id", "call_type",
    "timestamp_start", "timestamp_end",
    "latency_ms", "llm_latency_ms",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "coordinate_mode", "domain",
    "iat_s",
    "http_status", "error",
    "cost_estimate_usd", "total_llm_calls", "total_agent_hops",
]


def _save_task_artifacts(
    tasks_dir: Path,
    task: "MarbleTask",
    topo_result: Optional["TopologyResult"],
    score: Optional["MarbleScore"],
    run_ts: str,
    run_start_ms: int,
    run_end_ms: int,
    call_log_path: str,
) -> Path:
    """
    Write per-task artifacts to a timestamped subdirectory:
        <tasks_dir>/<run_ts>_<domain>_<topology>_<task_uuid>/
            meta.json      — task metadata + scores
            response.json  — full agent outputs + iteration detail
            calls.csv      — per-LLM-call metrics for this task_id
    Returns the task directory path.
    """
    topology = (topo_result.coordinate_mode if topo_result else
                task.coordinate_mode)
    task_uuid = topo_result.task_id if topo_result else "error"
    slug = f"{run_ts}_{task.domain}_{topology}_{task_uuid}"
    task_dir = tasks_dir / slug
    task_dir.mkdir(parents=True, exist_ok=True)

    duration_s = topo_result.duration_s if topo_result else 0.0

    # ------------------------------------------------------------------
    # meta.json
    # ------------------------------------------------------------------
    meta: Dict[str, Any] = {
        "benchmark_source": "marble",
        "domain": task.domain,
        "topology": topology,
        "marble_task_id": task.task_id,
        "task_id": task_uuid,
        "task_content": task.task_content,
        "agent_count": task.agent_count,
        "agent_ids": task.agent_ids,
        "run_ts": run_ts,
        "run_start_ms": run_start_ms,
        "run_end_ms": run_end_ms,
        "duration_s": duration_s,
        # scores
        "score": score.aggregate if score else None,
        "task_quality": score.task_quality if score else None,
        "communication_quality": score.communication_quality if score else None,
        "planning_quality": score.planning_quality if score else None,
        "collaboration_quality": score.collaboration_quality if score else None,
        "domain_score": score.domain_score if score else None,
        "score_error": score.error if score else None,
        # topology stats
        "total_agent_calls": topo_result.total_agent_calls if topo_result else 0,
        "total_llm_calls": topo_result.total_llm_calls if topo_result else 0,
        "total_tokens": topo_result.total_tokens if topo_result else 0,
        "iterations": len(topo_result.iterations) if topo_result else 0,
        "communication_sessions": len(topo_result.communications) if topo_result else 0,
        "topology_error": topo_result.error if topo_result else None,
    }
    (task_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ------------------------------------------------------------------
    # response.json
    # ------------------------------------------------------------------
    response: Dict[str, Any] = {
        "task_id": task_uuid,
        "marble_task_id": task.task_id,
        "domain": task.domain,
        "coordinate_mode": topology,
        "task_content": task.task_content,
        "agent_profiles": [
            {"agent_id": a.agent_id, "profile": a.profile}
            for a in task.agents
        ],
        "final_output": topo_result.final_output if topo_result else None,
        "agent_outputs": topo_result.agent_outputs if topo_result else {},
        "iterations": topo_result.iterations if topo_result else [],
        "communications": topo_result.communications if topo_result else [],
        "total_agent_calls": topo_result.total_agent_calls if topo_result else 0,
        "total_llm_calls": topo_result.total_llm_calls if topo_result else 0,
        "total_tokens": topo_result.total_tokens if topo_result else 0,
        "duration_s": duration_s,
        "error": topo_result.error if topo_result else None,
        "score": {
            "aggregate": score.aggregate if score else None,
            "task_quality": score.task_quality if score else None,
            "communication_quality": score.communication_quality if score else None,
            "planning_quality": score.planning_quality if score else None,
            "collaboration_quality": score.collaboration_quality if score else None,
            "domain_score": score.domain_score if score else None,
            "details": score.details if score else {},
            "error": score.error if score else None,
        },
    }
    (task_dir / "response.json").write_text(
        json.dumps(response, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ------------------------------------------------------------------
    # calls.csv  — filter marble_llm_calls.jsonl by task_id
    # ------------------------------------------------------------------
    calls: List[Dict[str, Any]] = []
    if os.path.exists(call_log_path):
        with open(call_log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("task_id") == task_uuid:
                    calls.append(rec)

    # Sort by timestamp_start and compute IAT within this task
    calls.sort(key=lambda r: r.get("timestamp_start", ""))
    prev_ts: Optional[float] = None
    for rec in calls:
        ts_str = rec.get("timestamp_start", "")
        try:
            ts = datetime.fromisoformat(ts_str).timestamp()
        except (ValueError, TypeError):
            ts = None
        rec["iat_s"] = round(ts - prev_ts, 6) if (ts is not None and prev_ts is not None) else None
        if ts is not None:
            prev_ts = ts

    csv_path = task_dir / "calls.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as cf:
        writer = csv.DictWriter(cf, fieldnames=_CALLS_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(calls)

    return task_dir


def _append_runs_jsonl(
    runs_jsonl: Path,
    task: "MarbleTask",
    topo_result: Optional["TopologyResult"],
    score: Optional["MarbleScore"],
    task_dir: Path,
    run_ts: str,
    run_start_ms: int,
    run_end_ms: int,
) -> None:
    """Append one summary line per completed task to the experiment runs.jsonl."""
    topology = (topo_result.coordinate_mode if topo_result else task.coordinate_mode)
    record = {
        "run_dir": str(task_dir),
        "domain": task.domain,
        "topology": topology,
        "marble_task_id": task.task_id,
        "task_id": topo_result.task_id if topo_result else None,
        "run_ts": run_ts,
        "run_start_ms": run_start_ms,
        "run_end_ms": run_end_ms,
        "duration_s": topo_result.duration_s if topo_result else 0.0,
        "score": score.aggregate if score else None,
        "total_agent_calls": topo_result.total_agent_calls if topo_result else 0,
        "total_llm_calls": topo_result.total_llm_calls if topo_result else 0,
        "total_tokens": topo_result.total_tokens if topo_result else 0,
        "topology_error": topo_result.error if topo_result else None,
    }
    runs_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with runs_jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run MARBLE (MultiAgentBench) tasks through the testbed's "
            "Docker-deployed agents and record per-task results."
        ),
    )
    parser.add_argument(
        "--domain",
        default=os.environ.get("MARBLE_DOMAIN", "research"),
        help=(
            "MARBLE domain to run (coding, research, bargaining, database, minecraft). "
            "Default: research."
        ),
    )
    parser.add_argument(
        "--topology",
        default=os.environ.get("MARBLE_TOPOLOGY"),
        choices=["star", "chain", "tree", "graph"],
        help=(
            "Force all tasks to use this topology (overrides JSONL coordinate_mode). "
            "Omit to use each task's native topology."
        ),
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Maximum number of tasks to run (default: all).",
    )
    parser.add_argument(
        "--task-ids",
        type=str,
        default=None,
        help="Comma-separated list of specific task IDs to run (e.g. '1,2,5').",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=int(os.environ.get("MARBLE_MAX_ITERATIONS", "3")),
        help="Max coordination iterations per task (default: 3).",
    )
    parser.add_argument(
        "--skip-judge",
        action="store_true",
        default=False,
        help="Skip LLM-as-judge scoring (faster, no score output).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("logs/benchmarks/marble_results.jsonl"),
        help="Path to the output JSONL file.",
    )
    parser.add_argument(
        "--tasks-dir",
        type=Path,
        default=None,
        help=(
            "Directory for per-task artifact subdirectories "
            "(meta.json, response.json, calls.csv). "
            "Defaults to <output-parent>/tasks/."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("MARBLE_TIMEOUT_SECONDS", "300")),
        help="Per-request timeout in seconds (default: 300).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Print per-task results to stderr.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)

    # Set timeout env var so topology module picks it up
    os.environ["MARBLE_TIMEOUT_SECONDS"] = str(args.timeout)

    task_ids: Optional[List[int]] = None
    if args.task_ids:
        task_ids = [int(x.strip()) for x in args.task_ids.split(",")]

    run_marble_benchmark(
        domain=args.domain,
        max_tasks=args.max_tasks,
        task_ids=task_ids,
        topology_override=args.topology,
        max_iterations=args.max_iterations,
        output_path=args.output,
        timeout=args.timeout,
        skip_judge=args.skip_judge,
        verbose=args.verbose,
        tasks_dir=args.tasks_dir,
    )


if __name__ == "__main__":
    main()
