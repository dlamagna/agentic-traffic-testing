"""
_common.py
==========
Shared constants and utilities for agentverse experiment analysis scripts.
"""

from pathlib import Path

# Name of the subdirectory within an experiment folder that holds per-task run dirs.
TASKS_SUBDIR = "tasks"


def _tasks_dir(experiment_dir: Path) -> Path:
    """Return the tasks subdirectory if present (new layout), else experiment_dir (old layout)."""
    tasks = experiment_dir / TASKS_SUBDIR
    return tasks if tasks.is_dir() else experiment_dir
