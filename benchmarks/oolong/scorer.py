from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple, Union


Number = Union[int, float]


@dataclass
class ScoreResult:
    """
    Container for a single OOLONG-style score.

    The numeric score is always in [0, 1]. For numerical answers the
    absolute error is also reported for convenience.
    """

    score: float
    is_numeric: bool
    abs_error: Optional[float] = None


def _try_parse_number(text: str) -> Optional[Number]:
    """
    Attempt to parse a string as an int or float.

    Returns None if parsing fails.
    """

    text = text.strip()
    if not text:
        return None

    # Common patterns in LLM outputs: "Answer: 3", "3.", "3.0"
    for prefix in ("Answer:", "answer:", "Prediction:", "prediction:"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()

    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return None


def oolong_score(
    y_true: str,
    y_pred: str,
) -> ScoreResult:
    """
    Compute the OOLONG scoring function between a ground-truth answer and
    a model prediction.

    - For numerical answers: score(ŷ) = 0.75 ** |y - ŷ|
    - For non-numerical answers: exact-match over normalised strings
      (case-folded and stripped).
    """

    true_num = _try_parse_number(y_true)
    pred_num = _try_parse_number(y_pred)

    if true_num is not None and pred_num is not None:
        abs_err = abs(float(true_num) - float(pred_num))
        score = 0.75 ** abs_err
        # Clamp for numerical stability
        score = max(0.0, min(1.0, float(score)))
        return ScoreResult(score=score, is_numeric=True, abs_error=abs_err)

    # Non-numeric: use strict string equality after light normalisation.
    norm_true = y_true.strip().lower()
    norm_pred = y_pred.strip().lower()
    return ScoreResult(score=1.0 if norm_true == norm_pred else 0.0, is_numeric=False)


def oolong_score_scalar(y_true: str, y_pred: str) -> float:
    """
    Convenience wrapper that returns just the scalar score in [0, 1].
    """

    return oolong_score(y_true, y_pred).score

