from __future__ import annotations

import json
import re
import string
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from benchmarks.agentbench.loader import TASK_TYPE_DB, TASK_TYPE_KG, TASK_TYPE_OS


# ---------------------------------------------------------------------------
# Score result
# ---------------------------------------------------------------------------


@dataclass
class ScoreResult:
    """Result of scoring a single AgentBench task."""

    score: float                   # 0.0–1.0
    metric: str                    # "success_rate" or "f1"
    is_correct: Optional[bool]     # for OS/DB: pass/fail; None for KG
    abs_error: Optional[float]     # reserved for numeric tasks
    score_details: Dict[str, Any]  # extra info (predicted, gold, etc.)
    parse_confidence: str          # "exact", "partial", "failed"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_text(text: str) -> str:
    """Lowercase, remove punctuation, collapse whitespace."""
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    return " ".join(text.split())


def _tokenise(text: str) -> List[str]:
    return _normalise_text(text).split()


def _f1_token(pred: str, gold: str) -> float:
    """Token-level F1 between two strings."""
    pred_tokens = _tokenise(pred)
    gold_tokens = _tokenise(gold)
    if not pred_tokens and not gold_tokens:
        return 1.0
    if not pred_tokens or not gold_tokens:
        return 0.0
    pred_set = set(pred_tokens)
    gold_set = set(gold_tokens)
    tp = len(pred_set & gold_set)
    if tp == 0:
        return 0.0
    precision = tp / len(pred_set)
    recall = tp / len(gold_set)
    return 2 * precision * recall / (precision + recall)


def _set_f1(predicted: Set[str], gold: Set[str]) -> float:
    """Entity-set F1 (exact entity match)."""
    if not predicted and not gold:
        return 1.0
    if not predicted or not gold:
        return 0.0
    tp = len(predicted & gold)
    if tp == 0:
        return 0.0
    precision = tp / len(predicted)
    recall = tp / len(gold)
    return 2 * precision * recall / (precision + recall)


def _parse_db_answer(model_answer: str) -> List[str]:
    """
    Extract the model's answer value(s) from free-form text.

    Tries several strategies:
    1. JSON array literal                  e.g. ``["Women +60kg Bronze"]``
    2. A line prefixed with "Answer:"     e.g. ``Answer: Women +60kg Bronze``
    3. First non-empty line               fallback
    """
    # Strategy 1: JSON array
    m = re.search(r'\[.*?\]', model_answer, re.DOTALL)
    if m:
        try:
            val = json.loads(m.group(0))
            if isinstance(val, list):
                return [str(v).strip() for v in val]
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 2: "Answer: ..." line
    m = re.search(r'(?:answer|result)\s*:\s*(.+)', model_answer, re.IGNORECASE)
    if m:
        return [m.group(1).strip()]

    # Strategy 3: first non-empty line
    for line in model_answer.splitlines():
        line = line.strip()
        if line:
            return [line]
    return []


def _normalise_numeric(text: str) -> str:
    """
    Normalise a numeric string to a canonical float representation.

    Handles:
    - Comma-formatted numbers (``"1,174"`` → ``"1174.0"``)
    - Integer-encoded floats (``"1"`` → ``"1.0"``)

    Returns the original text if it is not a number.
    """
    # Strip commas used as thousands separators
    stripped = text.replace(",", "").strip()
    try:
        return str(float(stripped))
    except (ValueError, TypeError):
        return text


