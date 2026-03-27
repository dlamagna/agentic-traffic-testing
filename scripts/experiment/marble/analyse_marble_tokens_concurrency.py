#!/usr/bin/env python3
"""
analyse_marble_tokens_concurrency.py
======================================
Combined token-usage and concurrency analysis for MARBLE experiments.

Data sources
------------
  results/        — per-task JSONL files: total_tokens, total_llm_calls,
                    total_agent_calls, duration_s, coordinate_mode, benchmark_domain
  marble_llm_calls.jsonl — per-call records: timestamp_start, timestamp_end,
                    coordinate_mode, call_type, agent_id

Derived metrics
---------------
  per_call_latency_s   = timestamp_end - timestamp_start  (from call log)
  instantaneous_concurrency = # calls whose [start, end] windows overlap this call
  tokens_per_task      = total_tokens from results JSONL (task level)
  tokens_per_call      = total_tokens / total_llm_calls  (task-level average)

Outputs (in --output-dir, default <experiment-dir>/plots/tokens_concurrency/)
---------------------------------------------------------------------------
  token_distribution.png       — tokens per task, box by topology + domain
  tokens_per_call.png          — mean tokens/call per topology (bar)
  call_latency.png             — per-call HTTP latency distribution by topology
  concurrency_timeline.png     — instantaneous concurrency over experiment time
  concurrency_vs_latency.png   — scatter: concurrency level vs call latency
  tokens_concurrency_summary.txt

Usage
-----
    python analyse_marble_tokens_concurrency.py
           --results-dir  <experiment_dir>/results/
           --call-log     logs/marble_llm_calls.jsonl
           --output-dir   <experiment_dir>/plots/tokens_concurrency/
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
except ImportError as exc:
    sys.exit(f"ERROR: missing dependency – {exc}\nInstall: pip install matplotlib numpy")

try:
    from scipy import stats as scipy_stats
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------
DARK_BG  = "white"
PANEL_BG = "#f7f7f7"
GRID_COL = "#cccccc"
TEXT_COL = "#222222"

TOPO_COLORS = {
    "star":  "#1f77b4",
    "chain": "#ff7f0e",
    "tree":  "#2ca02c",
    "graph": "#d62728",
}
DOMAIN_COLORS = {
    "research":   "#9467bd",
    "coding":     "#8c564b",
    "bargaining": "#e377c2",
}
TOPO_ORDER   = ["star", "chain", "tree", "graph"]
DOMAIN_ORDER = ["research", "coding", "bargaining"]

plt.rcParams.update({
    "figure.facecolor": DARK_BG,
    "axes.facecolor":   PANEL_BG,
    "axes.edgecolor":   GRID_COL,
    "axes.labelcolor":  TEXT_COL,
    "xtick.color":      TEXT_COL,
    "ytick.color":      TEXT_COL,
    "text.color":       TEXT_COL,
    "grid.color":       GRID_COL,
    "grid.linestyle":   "--",
    "grid.alpha":       0.6,
    "legend.facecolor": PANEL_BG,
    "font.size":        10,
})


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_results(results_dir: Path) -> List[Dict[str, Any]]:
    records = []
    for path in sorted(results_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return records


def _parse_ts(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def load_call_log(path: Path) -> List[Dict[str, Any]]:
    """Load call log, adding derived fields: latency_s, ts_start_epoch, ts_end_epoch."""
    calls = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_start = _parse_ts(rec.get("timestamp_start"))
            ts_end   = _parse_ts(rec.get("timestamp_end"))
            if ts_start is not None and ts_end is not None:
                rec["ts_start_epoch"] = ts_start
                rec["ts_end_epoch"]   = ts_end
                rec["latency_s"]      = max(0.0, ts_end - ts_start)
            else:
                rec["ts_start_epoch"] = None
                rec["ts_end_epoch"]   = None
                rec["latency_s"]      = None
            calls.append(rec)
    return calls


def compute_concurrency(calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    For each call with valid timestamps, compute instantaneous_concurrency:
    the number of other calls whose [start, end] window overlaps this call's start.
    Also attach experiment_elapsed_s = time since first call start.
    """
    valid = [c for c in calls if c.get("ts_start_epoch") is not None]
    if not valid:
        return calls

    t0 = min(c["ts_start_epoch"] for c in valid)
    starts = np.array([c["ts_start_epoch"] for c in valid])
    ends   = np.array([c["ts_end_epoch"]   for c in valid])

    for i, c in enumerate(valid):
        t = c["ts_start_epoch"]
        # count calls active at time t (started before t and ending after t), excluding self
        active = int(np.sum((starts <= t) & (ends >= t))) - 1
        c["instantaneous_concurrency"] = max(0, active)
        c["experiment_elapsed_s"] = t - t0

    return calls


