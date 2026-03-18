from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

# ---------------------------------------------------------------------------
# AgentBench task type constants
# ---------------------------------------------------------------------------

TASK_TYPE_OS = "os"
TASK_TYPE_DB = "db"
TASK_TYPE_KG = "kg"
TASK_TYPE_AF = "af"
TASK_TYPE_WS = "ws"

# Maps task_type → controller task name (used with --agentbench-url)
_CONTROLLER_TASK_NAMES: Dict[str, str] = {
    TASK_TYPE_OS: "os-std",
    TASK_TYPE_DB: "dbbench-std",
    TASK_TYPE_KG: "kg-std",
}

# Tool definitions per task type (OpenAI function-calling format)
_OS_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash_action",
            "description": "Execute bash code to perform an operation in the Linux environment.",
            "parameters": {
                "type": "object",
                "properties": {"script": {"type": "string", "description": "The bash script to execute."}},
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_action",
            "description": "Indicate that the task has been finished.",
            "parameters": {
                "type": "object",
                "properties": {"thought": {"type": "string", "description": "Reason indicating the task is finished."}},
                "required": ["thought"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "answer_action",
            "description": "Provide the answer to the question.",
            "parameters": {
                "type": "object",
                "properties": {"answer": {"type": "string", "description": "The answer to the question."}},
                "required": ["answer"],
            },
        },
    },
]

_DB_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "execute_sql",
            "description": "Execute a SQL query on the database and return the result.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The SQL query to execute."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commit_final_answer",
            "description": "Commit the final answer after all operations are complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "answers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The list of final answer values.",
                    }
                },
                "required": ["answers"],
            },
        },
    },
]


def controller_task_name(task_type: str) -> str:
    """Return the AgentBench controller task name for a given task_type."""
    return _CONTROLLER_TASK_NAMES.get(task_type, task_type)


def tools_for(task_type: str) -> List[Dict[str, Any]]:
    """Return OpenAI-format tool definitions for the given task type."""
    if task_type == TASK_TYPE_OS:
        return _OS_TOOLS
    if task_type == TASK_TYPE_DB:
        return _DB_TOOLS
    return []


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AgentBenchTask:
    """
    Canonical representation of a single AgentBench task for this testbed.

    ``ground_truth`` is serialised to a string for uniform handling, but
    the raw record is preserved in ``raw`` for scorer use.
    ``tools`` are the OpenAI-format function definitions for this task type.
    """

    task_id: str
    task_type: str                        # "os", "db", "kg"
    description: str                      # human-readable task prompt
    tools: List[Dict[str, Any]]           # OpenAI tool definitions
    ground_truth: str                     # serialised expected answer
    raw: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _load_os_tasks(
    agentbench_root: Path,
    max_tasks: Optional[int],
) -> Generator[AgentBenchTask, None, None]:
    """Yield OS interaction tasks from the AgentBench data directory."""
    data_root = agentbench_root / "data" / "os_interaction" / "data"
    if not data_root.exists():
        raise FileNotFoundError(f"OS data directory not found: {data_root}")

    task_idx = 0
    for subset_dir in sorted(data_root.iterdir()):
        if not subset_dir.is_dir():
            continue
        for json_file in sorted(subset_dir.glob("*.json")):
            with open(json_file, encoding="utf-8") as f:
                records = json.load(f)
            if isinstance(records, dict):
                records = [records]
            for item_idx, record in enumerate(records):
                if max_tasks is not None and task_idx >= max_tasks:
                    return
                eval_info = record.get("evaluation", {})
                if "match" in eval_info:
                    match = eval_info["match"]
                    gt = match.get("answer", "") if isinstance(match, dict) else str(match)
                elif "check" in eval_info:
                    gt = "<check_script>"
                else:
                    gt = ""

                yield AgentBenchTask(
                    task_id=f"os-{subset_dir.name}-{json_file.stem}-{item_idx}",
                    task_type=TASK_TYPE_OS,
                    description=record.get("description", ""),
                    tools=_OS_TOOLS,
                    ground_truth=gt,
                    raw=record,
                )
                task_idx += 1


def _format_db_table(record: Dict[str, Any]) -> str:
    """Format the DB task table as a readable text block."""
    tables = record.get("table", [])
    if isinstance(tables, dict):
        tables = [tables]
    lines = []
    for table in tables:
        name = table.get("table_name", "table")
        info = table.get("table_info", {})
        cols = [c["name"] for c in info.get("columns", [])]
        rows = info.get("rows", [])
        lines.append(f"Table: {name}")
        lines.append(" | ".join(cols))
        lines.append("-" * max(len(" | ".join(cols)), 20))
        for row in rows[:50]:  # cap at 50 rows to avoid huge prompts
            lines.append(" | ".join(str(v) for v in row))
        if len(rows) > 50:
            lines.append(f"... ({len(rows) - 50} more rows)")
    return "\n".join(lines)


