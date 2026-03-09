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
# Grafana-inspired dark style
# ---------------------------------------------------------------------------
DARK_BG   = "#111217"
PANEL_BG  = "#181b1f"
GRID_COL  = "#2a2d35"
TEXT_COL  = "#d8d9da"
PALETTE   = [
    "#7eb26d", "#eab839", "#6ed0e0", "#ef843c",
    "#e24d42", "#1f78c1", "#ba43a9", "#705da0",
    "#508642", "#cca300",
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
    "grid.alpha":        0.5,
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
    for run_dir in sorted(experiment_dir.iterdir()):
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
    for run_dir in sorted(experiment_dir.iterdir()):
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
    for run_dir in sorted(experiment_dir.iterdir()):
        meta = run_dir / "meta.json"
        if meta.exists():
            metas.append(json.loads(meta.read_text()))

    if not metas:
        return

    df_meta = pd.DataFrame(metas)

    # ---- Compute per-run metric means ----
    run_stats = []

    for run_dir in sorted(experiment_dir.iterdir()):
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
            output_dir     = plots_dir,
            section_idx    = idx,
        )

    # -----------------------------------------------------------------------
    # 2. Interarrival specialised plots
    # -----------------------------------------------------------------------
    plot_interarrival_distribution(df_all, plots_dir)
    plot_interarrival_ecdf(df_all, plots_dir)

    # -----------------------------------------------------------------------
    # 3. Per-run summary (duration, etc.)
    # -----------------------------------------------------------------------
    plot_per_run_summary(experiment_dir, df_all, plots_dir)
    plot_task_comparison(experiment_dir, df_all, plots_dir)
    # -----------------------------------------------------------------------
    # 4. Statistics table
    # -----------------------------------------------------------------------
    print_stats_table(df_all, plots_dir)

    print(f"\n  all plots saved to {plots_dir}/")


if __name__ == "__main__":
    main()
