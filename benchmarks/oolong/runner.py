from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from benchmarks.oolong.loader import iter_trec_coarse_tuples
from benchmarks.oolong.scorer import oolong_score


def _send_task(
    agent_url: str,
    task: str,
    scenario: str,
    timeout: float,
) -> Dict[str, Any]:
    """
    Send a single task to Agent A's /task endpoint and return the parsed JSON.
    """

    try:
        import httpx  # type: ignore[import-error]
    except ImportError as exc:  # pragma: no cover - runtime environment specific
        raise RuntimeError(
            "httpx is required to run the OOLONG benchmark runner. "
            "Install it with `pip install httpx` in your environment."
        ) from exc

    payload: Dict[str, Any] = {
        "task": task,
        "scenario": scenario,
        # Tag so downstream systems can recognise benchmark-origin traffic.
        "benchmark_source": "oolong",
    }

    resp = httpx.post(agent_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _truncate_context(context: str, max_chars: Optional[int]) -> str:
    """
    Apply a simple character-based truncation to approximate different context sizes.

    Phase 1.2 calls for a --context-size flag in tokens; here we use characters
    as a lightweight proxy. This can be upgraded to true token-based truncation
    later without changing the CLI surface.
    """

    if not max_chars or max_chars <= 0:
        return context
    if len(context) <= max_chars:
        return context
    # Keep the tail of the context, which often contains the most recent items.
    return context[-max_chars:]


def run_oolong_benchmark(
    agent_url: str,
    scenario: str,
    max_tasks: Optional[int],
    context_size: Optional[int],
    output_path: Path,
    timeout: float,
) -> None:
    """
    Execute the OOLONG trec_coarse split against Agent A and write per-task
    results to JSONL.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    num_numeric = 0
    score_sum = 0.0

    with output_path.open("w", encoding="utf-8") as out_f:
        for idx, (task_id, input_context, query, ground_truth) in enumerate(
            iter_trec_coarse_tuples()
        ):
            if max_tasks is not None and idx >= max_tasks:
                break

            truncated_context = _truncate_context(input_context, context_size)
            combined_task = f"{truncated_context}\n\nQuestion: {query}"

            try:
                response = _send_task(
                    agent_url=agent_url,
                    task=combined_task,
                    scenario=scenario,
                    timeout=timeout,
                )
            except Exception as exc:
                # Log a failed item with score 0.0 for transparency.
                record: Dict[str, Any] = {
                    "benchmark_source": "oolong",
                    "benchmark_split": "trec_coarse",
                    "oolong_task_id": task_id,
                    "scenario": scenario,
                    "context_size_chars": context_size,
                    "error": str(exc),
                    "score": 0.0,
                    "is_numeric": False,
                    "abs_error": None,
                    "ground_truth": ground_truth,
                    "model_answer": None,
                    "agent_response": None,
                }
                out_f.write(json.dumps(record) + "\n")
                out_f.flush()
                continue

            # Agent A task-level schema: we expect at least an "output" field,
            # plus optional task metadata. We keep this loose to remain robust
            # to future changes.
            model_answer = response.get("output")
            if model_answer is None:
                # Fallback: serialise the whole response.
                model_answer = json.dumps(response, ensure_ascii=False)

            score_result = oolong_score(ground_truth, model_answer)

            record = {
                "benchmark_source": "oolong",
                "benchmark_split": "trec_coarse",
                "oolong_task_id": task_id,
                # Application-level metadata (if present in Agent A response)
                "task_id": response.get("task_id"),
                "scenario": scenario,
                "context_size_chars": context_size,
                # OOLONG-specific fields
                "ground_truth": ground_truth,
                "model_answer": model_answer,
                "score": score_result.score,
                "is_numeric": score_result.is_numeric,
                "abs_error": score_result.abs_error,
                # Raw agent response for debugging / offline analysis
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
            f"OOLONG trec_coarse run complete: {total} tasks, "
            f"mean score={mean_score:.4f}, numeric_items={num_numeric}",
            file=sys.stderr,
        )
    else:
        print("No OOLONG tasks were processed (max_tasks=0?)", file=sys.stderr)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the OOLONG trec_coarse benchmark against Agent A."
    )
    parser.add_argument(
        "--agent-url",
        default=os.environ.get("AGENT_A_URL", "http://localhost:8101/task"),
        help="Agent A /task endpoint URL (default: %(default)s).",
    )
    parser.add_argument(
        "--scenario",
        default="agentic_simple",
        choices=["agentic_simple", "agentic_multi_hop", "agentic_parallel"],
        help="Agentic scenario to use for the run.",
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
            "If set, the input_context is truncated to this many characters."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("logs/benchmarks/oolong_trec_coarse.jsonl"),
        help="Path to the JSONL file where per-task results will be written.",
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
        scenario=args.scenario,
        max_tasks=args.max_tasks,
        context_size=args.context_size,
        output_path=args.output,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    main()

