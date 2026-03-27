#!/usr/bin/env python3
"""
analyse_marble_iat.py
=====================
Statistical analysis of LLM inter-arrival time (IAT) distributions across
MARBLE coordination topologies (star, chain, tree, graph).

Reads logs/marble_llm_calls.jsonl (written by benchmarks/marble/topology.py),
groups records by coordinate_mode, computes IAT between consecutive calls
within each topology, and produces:

  iat_histogram.png   — overlaid KDE-smoothed histograms per topology
  iat_ecdf.png        — empirical CDF per topology
  iat_boxplot.png     — box plot (log scale) per topology
  iat_statistics.txt  — descriptive stats + KS pairwise tests

Usage:
    python analyse_marble_iat.py
           --call-log logs/marble_llm_calls.jsonl
           --output-dir <experiment_dir>/plots/iat/
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

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
    print("WARNING: scipy not available — KS tests and KDE will be skipped", file=sys.stderr)

# ---------------------------------------------------------------------------
# Style — matches agentverse scripts
# ---------------------------------------------------------------------------
DARK_BG  = "white"
PANEL_BG = "#f7f7f7"
GRID_COL = "#cccccc"
TEXT_COL = "#222222"
IAT_MAX_S = 300  # clip outliers beyond this for display

TOPO_COLORS = {
    "star":  "#1f77b4",
    "chain": "#ff7f0e",
    "tree":  "#2ca02c",
    "graph": "#d62728",
}
TOPO_ORDER = ["star", "chain", "tree", "graph"]

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

def load_call_log(path: Path) -> Dict[str, List[float]]:
    """
    Read marble_llm_calls.jsonl and return a dict mapping coordinate_mode
    to a sorted list of timestamp_start epoch seconds.
    """
    records: Dict[str, List[float]] = defaultdict(list)
    missing = 0
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"  [warn] line {lineno}: JSON parse error: {exc}", file=sys.stderr)
                continue
            mode = rec.get("coordinate_mode") or "unknown"
            ts_raw = rec.get("timestamp_start")
            if not ts_raw:
                missing += 1
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
                records[mode].append(ts)
            except (ValueError, TypeError) as exc:
                print(f"  [warn] line {lineno}: bad timestamp '{ts_raw}': {exc}", file=sys.stderr)

    if missing:
        print(f"  [warn] {missing} records missing timestamp_start (skipped)", file=sys.stderr)

    # Sort each topology's timestamps
    for mode in records:
        records[mode].sort()

    return dict(records)


def compute_iats(timestamps: List[float]) -> List[float]:
    """Return list of inter-arrival times (seconds) from sorted timestamps."""
    if len(timestamps) < 2:
        return []
    return [timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))]


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def describe(iats: List[float]) -> dict:
    if not iats:
        return {}
    arr = np.array(iats)
    d: dict = {
        "n": len(arr),
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "std": float(np.std(arr)),
        "min": float(np.min(arr)),
        "p25": float(np.percentile(arr, 25)),
        "p75": float(np.percentile(arr, 75)),
        "p95": float(np.percentile(arr, 95)),
        "max": float(np.max(arr)),
    }
    if SCIPY_AVAILABLE:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            d["skew"] = float(scipy_stats.skew(arr))
            d["kurtosis"] = float(scipy_stats.kurtosis(arr))
    return d


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def _present_topologies(iat_data: Dict[str, List[float]]) -> List[str]:
    return [t for t in TOPO_ORDER if t in iat_data and len(iat_data[t]) >= 2]


def plot_histogram(iat_data: Dict[str, List[float]], out_dir: Path) -> None:
    topos = _present_topologies(iat_data)
    if not topos:
        return
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.set_facecolor(PANEL_BG)
    for topo in topos:
        arr = np.array(iat_data[topo])
        arr_clip = arr[arr <= IAT_MAX_S]
        color = TOPO_COLORS.get(topo, "gray")
        ax.hist(arr_clip, bins=40, density=True, alpha=0.35, color=color, label=f"{topo} (n={len(arr)})")
        if SCIPY_AVAILABLE and len(arr_clip) >= 2:
            kde = scipy_stats.gaussian_kde(arr_clip)
            xs = np.linspace(0, arr_clip.max(), 300)
            ax.plot(xs, kde(xs), color=color, linewidth=2)
    ax.set_xlabel("Inter-arrival time (s)")
    ax.set_ylabel("Density")
    ax.set_title("MARBLE — LLM Request IAT by Topology")
    ax.legend()
    ax.grid(True)
    fig.tight_layout()
    out = out_dir / "iat_histogram.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  Saved: {out}")


def plot_ecdf(iat_data: Dict[str, List[float]], out_dir: Path) -> None:
    topos = _present_topologies(iat_data)
    if not topos:
        return
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.set_facecolor(PANEL_BG)
    for topo in topos:
        arr = np.sort(np.array(iat_data[topo]))
        arr_clip = arr[arr <= IAT_MAX_S]
        ecdf = np.arange(1, len(arr_clip) + 1) / len(arr_clip)
        color = TOPO_COLORS.get(topo, "gray")
        ax.plot(arr_clip, ecdf, color=color, linewidth=2, label=f"{topo} (n={len(arr)})")
    ax.set_xlabel("Inter-arrival time (s)")
    ax.set_ylabel("Cumulative probability")
    ax.set_title("MARBLE — LLM Request IAT ECDF by Topology")
    ax.legend()
    ax.grid(True)
    fig.tight_layout()
    out = out_dir / "iat_ecdf.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  Saved: {out}")


def plot_boxplot(iat_data: Dict[str, List[float]], out_dir: Path) -> None:
    topos = _present_topologies(iat_data)
    if not topos:
        return
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.set_facecolor(PANEL_BG)
    data = [np.array(iat_data[t]) for t in topos]
    colors = [TOPO_COLORS.get(t, "gray") for t in topos]
    bp = ax.boxplot(data, patch_artist=True, tick_labels=topos, showfliers=True)
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)
    ax.set_yscale("log")
    ax.set_ylabel("Inter-arrival time (s) — log scale")
    ax.set_title("MARBLE — LLM Request IAT Box Plot by Topology")
    ax.grid(True, axis="y")
    fig.tight_layout()
    out = out_dir / "iat_boxplot.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  Saved: {out}")


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------

def write_text_report(
    iat_data: Dict[str, List[float]],
    out_dir: Path,
) -> None:
    topos = _present_topologies(iat_data)
    lines: List[str] = [
        "MARBLE LLM Inter-Arrival Time Statistics",
        "=" * 60,
        "",
    ]

    for topo in topos:
        d = describe(iat_data[topo])
        lines += [
            f"Topology: {topo}  (n={d.get('n',0)} IATs)",
            f"  mean={d.get('mean',0):.3f}s  median={d.get('median',0):.3f}s  std={d.get('std',0):.3f}s",
            f"  min={d.get('min',0):.3f}s  p25={d.get('p25',0):.3f}s  p75={d.get('p75',0):.3f}s  p95={d.get('p95',0):.3f}s  max={d.get('max',0):.3f}s",
        ]
        if "skew" in d:
            lines.append(f"  skewness={d['skew']:.3f}  excess_kurtosis={d['kurtosis']:.3f}")
        lines.append("")

    # KS pairwise tests
    if SCIPY_AVAILABLE and len(topos) >= 2:
        lines += ["Kolmogorov-Smirnov pairwise tests", "-" * 40]
        for i, t1 in enumerate(topos):
            for t2 in topos[i + 1:]:
                a1 = np.array(iat_data[t1])
                a2 = np.array(iat_data[t2])
                ks_stat, p_val = scipy_stats.ks_2samp(a1, a2)
                sig = "***" if p_val < 0.001 else ("**" if p_val < 0.01 else ("*" if p_val < 0.05 else "ns"))
                lines.append(f"  {t1} vs {t2}: KS={ks_stat:.4f}  p={p_val:.4g}  {sig}")
        lines.append("")

    out = out_dir / "iat_statistics.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Saved: {out}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Analyse MARBLE LLM IAT distributions by topology.")
    parser.add_argument(
        "--call-log",
        type=Path,
        default=Path("logs/marble_llm_calls.jsonl"),
        help="Path to marble_llm_calls.jsonl (default: logs/marble_llm_calls.jsonl)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to write plots into (default: <call-log parent>/plots/iat/)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    call_log = args.call_log
    if not call_log.is_file():
        sys.exit(f"ERROR: call log not found: {call_log}")

    out_dir = args.output_dir or (call_log.parent.parent / "plots" / "iat")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[marble-iat] Reading {call_log} ...", file=sys.stderr)
    timestamps_by_topo = load_call_log(call_log)

    if not timestamps_by_topo:
        sys.exit("ERROR: no records found in call log")

    iat_data: Dict[str, List[float]] = {}
    for mode, tss in timestamps_by_topo.items():
        iats = compute_iats(tss)
        print(f"  {mode}: {len(tss)} calls → {len(iats)} IATs", file=sys.stderr)
        if iats:
            iat_data[mode] = iats

    if not iat_data:
        sys.exit("ERROR: not enough calls (need ≥2 per topology) to compute IAT")

    plot_histogram(iat_data, out_dir)
    plot_ecdf(iat_data, out_dir)
    plot_boxplot(iat_data, out_dir)
    write_text_report(iat_data, out_dir)

    print(f"[marble-iat] Done. Output: {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