# ---------------------------------------------------------------------------
# Grouping helpers
# ---------------------------------------------------------------------------

def _present(order: List[str], groups: Dict[str, list]) -> List[str]:
    return [k for k in order if k in groups and groups[k]]


def group_by(records: List[Dict[str, Any]], key: str) -> Dict[str, List[Dict[str, Any]]]:
    g: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        g[r.get(key, "unknown")].append(r)
    return dict(g)


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Plots — tokens
# ---------------------------------------------------------------------------

def plot_token_distribution(results: List[Dict[str, Any]], out_dir: Path) -> None:
    """Box plot: tokens per task, side-by-side topology vs domain."""
    by_topo   = group_by(results, "coordinate_mode")
    by_domain = group_by(results, "benchmark_domain")
    topos   = _present(TOPO_ORDER,   {t: [r for r in recs if r.get("total_tokens")] for t, recs in by_topo.items()})
    domains = _present(DOMAIN_ORDER, {d: [r for r in recs if r.get("total_tokens")] for d, recs in by_domain.items()})
    if not topos and not domains:
        return

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
    for ax in (ax1, ax2):
        ax.set_facecolor(PANEL_BG)

    # Left: by topology
    if topos:
        data   = [[safe_float(r["total_tokens"]) for r in by_topo[t]] for t in topos]
        colors = [TOPO_COLORS.get(t, "gray") for t in topos]
        bp = ax1.boxplot(data, patch_artist=True, tick_labels=topos)
        for patch, color in zip(bp["boxes"], colors):
            patch.set_facecolor(color); patch.set_alpha(0.7)
        ax1.set_ylabel("Tokens per task")
        ax1.set_title("Token Usage by Topology")
        ax1.grid(True, axis="y")

    # Right: by domain
    if domains:
        data   = [[safe_float(r["total_tokens"]) for r in by_domain[d]] for d in domains]
        colors = [DOMAIN_COLORS.get(d, "gray") for d in domains]
        bp = ax2.boxplot(data, patch_artist=True, tick_labels=domains)
        for patch, color in zip(bp["boxes"], colors):
            patch.set_facecolor(color); patch.set_alpha(0.7)
        ax2.set_ylabel("Tokens per task")
        ax2.set_title("Token Usage by Domain")
        ax2.grid(True, axis="y")

    fig.suptitle("MARBLE — Token Distribution", fontsize=12)
    fig.tight_layout()
    out = out_dir / "token_distribution.png"
    fig.savefig(out, dpi=150); plt.close(fig)
    print(f"  Saved: {out}")


