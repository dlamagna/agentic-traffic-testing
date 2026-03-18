#!/usr/bin/env python3
"""
compare_discussion_structures.py
=================================
Compare LLM request interarrival time (IAT) distributions between AgentVerse
horizontal and vertical discussion modes, using existing response.json data.

Each response.json contains stages.recruitment.communication_structure
("horizontal" or "vertical") and llm_requests[].start_time_utc timestamps.

Three output files are produced:
  horizontal_iat.png             — same 3×2 plot as plot_results.py but only horizontal runs
  vertical_iat.png               — same 3×2 plot as plot_results.py but only vertical runs
  horizontal_vs_vertical_iat.png — side-by-side comparison (histogram, ECDF, box, bar)

Usage:
    python compare_discussion_structures.py [DATA_DIR ...] \
        [--output-dir DIR] [--max-iat 180]

    DATA_DIR: one or more experiment root directories containing per-run
              subdirectories with response.json + meta.json.
              Defaults to the most recent data/runs/100_RUNS_* directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import warnings

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
except ImportError as exc:
    sys.exit(f"ERROR: missing dependency – {exc}\nInstall with: pip install matplotlib numpy")

try:
    from scipy import stats as scipy_stats
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

# ---------------------------------------------------------------------------
# Styling — matches plot_results.py
# ---------------------------------------------------------------------------
DARK_BG  = "white"
PANEL_BG = "#f7f7f7"
GRID_COL = "#cccccc"
TEXT_COL = "#222222"

IAT_MAX_S = 180   # hard cap on x-axis, matches plot_results.py

PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
    "#bcbd22", "#17becf",
]

# Fixed colours for the two structures in comparison plots
H_COLOR = "#1f77b4"   # blue   → horizontal
V_COLOR = "#ff7f0e"   # orange → vertical

# Label prefixes that belong to the Stage-2 collaborative discussion.
# All other stages (recruitment, execution, evaluation, synthesis, final output)
# are excluded when --filter-discussion is active.
DISCUSSION_LABEL_PREFIXES = (
    "horizontal_discussion",   # horizontal rounds
    "synthesize_discussion",   # horizontal synthesis
    "vertical_solver",         # vertical solver proposals
    "vertical_reviewer",       # vertical reviewer critiques
)

plt.rcParams.update({
    "figure.facecolor": DARK_BG,
    "axes.facecolor":   PANEL_BG,
    "axes.edgecolor":   GRID_COL,
    "axes.labelcolor":  TEXT_COL,
    "xtick.color":      TEXT_COL,
    "ytick.color":      TEXT_COL,
    "text.color":       TEXT_COL,
})


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class StructureData:
    """All timestamp data for one communication structure."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.by_task:   dict[str, list[float]] = defaultdict(list)   # task_slug → timestamps
        self.by_source: dict[str, list[float]] = defaultdict(list)   # source    → timestamps
        self.all_ts:    list[float] = []                              # globally sorted
        self.run_iats:  list[np.ndarray] = []                        # within-run IATs
        self.n_runs:    int = 0
        # Per-run timestamp groups for scatter normalization (each entry = one run)
        self.runs_by_task:   dict[str, list[list[float]]] = defaultdict(list)
        self.runs_by_source: dict[str, list[list[float]]] = defaultdict(list)

    def add_run(self, task_slug: str, timestamps: list[float], requests: list[dict]) -> None:
        """Ingest one run's data."""
        self.n_runs += 1
        run_by_source: dict[str, list[float]] = defaultdict(list)
        for ts, req in zip(timestamps, requests):
            source = req.get("source", "unknown")
            self.by_task[task_slug].append(ts)
            self.by_source[source].append(ts)
            self.all_ts.append(ts)
            run_by_source[source].append(ts)
        if timestamps:
            self.runs_by_task[task_slug].append(sorted(timestamps))
        for src, src_ts in run_by_source.items():
            self.runs_by_source[src].append(sorted(src_ts))
        if len(timestamps) >= 2:
            sorted_ts = sorted(timestamps)
            self.run_iats.append(np.diff(np.array(sorted_ts)))

    def sort(self) -> None:
        for d in (self.by_task, self.by_source):
            for k in d:
                d[k].sort()
        self.all_ts.sort()

    @property
    def pooled_run_iats(self) -> np.ndarray:
        """All within-run IATs concatenated."""
        return np.concatenate(self.run_iats) if self.run_iats else np.array([])

    @property
    def global_iats(self) -> np.ndarray:
        """IATs over the globally sorted stream (mirrors plot_results.py view)."""
        return np.diff(np.array(self.all_ts)) if len(self.all_ts) >= 2 else np.array([])


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _resolve_data_dirs(cli_dirs: list[str]) -> list[Path]:
    if cli_dirs:
        paths = [Path(d) for d in cli_dirs]
        for p in paths:
            if not p.is_dir():
                sys.exit(f"ERROR: directory not found: {p}")
        return paths
    repo_root = Path(__file__).resolve().parents[2]
    runs_root = repo_root / "data" / "runs"
    candidates = sorted(runs_root.glob("100_RUNS_*"), reverse=True)
    if not candidates:
        candidates = sorted(runs_root.glob("experiment_*"), reverse=True)
    if not candidates:
        sys.exit(f"ERROR: no experiment directories found under {runs_root}")
    chosen = candidates[0]
    print(f"[info] auto-selected dataset: {chosen}")
    return [chosen]


