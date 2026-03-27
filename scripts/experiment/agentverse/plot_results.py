#!/usr/bin/env python3
"""
plot_results.py
===============
Generate Grafana-style matplotlib plots from experiment CSV data.

Reads:
  - <experiment-dir>/metrics.csv          (aggregate, full experiment window)
  - <experiment-dir>/*/metrics.csv        (per-run, individual windows)
  - infra/monitoring/grafana/.../agentic-traffic.json  (panel layout / units)

Outputs (all under <experiment-dir>/plots/):
  - One PNG per dashboard row section      (e.g. 01_Overview.png)
  - interarrival_distribution.png          (key metric: comparison across tasks)
  - interarrival_ecdf.png                  (empirical CDF of inter-arrival times)
  - per_run_summary.png                    (summary across iterations)

Dependencies:
  pip install matplotlib pandas numpy scipy
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import _tasks_dir  # noqa: E402

# ---------------------------------------------------------------------------
# Guard: friendly error if matplotlib / pandas not installed
# ---------------------------------------------------------------------------
try:
    import matplotlib
    matplotlib.use("Agg")          # non-interactive backend
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    from matplotlib.gridspec import GridSpec
    import numpy as np
    import pandas as pd
except ImportError as exc:
    print(f"ERROR: missing dependency – {exc}\n"
          "Install with:  pip install matplotlib pandas numpy",
          file=sys.stderr)
    sys.exit(1)

try:
    from scipy import stats as scipy_stats
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

# ---------------------------------------------------------------------------
# White background style
# ---------------------------------------------------------------------------
DARK_BG   = "white"
PANEL_BG  = "#f7f7f7"
GRID_COL  = "#cccccc"
TEXT_COL  = "#222222"

# Hard cap on the interarrival-time x-axis — matches the LLM request timeout
IAT_MAX_S = 100
PALETTE   = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
    "#bcbd22", "#17becf",
]

plt.rcParams.update({
    "figure.facecolor":  DARK_BG,
    "axes.facecolor":    PANEL_BG,
    "axes.edgecolor":    GRID_COL,
    "axes.labelcolor":   TEXT_COL,
    "axes.prop_cycle":   plt.cycler(color=PALETTE),
    "xtick.color":       TEXT_COL,
    "ytick.color":       TEXT_COL,
    "text.color":        TEXT_COL,
    "grid.color":        GRID_COL,
    "grid.linestyle":    "--",
    "grid.alpha":        0.6,
    "legend.facecolor":  PANEL_BG,
    "legend.edgecolor":  GRID_COL,
    "legend.fontsize":   7,
    "font.size":         9,
    "axes.titlesize":    9,
    "axes.titlepad":     6,
    "figure.dpi":        120,
})

# ---------------------------------------------------------------------------
# Unit formatting helpers
# ---------------------------------------------------------------------------
UNIT_LABEL: dict[str, str] = {
    "short":       "",
    "s":           "seconds",
    "ms":          "milliseconds",
    "Bps":         "bytes / s",
    "pps":         "packets / s",
    "bytes":       "bytes",
    "percentunit": "fraction (0–1)",
    "percent":     "%",
}


def unit_label(unit: str) -> str:
    return UNIT_LABEL.get(unit, unit)


def _fmt_ts_axis(ax: plt.Axes, timestamps: pd.Series) -> None:
    """Format x-axis as HH:MM:SS relative time."""
    if timestamps.empty:
        return
    t0 = timestamps.min()
    ax.xaxis.set_major_formatter(
        mticker.FuncFormatter(
            lambda x, _: f"+{int(x - t0)}s" if (x - t0) >= 0 else ""
        )
    )
    ax.set_xlabel("time (relative)", fontsize=7)


# ---------------------------------------------------------------------------
# Dashboard panel loading (same logic as scrape_metrics.py)
# ---------------------------------------------------------------------------

def load_dashboard_panels(path: str | Path) -> list[dict]:
    with open(path) as f:
        dashboard = json.load(f)

    raw = dashboard.get("panels", [])
    # Sort by y then x so row headers always precede their child panels.
    raw_sorted = sorted(raw, key=lambda p: (p.get("gridPos", {}).get("y", 0),
                                            p.get("gridPos", {}).get("x", 0)))

    panels: list[dict] = []
    current_row = "General"

    for panel in raw_sorted:
        ptype = panel.get("type", "")
        if ptype == "row":
            current_row = panel.get("title", "General")
            continue
        targets = [t for t in panel.get("targets", []) if t.get("expr", "").strip()]
        if not targets:
            continue
        field_cfg = panel.get("fieldConfig", {}).get("defaults", {})
        panels.append({
            "id":      panel.get("id"),
            "title":   panel.get("title", f"Panel {panel.get('id')}"),
            "type":    ptype,
            "row":     current_row,
            "gridPos": panel.get("gridPos", {}),
            "unit":    field_cfg.get("unit", "short"),
            "targets": [
                {
                    "refId":        t.get("refId", "A"),
                    "expr":         t["expr"].strip(),
                    "legendFormat": t.get("legendFormat", ""),
                }
                for t in targets
            ],
        })
    return panels


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

def load_metrics_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if df.empty:
        return df
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df["value"]     = pd.to_numeric(df["value"],     errors="coerce")
    df.dropna(subset=["timestamp", "value"], inplace=True)
    return df


def load_all_run_csvs(experiment_dir: Path) -> pd.DataFrame:
    """Load and concatenate all per-run metrics.csv files."""
    frames: list[pd.DataFrame] = []
    for run_dir in sorted(_tasks_dir(experiment_dir).iterdir()):
        csv_path = run_dir / "metrics.csv"
        if csv_path.exists() and run_dir.is_dir():
            df = load_metrics_csv(csv_path)
            if not df.empty:
                frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ---------------------------------------------------------------------------
# Core plotting helpers
# ---------------------------------------------------------------------------

def _plot_timeseries_panel(
    ax: plt.Axes,
    df: pd.DataFrame,
    panel: dict,
) -> None:
    """Plot all targets of a timeseries panel on *ax*."""
    ax.set_title(panel["title"], loc="left", fontsize=8, pad=4)
    ax.grid(True)
    ul = unit_label(panel.get("unit", "short"))
    if ul:
        ax.set_ylabel(ul, fontsize=7)

    sub = df[df["panel_title"] == panel["title"]].copy()
    if sub.empty:
        ax.text(0.5, 0.5, "no data", ha="center", va="center",
                transform=ax.transAxes, color="#888", fontsize=8)
        return

    sub.sort_values("timestamp", inplace=True)

    # Group by (legend_format, labels) to get one line per series
    sub["_series_key"] = sub["legend_format"].fillna("") + "|" + sub["labels"].fillna("{}")

    for i, (key, grp) in enumerate(sub.groupby("_series_key")):
        legend_fmt, labels_json = key.split("|", 1)
        try:
            lbl_dict = json.loads(labels_json)
        except Exception:
            lbl_dict = {}

        # Resolve legendFormat template variables like {{label}}
        label = legend_fmt
        for k, v in lbl_dict.items():
            label = label.replace(f"{{{{{k}}}}}", v)
        if not label.strip():
            label = " / ".join(f"{k}={v}" for k, v in lbl_dict.items()) or "series"

        color = PALETTE[i % len(PALETTE)]
        ax.plot(
            grp["timestamp"], grp["value"],
            label=label, color=color, linewidth=1.2,
            alpha=0.9,
        )
        ax.fill_between(
            grp["timestamp"], grp["value"],
            alpha=0.12, color=color,
        )

    _fmt_ts_axis(ax, sub["timestamp"])
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(handles, labels, fontsize=6, loc="upper left",
                  framealpha=0.6, ncol=1)


def _plot_stat_panel(
    ax: plt.Axes,
    df: pd.DataFrame,
    panel: dict,
) -> None:
    """Display last-not-null value as a large stat (text)."""
    ax.set_title(panel["title"], loc="left", fontsize=8, pad=4)
    ax.axis("off")

    sub = df[df["panel_title"] == panel["title"]].copy()
    if sub.empty or sub["value"].dropna().empty:
        ax.text(0.5, 0.5, "—", ha="center", va="center",
                transform=ax.transAxes, fontsize=22, color="#888")
        return

    last_val = sub.sort_values("timestamp")["value"].dropna().iloc[-1]
    unit = panel.get("unit", "short")
    if unit == "percentunit":
        display = f"{last_val * 100:.1f}%"
    elif unit in ("s", "ms"):
        display = f"{last_val:.3f} {unit}"
    elif unit == "bytes":
        display = f"{last_val / 1e6:.2f} MB"
    elif unit == "Bps":
        display = f"{last_val / 1e3:.1f} kBps"
    else:
        display = f"{last_val:,.1f}"

    ax.text(0.5, 0.6, display, ha="center", va="center",
            transform=ax.transAxes, fontsize=20, fontweight="bold",
            color=PALETTE[0])
    # Show legend text below
    legend_fmt = sub.iloc[-1].get("legend_format", "")
    if legend_fmt:
        ax.text(0.5, 0.3, legend_fmt, ha="center", va="center",
                transform=ax.transAxes, fontsize=7, color=TEXT_COL)


# ---------------------------------------------------------------------------
# Section figure builder
# ---------------------------------------------------------------------------

def _section_slug(row_title: str, idx: int) -> str:
    safe = "".join(c if c.isalnum() or c in " _-" else "" for c in row_title)
    return f"{idx:02d}_{safe.strip().replace(' ', '_')}"


def plot_section(
    section_title: str,
    panels: list[dict],
    df: pd.DataFrame,
    output_dir: Path,
    section_idx: int,
) -> Path:
    """Generate one PNG figure for a dashboard row section."""
    n = len(panels)
    ncols = min(3, n)
    nrows = (n + ncols - 1) // ncols

    fig_w = max(14, ncols * 5)
    fig_h = max(4, nrows * 3.5)
    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(fig_w, fig_h),
                             squeeze=False)
    fig.patch.set_facecolor(DARK_BG)
    fig.suptitle(section_title, fontsize=11, color=TEXT_COL, y=1.01,
                 fontweight="bold")

    for i, panel in enumerate(panels):
        row_i, col_i = divmod(i, ncols)
        ax = axes[row_i][col_i]
        ax.set_facecolor(PANEL_BG)

        ptype = panel.get("type", "timeseries")
        if ptype == "stat":
            _plot_stat_panel(ax, df, panel)
        else:
            _plot_timeseries_panel(ax, df, panel)

    # Hide unused axes
    for j in range(n, nrows * ncols):
        row_j, col_j = divmod(j, ncols)
        axes[row_j][col_j].set_visible(False)

    plt.tight_layout(rect=[0, 0, 1, 0.98])
    slug = _section_slug(section_title, section_idx)
    out_path = output_dir / f"{slug}.png"
    fig.savefig(out_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"  saved  {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Specialised: interarrival distribution comparison
# ---------------------------------------------------------------------------

def plot_interarrival_distribution(
    df_all: pd.DataFrame,
    output_dir: Path,
) -> None:
    """Compare interarrival time distributions for math vs coding tasks.

    Interarrival time is derived from 'LLM Interarrival Time' panel or
    reconstructed from llm_requests_total counter.
    """
    # Look for the interarrival panel
    IAT_TITLE = "LLM Interarrival Time (30s rolling avg)"
    sub = df_all[df_all["panel_title"] == IAT_TITLE].copy()

    if sub.empty:
        print("  WARN  no interarrival data found – skipping distribution plot")
        return

    tasks = sub["task_slug"].dropna().unique()
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.patch.set_facecolor(DARK_BG)
    fig.suptitle("LLM Interarrival Time Distribution", fontsize=12,
                 color=TEXT_COL, fontweight="bold")

    # --- Left: time-series per task ---
    ax = axes[0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Interarrival Time over Experiment", loc="left", fontsize=9)
    ax.set_ylabel("seconds")
    ax.grid(True)
    for i, task in enumerate(sorted(tasks)):
        tsub = sub[sub["task_slug"] == task].sort_values("timestamp")
        ax.plot(tsub["timestamp"], tsub["value"],
                label=task, color=PALETTE[i % len(PALETTE)],
                linewidth=1.2, alpha=0.85)
        ax.fill_between(tsub["timestamp"], tsub["value"],
                        alpha=0.1, color=PALETTE[i % len(PALETTE)])
    _fmt_ts_axis(ax, sub["timestamp"])
    ax.legend(fontsize=7)

    # --- Right: histogram / density per task ---
    ax = axes[1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Distribution (histogram)", loc="left", fontsize=9)
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("density")
    ax.grid(True)
    for i, task in enumerate(sorted(tasks)):
        vals = sub[sub["task_slug"] == task]["value"].dropna().values
        if len(vals) < 2:
            continue
        color = PALETTE[i % len(PALETTE)]
        ax.hist(vals, bins=30, density=True, alpha=0.45, color=color,
                label=f"{task} (n={len(vals)})")
        # KDE overlay if scipy available
        if SCIPY_AVAILABLE and len(vals) > 10:
            kde = scipy_stats.gaussian_kde(vals)
            xs = np.linspace(vals.min(), vals.max(), 300)
            ax.plot(xs, kde(xs), color=color, linewidth=2)
    ax.legend(fontsize=7)

    plt.tight_layout()
    out_path = output_dir / "interarrival_distribution.png"
    fig.savefig(out_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"  saved  {out_path}")


def plot_interarrival_ecdf(df_all: pd.DataFrame, output_dir: Path) -> None:
    """Empirical CDF of interarrival times by task."""
    IAT_TITLE = "LLM Interarrival Time (30s rolling avg)"
    sub = df_all[df_all["panel_title"] == IAT_TITLE].copy()
    if sub.empty:
        return

    tasks = sub["task_slug"].dropna().unique()
    fig, ax = plt.subplots(figsize=(8, 5))
    fig.patch.set_facecolor(DARK_BG)
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Interarrival Time – Empirical CDF", loc="left", fontsize=10,
                 fontweight="bold")
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("P(X ≤ x)")
    ax.grid(True)
    ax.set_ylim(0, 1.05)

    for i, task in enumerate(sorted(tasks)):
        vals = np.sort(sub[sub["task_slug"] == task]["value"].dropna().values)
        if len(vals) < 2:
            continue
        ecdf_y = np.arange(1, len(vals) + 1) / len(vals)
        color = PALETTE[i % len(PALETTE)]
        ax.plot(vals, ecdf_y, label=f"{task} (n={len(vals)})",
                color=color, linewidth=2)
        # Mark p50, p95
        for pct, ls in [(50, "--"), (95, ":")]:
            pval = np.percentile(vals, pct)
            ax.axvline(pval, color=color, linestyle=ls, alpha=0.6, linewidth=1)
            ax.text(pval, 0.02 + i * 0.06, f"p{pct}={pval:.2f}s",
                    color=color, fontsize=6, ha="left")

    ax.legend(fontsize=8)
    plt.tight_layout()
    out_path = output_dir / "interarrival_ecdf.png"
    fig.savefig(out_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"  saved  {out_path}")


# ---------------------------------------------------------------------------
# Specialised: per-run summary (duration, latency, interarrival)
# ---------------------------------------------------------------------------

def plot_per_run_summary(
    experiment_dir: Path,
    df_all: pd.DataFrame,
    output_dir: Path,
) -> None:
    """Bar/line chart summarising key stats per run."""
    # Load meta.json files from each run directory
    run_metas: list[dict] = []
    for run_dir in sorted(_tasks_dir(experiment_dir).iterdir()):
        meta_path = run_dir / "meta.json"
        if meta_path.exists() and run_dir.is_dir():
            with open(meta_path) as f:
                run_metas.append(json.load(f))

    if not run_metas:
        return

    df_meta = pd.DataFrame(run_metas)
    if "duration_s" not in df_meta.columns:
        return

    tasks = df_meta["task_slug"].dropna().unique() if "task_slug" in df_meta.columns else []

    fig, axes = plt.subplots(1, max(2, len(tasks)), figsize=(14, 5), squeeze=False)
    fig.patch.set_facecolor(DARK_BG)
    fig.suptitle("Per-Run Summary", fontsize=12, color=TEXT_COL,
                 fontweight="bold")

    for ax_idx, task in enumerate(sorted(tasks)):
        ax = axes[0][ax_idx]
        ax.set_facecolor(PANEL_BG)
        ax.set_title(task, loc="left", fontsize=9)
        ax.set_xlabel("iteration")
        ax.set_ylabel("duration (s)")
        ax.grid(True)

        tmask = df_meta["task_slug"] == task
        tdf   = df_meta[tmask].copy()
        if "iteration" in tdf.columns:
            tdf.sort_values("iteration", inplace=True)

        color = PALETTE[ax_idx % len(PALETTE)]
        iters = tdf.get("iteration", range(len(tdf)))
        durs  = tdf.get("duration_s", [0] * len(tdf))
        ax.bar(iters, durs, color=color, alpha=0.7, label="duration (s)")
        ax.axhline(float(pd.to_numeric(durs, errors="coerce").mean()),
                   color=color, linewidth=1.5, linestyle="--",
                   label=f"mean={float(pd.to_numeric(durs, errors='coerce').mean()):.1f}s")
        ax.legend(fontsize=7)

    # Hide unused axes
    for j in range(len(tasks), axes.shape[1]):
        axes[0][j].set_visible(False)

    plt.tight_layout()
    out_path = output_dir / "per_run_summary.png"
    fig.savefig(out_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"  saved  {out_path}")

def plot_task_comparison(
    experiment_dir: Path,
    df_all: pd.DataFrame,
    output_dir: Path,
) -> None:
    """
    Compare math vs coding tasks using run-level aggregates.
    """

    IAT_TITLE = "LLM Interarrival Time (30s rolling avg)"
    LAT_TITLE = "LLM End-to-end Latency (p50/p95)"

    # ---- Load run metadata ----
    metas = []
    for run_dir in sorted(_tasks_dir(experiment_dir).iterdir()):
        meta = run_dir / "meta.json"
        if meta.exists():
            metas.append(json.loads(meta.read_text()))

    if not metas:
        return

    df_meta = pd.DataFrame(metas)

    # ---- Compute per-run metric means ----
    run_stats = []

    for run_dir in sorted(_tasks_dir(experiment_dir).iterdir()):
        csv_path = run_dir / "metrics.csv"
        meta_path = run_dir / "meta.json"

        if not csv_path.exists() or not meta_path.exists():
            continue

        meta = json.loads(meta_path.read_text())
        df = load_metrics_csv(csv_path)

        def mean_metric(title):
            sub = df[df["panel_title"] == title]["value"].dropna()
            return sub.mean() if not sub.empty else np.nan

        run_stats.append({
            "task_slug": meta.get("task_slug"),
            "iteration": meta.get("iteration"),
            "duration_s": meta.get("duration_s"),
            "iat_mean": mean_metric(IAT_TITLE),
            "lat_mean": mean_metric(LAT_TITLE),
        })

    df_runs = pd.DataFrame(run_stats)

    if df_runs.empty:
        return

    # ---- Aggregate across runs per task ----
    summary = (
        df_runs
        .groupby("task_slug")
        .agg({
            "duration_s": ["mean", "std"],
            "iat_mean": ["mean", "std"],
            "lat_mean": ["mean", "std"],
        })
    )

    summary.columns = ["_".join(c) for c in summary.columns]
    summary.reset_index(inplace=True)

    # ---- Plot comparison figure ----
    fig, axes = plt.subplots(1, 3, figsize=(14, 4))
    fig.patch.set_facecolor(DARK_BG)
    fig.suptitle("Task-Level Comparison (Run Aggregates)", fontsize=12, fontweight="bold")

    metrics = [
        ("duration_s_mean", "Mean Duration (s)"),
        ("iat_mean_mean", "Mean Interarrival Time (s)"),
        ("lat_mean_mean", "Mean Latency (s)"),
    ]

    for ax, (col, label) in zip(axes, metrics):
        ax.set_facecolor(PANEL_BG)
        ax.set_title(label, loc="left")
        ax.grid(True)

        x = np.arange(len(summary))
        vals = summary[col].values

        ax.bar(x, vals, color=[PALETTE[i] for i in range(len(x))], alpha=0.8)

        ax.set_xticks(x)
        ax.set_xticklabels(summary["task_slug"], rotation=20)

        for i, v in enumerate(vals):
            ax.text(i, v, f"{v:.2f}", ha="center", va="bottom", fontsize=8)

    plt.tight_layout()

    out_path = output_dir / "task_comparison_summary.png"
    fig.savefig(out_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)

    print(f"  saved  {out_path}")
# ---------------------------------------------------------------------------
# Specialised: interarrival times from response.json LLM request timestamps
# ---------------------------------------------------------------------------

def load_arrival_times_from_responses(
    experiment_dir: Path,
) -> tuple[dict[str, list[float]], dict[str, list[float]], list[float]]:
    """Walk run dirs, read response.json llm_requests, collect arrival timestamps.

    Captures every LLM request from every agent source (Agent A, agent-b-*, etc.)
    that reached the LLM backend, as recorded in each run's response.json.

    Returns:
        by_task   – {task_slug: sorted timestamps}  (requests grouped by run task)
        by_source – {source: sorted timestamps}      (requests grouped by agent type)
        all_ts    – globally sorted list of all timestamps (LLM-server perspective)
    """
    from datetime import datetime

    by_task:   dict[str, list[float]] = {}
    by_source: dict[str, list[float]] = {}
    all_ts:    list[float] = []

    for run_dir in sorted(_tasks_dir(experiment_dir).iterdir()):
        if not run_dir.is_dir():
            continue
        resp_path = run_dir / "response.json"
        if not resp_path.exists():
            continue

        # Determine task slug: prefer meta.json, fall back to dir name parsing
        task_slug: str | None = None
        meta_path = run_dir / "meta.json"
        if meta_path.exists():
            try:
                task_slug = json.loads(meta_path.read_text()).get("task_slug")
            except Exception:
                pass
        if not task_slug:
            # dir name pattern: YYYY-MM-DD_HH-MM-SS_<task-slug>_<uuid>
            parts = run_dir.name.split("_")
            if len(parts) >= 4:
                task_slug = "_".join(parts[2:-1])
            else:
                task_slug = run_dir.name

        try:
            data = json.loads(resp_path.read_text())
        except Exception:
            continue

        for req in data.get("llm_requests", []):
            ts_str = req.get("start_time_utc")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str).timestamp()
            except Exception:
                continue
            source = req.get("source", "unknown")
            by_task.setdefault(task_slug, []).append(ts)
            by_source.setdefault(source, []).append(ts)
            all_ts.append(ts)

    for d in (by_task, by_source):
        for k in d:
            d[k].sort()
    all_ts.sort()

    return by_task, by_source, all_ts


def plot_interarrival_from_responses(
    experiment_dir: Path,
    output_dir: Path,
) -> None:
    """Compute and plot interarrival time distribution from response.json timestamps.

    Produces a separate PNG (interarrival_from_responses.png) that is
    independent of Grafana / Prometheus data.
    """
    by_task, by_source, all_ts = load_arrival_times_from_responses(experiment_dir)

    if not all_ts:
        print("  WARN  no response.json llm_requests found – skipping response-based IAT plot")
        return

    # Per-task IATs (diff within each task's sorted stream)
    iat_by_task: dict[str, np.ndarray] = {
        task: np.diff(np.array(ts))
        for task, ts in by_task.items()
        if len(ts) >= 2
    }
    # Per-source IATs
    iat_by_source: dict[str, np.ndarray] = {
        src: np.diff(np.array(ts))
        for src, ts in by_source.items()
        if len(ts) >= 2
    }
    # Global IAT: diff over the entire sorted stream — true LLM-server view
    global_iats = np.diff(np.array(all_ts))

    if not iat_by_task:
        print("  WARN  insufficient arrival timestamps for IAT calculation")
        return

    tasks   = sorted(iat_by_task.keys())
    sources = sorted(iat_by_source.keys())
    all_t0  = all_ts[0]

    def _hist_kde(ax, vals, color, label):
        clipped = vals[vals <= IAT_MAX_S]
        n_over = len(vals) - len(clipped)
        lbl = label if n_over == 0 else f"{label} ({n_over} > {IAT_MAX_S}s clipped)"
        ax.hist(clipped, bins=30, density=True, alpha=0.4, color=color, label=lbl)
        if SCIPY_AVAILABLE and len(clipped) > 5:
            kde      = scipy_stats.gaussian_kde(clipped)
            xs       = np.linspace(0, IAT_MAX_S, 300)
            kde_vals = kde(xs)
            kde_vals[xs < clipped.min()] = 0.0
            ax.plot(xs, kde_vals, color=color, linewidth=2)
        ax.set_xlim(0, IAT_MAX_S)

    def _annotate_pct(ax, vals, color, row_offset=0):
        for pct, ls in [(50, "--"), (95, ":")]:
            pval = np.percentile(vals, pct)
            ax.axvline(pval, color=color, linestyle=ls, alpha=0.6, linewidth=1)
            ax.text(pval, 0.02 + row_offset * 0.07, f"p{pct}={pval:.2f}s",
                    color=color, fontsize=6, ha="left")

    fig, axes = plt.subplots(3, 2, figsize=(16, 14))
    fig.patch.set_facecolor(DARK_BG)
    fig.suptitle(
        "LLM Interarrival Time Distribution — all agents → LLM backend"
        " (source: response.json request timestamps)",
        fontsize=11, color=TEXT_COL, fontweight="bold",
    )

    # ── Row 0 left: arrival timeline coloured by task ──────────────────────
    ax = axes[0][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Request Arrival Times (by task)", loc="left", fontsize=9)
    ax.set_ylabel("task")
    ax.set_xlabel("time (relative, s)")
    ax.grid(True)
    for i, task in enumerate(tasks):
        ts = np.array(by_task[task]) - all_t0
        color = PALETTE[i % len(PALETTE)]
        ax.scatter(ts, [task] * len(ts), s=6, color=color, alpha=0.55, label=task)
    ax.legend(fontsize=6, loc="upper left")

    # ── Row 0 right: arrival timeline coloured by source agent ─────────────
    ax = axes[0][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Request Arrival Times (by agent source)", loc="left", fontsize=9)
    ax.set_ylabel("source")
    ax.set_xlabel("time (relative, s)")
    ax.grid(True)
    for i, src in enumerate(sources):
        ts = np.array(by_source[src]) - all_t0
        color = PALETTE[i % len(PALETTE)]
        ax.scatter(ts, [src] * len(ts), s=6, color=color, alpha=0.55, label=src)
    ax.legend(fontsize=6, loc="upper left")

    # ── Row 1 left: per-task IAT histogram + KDE ───────────────────────────
    ax = axes[1][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Interarrival Time Histogram (per task)", loc="left", fontsize=9)
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("density")
    ax.grid(True)
    for i, task in enumerate(tasks):
        _hist_kde(ax, iat_by_task[task], PALETTE[i % len(PALETTE)],
                  f"{task} (n={len(iat_by_task[task])})")
    ax.legend(fontsize=7)

    # ── Row 1 right: per-source IAT histogram + KDE ────────────────────────
    ax = axes[1][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Interarrival Time Histogram (per agent source)", loc="left", fontsize=9)
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("density")
    ax.grid(True)
    for i, src in enumerate(sources):
        _hist_kde(ax, iat_by_source[src], PALETTE[i % len(PALETTE)],
                  f"{src} (n={len(iat_by_source[src])})")
    ax.legend(fontsize=7)

    # ── Row 2 left: per-task ECDF ──────────────────────────────────────────
    ax = axes[2][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Interarrival Time ECDF (per task)", loc="left", fontsize=9)
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("P(X ≤ x)")
    ax.set_ylim(0, 1.05)
    ax.set_xlim(0, IAT_MAX_S)
    ax.grid(True)
    for i, task in enumerate(tasks):
        vals = np.sort(iat_by_task[task])
        ax.plot(vals, np.arange(1, len(vals) + 1) / len(vals),
                label=f"{task} (n={len(vals)})",
                color=PALETTE[i % len(PALETTE)], linewidth=2)
        _annotate_pct(ax, vals, PALETTE[i % len(PALETTE)], row_offset=i)
    ax.legend(fontsize=7)

    # ── Row 2 right: globally-sorted IAT (true LLM-server view) ───────────
    ax = axes[2][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title(
        f"Global Interarrival Time – all agents, all tasks (n={len(global_iats)})",
        loc="left", fontsize=9,
    )
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("density")
    ax.grid(True)
    agg_color = PALETTE[len(tasks) % len(PALETTE)]
    global_clipped = global_iats[global_iats <= IAT_MAX_S]
    n_over_global = len(global_iats) - len(global_clipped)
    global_lbl = f"global (n={len(global_iats)}" + (f", {n_over_global} clipped)" if n_over_global else ")")
    ax.hist(global_clipped, bins=40, density=True, alpha=0.5, color=agg_color, label=global_lbl)
    if SCIPY_AVAILABLE and len(global_clipped) > 5:
        kde      = scipy_stats.gaussian_kde(global_clipped)
        xs       = np.linspace(0, IAT_MAX_S, 300)
        kde_vals = kde(xs)
        kde_vals[xs < global_clipped.min()] = 0.0
        ax.plot(xs, kde_vals, color=agg_color, linewidth=2.5, label="KDE")
    ax.set_xlim(0, IAT_MAX_S)
    for pct, ls in [(50, "--"), (95, ":"), (99, "-.")]:
        pval = np.percentile(global_iats, pct)
        ax.axvline(pval, color=TEXT_COL, linestyle=ls, alpha=0.7, linewidth=1.2)
        ax.text(pval, 0, f"p{pct}={pval:.2f}s", color=TEXT_COL, fontsize=7,
                ha="left", transform=ax.get_xaxis_transform(), va="bottom")
    ax.legend(fontsize=7)

    plt.tight_layout()
    out_path = output_dir / "interarrival_from_responses.png"
    fig.savefig(out_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"  saved  {out_path}")


# ---------------------------------------------------------------------------
# Aggregation hypothesis: vertical simultaneous-reviewer collapsing
# ---------------------------------------------------------------------------

def plot_aggregated_iat_comparison(
    experiment_dir: Path,
    output_dir: Path,
) -> None:
    """Test the aggregation hypothesis using response.json timestamps.

    For vertical discussion runs, reviewers within the same round fire
    concurrently (sub-millisecond IATs).  This function collapses those
    simultaneous reviewer requests into a single virtual request (keeping
    only the minimum timestamp per round-reviewer group) and then compares
    the resulting IAT distribution against:
      • horizontal (raw)
      • vertical (raw)
    to test whether aggregation makes the vertical distribution resemble
    horizontal more closely.

    Produces: aggregated_iat_comparison.png
    """
    from datetime import datetime as _dt
    from collections import defaultdict as _dd

    # ---- constants matching compare_discussion_structures.py ---------------
    DISC_PREFIXES = (
        "horizontal_discussion",
        "synthesize_discussion",
        "vertical_solver",
        "vertical_reviewer",
    )
    H_COLOR  = "#1f77b4"
    V_COLOR  = "#ff7f0e"
    VA_COLOR = "#2ca02c"

    def _aggregate_vertical_run(timestamps: list, requests: list) -> list:
        groups: dict = _dd(list)
        for ts, req in zip(timestamps, requests):
            label   = req.get("label", "")
            round_n = req.get("round")
            seq     = req.get("seq", ts)
            if label.startswith("vertical_reviewer"):
                key: tuple = ("reviewers", round_n)
            elif label.startswith("vertical_solver"):
                key = ("solver", round_n)
            else:
                key = ("other", seq)
            groups[key].append(ts)
        return sorted(min(v) for v in groups.values())

    # ---- load all runs -----------------------------------------------------
    by_structure: dict[str, list[np.ndarray]] = {"horizontal": [], "vertical": [], "vertical_agg": []}

    for run_dir in sorted(_tasks_dir(experiment_dir).iterdir()):
        if not run_dir.is_dir():
            continue
        resp_path = run_dir / "response.json"
        if not resp_path.exists():
            continue
        try:
            data = json.loads(resp_path.read_text())
        except Exception:
            continue

        # Infer structure from actual request labels — overrides metadata when
        # labels contradict it (two runs found with metadata='horizontal' but
        # vertical_solver_*/vertical_reviewer_* labels in llm_requests).
        all_labels = {req.get("label", "") for req in data.get("llm_requests", [])}
        has_horiz = any(l.startswith("horizontal_discussion") for l in all_labels)
        has_vert  = any(l.startswith(("vertical_solver", "vertical_reviewer")) for l in all_labels)
        if has_horiz and not has_vert:
            structure = "horizontal"
        elif has_vert and not has_horiz:
            structure = "vertical"
        elif has_horiz and has_vert:
            structure = "vertical"   # mixed — treat as vertical
        else:
            # No discussion labels — fall back to metadata
            stages    = data.get("stages", {})
            structure = (
                stages.get("recruitment", {}).get("communication_structure")
                or stages.get("decision",   {}).get("structure_used", "")
            )
            if not isinstance(structure, str):
                continue
            structure = structure.lower()
        if structure not in ("horizontal", "vertical"):
            continue

        timestamps: list[float] = []
        requests:   list[dict]  = []
        for req in data.get("llm_requests", []):
            ts_str = req.get("start_time_utc", "")
            label  = req.get("label", "")
            if not ts_str:
                continue
            if not any(label.startswith(p) for p in DISC_PREFIXES):
                continue
            try:
                ts = _dt.fromisoformat(ts_str).timestamp()
                timestamps.append(ts)
                requests.append(req)
            except Exception:
                continue

        if len(timestamps) < 2:
            continue

        sorted_ts = sorted(timestamps)
        run_iats  = np.diff(np.array(sorted_ts))
        by_structure[structure].append(run_iats)

        if structure == "vertical":
            agg_ts   = _aggregate_vertical_run(timestamps, requests)
            if len(agg_ts) >= 2:
                by_structure["vertical_agg"].append(np.diff(np.array(agg_ts)))

    def _pool(key: str) -> np.ndarray:
        arrs = by_structure[key]
        return np.concatenate(arrs) if arrs else np.array([])

    h_pool  = _pool("horizontal")
    v_pool  = _pool("vertical")
    va_pool = _pool("vertical_agg")

    if len(v_pool) == 0:
        print("  WARN  no vertical runs found — skipping aggregated IAT comparison plot")
        return

    series = [
        (h_pool,  H_COLOR,  f"horizontal (n={len(h_pool)} IATs, {len(by_structure['horizontal'])} runs)"),
        (v_pool,  V_COLOR,  f"vertical raw (n={len(v_pool)} IATs, {len(by_structure['vertical'])} runs)"),
        (va_pool, VA_COLOR, f"vertical aggregated (n={len(va_pool)} IATs)"),
    ]

    def _hist_kde_local(ax, vals, color, label):
        if len(vals) == 0:
            return
        clipped = vals[vals <= IAT_MAX_S]
        n_over  = len(vals) - len(clipped)
        lbl     = label if n_over == 0 else f"{label} ({n_over} clipped)"
        ax.hist(clipped, bins=35, density=True, alpha=0.35, color=color, label=lbl)
        if SCIPY_AVAILABLE and len(clipped) > 5:
            kde      = scipy_stats.gaussian_kde(clipped)
            xs       = np.linspace(0, IAT_MAX_S, 400)
            kde_vals = kde(xs)
            kde_vals[xs < clipped.min()] = 0.0
            ax.plot(xs, kde_vals, color=color, linewidth=2.2)
        ax.set_xlim(0, IAT_MAX_S)

    def _ecdf_local(ax, vals, color, label, row_offset=0):
        clipped = np.sort(vals[vals <= IAT_MAX_S]) if len(vals) else np.array([])
        if len(clipped) == 0:
            return
        y = np.arange(1, len(clipped) + 1) / len(clipped)
        ax.plot(clipped, y, color=color, linewidth=2, label=label)
        for pct, ls in [(50, "--"), (95, ":")]:
            pval = np.percentile(clipped, pct)
            ax.axvline(pval, color=color, linestyle=ls, alpha=0.6, linewidth=1)
            ax.text(pval, 0.02 + row_offset * 0.07,
                    f"p{pct}={pval:.2f}s", color=color, fontsize=6, ha="left")

    fig, axes = plt.subplots(2, 2, figsize=(16, 11))
    fig.patch.set_facecolor(DARK_BG)
    fig.suptitle(
        "Aggregation Hypothesis — Collapsing Simultaneous Vertical Reviewer Requests\n"
        "Discussion stage only  |  within-run IATs",
        fontsize=11, color=TEXT_COL, fontweight="bold",
    )

    # ── [0,0] Histogram + KDE ─────────────────────────────────────────────
    ax = axes[0][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("IAT Histogram + KDE (within-run)", loc="left", fontsize=9)
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("density")
    ax.grid(True, color=GRID_COL, linewidth=0.5)
    for vals, color, label in series:
        _hist_kde_local(ax, vals, color, label)
    ax.legend(fontsize=7)

    # ── [0,1] ECDF ────────────────────────────────────────────────────────
    ax = axes[0][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("ECDF  — dashed=p50, dotted=p95", loc="left", fontsize=9)
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("P(X ≤ x)")
    ax.set_ylim(0, 1.05)
    ax.set_xlim(0, IAT_MAX_S)
    ax.grid(True, color=GRID_COL, linewidth=0.5)
    for i, (vals, color, label) in enumerate(series):
        _ecdf_local(ax, vals, color, label, row_offset=i)
    ax.legend(fontsize=7)

    # ── [1,0] Box plot ────────────────────────────────────────────────────
    ax = axes[1][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("IAT Box Plot (within-run, clipped at max-iat)", loc="left", fontsize=9)
    ax.set_xlabel("interarrival time (s)")
    ax.grid(True, color=GRID_COL, linewidth=0.5, axis="x")

    bp_data, bp_labels, bp_colors = [], [], []
    for vals, color, label in series:
        if len(vals):
            bp_data.append(vals[vals <= IAT_MAX_S])
            bp_labels.append(label.split(" (")[0])
            bp_colors.append(color)
    if bp_data:
        positions = list(range(1, len(bp_data) + 1))
        bp = ax.boxplot(
            bp_data, positions=positions, vert=False, patch_artist=True,
            widths=0.5, flierprops=dict(marker=".", markersize=2, alpha=0.3),
        )
        for patch, color in zip(bp["boxes"], bp_colors):
            patch.set_facecolor(color)
            patch.set_alpha(0.55)
        for median in bp["medians"]:
            median.set_color(TEXT_COL)
            median.set_linewidth(1.5)
        ax.set_yticks(positions)
        ax.set_yticklabels(bp_labels, fontsize=8)
        ax.set_xlim(0, IAT_MAX_S)

    # ── [1,1] Near-zero fraction + statistics table ───────────────────────
    ax = axes[1][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Fraction of Near-Zero IATs (< 1 s)  &  Summary Stats",
                 loc="left", fontsize=9)
    ax.axis("off")

    rows = []
    for vals, _, label in series:
        if len(vals) == 0:
            rows.append([label.split(" (")[0], "—", "—", "—", "—", "—"])
            continue
        clipped  = vals[vals <= IAT_MAX_S]
        frac_z   = float(np.mean(vals < 1.0))
        rows.append([
            label.split(" (")[0],
            f"{len(vals)}",
            f"{np.mean(clipped):.2f}s",
            f"{np.median(clipped):.2f}s",
            f"{np.percentile(clipped, 95):.2f}s",
            f"{frac_z:.1%}",
        ])
    col_labels = ["Structure", "N IATs", "Mean", "Median", "p95", "IAT < 1 s"]
    tbl = ax.table(
        cellText=rows,
        colLabels=col_labels,
        cellLoc="center",
        loc="center",
        bbox=[0.0, 0.35, 1.0, 0.55],
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8)
    for (row_idx, col_idx), cell in tbl.get_celld().items():
        cell.set_edgecolor(GRID_COL)
        if row_idx == 0:
            cell.set_facecolor("#dddddd")
            cell.set_text_props(fontweight="bold")
        elif row_idx % 2 == 0:
            cell.set_facecolor("#eeeeee")
        else:
            cell.set_facecolor("white")

    ax.text(
        0.5, 0.28,
        "Aggregation: within each vertical run, all reviewer requests in the\n"
        "same round are collapsed to a single virtual request (min timestamp).\n"
        "Solver requests remain individual.",
        ha="center", va="top", fontsize=7.5, color="#444444",
        transform=ax.transAxes,
    )

    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("ignore", UserWarning)
        plt.tight_layout(rect=[0, 0, 1, 0.94])

    out_path = output_dir / "aggregated_iat_comparison.png"
    fig.savefig(out_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"  saved  {out_path}")


# ---------------------------------------------------------------------------
# Distribution fitting & hypothesis tests for interarrival times
# ---------------------------------------------------------------------------

# Candidate distributions with display names
_CANDIDATE_DISTS = [
    ("expon",       "Exponential (Poisson process)"),
    ("weibull_min", "Weibull"),
    ("lognorm",     "Log-normal"),
    ("gamma",       "Gamma"),
    ("pareto",      "Pareto (heavy-tail)"),
]


def _fit_and_test(vals: np.ndarray) -> list[dict]:
    """Fit each candidate distribution to *vals* via MLE, run KS test, compute AIC."""
    results = []
    n = len(vals)
    for dist_name, dist_label in _CANDIDATE_DISTS:
        dist = getattr(scipy_stats, dist_name)
        try:
            params = dist.fit(vals, floc=0)   # fix location=0 (IATs ≥ 0)
            log_ll = np.sum(dist.logpdf(vals, *params))
            k      = len(params)
            aic    = 2 * k - 2 * log_ll
            bic    = k * np.log(n) - 2 * log_ll
            ks_stat, ks_p = scipy_stats.kstest(vals, dist_name, args=params)
            results.append({
                "name":    dist_name,
                "label":   dist_label,
                "params":  params,
                "log_ll":  log_ll,
                "aic":     aic,
                "bic":     bic,
                "ks_stat": ks_stat,
                "ks_p":    ks_p,
            })
        except Exception:
            pass
    results.sort(key=lambda r: r["aic"])
    return results


def _descriptive_stats(vals: np.ndarray) -> dict:
    """Return a dict of descriptive statistics relevant to IAT characterisation."""
    mean = vals.mean()
    std  = vals.std()
    cv   = std / mean if mean > 0 else float("nan")   # CV=1 → exponential

    # Autocorrelation at lags 1..5 (Poisson ≈ 0 at all lags)
    acf = [float(pd.Series(vals).autocorr(lag=lag)) for lag in range(1, 6)]

    # Ljung-Box test for independence (H0: no autocorrelation up to lag 10)
    try:
        from statsmodels.stats.diagnostic import acorr_ljungbox  # type: ignore
        lb_result = acorr_ljungbox(vals, lags=[10], return_df=True)
        lb_stat = float(lb_result["lb_stat"].iloc[0])
        lb_p    = float(lb_result["lb_pvalue"].iloc[0])
    except Exception:
        lb_stat, lb_p = float("nan"), float("nan")

    return {
        "n":        len(vals),
        "mean":     mean,
        "std":      std,
        "cv":       cv,
        "skewness": float(scipy_stats.skew(vals)),
        "kurtosis": float(scipy_stats.kurtosis(vals)),
        "p50":      float(np.percentile(vals, 50)),
        "p95":      float(np.percentile(vals, 95)),
        "p99":      float(np.percentile(vals, 99)),
        "acf":      acf,
        "lb_stat":  lb_stat,
        "lb_p":     lb_p,
    }


def _interpret(stats: dict, fits: list[dict]) -> list[str]:
    """Return plain-English interpretation lines."""
    lines = []
    cv = stats["cv"]
    if cv < 0.8:
        lines.append(f"  CV={cv:.3f} < 1  → more regular than Poisson (sub-exponential variability)")
    elif cv > 1.2:
        lines.append(f"  CV={cv:.3f} > 1  → burstier than Poisson (super-exponential variability)")
    else:
        lines.append(f"  CV={cv:.3f} ≈ 1  → variability consistent with Poisson/exponential")

    if not np.isnan(stats["lb_p"]):
        if stats["lb_p"] < 0.05:
            lines.append(
                f"  Ljung-Box p={stats['lb_p']:.4f} < 0.05  → significant autocorrelation; "
                "arrivals are NOT independent (not pure Poisson)"
            )
        else:
            lines.append(
                f"  Ljung-Box p={stats['lb_p']:.4f} ≥ 0.05  → no significant autocorrelation; "
                "arrival independence assumption not rejected"
            )

    best = fits[0]
    lines.append(
        f"  Best-fit by AIC: {best['label']}  "
        f"(AIC={best['aic']:.1f}, KS p={best['ks_p']:.4f})"
    )
    if best["ks_p"] >= 0.05:
        lines.append(f"  KS test does NOT reject {best['label']} at α=0.05")
    else:
        lines.append(
            f"  KS test REJECTS {best['label']} at α=0.05 — "
            "no candidate fits perfectly; consider a mixture or empirical model"
        )
    return lines


def _format_fit_table(fits: list[dict]) -> list[str]:
    lines = []
    lines.append(
        f"  {'Distribution':<36} {'AIC':>10} {'BIC':>10} "
        f"{'KS stat':>9} {'KS p':>8}  {'not rejected?':>14}"
    )
    lines.append("  " + "-" * 92)
    for r in fits:
        reject = "yes (α=0.05)" if r["ks_p"] >= 0.05 else "NO"
        lines.append(
            f"  {r['label']:<36} {r['aic']:>10.1f} {r['bic']:>10.1f} "
            f"{r['ks_stat']:>9.4f} {r['ks_p']:>8.4f}  {reject:>14}"
        )
    return lines


def analyse_iat_distributions(
    experiment_dir: Path,
    output_dir: Path,
) -> None:
    """Fit candidate distributions and run hypothesis tests on IAT data.

    Outputs:
      - interarrival_fit_report.txt  – detailed text report
      - interarrival_fit.png         – histogram + fitted PDFs + probability plots
    """
    if not SCIPY_AVAILABLE:
        print("  WARN  scipy not available – skipping distribution fitting")
        return

    _, _, all_ts = load_arrival_times_from_responses(experiment_dir)
    if len(all_ts) < 10:
        print("  WARN  too few arrival timestamps for distribution fitting")
        return

    global_iats = np.diff(np.array(all_ts))
    # Remove exact zeros (simultaneous log entries) to avoid degenerate fits
    vals = global_iats[global_iats > 0]

    stats  = _descriptive_stats(vals)
    fits   = _fit_and_test(vals)
    interp = _interpret(stats, fits)

    # ── Text report ──────────────────────────────────────────────────────────
    report_lines: list[str] = []
    report_lines.append("=" * 96)
    report_lines.append("  Interarrival Time Distribution Analysis  (global stream — all agents)")
    report_lines.append("=" * 96)
    report_lines.append(
        f"\n  Samples (n):  {stats['n']}   "
        f"mean={stats['mean']:.4f}s   std={stats['std']:.4f}s   "
        f"CV={stats['cv']:.3f}"
    )
    report_lines.append(
        f"  p50={stats['p50']:.4f}s   p95={stats['p95']:.4f}s   p99={stats['p99']:.4f}s"
    )
    report_lines.append(
        f"  skewness={stats['skewness']:.3f}   excess kurtosis={stats['kurtosis']:.3f}"
    )
    acf_str = "  ".join(f"lag{i+1}={v:.3f}" for i, v in enumerate(stats["acf"]))
    report_lines.append(f"\n  ACF: {acf_str}")
    if not np.isnan(stats["lb_stat"]):
        report_lines.append(
            f"  Ljung-Box (lag=10): stat={stats['lb_stat']:.3f}  p={stats['lb_p']:.4f}"
        )

    report_lines.append("\n  Goodness-of-fit (MLE fit, KS test, AIC/BIC):\n")
    report_lines.extend(_format_fit_table(fits))

    report_lines.append("\n  Interpretation:\n")
    report_lines.extend(interp)
    report_lines.append("\n" + "=" * 96)

    report_text = "\n".join(report_lines)
    print(report_text)
    (output_dir / "interarrival_fit_report.txt").write_text(report_text + "\n")

    # ── Plot ─────────────────────────────────────────────────────────────────
    n_prob = min(3, len(fits))   # probability plots for top-N fits
    fig = plt.figure(figsize=(16, 10))
    fig.patch.set_facecolor(DARK_BG)
    fig.suptitle(
        "IAT Distribution Fitting — all agents → LLM backend (global stream)",
        fontsize=11, color=TEXT_COL, fontweight="bold",
    )

    # Left panel: histogram + fitted PDFs
    ax_hist = fig.add_subplot(1, 2, 1)
    ax_hist.set_facecolor(PANEL_BG)
    ax_hist.set_title("Global IAT: histogram + fitted PDFs", loc="left", fontsize=9)
    ax_hist.set_xlabel("interarrival time (s)")
    ax_hist.set_ylabel("density")
    ax_hist.grid(True)

    vals_clipped = vals[vals <= IAT_MAX_S]
    n_over_fit = len(vals) - len(vals_clipped)
    fit_lbl = f"observed (n={len(vals)}" + (f", {n_over_fit} clipped)" if n_over_fit else ")")
    ax_hist.hist(vals_clipped, bins=50, density=True, alpha=0.35,
                 color=PALETTE[0], label=fit_lbl)
    # KDE of empirical data
    kde      = scipy_stats.gaussian_kde(vals_clipped)
    xs       = np.linspace(0, IAT_MAX_S, 500)
    kde_vals = kde(xs)
    kde_vals[xs < vals_clipped.min()] = 0.0
    ax_hist.plot(xs, kde_vals, color=PALETTE[0], linewidth=2, linestyle="--", label="empirical KDE")

    for i, fit in enumerate(fits):
        dist   = getattr(scipy_stats, fit["name"])
        params = fit["params"]
        color  = PALETTE[(i + 1) % len(PALETTE)]
        reject = "" if fit["ks_p"] >= 0.05 else " ✗"
        ax_hist.plot(
            xs, dist.pdf(xs, *params),
            color=color, linewidth=1.8,
            label=f"{fit['label']}{reject}  (AIC={fit['aic']:.0f}, p={fit['ks_p']:.3f})",
        )
    ax_hist.set_xlim(0, IAT_MAX_S)
    ax_hist.legend(fontsize=6.5)

    # Right panel(s): probability plots for top-N candidates, stacked vertically
    gs_right = fig.add_gridspec(n_prob, 2, left=0.55, right=0.97,
                                hspace=0.55, wspace=0.3)
    for i in range(n_prob):
        fit   = fits[i]
        dist  = getattr(scipy_stats, fit["name"])
        color = PALETTE[(i + 1) % len(PALETTE)]

        ax_pp = fig.add_subplot(gs_right[i, 0])
        ax_pp.set_facecolor(PANEL_BG)
        ax_pp.set_title(f"Prob. plot: {fit['label']}", loc="left", fontsize=7.5)
        ax_pp.grid(True)

        (osm, osr), (slope, intercept, r) = scipy_stats.probplot(
            vals, dist=fit["name"], sparams=fit["params"][:-2],   # shape params only
            fit=True,
        )
        ax_pp.scatter(osm, osr, s=4, alpha=0.5, color=color)
        line_x = np.array([osm.min(), osm.max()])
        ax_pp.plot(line_x, slope * line_x + intercept,
                   color=TEXT_COL, linewidth=1.2, linestyle="--",
                   label=f"R²={r**2:.4f}")
        ax_pp.set_xlabel("theoretical quantiles", fontsize=7)
        ax_pp.set_ylabel("sample quantiles", fontsize=7)
        ax_pp.legend(fontsize=6.5)

        # ACF bar chart alongside each prob plot
        ax_acf = fig.add_subplot(gs_right[i, 1])
        ax_acf.set_facecolor(PANEL_BG)
        ax_acf.set_title("Autocorrelation (lags 1–5)", loc="left", fontsize=7.5)
        ax_acf.grid(True)
        lags = list(range(1, 6))
        acf_vals = stats["acf"]
        bar_colors = [PALETTE[2] if abs(v) < 0.1 else PALETTE[3] for v in acf_vals]
        ax_acf.bar(lags, acf_vals, color=bar_colors, alpha=0.75)
        ax_acf.axhline(0, color=TEXT_COL, linewidth=0.8)
        # ±1.96/√n confidence bounds
        ci = 1.96 / np.sqrt(len(vals))
        ax_acf.axhline(ci,  color=PALETTE[3], linewidth=0.8, linestyle="--", label=f"±95% CI ({ci:.3f})")
        ax_acf.axhline(-ci, color=PALETTE[3], linewidth=0.8, linestyle="--")
        ax_acf.set_xlabel("lag", fontsize=7)
        ax_acf.set_ylabel("ACF", fontsize=7)
        ax_acf.legend(fontsize=6)
        break   # only draw ACF once (top row), leave remaining rows for prob plots only

    plt.tight_layout(rect=[0, 0, 0.53, 1])
    out_path = output_dir / "interarrival_fit.png"
    fig.savefig(out_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"  saved  {out_path}")


# ---------------------------------------------------------------------------
# Statistics table (printed to stdout and saved as text)
# ---------------------------------------------------------------------------

INTERARRIVAL_METRICS = [
    "LLM Interarrival Time (30s rolling avg)",
    "LLM End-to-end Latency (p50/p95)",
    "LLM Time-to-First-Token (TTFT p50/p95)",
    "In-flight LLM Requests",
    "LLM Request Rate — success vs error",
]


def print_stats_table(df_all: pd.DataFrame, output_dir: Path) -> None:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("  Experiment Statistics")
    lines.append("=" * 72)

    for metric in INTERARRIVAL_METRICS:
        sub = df_all[df_all["panel_title"] == metric]
        if sub.empty:
            continue
        lines.append(f"\n  {metric}")
        for task in sorted(sub["task_slug"].dropna().unique()):
            vals = sub[sub["task_slug"] == task]["value"].dropna()
            if vals.empty:
                continue
            lines.append(
                f"    {task:20s}  "
                f"n={len(vals):4d}  "
                f"mean={vals.mean():8.3f}  "
                f"p50={vals.quantile(0.50):8.3f}  "
                f"p95={vals.quantile(0.95):8.3f}  "
                f"max={vals.max():8.3f}"
            )

    lines.append("\n" + "=" * 72)
    text = "\n".join(lines)
    print(text)
    (output_dir / "statistics.txt").write_text(text + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Plot experiment metrics from CSV files, mirroring the Grafana dashboard layout."
    )
    p.add_argument("--experiment-dir", required=True,
                   help="Experiment output directory (contains metrics.csv and run subdirs)")
    p.add_argument("--dashboard-json",
                   default="infra/monitoring/grafana/provisioning/dashboards/agentic-traffic.json",
                   help="Path to Grafana dashboard JSON")
    return p


def main() -> None:
    args = build_parser().parse_args()

    experiment_dir = Path(args.experiment_dir)
    if not experiment_dir.exists():
        print(f"ERROR: experiment-dir does not exist: {experiment_dir}", file=sys.stderr)
        sys.exit(1)

    plots_dir = experiment_dir / "plots"
    plots_dir.mkdir(exist_ok=True)

    # Subdirectory layout
    dashboard_dir     = plots_dir / "dashboard"
    iat_dir           = plots_dir / "iat_analysis"
    for d in (dashboard_dir, iat_dir):
        d.mkdir(exist_ok=True)

    # Load panels from dashboard JSON
    dashboard_path = Path(args.dashboard_json)
    if not dashboard_path.exists():
        print(f"ERROR: dashboard JSON not found: {dashboard_path}", file=sys.stderr)
        sys.exit(1)

    panels = load_dashboard_panels(dashboard_path)
    print(f"  loaded {len(panels)} panels from dashboard JSON")

    # Load aggregate CSV (full experiment window)
    agg_csv = experiment_dir / "metrics.csv"
    df_agg  = load_metrics_csv(agg_csv)
    if df_agg.empty:
        print("  WARN  aggregate metrics.csv is empty or missing – "
              "trying per-run CSVs")

    # Load all per-run CSVs for task-level comparisons
    df_runs = load_all_run_csvs(experiment_dir)
    df_all  = pd.concat([df_agg, df_runs], ignore_index=True) if not df_runs.empty else df_agg

    if df_all.empty:
        print("ERROR: no metric data found. Make sure Prometheus was running "
              "during the experiment.", file=sys.stderr)
        sys.exit(1)

    print(f"  total rows loaded: {len(df_all)}")

    # -----------------------------------------------------------------------
    # 1. Dashboard section plots (mirrors Grafana row layout)
    # -----------------------------------------------------------------------
    # Group panels by row section
    sections: dict[str, list[dict]] = defaultdict(list)
    for panel in panels:
        sections[panel["row"]].append(panel)

    for idx, (section_title, section_panels) in enumerate(sections.items(), start=1):
        plot_section(
            section_title  = section_title,
            panels         = section_panels,
            df             = df_all,
            output_dir     = dashboard_dir,
            section_idx    = idx,
        )

    # -----------------------------------------------------------------------
    # 2. Interarrival from raw response.json timestamps  →  iat_analysis/
    # -----------------------------------------------------------------------
    plot_interarrival_from_responses(experiment_dir, iat_dir)

    # -----------------------------------------------------------------------
    # 3. Aggregation hypothesis: collapsing simultaneous vertical reviewers
    # -----------------------------------------------------------------------
    plot_aggregated_iat_comparison(experiment_dir, iat_dir)

    # -----------------------------------------------------------------------
    # 4. Distribution fitting & hypothesis tests  →  iat_analysis/
    # -----------------------------------------------------------------------
    analyse_iat_distributions(experiment_dir, iat_dir)

    # -----------------------------------------------------------------------
    # 5. Statistics table  →  dashboard/
    # -----------------------------------------------------------------------
    print_stats_table(df_all, dashboard_dir)

    print(f"\n  plots saved to subdirectories under {plots_dir}/")
    print(f"    dashboard/    — Grafana section screenshots + statistics.txt")
    print(f"    iat_analysis/ — interarrival timing plots and distribution fits")
    print(f"  (statistical analysis outputs go to plots/analysis/ — run analysis scripts separately)")


if __name__ == "__main__":
    main()