def _load_db_tasks(
    agentbench_root: Path,
    split: str,
    max_tasks: Optional[int],
) -> Generator[AgentBenchTask, None, None]:
    """Yield DB tasks from the AgentBench data directory."""
    filename = "standard.jsonl" if split == "standard" else f"{split}.jsonl"
    data_file = agentbench_root / "data" / "dbbench" / filename
    if not data_file.exists():
        raise FileNotFoundError(f"DB data file not found: {data_file}")

    with open(data_file, encoding="utf-8") as f:
        lines = f.readlines()

    for idx, line in enumerate(lines):
        if max_tasks is not None and idx >= max_tasks:
            break
        record = json.loads(line)
        task_type_list = record.get("type", ["SELECT"])
        ans_key = "answer_md5" if task_type_list[0] in ("INSERT", "DELETE", "UPDATE") else "label"
        gt = record.get(ans_key, [])
        if isinstance(gt, list):
            gt_str = json.dumps(gt)
        else:
            gt_str = str(gt)

        # Build description: table + question
        table_text = _format_db_table(record)
        description = record.get("description", "")
        evidence = record.get("evidence", "")
        if evidence:
            description = f"Evidence: {evidence}\n{description}"

        yield AgentBenchTask(
            task_id=f"db-{split}-{idx}",
            task_type=TASK_TYPE_DB,
            description=description,
            tools=_DB_TOOLS,
            ground_truth=gt_str,
            raw={**record, "_table_text": table_text, "_answer_key": ans_key},
        )


def _load_kg_tasks(
    agentbench_root: Path,
    split: str,
    max_tasks: Optional[int],
) -> Generator[AgentBenchTask, None, None]:
    """Yield KG tasks from the AgentBench data directory."""
    filename = "std.json" if split == "standard" else f"{split}.json"
    data_file = agentbench_root / "data" / "knowledgegraph" / filename
    if not data_file.exists():
        raise FileNotFoundError(f"KG data file not found: {data_file}")

    with open(data_file, encoding="utf-8") as f:
        records = json.load(f)

    for idx, record in enumerate(records):
        if max_tasks is not None and idx >= max_tasks:
            break
        answer_set = set()
        for a in record.get("answer", []):
            answer_set.add(a["answer_argument"])
        gt_str = json.dumps(sorted(answer_set))

        question = record.get("question", "")
        entities = record.get("entities", {})
        entity_str = ", ".join(f"{name} ({eid})" for name, eid in entities.items())
        description = f"{question}\nEntities: {entity_str}"

        yield AgentBenchTask(
            task_id=f"kg-{split}-{idx}",
            task_type=TASK_TYPE_KG,
            description=description,
            tools=[],  # KG tools are SPARQL-specific, handled by controller
            ground_truth=gt_str,
            raw={**record, "_answer_set": sorted(answer_set)},
        )


def load_tasks(
    task_type: str,
    split: str = "standard",
    max_tasks: Optional[int] = None,
    agentbench_root: Optional[Path] = None,
) -> Generator[AgentBenchTask, None, None]:
    """
    Yield AgentBenchTask items from the local AgentBench data files.

    Args:
        task_type:       ``"os"``, ``"db"``, or ``"kg"``.
        split:           Dataset split: ``"standard"`` (test) or ``"train"``.
        max_tasks:       Optional cap on number of tasks yielded.
        agentbench_root: Path to the cloned AgentBench repo.
                         Falls back to the ``AGENTBENCH_ROOT`` env var, then
                         ``../AgentBench`` relative to the project root.
    """
    if agentbench_root is None:
        env_root = os.environ.get("AGENTBENCH_ROOT")
        if env_root:
            agentbench_root = Path(env_root)
        else:
            # default: sibling directory
            agentbench_root = Path(__file__).parents[3] / ".." / "AgentBench"
    agentbench_root = Path(agentbench_root).resolve()

    if not agentbench_root.exists():
        raise FileNotFoundError(
            f"AgentBench root not found at {agentbench_root}. "
            "Set AGENTBENCH_ROOT env var or pass --agentbench-root."
        )

    if task_type == TASK_TYPE_OS:
        yield from _load_os_tasks(agentbench_root, max_tasks)
    elif task_type == TASK_TYPE_DB:
        yield from _load_db_tasks(agentbench_root, split, max_tasks)
    elif task_type == TASK_TYPE_KG:
        yield from _load_kg_tasks(agentbench_root, split, max_tasks)
    else:
        raise ValueError(
            f"Unknown task_type {task_type!r}. "
            f"Supported: {TASK_TYPE_OS}, {TASK_TYPE_DB}, {TASK_TYPE_KG}"
        )