def _compare_db_answers(
    predicted: List[str], ground_truth: str
) -> bool:
    """
    Order-insensitive comparison: text (case-insensitive) or numeric (float-aware).

    ``ground_truth`` is a JSON-serialised list like ``'["Women +60kg Bronze"]'``.
    Numbers are normalised so that ``"1"`` matches ``"1.0"`` and
    ``"1,174"`` matches ``"1174.0"``.
    """
    try:
        gold_list = json.loads(ground_truth)
    except (json.JSONDecodeError, TypeError):
        gold_list = [str(ground_truth)]

    if not isinstance(gold_list, list):
        gold_list = [gold_list]

    # Text-normalised gold set (for string comparison)
    gold_text = {_normalise_text(str(v)) for v in gold_list}
    # Numeric-normalised gold set: apply _normalise_numeric to the RAW value
    # (before _normalise_text removes "." and ",") then normalise text on that
    gold_numeric = {_normalise_text(_normalise_numeric(str(v))) for v in gold_list}

    for pred in predicted:
        pred_text = _normalise_text(pred)
        # Numeric path: strip commas from raw pred, parse float, then normalise text
        pred_numeric = _normalise_text(_normalise_numeric(pred))
        if pred_text in gold_text or pred_numeric in gold_numeric:
            return True
    return False


def _parse_kg_entities(model_answer: str) -> Set[str]:
    """
    Extract entity URIs / values from free-form model text.

    Looks for a section like "Answer: ..." or parses comma-separated values.
    """
    # Try JSON array
    m = re.search(r'\[.*?\]', model_answer, re.DOTALL)
    if m:
        try:
            val = json.loads(m.group(0))
            if isinstance(val, list):
                return {str(v).strip() for v in val if v}
        except (json.JSONDecodeError, ValueError):
            pass

    # Try "Answer: ..." line
    m = re.search(r'(?:answer|entities|result)\s*:\s*(.+)', model_answer, re.IGNORECASE)
    if m:
        raw = m.group(1)
    else:
        raw = model_answer

    # Comma-separated or newline-separated
    parts = re.split(r'[,\n]+', raw)
    return {p.strip() for p in parts if p.strip()}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def agentbench_score(
    task_type: str,
    task: Any,  # AgentBenchTask
    model_answer: str,
    controller_result: Optional[Dict[str, Any]] = None,
) -> ScoreResult:
    """
    Score a single AgentBench task.

    If ``controller_result`` is provided (the ``result`` field from the
    AgentBench session output), it is used directly (environment-side scoring).
    Otherwise, offline scoring is applied.

    Args:
        task_type:         ``"os"``, ``"db"``, or ``"kg"``.
        task:              The ``AgentBenchTask`` instance.
        model_answer:      The agent's free-text response.
        controller_result: Optional dict from the AgentBench task server result
                           (e.g. ``{"result": True}`` for OS, ``{"is_correct": True}``
                           for DB).  When set, offline parsing is skipped.
    """
    if task_type == TASK_TYPE_OS:
        return _score_os(task, model_answer, controller_result)
    if task_type == TASK_TYPE_DB:
        return _score_db(task, model_answer, controller_result)
    if task_type == TASK_TYPE_KG:
        return _score_kg(task, model_answer, controller_result)
    raise ValueError(f"Unknown task_type: {task_type!r}")


def agentbench_score_scalar(
    task_type: str,
    task: Any,
    model_answer: str,
    controller_result: Optional[Dict[str, Any]] = None,
) -> float:
    """Convenience wrapper — returns just the scalar score (0.0–1.0)."""
    return agentbench_score(task_type, task, model_answer, controller_result).score


# ---------------------------------------------------------------------------
# Per-task-type scorers
# ---------------------------------------------------------------------------


def _score_os(
    task: Any,
    model_answer: str,
    controller_result: Optional[Dict[str, Any]],
) -> ScoreResult:
    """OS interaction: binary pass/fail."""
    if controller_result is not None:
        # Environment-side result from AgentBench task server
        passed = bool(controller_result.get("result", False))
        return ScoreResult(
            score=1.0 if passed else 0.0,
            metric="success_rate",
            is_correct=passed,
            abs_error=None,
            score_details={"source": "controller", "result": controller_result},
            parse_confidence="exact",
        )

    # Offline: check against evaluation.match.answer if available
    eval_info = task.raw.get("evaluation", {})
    if "match" in eval_info:
        match = eval_info["match"]
        expected = match.get("answer", "") if isinstance(match, dict) else str(match)
        strip = match.get("strip", True) if isinstance(match, dict) else True
        candidate = model_answer.strip() if strip else model_answer
        passed = candidate == expected
        return ScoreResult(
            score=1.0 if passed else 0.0,
            metric="success_rate",
            is_correct=passed,
            abs_error=None,
            score_details={
                "source": "offline_match",
                "expected": expected,
                "predicted": candidate,
            },
            parse_confidence="exact" if passed else "partial",
        )

    # check evaluation — cannot score without environment
    return ScoreResult(
        score=0.0,
        metric="success_rate",
        is_correct=False,
        abs_error=None,
        score_details={"source": "offline_check", "note": "requires_environment"},
        parse_confidence="failed",
    )