def plot_tokens_per_call(results: List[Dict[str, Any]], out_dir: Path) -> None:
    """Bar chart: mean tokens/call per topology (total_tokens / total_llm_calls)."""
    by_topo = group_by(results, "coordinate_mode")
    topos = _present(TOPO_ORDER, by_topo)
    if not topos:
        return

    means, stds = [], []
    for t in topos:
        ratios = []
        for r in by_topo[t]:
            tok = safe_float(r.get("total_tokens"))
            calls = safe_float(r.get("total_llm_calls"), 1)
            if tok > 0 and calls > 0:
                ratios.append(tok / calls)
        means.append(float(np.mean(ratios)) if ratios else 0.0)
        stds.append(float(np.std(ratios)) if ratios else 0.0)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.set_facecolor(PANEL_BG)
    colors = [TOPO_COLORS.get(t, "gray") for t in topos]
    bars = ax.bar(topos, means, yerr=stds, color=colors, alpha=0.8,
                  edgecolor="white", capsize=5)
    for bar, val in zip(bars, means):
        ax.text(bar.get_x() + bar.get_width() / 2,
                bar.get_height() + max(stds) * 0.05 + 5,
                f"{val:.0f}", ha="center", va="bottom", fontsize=9)
    ax.set_ylabel("Mean tokens per LLM call (±std)")
    ax.set_title("MARBLE — Tokens per Call by Topology")
    ax.grid(True, axis="y")
    fig.tight_layout()
    out = out_dir / "tokens_per_call.png"
    fig.savefig(out, dpi=150); plt.close(fig)
    print(f"  Saved: {out}")


# ---------------------------------------------------------------------------
# Plots — latency
# ---------------------------------------------------------------------------

def plot_call_latency(calls: List[Dict[str, Any]], out_dir: Path) -> None:
    """Overlaid KDE + box plot of per-call HTTP latency by topology."""
    by_topo = group_by([c for c in calls if c.get("latency_s") is not None], "coordinate_mode")
    topos = _present(TOPO_ORDER, by_topo)
    if not topos:
        return

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
    for ax in (ax1, ax2):
        ax.set_facecolor(PANEL_BG)

    # Left: KDE histogram
    for t in topos:
        arr = np.array([c["latency_s"] for c in by_topo[t]])
        arr_clip = arr[arr <= np.percentile(arr, 99)]  # clip top 1% for display
        color = TOPO_COLORS.get(t, "gray")
        ax1.hist(arr_clip, bins=40, density=True, alpha=0.3, color=color)
        if SCIPY_AVAILABLE and len(arr_clip) >= 2:
            kde = scipy_stats.gaussian_kde(arr_clip)
            xs = np.linspace(0, arr_clip.max(), 300)
            ax1.plot(xs, kde(xs), color=color, linewidth=2, label=f"{t} (n={len(arr)})")
        else:
            ax1.plot([], [], color=color, linewidth=2, label=f"{t} (n={len(arr)})")
    ax1.set_xlabel("Call latency (s)")
    ax1.set_ylabel("Density")
    ax1.set_title("Per-call Latency Distribution")
    ax1.legend(); ax1.grid(True)

    # Right: box plot log-scale
    data   = [np.array([c["latency_s"] for c in by_topo[t]]) for t in topos]
    colors = [TOPO_COLORS.get(t, "gray") for t in topos]
    bp = ax2.boxplot(data, patch_artist=True, tick_labels=topos)
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color); patch.set_alpha(0.7)
    ax2.set_yscale("log")
    ax2.set_ylabel("Call latency (s) — log scale")
    ax2.set_title("Per-call Latency Box Plot")
    ax2.grid(True, axis="y")

    fig.suptitle("MARBLE — HTTP Call Latency by Topology", fontsize=12)
    fig.tight_layout()
    out = out_dir / "call_latency.png"
    fig.savefig(out, dpi=150); plt.close(fig)
    print(f"  Saved: {out}")


# ---------------------------------------------------------------------------
# Plots — concurrency
# ---------------------------------------------------------------------------

