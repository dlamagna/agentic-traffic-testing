from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from benchmarks.oolong.loader import load_oolong_synth, default_split_for
from benchmarks.oolong.scorer import oolong_score


def _send_task(
    agent_url: str,
    task: str,
    scenario: str,
    timeout: float,
) -> Dict[str, Any]:
    """Send a single task to Agent A's /task endpoint and return parsed JSON."""
    try:
        import httpx  # type: ignore[import-error]
    except ImportError as exc:
        raise RuntimeError(
            "httpx is required to run the OOLONG benchmark runner. "
            "Install it with: pip install httpx"
        ) from exc

    payload: Dict[str, Any] = {
        "task": task,
        "scenario": scenario,
        "benchmark_source": "oolong",
    }

    resp = httpx.post(agent_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _send_agentverse(
    workflow_url: str,
    task: str,
    timeout: float,
    agentverse_max_iterations: int = 3,
    agentverse_success_threshold: int = 80,
) -> Dict[str, Any]:
    """
    Send a single task to Agent A's /agentverse endpoint and return parsed JSON.

    ``agentverse_max_iterations`` and ``agentverse_success_threshold`` are
    AgentVerse-specific parameters; they are prefixed accordingly so it is
    clear they only apply to this workflow type.
    """
    try:
        import httpx  # type: ignore[import-error]
    except ImportError as exc:
        raise RuntimeError(
            "httpx is required to run the OOLONG benchmark runner. "
            "Install it with: pip install httpx"
        ) from exc

    payload: Dict[str, Any] = {
        "task": task,
        "benchmark_source": "oolong",
        "max_iterations": agentverse_max_iterations,
        "success_threshold": agentverse_success_threshold,
    }

    resp = httpx.post(workflow_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _extract_answer(response: Dict[str, Any], workflow_name: Optional[str]) -> str:
    """
    Extract the model's answer text from an Agent A response.

    /task endpoint returns ``output``.
    /agentverse endpoint returns ``final_output``.
    Tries both fields in order, falling back to serialising the whole response.
    """
    answer = response.get("output") or response.get("final_output")
    if answer is None:
        answer = json.dumps(response, ensure_ascii=False)
    return str(answer)


def _truncate_context(context: str, max_chars: Optional[int]) -> str:
    """
    Truncate the context window to at most ``max_chars`` characters.

    Keeps the **tail** of the context (most recent items), which is the most
    common convention for OOLONG-style aggregation tasks where items near the
    end may be more densely sampled.

    Note: OOLONG context windows are pre-constructed aggregation tasks;
    truncation may break their internal structure and is intended only for the
    context-size scaling experiments described in Phase 6.2 of the roadmap.
    Use full context (omit ``--context-size``) for standard benchmark runs.
    """
    if not max_chars or max_chars <= 0:
        return context
    if len(context) <= max_chars:
        return context
    return context[-max_chars:]


def _verbose_print(idx: int, example: Any, model_answer: str, score_result: Any) -> None:
    """Print a compact per-task summary to stderr."""
    sep = "-" * 60
    print(sep, file=sys.stderr)
    print(f"[task {idx}] id={example.task_id}", file=sys.stderr)
    print(f"  ground_truth    : {example.ground_truth}", file=sys.stderr)
    print(f"  attempted_parse : {score_result.attempted_parse!r}", file=sys.stderr)
    print(f"  parse_confidence: {score_result.parse_confidence}", file=sys.stderr)
    print(f"  score           : {score_result.score:.4f}", file=sys.stderr)
    print(f"  model_answer    : {model_answer[:400]!r}", file=sys.stderr)


def run_oolong_benchmark(
    agent_url: str,
    scenario: str,
    dataset_filter: str,
    max_tasks: Optional[int],
    context_size: Optional[int],
    output_path: Path,
    timeout: float,
    workflow_url: Optional[str] = None,
    workflow_name: Optional[str] = None,
    agentverse_max_iterations: int = 3,
    agentverse_success_threshold: int = 80,
    verbose: bool = False,
) -> None:
    """
    Run the OOLONG-synth benchmark against Agent A and write per-task results
    to a JSONL file.

    By default tasks are sent to ``agent_url`` (/task) with the given
    ``scenario``.  If ``workflow_url`` is set, tasks are instead sent to that
    URL (e.g. /agentverse) and ``scenario`` is ignored.  Use ``workflow_name``
    to label the run in the JSONL output (defaults to the URL path if omitted).

    AgentVerse-specific parameters (``agentverse_max_iterations``,
    ``agentverse_success_threshold``) are only included in the payload when
    ``workflow_url`` is set and the workflow is /agentverse.

    Failed tasks are recorded with ``score=0.0`` and counted toward the
    aggregate.
    """
    use_workflow = bool(workflow_url)

    # Derive a human-readable label for the JSONL ``scenario`` field.
    if use_workflow:
        if workflow_name:
            effective_scenario = workflow_name
        else:
            # Fall back to the URL path segment, e.g. "/agentverse" → "agentverse"
            from urllib.parse import urlparse
            effective_scenario = urlparse(workflow_url).path.strip("/") or "workflow"
    else:
        effective_scenario = scenario

    output_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    num_errors = 0
    num_numeric = 0
    score_sum = 0.0

    hf_split = default_split_for(dataset_filter)

    with output_path.open("w", encoding="utf-8") as out_f:
        for idx, example in enumerate(
            load_oolong_synth(dataset_filter=dataset_filter, split=hf_split)
        ):
            if max_tasks is not None and idx >= max_tasks:
                break

            truncated_context = _truncate_context(example.input_context, context_size)
            combined_task = f"{truncated_context}\n\nQuestion: {example.query}"

            try:
                if use_workflow:
                    response = _send_agentverse(
                        workflow_url=workflow_url,  # type: ignore[arg-type]
                        task=combined_task,
                        timeout=timeout,
                        agentverse_max_iterations=agentverse_max_iterations,
                        agentverse_success_threshold=agentverse_success_threshold,
                    )
                else:
                    response = _send_task(
                        agent_url=agent_url,
                        task=combined_task,
                        scenario=scenario,
                        timeout=timeout,
                    )
            except Exception as exc:
                record: Dict[str, Any] = {
                    "benchmark_source": "oolong",
                    "benchmark_split": dataset_filter,
                    "oolong_task_id": example.task_id,
                    "task_id": None,
                    "scenario": effective_scenario,
                    "context_size_chars": context_size,
                    "ground_truth": example.ground_truth,
                    "model_answer": None,
                    "score": 0.0,
                    "is_numeric": False,
                    "abs_error": None,
                    "parse_confidence": "error",
                    "attempted_parse": None,
                    "error": str(exc),
                    "agent_response": None,
                }
                out_f.write(json.dumps(record) + "\n")
                out_f.flush()
                total += 1
                num_errors += 1
                if verbose:
                    print(f"[task {idx}] id={example.task_id}  ERROR: {exc}", file=sys.stderr)
                continue

            model_answer = _extract_answer(response, workflow_name)

            score_result = oolong_score(
                datapoint=example.raw,
                y_pred=model_answer,
            )

            if verbose:
                _verbose_print(idx, example, model_answer, score_result)

            record = {
                "benchmark_source": "oolong",
                "benchmark_split": dataset_filter,
                "oolong_task_id": example.task_id,
                "task_id": response.get("task_id"),
                "scenario": effective_scenario,
                "context_size_chars": context_size,
                "ground_truth": example.ground_truth,
                "model_answer": model_answer,
                "score": score_result.score,
                "is_numeric": score_result.is_numeric,
                "abs_error": score_result.abs_error,
                "parse_confidence": score_result.parse_confidence,
                "attempted_parse": score_result.attempted_parse,
                "agent_response": response,
            }

            out_f.write(json.dumps(record) + "\n")
            out_f.flush()

            total += 1
            score_sum += score_result.score
            if score_result.is_numeric:
                num_numeric += 1

    if total > 0:
        mean_score = score_sum / total
        print(
            f"OOLONG {dataset_filter} [{effective_scenario}] run complete: "
            f"{total} tasks (errors={num_errors}), "
            f"mean score={mean_score:.4f}, "
            f"numeric_items={num_numeric}",
            file=sys.stderr,
        )
    else:
        print("No OOLONG tasks were processed (max_tasks=0?).", file=sys.stderr)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an OOLONG-synth split against Agent A."
    )
    parser.add_argument(
        "--agent-url",
        default=os.environ.get("AGENT_A_URL", "http://localhost:8101/task"),
        help="Agent A /task endpoint URL (default: %(default)s).",
    )
    parser.add_argument(
        "--workflow-url",
        default=os.environ.get("OOLONG_WORKFLOW_URL"),
        help=(
            "Alternative workflow endpoint URL (e.g. http://localhost:8101/agentverse). "
            "When set, tasks bypass /task and go to this URL instead. "
            "Set via OOLONG_WORKFLOW_URL in infra/.env.experiment."
        ),
    )
    parser.add_argument(
        "--workflow-name",
        default=os.environ.get("OOLONG_WORKFLOW_NAME"),
        help=(
            "Label written to the 'scenario' field in JSONL output when "
            "--workflow-url is set (default: derived from URL path, e.g. 'agentverse'). "
            "Set via OOLONG_WORKFLOW_NAME in infra/.env.experiment."
        ),
    )
    # AgentVerse-specific parameters — only sent when --workflow-url points at /agentverse.
    parser.add_argument(
        "--agentverse-max-iterations",
        type=int,
        default=int(os.environ.get("OOLONG_AGENTVERSE_MAX_ITERATIONS", "3")),
        help="AgentVerse max workflow iterations (default: 3, server cap: 5).",
    )
    parser.add_argument(
        "--agentverse-success-threshold",
        type=int,
        default=int(os.environ.get("OOLONG_AGENTVERSE_SUCCESS_THRESHOLD", "80")),
        help="AgentVerse success threshold 0–100; result accepted without retry above this (default: 80).",
    )
    parser.add_argument(
        "--scenario",
        default="agentic_simple",
        choices=["agentic_simple", "agentic_multi_hop", "agentic_parallel"],
        help="Agentic scenario for /task runs (ignored when --workflow-url is set).",
    )
    parser.add_argument(
        "--dataset",
        default="trec_coarse",
        help=(
            "OOLONG-synth dataset to filter by (default: trec_coarse). "
            "Pass an empty string to run all datasets."
        ),
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Maximum number of OOLONG tasks to run (default: all).",
    )
    parser.add_argument(
        "--context-size",
        type=int,
        default=None,
        help=(
            "Approximate maximum context size in characters. "
            "If set, context_window_text is truncated to this many characters "
            "(tail kept). Intended for scaling experiments only — omit for "
            "standard benchmark runs."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("logs/benchmarks/oolong_trec_coarse.jsonl"),
        help="Path to the JSONL file where per-task results will be written.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help=(
            "Print per-task results to stderr as they complete: "
            "ground truth, attempted parse, confidence, score, and a "
            "preview of the model answer."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Per-request timeout in seconds for Agent A calls.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    run_oolong_benchmark(
        agent_url=args.agent_url,
        workflow_url=args.workflow_url,
        workflow_name=args.workflow_name,
        agentverse_max_iterations=args.agentverse_max_iterations,
        agentverse_success_threshold=args.agentverse_success_threshold,
        scenario=args.scenario,
        dataset_filter=args.dataset or None,
        max_tasks=args.max_tasks,
        context_size=args.context_size,
        output_path=args.output,
        timeout=args.timeout,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