def _score_db(
    task: Any,
    model_answer: str,
    controller_result: Optional[Dict[str, Any]],
) -> ScoreResult:
    """DB: binary pass/fail."""
    if controller_result is not None:
        passed = bool(controller_result.get("is_correct", False))
        return ScoreResult(
            score=1.0 if passed else 0.0,
            metric="success_rate",
            is_correct=passed,
            abs_error=None,
            score_details={"source": "controller", "result": controller_result},
            parse_confidence="exact",
        )

    # Offline: parse answer from model text and compare to label
    predicted = _parse_db_answer(model_answer)
    passed = _compare_db_answers(predicted, task.ground_truth) if predicted else False
    return ScoreResult(
        score=1.0 if passed else 0.0,
        metric="success_rate",
        is_correct=passed,
        abs_error=None,
        score_details={
            "source": "offline",
            "predicted": predicted,
            "ground_truth": task.ground_truth,
        },
        parse_confidence="partial" if predicted else "failed",
    )


def _score_kg(
    task: Any,
    model_answer: str,
    controller_result: Optional[Dict[str, Any]],
) -> ScoreResult:
    """KG: token-level F1 over entity sets."""
    gold_list = task.raw.get("_answer_set", [])
    if not gold_list:
        try:
            gold_list = json.loads(task.ground_truth)
        except (json.JSONDecodeError, TypeError):
            gold_list = [task.ground_truth]
    gold_set = {str(v).strip() for v in gold_list if v}

    if controller_result is not None:
        # Controller returns F1 in the task result or overall
        f1 = float(controller_result.get("f1", 0.0))
        return ScoreResult(
            score=f1,
            metric="f1",
            is_correct=None,
            abs_error=None,
            score_details={"source": "controller", "result": controller_result},
            parse_confidence="exact",
        )

    # Offline: parse entities from model text
    predicted_set = _parse_kg_entities(model_answer)
    f1 = _set_f1(predicted_set, gold_set)
    return ScoreResult(
        score=f1,
        metric="f1",
        is_correct=None,
        abs_error=None,
        score_details={
            "source": "offline",
            "predicted": sorted(predicted_set),
            "ground_truth": sorted(gold_set),
        },
        parse_confidence="partial" if predicted_set else "failed",
    )


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------


def compute_aggregate(
    task_type: str,
    results: List[ScoreResult],
) -> Dict[str, Any]:
    """
    Compute aggregate metrics for a completed run.

    Returns a dict with ``mean_score``, ``success_rate`` (OS/DB) or
    ``mean_f1`` (KG), and per-confidence counts.
    """
    if not results:
        return {"total": 0}

    total = len(results)
    mean_score = sum(r.score for r in results) / total

    if task_type in (TASK_TYPE_OS, TASK_TYPE_DB):
        correct = sum(1 for r in results if r.is_correct)
        agg = {
            "total": total,
            "correct": correct,
            "success_rate": correct / total,
            "mean_score": mean_score,
        }
    else:
        agg = {
            "total": total,
            "mean_f1": mean_score,
            "mean_score": mean_score,
        }

    conf_counts: Dict[str, int] = {}
    for r in results:
        conf_counts[r.parse_confidence] = conf_counts.get(r.parse_confidence, 0) + 1
    agg["parse_confidence_counts"] = conf_counts
    return agg