def plot_concurrency_timeline(calls: List[Dict[str, Any]], out_dir: Path) -> None:
    """Scatter: instantaneous concurrency over experiment elapsed time, coloured by topology."""
    valid = [c for c in calls
             if c.get("experiment_elapsed_s") is not None
             and c.get("instantaneous_concurrency") is not None]
    if not valid:
        return

    by_topo = group_by(valid, "coordinate_mode")
    topos = _present(TOPO_ORDER, by_topo)

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.set_facecolor(PANEL_BG)
    for t in topos:
        xs = [c["experiment_elapsed_s"] / 60 for c in by_topo[t]]
        ys = [c["instantaneous_concurrency"] for c in by_topo[t]]
        color = TOPO_COLORS.get(t, "gray")
        ax.scatter(xs, ys, color=color, alpha=0.4, s=8, label=t)

    ax.set_xlabel("Experiment elapsed time (min)")
    ax.set_ylabel("Instantaneous concurrency (# overlapping calls)")
    ax.set_title("MARBLE — Concurrency Over Time by Topology")
    ax.legend(); ax.grid(True)
    fig.tight_layout()
    out = out_dir / "concurrency_timeline.png"
    fig.savefig(out, dpi=150); plt.close(fig)
    print(f"  Saved: {out}")


def plot_concurrency_vs_latency(calls: List[Dict[str, Any]], out_dir: Path) -> None:
    """Scatter: concurrency level vs call latency per topology with mean trend."""
    valid = [c for c in calls
             if c.get("instantaneous_concurrency") is not None
             and c.get("latency_s") is not None]
    if not valid:
        return

    by_topo = group_by(valid, "coordinate_mode")
    topos = _present(TOPO_ORDER, by_topo)

    fig, axes = plt.subplots(1, len(topos), figsize=(4 * len(topos), 5), sharey=True)
    if len(topos) == 1:
        axes = [axes]

    for ax, t in zip(axes, topos):
        ax.set_facecolor(PANEL_BG)
        color = TOPO_COLORS.get(t, "gray")
        xs = np.array([c["instantaneous_concurrency"] for c in by_topo[t]])
        ys = np.array([c["latency_s"] for c in by_topo[t]])

        ax.scatter(xs, ys, color=color, alpha=0.3, s=10)

        # Mean latency per concurrency level
        levels = sorted(set(xs))
        means  = [float(np.mean(ys[xs == lvl])) for lvl in levels]
        ax.plot(levels, means, color=color, linewidth=2, marker="o", markersize=4, label="mean")

        ax.set_xlabel("Concurrency level")
        ax.set_title(t)
        ax.grid(True)
        ax.legend(fontsize=8)

    axes[0].set_ylabel("Call latency (s)")
    fig.suptitle("MARBLE — Concurrency vs Call Latency", fontsize=12)
    fig.tight_layout()
    out = out_dir / "concurrency_vs_latency.png"
    fig.savefig(out, dpi=150); plt.close(fig)
    print(f"  Saved: {out}")


def plot_concurrency_distribution(calls: List[Dict[str, Any]], out_dir: Path) -> None:
    """Bar chart: mean instantaneous concurrency per topology."""
    valid = [c for c in calls if c.get("instantaneous_concurrency") is not None]
    by_topo = group_by(valid, "coordinate_mode")
    topos = _present(TOPO_ORDER, by_topo)
    if not topos:
        return

    means = [float(np.mean([c["instantaneous_concurrency"] for c in by_topo[t]])) for t in topos]
    maxes = [float(np.max([c["instantaneous_concurrency"] for c in by_topo[t]])) for t in topos]
    colors = [TOPO_COLORS.get(t, "gray") for t in topos]

    x = np.arange(len(topos))
    width = 0.35
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.set_facecolor(PANEL_BG)
    bars_mean = ax.bar(x - width/2, means, width, color=colors, alpha=0.8,
                       edgecolor="white", label="mean concurrency")
    bars_max  = ax.bar(x + width/2, maxes, width, color=colors, alpha=0.4,
                       edgecolor="white", label="max concurrency", hatch="//")
    ax.set_xticks(x); ax.set_xticklabels(topos)
    ax.set_ylabel("Instantaneous concurrency (# parallel calls)")
    ax.set_title("MARBLE — Concurrency by Topology")
    ax.legend(); ax.grid(True, axis="y")
    for bar, val in zip(bars_mean, means):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.05,
                f"{val:.1f}", ha="center", va="bottom", fontsize=9)
    fig.tight_layout()
    out = out_dir / "concurrency_distribution.png"
    fig.savefig(out, dpi=150); plt.close(fig)
    print(f"  Saved: {out}")


