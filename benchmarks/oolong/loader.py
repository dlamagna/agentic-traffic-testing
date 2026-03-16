import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Iterable, Tuple


@dataclass
class OolongExample:
    """
    Canonical representation of a single OOLONG item for this testbed.

    Fields are intentionally generic so they can be re-used by other
    benchmarks if needed.
    """

    task_id: str
    input_context: str
    query: str
    ground_truth: str


def _default_oolong_root() -> Path:
    """
    Return the expected root of the OOLONG repo.

    By default we look for a sibling checkout at ../OOLONG relative to the
    project root, but this can be overridden via the OOLONG_ROOT environment
    variable.
    """

    env = os.getenv("OOLONG_ROOT")
    if env:
        return Path(env).expanduser().resolve()

    # Fallback: assume the user cloned OOLONG next to this repo
    #   /path/to/
    #     agentic-traffic-testing/
    #     OOLONG/
    project_root = Path(__file__).resolve().parents[2]
    return project_root.parent / "OOLONG"


def _trec_coarse_path(root: Path) -> Path:
    """
    Locate the trec_coarse split within the OOLONG repo.

    The upstream repo organises data under data/oolong/.
    We keep this helper small so it can be updated easily if the layout
    changes.
    """

    # Common layout used by the public OOLONG repo:
    #   OOLONG/
    #     data/
    #       oolong/
    #         trec_coarse.jsonl
    candidates = [
        root / "data" / "oolong" / "trec_coarse.jsonl",
        root / "data" / "trec_coarse.jsonl",
    ]

    for path in candidates:
        if path.is_file():
            return path

    raise FileNotFoundError(
        f"Could not find trec_coarse split under OOLONG root {root!s}. "
        "Set OOLONG_ROOT to the OOLONG checkout and ensure trec_coarse.jsonl exists."
    )


def load_trec_coarse(
    oolong_root: Path | None = None,
) -> Generator[OolongExample, None, None]:
    """
    Yield OOLONG trec_coarse examples as OolongExample instances.

    Each record is exposed as a (task_id, input_context, query, ground_truth) tuple
    via the OolongExample dataclass, matching the docs/to_do.md Phase 1.1 schema.
    """

    root = oolong_root or _default_oolong_root()
    data_path = _trec_coarse_path(root)

    with data_path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue

            record = json.loads(line)

            # The exact schema in the upstream repo may evolve; we handle a few
            # obvious variants and fail loudly if none match.
            #
            # Expected canonical fields (based on OOLONG paper and repo):
            #   - "context": long input text (e.g. question dataset chunk)
            #   - "question": the query for this item
            #   - "answer": ground truth label / value
            #
            # Some variants may use "input" / "query" / "label".

            context = (
                record.get("context")
                or record.get("input_context")
                or record.get("input")
                or ""
            )
            query = record.get("question") or record.get("query") or ""
            ground_truth = (
                record.get("answer")
                or record.get("ground_truth")
                or record.get("label")
                or ""
            )

            if not query:
                raise ValueError(
                    f"Missing query/question field in trec_coarse record #{idx}"
                )

            if ground_truth == "":
                raise ValueError(
                    f"Missing ground_truth/answer field in trec_coarse record #{idx}"
                )

            task_id = str(record.get("id", f"trec_coarse_{idx}"))

            yield OolongExample(
                task_id=task_id,
                input_context=context,
                query=query,
                ground_truth=str(ground_truth),
            )


def iter_trec_coarse_tuples(
    oolong_root: Path | None = None,
) -> Iterable[Tuple[str, str, str, str]]:
    """
    Convenience wrapper that yields plain tuples:

        (task_id, input_context, query, ground_truth)

    This matches the interface described in docs/to_do.md Phase 1.1 and keeps
    the benchmarks/oolong/runner.py script free of dataclass imports.
    """

    for ex in load_trec_coarse(oolong_root=oolong_root):
        yield ex.task_id, ex.input_context, ex.query, ex.ground_truth