def load_runs(data_dirs: list[Path], filter_discussion: bool = True) -> tuple[
    StructureData,               # horizontal
    StructureData,               # vertical
    dict[str, dict[str, int]],  # counts_by_task[slug][structure]
    int,                         # n_skipped
]:
    horiz = StructureData("horizontal")
    vert  = StructureData("vertical")
    counts_by_task: dict[str, dict[str, int]] = defaultdict(lambda: {"horizontal": 0, "vertical": 0})
    n_skipped = 0

    for data_dir in data_dirs:
        for run_dir in sorted(data_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            resp_path = run_dir / "response.json"
            if not resp_path.exists():
                continue

            try:
                data = json.loads(resp_path.read_text())
            except Exception:
                n_skipped += 1
                continue

            stages = data.get("stages", {})
            structure = (
                stages.get("recruitment", {}).get("communication_structure")
                or stages.get("decision", {}).get("structure_used")
            )
            if not isinstance(structure, str):
                n_skipped += 1
                continue
            structure = structure.lower()
            if structure not in ("horizontal", "vertical"):
                n_skipped += 1
                continue

            # task slug
            task_slug = "unknown"
            meta_path = run_dir / "meta.json"
            if meta_path.exists():
                try:
                    task_slug = json.loads(meta_path.read_text()).get("task_slug", "unknown") or "unknown"
                except Exception:
                    pass
            if task_slug == "unknown":
                parts = run_dir.name.split("_")
                if len(parts) >= 4:
                    task_slug = "_".join(parts[2:-1])

            # timestamps + request records in sync
            timestamps: list[float] = []
            requests: list[dict] = []
            for req in data.get("llm_requests", []):
                ts_str = req.get("start_time_utc")
                if not ts_str:
                    continue
                if filter_discussion:
                    label = req.get("label", "")
                    if not any(label.startswith(p) for p in DISCUSSION_LABEL_PREFIXES):
                        continue
                try:
                    ts = datetime.fromisoformat(ts_str).timestamp()
                    timestamps.append(ts)
                    requests.append(req)
                except Exception:
                    continue

            bucket = horiz if structure == "horizontal" else vert
            bucket.add_run(task_slug, timestamps, requests)
            counts_by_task[task_slug][structure] += 1

    horiz.sort()
    vert.sort()
    return horiz, vert, dict(counts_by_task), n_skipped


# ---------------------------------------------------------------------------
# Shared plot helpers
# ---------------------------------------------------------------------------

def _hist_kde(ax: plt.Axes, vals: np.ndarray, color: str, label: str, max_iat: float) -> None:
    clipped = vals[vals <= max_iat]
    n_over = len(vals) - len(clipped)
    lbl = label if n_over == 0 else f"{label} ({n_over} > {max_iat:.0f}s clipped)"
    ax.hist(clipped, bins=35, density=True, alpha=0.35, color=color, label=lbl)
    if SCIPY_AVAILABLE and len(clipped) > 5:
        kde = scipy_stats.gaussian_kde(clipped)
        xs = np.linspace(0, max_iat, 400)
        ax.plot(xs, kde(xs), color=color, linewidth=2.2)
    ax.set_xlim(0, max_iat)


def _annotate_pct(ax: plt.Axes, vals: np.ndarray, color: str, row_offset: int = 0) -> None:
    for pct, ls in [(50, "--"), (95, ":")]:
        pval = np.percentile(vals, pct)
        ax.axvline(pval, color=color, linestyle=ls, alpha=0.6, linewidth=1)
        ax.text(pval, 0.02 + row_offset * 0.07, f"p{pct}={pval:.2f}s",
                color=color, fontsize=6, ha="left")


def _ecdf_line(ax: plt.Axes, vals: np.ndarray, color: str, label: str, max_iat: float,
               row_offset: int = 0) -> None:
    clipped = np.sort(vals[vals <= max_iat])
    if len(clipped) == 0:
        return
    y = np.arange(1, len(clipped) + 1) / len(clipped)
    ax.plot(clipped, y, color=color, linewidth=2, label=label)
    _annotate_pct(ax, clipped, color, row_offset)


# ---------------------------------------------------------------------------
# Plot 1 & 2: per-structure 3×2 (mirrors plot_results.py interarrival plots)
# ---------------------------------------------------------------------------

def plot_structure_iat(sd: StructureData, output_path: Path, max_iat: float,
                       filter_discussion: bool = True) -> None:
    """Produce the same 3×2 layout as plot_interarrival_from_responses() for one structure."""
    if not sd.all_ts:
        print(f"[warn] no data for structure '{sd.name}' — skipping {output_path.name}")
        return

    by_source = sd.by_source
    all_ts    = sd.all_ts

    # Compute IATs within each run then concatenate — avoids inter-run gaps
    # (hours apart) stretching the axes to thousands of seconds.
    iat_by_task: dict[str, np.ndarray] = {}
    for task, run_list in sd.runs_by_task.items():
        parts = [np.diff(np.array(r)) for r in run_list if len(r) >= 2]
        if parts:
            iat_by_task[task] = np.concatenate(parts)

    iat_by_source: dict[str, np.ndarray] = {}
    for src, run_list in sd.runs_by_source.items():
        parts = [np.diff(np.array(r)) for r in run_list if len(r) >= 2]
        if parts:
            iat_by_source[src] = np.concatenate(parts)

    # Global pooled within-run IATs (LLM-server view, no inter-run gaps)
    global_iats = sd.pooled_run_iats

    tasks   = sorted(iat_by_task.keys())
    sources = sorted(iat_by_source.keys())

    fig, axes = plt.subplots(3, 2, figsize=(16, 14))
    fig.patch.set_facecolor(DARK_BG)
    scope = "discussion stage only" if filter_discussion else "all workflow stages"
    fig.suptitle(
        f"LLM Interarrival Time Distribution — {sd.name.upper()} discussion  [{scope}]"
        f"\n({sd.n_runs} runs, {len(all_ts)} LLM requests)  "
        "source: response.json request timestamps",
        fontsize=11, color=TEXT_COL, fontweight="bold",
    )

    # ── Row 0 left: arrival timeline by task (normalized per-run) ────────
    # Each run's timestamps are zeroed to that run's first request so that
    # runs separated by hours do not stretch the x-axis.
    ax = axes[0][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Request Arrival Times by task (time within each run)", loc="left", fontsize=9)
    ax.set_ylabel("task")
    ax.set_xlabel("time within run (s)")
    ax.grid(True, color=GRID_COL, linewidth=0.5)
    max_span = 0.0
    for i, task in enumerate(tasks):
        color = PALETTE[i % len(PALETTE)]
        first = True
        for run_ts in sd.runs_by_task[task]:
            arr = np.array(run_ts)
            rel = arr - arr[0]
            max_span = max(max_span, rel[-1] if len(rel) else 0)
            ax.scatter(rel, [task] * len(rel), s=6, color=color, alpha=0.55,
                       label=task if first else "_")
            first = False
    ax.set_xlim(0, max(max_span * 1.05, 1.0))
    ax.legend(fontsize=6, loc="upper left")

    # ── Row 0 right: arrival timeline by source (normalized per-run) ─────
    ax = axes[0][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Request Arrival Times by source (time within each run)", loc="left", fontsize=9)
    ax.set_ylabel("source")
    ax.set_xlabel("time within run (s)")
    ax.grid(True, color=GRID_COL, linewidth=0.5)
    max_span = 0.0
    for i, src in enumerate(sources):
        color = PALETTE[i % len(PALETTE)]
        first = True
        for run_ts in sd.runs_by_source[src]:
            arr = np.array(run_ts)
            rel = arr - arr[0]
            max_span = max(max_span, rel[-1] if len(rel) else 0)
            ax.scatter(rel, [src] * len(rel), s=6, color=color, alpha=0.55,
                       label=src if first else "_")
            first = False
    ax.set_xlim(0, max(max_span * 1.05, 1.0))
    ax.legend(fontsize=6, loc="upper left")

    # ── Row 1 left: per-task IAT histogram + KDE ─────────────────────────
    ax = axes[1][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Interarrival Time Histogram (per task)", loc="left", fontsize=9)
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("density")
    ax.grid(True, color=GRID_COL, linewidth=0.5)
    for i, task in enumerate(tasks):
        _hist_kde(ax, iat_by_task[task], PALETTE[i % len(PALETTE)],
                  f"{task} (n={len(iat_by_task[task])})", max_iat)
    ax.legend(fontsize=7)

    # ── Row 1 right: per-source IAT histogram + KDE ──────────────────────
    ax = axes[1][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Interarrival Time Histogram (per agent source)", loc="left", fontsize=9)
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("density")
    ax.grid(True, color=GRID_COL, linewidth=0.5)
    for i, src in enumerate(sources):
        _hist_kde(ax, iat_by_source[src], PALETTE[i % len(PALETTE)],
                  f"{src} (n={len(iat_by_source[src])})", max_iat)
    ax.legend(fontsize=7)

    # ── Row 2 left: per-task ECDF ────────────────────────────────────────
    ax = axes[2][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Interarrival Time ECDF (per task)", loc="left", fontsize=9)
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("P(X ≤ x)")
    ax.set_ylim(0, 1.05)
    ax.set_xlim(0, max_iat)
    ax.grid(True, color=GRID_COL, linewidth=0.5)
    for i, task in enumerate(tasks):
        vals = np.sort(iat_by_task[task])
        ax.plot(vals, np.arange(1, len(vals) + 1) / len(vals),
                label=f"{task} (n={len(vals)})",
                color=PALETTE[i % len(PALETTE)], linewidth=2)
        _annotate_pct(ax, vals, PALETTE[i % len(PALETTE)], row_offset=i)
    ax.legend(fontsize=7)

    # ── Row 2 right: global IAT histogram + KDE ──────────────────────────
    ax = axes[2][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title(
        f"Global Interarrival Time — all agents, all tasks (n={len(global_iats)})",
        loc="left", fontsize=9,
    )
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("density")
    ax.grid(True, color=GRID_COL, linewidth=0.5)
    agg_color = H_COLOR if sd.name == "horizontal" else V_COLOR
    global_clipped = global_iats[global_iats <= max_iat]
    n_over = len(global_iats) - len(global_clipped)
    lbl = f"global (n={len(global_iats)}" + (f", {n_over} clipped)" if n_over else ")")
    ax.hist(global_clipped, bins=40, density=True, alpha=0.5, color=agg_color, label=lbl)
    if SCIPY_AVAILABLE and len(global_clipped) > 5:
        kde = scipy_stats.gaussian_kde(global_clipped)
        xs = np.linspace(0, max_iat, 400)
        ax.plot(xs, kde(xs), color=agg_color, linewidth=2.5, label="KDE")
    ax.set_xlim(0, max_iat)
    for pct, ls in [(50, "--"), (95, ":"), (99, "-.")]:
        pval = np.percentile(global_iats, pct)
        ax.axvline(pval, color=TEXT_COL, linestyle=ls, alpha=0.7, linewidth=1.2)
        ax.text(pval, 0, f"p{pct}={pval:.2f}s", color=TEXT_COL, fontsize=7,
                ha="left", transform=ax.get_xaxis_transform(), va="bottom")
    ax.legend(fontsize=7)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        plt.tight_layout(rect=[0, 0, 1, 0.93])
    fig.savefig(output_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"[info] saved plot → {output_path}")


# ---------------------------------------------------------------------------
# Plot 3: side-by-side comparison 2×2
# ---------------------------------------------------------------------------

def plot_comparison(
    horiz: StructureData,
    vert: StructureData,
    counts_by_task: dict[str, dict[str, int]],
    output_path: Path,
    max_iat: float,
    filter_discussion: bool = True,
) -> None:
    """2×2 comparison: histogram, ECDF, box plot, and run breakdown by task."""
    h_pool = horiz.pooled_run_iats   # within-run IATs (no inter-run gaps)
    v_pool = vert.pooled_run_iats

    fig, axes = plt.subplots(2, 2, figsize=(16, 11))
    fig.patch.set_facecolor(DARK_BG)
    scope = "discussion stage only" if filter_discussion else "all workflow stages"
    fig.suptitle(
        f"AgentVerse — Horizontal vs Vertical Discussion: LLM Request IAT Comparison  [{scope}]\n"
        f"(within-run IATs  |  horizontal: {horiz.n_runs} runs, {len(horiz.all_ts)} requests  "
        f"|  vertical: {vert.n_runs} runs, {len(vert.all_ts)} requests)",
        fontsize=11, color=TEXT_COL, fontweight="bold",
    )

    # ── [0,0] Histogram + KDE ─────────────────────────────────────────────
    ax = axes[0][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("IAT Histogram + KDE (within-run)", loc="left", fontsize=9)
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("density")
    ax.grid(True, color=GRID_COL, linewidth=0.5)
    if len(h_pool):
        _hist_kde(ax, h_pool, H_COLOR, f"horizontal (n={len(h_pool)} IATs)", max_iat)
    if len(v_pool):
        _hist_kde(ax, v_pool, V_COLOR, f"vertical (n={len(v_pool)} IATs)", max_iat)
    ax.legend(fontsize=8)

    # ── [0,1] ECDF ────────────────────────────────────────────────────────
    ax = axes[0][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("ECDF — dashed=p50, dotted=p95", loc="left", fontsize=9)
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("P(X ≤ x)")
    ax.set_ylim(0, 1.05)
    ax.set_xlim(0, max_iat)
    ax.grid(True, color=GRID_COL, linewidth=0.5)
    if len(h_pool):
        _ecdf_line(ax, h_pool, H_COLOR, f"horizontal (n={len(h_pool)})", max_iat, row_offset=0)
    if len(v_pool):
        _ecdf_line(ax, v_pool, V_COLOR, f"vertical (n={len(v_pool)})", max_iat, row_offset=1)
    ax.legend(fontsize=8)

    # ── [1,0] Box plot — horizontal vs vertical ───────────────────────────
    ax = axes[1][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("IAT Box Plot (within-run, clipped at max-iat)", loc="left", fontsize=9)
    ax.set_xlabel("interarrival time (s)")
    ax.grid(True, color=GRID_COL, linewidth=0.5, axis="x")

    data_groups, labels_bp, colors_bp = [], [], []
    for sd, color in [(horiz, H_COLOR), (vert, V_COLOR)]:
        pool = sd.pooled_run_iats
        if len(pool):
            clipped = pool[pool <= max_iat]
            data_groups.append(clipped)
            labels_bp.append(f"{sd.name}\n(n={len(clipped)} IATs, {sd.n_runs} runs)")
            colors_bp.append(color)

    if data_groups:
        positions = list(range(1, len(data_groups) + 1))
        bp = ax.boxplot(
            data_groups, positions=positions, vert=False, patch_artist=True,
            widths=0.5, flierprops=dict(marker=".", markersize=2, alpha=0.3),
        )
        for patch, color in zip(bp["boxes"], colors_bp):
            patch.set_facecolor(color)
            patch.set_alpha(0.55)
        for median in bp["medians"]:
            median.set_color(TEXT_COL)
            median.set_linewidth(1.5)
        ax.set_yticks(positions)
        ax.set_yticklabels(labels_bp, fontsize=8)
        ax.set_xlim(0, max_iat)
        ax.set_ylabel("")

    # ── [1,1] Stacked bar: run counts by task × structure ─────────────────
    ax = axes[1][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Run Count by Task × Structure", loc="left", fontsize=9)
    ax.set_xlabel("number of runs")
    ax.set_ylabel("task")
    ax.grid(True, color=GRID_COL, linewidth=0.5, axis="x")

    task_order = sorted(counts_by_task.keys())
    if task_order:
        h_counts = [counts_by_task[t].get("horizontal", 0) for t in task_order]
        v_counts = [counts_by_task[t].get("vertical",   0) for t in task_order]
        y_pos = np.arange(len(task_order))
        ax.barh(y_pos, h_counts, color=H_COLOR, alpha=0.7, label="horizontal")
        ax.barh(y_pos, v_counts, left=h_counts, color=V_COLOR, alpha=0.7, label="vertical")
        for i, (hc, vc) in enumerate(zip(h_counts, v_counts)):
            if hc > 0:
                ax.text(hc / 2, i, str(hc), ha="center", va="center",
                        fontsize=7, color="white", fontweight="bold")
            if vc > 0:
                ax.text(hc + vc / 2, i, str(vc), ha="center", va="center",
                        fontsize=7, color="white", fontweight="bold")
        ax.set_yticks(y_pos)
        ax.set_yticklabels(task_order, fontsize=8)
        ax.legend(fontsize=8)

    plt.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig(output_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"[info] saved plot → {output_path}")


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def print_summary(horiz: StructureData, vert: StructureData, max_iat: float) -> None:
    print()
    print("=" * 72)
    print("  Discussion Structure — IAT Summary  (within-run IATs)")
    print("=" * 72)
    print(f"{'Structure':<14} {'Runs':>6} {'IAT samples':>12} {'Mean (s)':>10} "
          f"{'Median (s)':>11} {'p95 (s)':>9}")
    print("-" * 72)
    for sd in (horiz, vert):
        pool = sd.pooled_run_iats
        if len(pool) == 0:
            print(f"{sd.name:<14} {sd.n_runs:>6} {'—':>12} {'—':>10} {'—':>11} {'—':>9}")
            continue
        clipped = pool[pool <= max_iat]
        print(f"{sd.name:<14} {sd.n_runs:>6} {len(pool):>12} "
              f"{np.mean(clipped):>10.2f} {np.median(clipped):>11.2f} "
              f"{np.percentile(clipped, 95):>9.2f}")
    print("=" * 72)
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "data_dirs", nargs="*", metavar="DATA_DIR",
        help="Experiment root dir(s) (defaults to most recent data/runs/100_RUNS_*)",
    )
    parser.add_argument(
        "--output-dir", metavar="DIR",
        help="Where to write PNGs (defaults to first DATA_DIR)",
    )
    parser.add_argument(
        "--max-iat", type=float, default=float(IAT_MAX_S), metavar="SECONDS",
        help=f"X-axis clip for IAT plots in seconds (default: {IAT_MAX_S})",
    )
    parser.add_argument(
        "--filter-discussion", action=argparse.BooleanOptionalAction, default=True,
        help="Only include Stage-2 discussion LLM requests (horizontal_discussion_*, "
             "synthesize_discussion, vertical_solver_*, vertical_reviewer_*). "
             "Use --no-filter-discussion to include all workflow stages. (default: True)",
    )
    args = parser.parse_args()

    data_dirs = _resolve_data_dirs(args.data_dirs)
    output_dir = Path(args.output_dir) if args.output_dir else data_dirs[0]
    output_dir.mkdir(parents=True, exist_ok=True)

    filter_discussion: bool = args.filter_discussion
    suffix = "_discussion" if filter_discussion else ""
    print(f"[info] filter_discussion={filter_discussion}  "
          f"({'discussion stage only' if filter_discussion else 'all workflow stages'})")
    print(f"[info] loading runs from {len(data_dirs)} director{'y' if len(data_dirs)==1 else 'ies'} …")
    horiz, vert, counts_by_task, n_skipped = load_runs(data_dirs, filter_discussion)

    total = horiz.n_runs + vert.n_runs
    if total == 0:
        sys.exit("ERROR: no valid runs found — check that response.json files exist with "
                 "stages.recruitment.communication_structure set")

    print(f"[info] loaded {total} runs  "
          f"(horizontal={horiz.n_runs}, vertical={vert.n_runs}, skipped={n_skipped})")

    print_summary(horiz, vert, args.max_iat)

    # Per-structure 3×2 plots (mirrors plot_results.py format)
    plot_structure_iat(horiz, output_dir / f"horizontal{suffix}_iat.png",
                       args.max_iat, filter_discussion)
    plot_structure_iat(vert,  output_dir / f"vertical{suffix}_iat.png",
                       args.max_iat, filter_discussion)

    # Side-by-side comparison 2×2 plot
    plot_comparison(horiz, vert, counts_by_task,
                    output_dir / f"horizontal_vs_vertical{suffix}_iat.png",
                    args.max_iat, filter_discussion)


if __name__ == "__main__":
    main()