# ---------------------------------------------------------------------------
# Text summary
# ---------------------------------------------------------------------------

def write_summary(
    results: List[Dict[str, Any]],
    calls: List[Dict[str, Any]],
    out_dir: Path,
) -> None:
    by_topo_res  = group_by(results, "coordinate_mode")
    by_topo_call = group_by([c for c in calls if c.get("latency_s") is not None], "coordinate_mode")
    topos = _present(TOPO_ORDER, by_topo_res)

    lines = [
        "MARBLE Tokens & Concurrency Summary",
        "=" * 60,
        f"Total tasks: {len(results)}   Total call records: {len(calls)}",
        "",
    ]

    for t in topos:
        recs  = by_topo_res.get(t, [])
        calls_t = by_topo_call.get(t, [])
        tokens  = [safe_float(r.get("total_tokens")) for r in recs if r.get("total_tokens")]
        tpc     = [safe_float(r["total_tokens"]) / max(safe_float(r.get("total_llm_calls"), 1), 1)
                   for r in recs if r.get("total_tokens")]
        latency = [c["latency_s"] for c in calls_t]
        conc    = [c["instantaneous_concurrency"] for c in calls_t
                   if c.get("instantaneous_concurrency") is not None]

        lines += [f"Topology: {t}  (tasks={len(recs)}, calls={len(calls_t)})"]
        if tokens:
            lines.append(f"  tokens/task: mean={np.mean(tokens):.0f}  median={np.median(tokens):.0f}  max={np.max(tokens):.0f}")
        if tpc:
            lines.append(f"  tokens/call: mean={np.mean(tpc):.0f}  median={np.median(tpc):.0f}")
        if latency:
            lines.append(f"  call latency: mean={np.mean(latency):.2f}s  median={np.median(latency):.2f}s  p95={np.percentile(latency,95):.2f}s  max={np.max(latency):.2f}s")
        if conc:
            lines.append(f"  concurrency:  mean={np.mean(conc):.2f}  median={np.median(conc):.0f}  max={np.max(conc):.0f}")
        lines.append("")

    out = out_dir / "tokens_concurrency_summary.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Saved: {out}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="MARBLE tokens and concurrency analysis.")
    parser.add_argument("--results-dir", type=Path, required=True,
                        help="Directory containing <domain>_<topology>.jsonl files")
    parser.add_argument("--call-log", type=Path,
                        default=Path("logs/marble_llm_calls.jsonl"),
                        help="Path to marble_llm_calls.jsonl")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Output directory (default: <results-dir>/../plots/tokens_concurrency/)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    results_dir = args.results_dir
    call_log    = args.call_log

    if not results_dir.is_dir():
        sys.exit(f"ERROR: results directory not found: {results_dir}")
    if not call_log.is_file():
        sys.exit(f"ERROR: call log not found: {call_log}")

    out_dir = args.output_dir or (results_dir.parent / "plots" / "tokens_concurrency")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[marble-tc] Loading results from {results_dir} ...", file=sys.stderr)
    results = load_results(results_dir)
    print(f"[marble-tc] {len(results)} task records loaded", file=sys.stderr)

    print(f"[marble-tc] Loading call log from {call_log} ...", file=sys.stderr)
    calls = load_call_log(call_log)
    calls = compute_concurrency(calls)
    valid_latency = sum(1 for c in calls if c.get("latency_s") is not None)
    print(f"[marble-tc] {len(calls)} call records ({valid_latency} with latency)", file=sys.stderr)

    plot_token_distribution(results, out_dir)
    plot_tokens_per_call(results, out_dir)
    plot_call_latency(calls, out_dir)
    plot_concurrency_timeline(calls, out_dir)
    plot_concurrency_vs_latency(calls, out_dir)
    plot_concurrency_distribution(calls, out_dir)
    write_summary(results, calls, out_dir)

    print(f"[marble-tc] Done. Output: {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
