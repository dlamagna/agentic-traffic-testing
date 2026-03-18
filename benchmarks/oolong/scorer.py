from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Scoring delegates to the official OOLONG eval_helpers module.
# Upstream: https://github.com/abertsch72/oolong
#
# Set OOLONG_ROOT to the root of the cloned OOLONG repo (default: ../oolong
# relative to this project).  The scorer will add
# {OOLONG_ROOT}/src/eval to sys.path and import synth_process_response from
# there.
# ---------------------------------------------------------------------------


@dataclass
class ScoreResult:
    """
    Result of scoring a single OOLONG prediction.

    ``score`` is always in [0, 1].  For numeric answer types, ``abs_error``
    is the integer difference between gold and predicted values.
    ``parse_confidence`` and ``attempted_parse`` are propagated directly from
    the official OOLONG scorer.
    """

    score: float
    is_numeric: bool
    abs_error: Optional[float] = None
    parse_confidence: str = "unknown"
    attempted_parse: Optional[str] = None


# ---------------------------------------------------------------------------
# OOLONG repo discovery
# ---------------------------------------------------------------------------

def _oolong_eval_dir() -> Path:
    """Return the path to the OOLONG repo's src/eval directory."""
    env = os.getenv("OOLONG_ROOT")
    if env:
        return Path(env).expanduser().resolve() / "src" / "eval"
    # Default: sibling checkout next to this project
    project_root = Path(__file__).resolve().parents[2]
    return project_root.parent / "oolong" / "src" / "eval"


_eval_helpers = None  # module-level cache


def _get_eval_helpers():
    """Import the official OOLONG eval_helpers module (cached after first call)."""
    global _eval_helpers
    if _eval_helpers is not None:
        return _eval_helpers

    eval_dir = _oolong_eval_dir()
    if not eval_dir.is_dir():
        raise ImportError(
            f"OOLONG eval directory not found at {eval_dir}.\n"
            "Clone https://github.com/abertsch72/oolong next to this repo "
            "(as '../oolong') or set the OOLONG_ROOT environment variable to "
            "the repo root."
        )

    eval_dir_str = str(eval_dir)
    if eval_dir_str not in sys.path:
        sys.path.insert(0, eval_dir_str)

    # eval_helpers.py has module-level imports for litellm, tiktoken, jsonlines,
    # transformers, and datasets that are only used by the inference runner and
    # DnD eval — not by synth_process_response which is the only function we
    # call.  Stub out any missing modules so the import succeeds without
    # requiring the full OOLONG dependency set.
    import types
    for _stub in ("litellm", "tiktoken", "jsonlines"):
        if _stub not in sys.modules:
            sys.modules[_stub] = types.ModuleType(_stub)
    if "transformers" not in sys.modules:
        _t = types.ModuleType("transformers")
        _t.AutoTokenizer = None  # type: ignore[attr-defined]
        sys.modules["transformers"] = _t

    import eval_helpers as _eh  # noqa: PLC0415
    _eval_helpers = _eh
    return _eval_helpers


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def oolong_score(
    datapoint: Dict[str, Any],
    y_pred: str,
    model: str = "local",
) -> ScoreResult:
    """
    Score a model prediction using the official OOLONG synth evaluation logic
    (``synth_process_response`` from the OOLONG repo's ``eval_helpers.py``).

    Args:
        datapoint: the full OOLONG record dict.  Must contain at minimum
                   ``"answer"``, ``"answer_type"``, ``"id"``,
                   ``"context_window_id"``, and ``"dataset"``.
        y_pred:    the model's raw text output (will be parsed by the OOLONG
                   answer parser).
        model:     model name label (used only as a metadata tag in the result).

    Returns:
        :class:`ScoreResult` with ``score`` in [0, 1] and additional metadata.
    """
    eh = _get_eval_helpers()
    result = eh.synth_process_response(datapoint, y_pred, model)

    score = max(0.0, min(1.0, float(result["score"])))
    parse_confidence = str(result.get("parse_confidence", "unknown"))
    attempted_parse = str(result.get("attempted_parse", ""))

    answer_type = datapoint.get("answer_type", "")
    is_numeric = answer_type == "ANSWER_TYPE.NUMERIC"

    abs_error: Optional[float] = None
    if is_numeric:
        try:
            import ast
            gold = int(ast.literal_eval(datapoint["answer"])[0])
            pred_val = int(attempted_parse)
            abs_error = float(abs(gold - pred_val))
        except Exception:
            pass

    return ScoreResult(
        score=score,
        is_numeric=is_numeric,
        abs_error=abs_error,
        parse_confidence=parse_confidence,
        attempted_parse=attempted_parse,
    )


def oolong_score_scalar(
    datapoint: Dict[str, Any],
    y_pred: str,
    model: str = "local",
) -> float:
    """Convenience wrapper returning just the scalar score in [0, 1]."""
    return oolong_score(datapoint, y_pred, model).score
