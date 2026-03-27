"""
MARBLE (MultiAgentBench) task loader.

Reads tasks from the MARBLE repository's multiagentbench/ JSONL files and
yields them as structured dataclasses.  Each JSONL line is one complete
benchmark instance containing the task content, agent definitions with
profiles, relationship graph, and evaluation metrics.

The MARBLE repo is expected at ``MARBLE_ROOT`` (env) or ``../MARBLE``
relative to this project.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Repo discovery
# ---------------------------------------------------------------------------

_DEFAULT_MARBLE_ROOT = str(
    Path(__file__).resolve().parents[2].parent / "MARBLE"
)

MARBLE_ROOT = Path(os.environ.get("MARBLE_ROOT", _DEFAULT_MARBLE_ROOT))

SUPPORTED_DOMAINS = ("coding", "research", "bargaining", "database", "minecraft")

_DOMAIN_JSONL: Dict[str, str] = {
    "coding": "multiagentbench/coding/coding_main.jsonl",
    "research": "multiagentbench/research/research_main.jsonl",
    "bargaining": "multiagentbench/bargaining/bargaining_main.jsonl",
    "database": "multiagentbench/database/database_main.jsonl",
    "minecraft": "multiagentbench/minecraft/minecraft_main.jsonl",
}

# Default topologies per domain (used when coordinate_mode is empty in the
# JSONL, mirroring MARBLE's jsonl2yaml.py defaults).
_DEFAULT_TOPOLOGY: Dict[str, str] = {
    "coding": "graph",
    "research": "graph",
    "bargaining": "graph",
    "database": "graph",
    "minecraft": "star",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MarbleAgent:
    """One agent definition from a MARBLE task."""

    agent_id: str
    profile: str
    agent_type: str = "BaseAgent"


@dataclass
class MarbleTask:
    """
    Canonical representation of one MARBLE benchmark instance.

    Mirrors the JSONL schema from ``multiagentbench/<domain>/<domain>_main.jsonl``.
    """

    task_id: int
    domain: str
    task_content: str
    agents: List[MarbleAgent]
    relationships: List[Tuple[str, str, str]]
    coordinate_mode: str
    environment: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    engine_planner: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def agent_count(self) -> int:
        return len(self.agents)

    @property
    def agent_ids(self) -> List[str]:
        return [a.agent_id for a in self.agents]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_agents(raw_agents: List[Dict[str, Any]]) -> List[MarbleAgent]:
    out: List[MarbleAgent] = []
    for a in raw_agents:
        out.append(MarbleAgent(
            agent_id=a.get("agent_id", "unknown"),
            profile=a.get("profile", ""),
            agent_type=a.get("type", "BaseAgent"),
        ))
    return out


def _parse_relationships(raw_rels: List[Any]) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    for rel in raw_rels:
        if isinstance(rel, (list, tuple)) and len(rel) == 3:
            out.append((str(rel[0]), str(rel[1]), str(rel[2])))
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def marble_root() -> Path:
    """Return the resolved MARBLE repo root."""
    return MARBLE_ROOT


def available_domains() -> List[str]:
    """Return domains for which JSONL files exist in the MARBLE repo."""
    found: List[str] = []
    for domain, rel_path in _DOMAIN_JSONL.items():
        if (MARBLE_ROOT / rel_path).is_file():
            found.append(domain)
    return found


def load_marble_tasks(
    domain: str,
    max_tasks: Optional[int] = None,
    task_ids: Optional[List[int]] = None,
    topology_override: Optional[str] = None,
) -> Generator[MarbleTask, None, None]:
    """
    Yield :class:`MarbleTask` instances from a MARBLE domain.

    Args:
        domain: one of :data:`SUPPORTED_DOMAINS`.
        max_tasks: stop after yielding this many tasks (``None`` = all).
        task_ids: if set, yield only tasks whose ``task_id`` is in this list.
        topology_override: force this ``coordinate_mode`` on every task,
            ignoring the value in the JSONL.  Useful for running the same
            tasks across all four topologies.
    """
    if domain not in _DOMAIN_JSONL:
        raise ValueError(
            f"Unknown MARBLE domain '{domain}'. "
            f"Supported: {list(_DOMAIN_JSONL.keys())}"
        )

    jsonl_path = MARBLE_ROOT / _DOMAIN_JSONL[domain]
    if not jsonl_path.is_file():
        raise FileNotFoundError(
            f"MARBLE JSONL not found at {jsonl_path}. "
            f"Ensure the MARBLE repo is cloned at {MARBLE_ROOT} "
            "(set MARBLE_ROOT env var to override)."
        )

    default_topo = _DEFAULT_TOPOLOGY.get(domain, "graph")
    count = 0

    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)

            tid = int(obj.get("task_id", 0))
            if task_ids is not None and tid not in task_ids:
                continue

            coord = topology_override or obj.get("coordinate_mode") or default_topo

            task_dict = obj.get("task", {})
            content = task_dict.get("content", "") if isinstance(task_dict, dict) else str(task_dict)

            yield MarbleTask(
                task_id=tid,
                domain=domain,
                task_content=content,
                agents=_parse_agents(obj.get("agents", [])),
                relationships=_parse_relationships(obj.get("relationships", [])),
                coordinate_mode=coord,
                environment=obj.get("environment", {}),
                metrics=obj.get("metrics", {}),
                engine_planner=obj.get("engine_planner", {}),
                raw=obj,
            )

            count += 1
            if max_tasks is not None and count >= max_tasks:
                return


def task_count(domain: str) -> int:
    """Return the total number of tasks available for *domain*."""
    jsonl_path = MARBLE_ROOT / _DOMAIN_JSONL.get(domain, "")
    if not jsonl_path.is_file():
        return 0
    n = 0
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                n += 1
    return n
