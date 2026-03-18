"""
RLM Benchmark Runner

Runs tasks against Agent A's /rlm endpoint and writes per-task JSONL records
following the same schema used by the OOLONG and AgentBench runners.

By default the runner uses the OOLONG-synth trec_coarse split as its task
source (the same dataset used in the RLM paper), but any task list can be
passed via --tasks-file as a JSONL of {"task_id", "task", "ground_truth"}.

Scoring
-------
Delegates to benchmarks/oolong/scorer.py when --use-oolong-scorer is set
(default when running OOLONG tasks), or writes raw outputs for manual
evaluation otherwise.

Usage
-----
    python -m benchmarks.rlm.runner \\
        --scenario rlm_recursive \\
        --max-tasks 50 \\
        --output logs/benchmarks/rlm_trec_coarse.jsonl

See --help for full options.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Task sources
# ---------------------------------------------------------------------------

def _iter_oolong_tasks(
    dataset_filter: str,
    max_tasks: Optional[int],
    context_size: Optional[int],
) -> Iterator[Tuple[str, str, str]]:
    """
    Yield (task_id, combined_task_text, ground_truth) from the OOLONG-synth
    dataset, optionally truncated to ``context_size`` characters.
    """
    from benchmarks.oolong.loader import load_oolong_synth, default_split_for  # type: ignore

    hf_split = default_split_for(dataset_filter)
    for idx, example in enumerate(
        load_oolong_synth(dataset_filter=dataset_filter, split=hf_split)
    ):
        if max_tasks is not None and idx >= max_tasks:
            break
        context = example.input_context
        if context_size and context_size > 0 and len(context) > context_size:
            context = context[-context_size:]
        combined = f"{context}\n\nQuestion: {example.query}"
        yield example.task_id, combined, example.ground_truth


def _iter_jsonl_tasks(
    path: Path,
    max_tasks: Optional[int],
) -> Iterator[Tuple[str, str, str]]:
    """
    Yield (task_id, task_text, ground_truth) from a JSONL file.
    Each line must be a JSON object with keys ``task`` and optionally
    ``task_id`` and ``ground_truth``.
    """
    import uuid as _uuid

    with path.open(encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if max_tasks is not None and idx >= max_tasks:
                break
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            task_id = str(obj.get("task_id") or _uuid.uuid4())
            task = str(obj.get("task", ""))
            ground_truth = str(obj.get("ground_truth", ""))
            yield task_id, task, ground_truth


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _send_rlm(
    rlm_url: str,
    task: str,
    scenario: str,
    max_depth: int,
    max_iterations: int,
    agent_count: int,
    timeout: float,
) -> Dict[str, Any]:
    """POST a task to Agent A's /rlm endpoint and return the parsed JSON."""
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError(
            "httpx is required. Install with: pip install httpx"
        ) from exc

    payload: Dict[str, Any] = {
        "task": task,
        "scenario": scenario,
        "max_depth": max_depth,
        "max_iterations": max_iterations,
        "benchmark_source": "rlm",
    }
    if agent_count > 0:
        payload["agent_count"] = agent_count

    resp = httpx.post(rlm_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _extract_answer(response: Dict[str, Any]) -> str:
    answer = response.get("output") or response.get("final_output")
    if answer is None:
        answer = json.dumps(response, ensure_ascii=False)
    return str(answer)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_oolong(datapoint: Any, model_answer: str) -> Dict[str, Any]:
    """Score using the OOLONG scorer; returns a dict of score fields."""
    try:
        from benchmarks.oolong.scorer import oolong_score  # type: ignore
        result = oolong_score(datapoint=datapoint, y_pred=model_answer)
        return {
            "score": result.score,
            "is_numeric": result.is_numeric,
            "abs_error": result.abs_error,
            "parse_confidence": result.parse_confidence,
            "attempted_parse": result.attempted_parse,
        }
    except Exception as exc:
        return {
            "score": 0.0,
            "is_numeric": False,
            "abs_error": None,
            "parse_confidence": "error",
            "attempted_parse": None,
            "score_error": str(exc),
        }


def _score_passthrough(ground_truth: str, model_answer: str) -> Dict[str, Any]:
    """
    Lightweight scorer for non-OOLONG tasks: exact-match gives 1.0, else 0.0.
    Suitable as a placeholder until a task-specific scorer is wired in.
    """
    score = 1.0 if ground_truth and ground_truth.strip() == model_answer.strip() else 0.0
    return {"score": score, "is_numeric": False, "abs_error": None, "parse_confidence": "exact_match"}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_rlm_benchmark(
    rlm_url: str,
    scenario: str,
    dataset_filter: str,
    max_tasks: Optional[int],
    context_size: Optional[int],
    max_depth: int,
    max_iterations: int,
    agent_count: int,
    output_path: Path,
    timeout: float,
    tasks_file: Optional[Path] = None,
    use_oolong_scorer: bool = True,
    verbose: bool = False,
) -> None:
    """
    Iterate over tasks, send each to /rlm, score the response, and write
    per-task records to a JSONL file.

    When ``tasks_file`` is set it takes priority over the OOLONG dataset.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    num_errors = 0
    score_sum = 0.0

    # Determine task source.
    if tasks_file is not None:
        task_iter = _iter_jsonl_tasks(tasks_file, max_tasks)
        _oolong_examples: Dict[str, Any] = {}
    else:
        # Load OOLONG and cache raw datapoints keyed by task_id for scoring.
        oolong_raw: Dict[str, Any] = {}
        from benchmarks.oolong.loader import load_oolong_synth, default_split_for  # type: ignore

        hf_split = default_split_for(dataset_filter)

        def _task_iter_with_cache() -> Iterator[Tuple[str, str, str]]:
            for idx, example in enumerate(
                load_oolong_synth(dataset_filter=dataset_filter, split=hf_split)
            ):
                if max_tasks is not None and idx >= max_tasks:
                    break
                oolong_raw[example.task_id] = example
                context = example.input_context
                if context_size and context_size > 0 and len(context) > context_size:
                    context = context[-context_size:]
                combined = f"{context}\n\nQuestion: {example.query}"
                yield example.task_id, combined, example.ground_truth

        task_iter = _task_iter_with_cache()  # type: ignore[assignment]
        _oolong_examples = oolong_raw

    with output_path.open("w", encoding="utf-8") as out_f:
        for oolong_task_id, task_text, ground_truth in task_iter:
            try:
                response = _send_rlm(
                    rlm_url=rlm_url,
                    task=task_text,
                    scenario=scenario,
                    max_depth=max_depth,
                    max_iterations=max_iterations,
                    agent_count=agent_count,
                    timeout=timeout,
                )
            except Exception as exc:
                record: Dict[str, Any] = {
                    "benchmark_source": "rlm",
                    "benchmark_split": dataset_filter or "custom",
                    "oolong_task_id": oolong_task_id,
                    "task_id": None,
                    "scenario": scenario,
                    "context_size_chars": context_size,
                    "ground_truth": ground_truth,
                    "model_answer": None,
                    "score": 0.0,
                    "is_numeric": False,
                    "abs_error": None,
                    "parse_confidence": "error",
                    "attempted_parse": None,
                    "error": str(exc),
                    "agent_response": None,
                    "rlm_iterations": None,
                    "rlm_subcalls": None,
                }
                out_f.write(json.dumps(record) + "\n")
                out_f.flush()
                total += 1
                num_errors += 1
                if verbose:
                    print(f"[task {total}] id={oolong_task_id}  ERROR: {exc}", file=sys.stderr)
                continue

            model_answer = _extract_answer(response)

            # Score.
            raw_example = _oolong_examples.get(oolong_task_id) if not tasks_file else None
            if use_oolong_scorer and raw_example is not None:
                score_fields = _score_oolong(
                    datapoint=raw_example.raw,
                    model_answer=model_answer,
                )
            else:
                score_fields = _score_passthrough(ground_truth, model_answer)

            if verbose:
                _sep = "-" * 60
                print(_sep, file=sys.stderr)
                print(f"[task {total + 1}] id={oolong_task_id}", file=sys.stderr)
                print(f"  ground_truth : {ground_truth}", file=sys.stderr)
                print(f"  score        : {score_fields.get('score', '?'):.4f}", file=sys.stderr)
                print(f"  model_answer : {model_answer[:400]!r}", file=sys.stderr)

            record = {
                "benchmark_source": "rlm",
                "benchmark_split": dataset_filter or "custom",
                "oolong_task_id": oolong_task_id,
                "task_id": response.get("task_id"),
                "scenario": scenario,
                "context_size_chars": context_size,
                "ground_truth": ground_truth,
                "model_answer": model_answer,
                # RLM-specific trace fields from agent response.
                "rlm_iterations": response.get("rlm_iterations"),
                "rlm_subcalls": response.get("rlm_subcalls"),
                "rlm_execution_time_s": response.get("rlm_execution_time_s"),
                # Standard aggregates.
                "total_llm_calls": response.get("total_llm_calls"),
                "total_agent_hops": response.get("total_agent_hops"),
                "total_tokens": response.get("total_tokens"),
                "total_latency_ms": response.get("total_latency_ms"),
                "agent_response": response,
                **score_fields,
            }

            out_f.write(json.dumps(record) + "\n")
            out_f.flush()

            total += 1
            score_sum += score_fields.get("score", 0.0)

    if total > 0:
        print(
            f"RLM [{scenario}] run complete: {total} tasks (errors={num_errors}), "
            f"mean score={score_sum / total:.4f}",
            file=sys.stderr,
        )
    else:
        print("No tasks were processed (max_tasks=0?).", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run tasks through Agent A's /rlm endpoint and record results."
    )
    parser.add_argument(
        "--rlm-url",
        default=os.environ.get("RLM_URL", "http://localhost:8101/rlm"),
        help="Agent A /rlm endpoint (default: %(default)s).",
    )
    parser.add_argument(
        "--scenario",
        default="rlm_recursive",
        choices=["rlm_simple", "rlm_recursive", "rlm_parallel"],
        help="RLM scenario (default: rlm_recursive).",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=int(os.environ.get("RLM_MAX_DEPTH", "1")),
        help="RLM recursion depth (0=no REPL, 1=REPL+recursive). Default: 1.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=int(os.environ.get("RLM_MAX_ITERATIONS", "30")),
        help="Max REPL loop iterations per completion. Default: 30.",
    )
    parser.add_argument(
        "--agent-count",
        type=int,
        default=int(os.environ.get("RLM_AGENT_COUNT", "0")),
        help=(
            "Number of Agent B workers to expose as REPL tools. "
            "0 = no Agent B tools (default)."
        ),
    )
    parser.add_argument(
        "--dataset",
        default="trec_coarse",
        help="OOLONG-synth dataset filter (default: trec_coarse). Ignored when --tasks-file is set.",
    )
    parser.add_argument(
        "--tasks-file",
        type=Path,
        default=None,
        help=(
            "Path to a JSONL file with custom tasks. Each line: "
            '{"task_id": "...", "task": "...", "ground_truth": "..."}. '
            "When set, --dataset is ignored."
        ),
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Maximum number of tasks to run (default: all).",
    )
    parser.add_argument(
        "--context-size",
        type=int,
        default=None,
        help="Truncate OOLONG context to this many characters (tail kept). Scaling experiments only.",
    )
    parser.add_argument(
        "--no-oolong-scorer",
        action="store_true",
        default=False,
        help="Disable OOLONG scorer and use exact-match fallback (for non-OOLONG tasks).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("logs/benchmarks/rlm_trec_coarse.jsonl"),
        help="Path to the output JSONL file.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Per-request timeout in seconds (default: 600).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Print per-task results to stderr.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    run_rlm_benchmark(
        rlm_url=args.rlm_url,
        scenario=args.scenario,
        dataset_filter=args.dataset,
        max_tasks=args.max_tasks,
        context_size=args.context_size,
        max_depth=args.max_depth,
        max_iterations=args.max_iterations,
        agent_count=args.agent_count,
        output_path=args.output,
        timeout=args.timeout,
        tasks_file=args.tasks_file,
        use_oolong_scorer=not args.no_oolong_scorer,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
