from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, Optional, Tuple

# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------
# OOLONG-synth is published on the HuggingFace Hub.  The datasets library
# handles downloading and caching automatically.
# Upstream: https://github.com/abertsch72/oolong
# Hub:      https://huggingface.co/datasets/oolongbench/oolong-synth

_HF_DATASET = "oolongbench/oolong-synth"
_HF_SPLIT = "test"
_DEFAULT_DATASET_FILTER = "trec_coarse"

# Some datasets only exist in a specific split.
# trec_coarse and spam are in "validation"; all others are in "test".
_DATASET_SPLIT_MAP: dict[str, str] = {
    "trec_coarse": "validation",
    "spam": "validation",
}


def default_split_for(dataset_filter: Optional[str]) -> str:
    """Return the correct HF split for a given dataset filter."""
    if dataset_filter and dataset_filter in _DATASET_SPLIT_MAP:
        return _DATASET_SPLIT_MAP[dataset_filter]
    return _HF_SPLIT


@dataclass
class OolongExample:
    """
    Canonical representation of a single OOLONG-synth item for this testbed.

    Fields map directly to the HuggingFace schema so that `raw` can be passed
    unchanged to the official OOLONG scoring functions.
    """

    task_id: str           # = str(record["id"])
    input_context: str     # = record["context_window_text"]
    query: str             # = record["question"]
    ground_truth: str      # = str(record["answer"]), e.g. "['entity']" or "[3]"
    raw: Dict[str, Any] = field(default_factory=dict)   # full HF record


def load_oolong_synth(
    dataset_filter: Optional[str] = _DEFAULT_DATASET_FILTER,
    split: str = _HF_SPLIT,
) -> Generator[OolongExample, None, None]:
    """
    Yield OOLONG-synth examples from the HuggingFace Hub.

    Args:
        dataset_filter: filter by the ``dataset`` field (e.g. ``"trec_coarse"``).
                        Pass ``None`` to yield every dataset in the split.
        split:          HuggingFace split to load (default: ``"test"``).
    """
    try:
        from datasets import load_dataset  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "The 'datasets' package is required to load OOLONG. "
            "Install it with: pip install datasets"
        ) from exc

    data = load_dataset(_HF_DATASET, split=split)

    if dataset_filter:
        data = data.filter(lambda x: x["dataset"] == dataset_filter)

    for record in data:
        # Normalise answer to string: the HF dataset may expose it as a string
        # like "['entity']" or as a Python list depending on the schema version.
        # synth_process_response() always calls ast.literal_eval() on it, which
        # requires a string.
        answer = record["answer"]
        if not isinstance(answer, str):
            answer = str(answer)

        yield OolongExample(
            task_id=str(record["id"]),
            input_context=record["context_window_text"],
            query=record["question"],
            ground_truth=answer,
            raw={**record, "answer": answer},  # keep the normalised answer
        )


# ---------------------------------------------------------------------------
# Backwards-compatible helpers (keep the trec_coarse-specific names)
# ---------------------------------------------------------------------------

def load_trec_coarse(
    oolong_root: Path | None = None,
) -> Generator[OolongExample, None, None]:
    """
    Yield OOLONG trec_coarse examples.

    trec_coarse lives in the ``validation`` split of oolongbench/oolong-synth
    (the ``test`` split contains the 8 other datasets).

    ``oolong_root`` is accepted for backwards compatibility but ignored —
    data is loaded directly from the HuggingFace Hub.
    """
    return load_oolong_synth(
        dataset_filter="trec_coarse",
        split=default_split_for("trec_coarse"),
    )


def iter_trec_coarse_tuples(
    oolong_root: Path | None = None,
) -> Iterable[Tuple[str, str, str, str]]:
    """
    Convenience wrapper yielding plain tuples:

        (task_id, input_context, query, ground_truth)
    """
    for ex in load_trec_coarse():
        yield ex.task_id, ex.input_context, ex.query, ex.ground_truth
