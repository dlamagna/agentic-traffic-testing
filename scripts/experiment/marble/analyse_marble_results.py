#!/usr/bin/env python3
"""
analyse_marble_results.py
=========================
Cross-topology and cross-domain analysis of MARBLE benchmark results.

Reads all <domain>_<topology>.jsonl files from an experiment results directory
and produces:

  score_by_topology.png     — box plot of aggregate score per topology
  score_by_domain.png       — box plot of aggregate score per domain
  duration_heatmap.png      — mean task duration (s) domain × topology
  fanout_comparison.png     — mean total_agent_calls per topology (bar)
  comms_comparison.png      — mean communication_sessions per topology (bar)
  error_rate.png            — % tasks with topology_error per topology (bar)
  results_summary.txt       — full numeric summary

Usage:
    python analyse_marble_results.py
           --results-dir <experiment_dir>/results/
           --output-dir  <experiment_dir>/plots/results/
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
except ImportError as exc:
    sys.exit(f"ERROR: missing dependency – {exc}\nInstall: pip install matplotlib numpy")

# ---------------------------------------------------------------------------
# Style — matches analyse_marble_iat.py and agentverse scripts
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
    "research":  "#9467bd",
    "coding":    "#8c564b",
    "bargaining": "#e377c2",
    "database":  "#7f7f7f",
}
TOPO_ORDER   = ["star", "chain", "tree", "graph"]
DOMAIN_ORDER = ["research", "coding", "bargaining", "database"]

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
    """Load all JSONL records from <domain>_<topology>.jsonl files."""
    records: List[Dict[str, Any]] = []
    for path in sorted(results_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    records.append(rec)
                except json.JSONDecodeError as exc:
                    print(f"  [warn] {path.name} line {lineno}: {exc}", file=sys.stderr)
    return records


def group_by(records: List[Dict[str, Any]], key: str) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in records:
        groups[rec.get(key, "unknown")].append(rec)
    return dict(groups)


def safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Plotting helpers
# ---------------------------------------------------------------------------

def _present(order: List[str], data: Dict[str, list]) -> List[str]:
    return [k for k in order if k in data and data[k]]


def plot_score_by_topology(records: List[Dict[str, Any]], out_dir: Path) -> None:
    by_topo = group_by(records, "coordinate_mode")
    topos = _present(TOPO_ORDER, {t: [r.get("score") for r in recs if r.get("score") is not None]
                                   for t, recs in by_topo.items()})
    if not topos:
        return
    data = [[safe_float(r.get("score")) for r in by_topo[t]] for t in topos]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.set_facecolor(PANEL_BG)
    bp = ax.boxplot(data, patch_artist=True, tick_labels=topos)
    for patch, topo in zip(bp["boxes"], topos):
        patch.set_facecolor(TOPO_COLORS.get(topo, "gray"))
        patch.set_alpha(0.7)
    ax.set_ylabel("Aggregate score (0–1)")
    ax.set_title("MARBLE — Task Score by Topology")
    ax.grid(True, axis="y")
    fig.tight_layout()
    out = out_dir / "score_by_topology.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  Saved: {out}")


def plot_score_by_domain(records: List[Dict[str, Any]], out_dir: Path) -> None:
    by_domain = group_by(records, "benchmark_domain")
    domains = _present(DOMAIN_ORDER, {d: [r.get("score") for r in recs if r.get("score") is not None]
                                       for d, recs in by_domain.items()})
    if not domains:
        return
    data = [[safe_float(r.get("score")) for r in by_domain[d]] for d in domains]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.set_facecolor(PANEL_BG)
    bp = ax.boxplot(data, patch_artist=True, tick_labels=domains)
    for patch, dom in zip(bp["boxes"], domains):
        patch.set_facecolor(DOMAIN_COLORS.get(dom, "gray"))
        patch.set_alpha(0.7)
    ax.set_ylabel("Aggregate score (0–1)")
    ax.set_title("MARBLE — Task Score by Domain")
    ax.grid(True, axis="y")
    fig.tight_layout()
    out = out_dir / "score_by_domain.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  Saved: {out}")


def plot_duration_heatmap(records: List[Dict[str, Any]], out_dir: Path) -> None:
    by_topo = group_by(records, "coordinate_mode")
    by_domain = group_by(records, "benchmark_domain")
    topos = _present(TOPO_ORDER, by_topo)
    domains = _present(DOMAIN_ORDER, by_domain)
    if not topos or not domains:
        return

    matrix = np.zeros((len(domains), len(topos)))
    for i, domain in enumerate(domains):
        for j, topo in enumerate(topos):
            subset = [r for r in records
                      if r.get("benchmark_domain") == domain
                      and r.get("coordinate_mode") == topo
                      and r.get("duration_s") is not None]
            if subset:
                matrix[i, j] = float(np.mean([safe_float(r["duration_s"]) for r in subset]))

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.set_facecolor(PANEL_BG)
    im = ax.imshow(matrix, aspect="auto", cmap="YlOrRd")
    ax.set_xticks(range(len(topos)))
    ax.set_xticklabels(topos)
    ax.set_yticks(range(len(domains)))
    ax.set_yticklabels(domains)
    plt.colorbar(im, ax=ax, label="Mean duration (s)")
    for i in range(len(domains)):
        for j in range(len(topos)):
            ax.text(j, i, f"{matrix[i, j]:.0f}s", ha="center", va="center", fontsize=9,
                    color="black" if matrix[i, j] < matrix.max() * 0.7 else "white")
    ax.set_title("MARBLE — Mean Task Duration (s): Domain × Topology")
    fig.tight_layout()
    out = out_dir / "duration_heatmap.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  Saved: {out}")


def _bar_chart(
    records: List[Dict[str, Any]],
    field: str,
    ylabel: str,
    title: str,
    out_path: Path,
) -> None:
    by_topo = group_by(records, "coordinate_mode")
    topos = _present(TOPO_ORDER, by_topo)
    if not topos:
        return
    means = [float(np.mean([safe_float(r.get(field)) for r in by_topo[t]])) for t in topos]
    colors = [TOPO_COLORS.get(t, "gray") for t in topos]
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.set_facecolor(PANEL_BG)
    bars = ax.bar(topos, means, color=colors, alpha=0.8, edgecolor="white")
    for bar, val in zip(bars, means):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.02 * max(means),
                f"{val:.1f}", ha="center", va="bottom", fontsize=9)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, axis="y")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {out_path}")


def plot_fanout(records: List[Dict[str, Any]], out_dir: Path) -> None:
    _bar_chart(records, "total_agent_calls", "Mean total_agent_calls",
               "MARBLE — Agent Calls (Fan-out) by Topology",
               out_dir / "fanout_comparison.png")


def plot_comms(records: List[Dict[str, Any]], out_dir: Path) -> None:
    _bar_chart(records, "communication_sessions", "Mean communication_sessions",
               "MARBLE — Communication Sessions by Topology",
               out_dir / "comms_comparison.png")


def plot_error_rate(records: List[Dict[str, Any]], out_dir: Path) -> None:
    by_topo = group_by(records, "coordinate_mode")
    topos = _present(TOPO_ORDER, by_topo)
    if not topos:
        return
    rates = []
    for t in topos:
        recs = by_topo[t]
        n_err = sum(1 for r in recs if r.get("topology_error"))
        rates.append(100.0 * n_err / len(recs) if recs else 0.0)
    colors = [TOPO_COLORS.get(t, "gray") for t in topos]
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.set_facecolor(PANEL_BG)
    bars = ax.bar(topos, rates, color=colors, alpha=0.8, edgecolor="white")
    for bar, val in zip(bars, rates):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                f"{val:.1f}%", ha="center", va="bottom", fontsize=9)
    ax.set_ylabel("Error rate (%)")
    ax.set_title("MARBLE — Task Error Rate by Topology")
    ax.set_ylim(0, max(rates) * 1.3 + 5)
    ax.grid(True, axis="y")
    fig.tight_layout()
    out = out_dir / "error_rate.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  Saved: {out}")


# ---------------------------------------------------------------------------
# Text summary
# ---------------------------------------------------------------------------

def write_summary(records: List[Dict[str, Any]], out_dir: Path) -> None:
    by_topo = group_by(records, "coordinate_mode")
    by_domain = group_by(records, "benchmark_domain")
    topos = _present(TOPO_ORDER, by_topo)
    domains = _present(DOMAIN_ORDER, by_domain)

    lines = [
        "MARBLE Results Summary",
        "=" * 60,
        f"Total records: {len(records)}",
        "",
        "Per-topology summary:",
        "-" * 40,
    ]
    for t in topos:
        recs = by_topo[t]
        scores = [safe_float(r.get("score")) for r in recs if r.get("score") is not None]
        durations = [safe_float(r.get("duration_s")) for r in recs if r.get("duration_s") is not None]
        calls = [safe_float(r.get("total_agent_calls")) for r in recs]
        comms = [safe_float(r.get("communication_sessions")) for r in recs]
        n_err = sum(1 for r in recs if r.get("topology_error"))
        lines += [
            f"  {t} (n={len(recs)}):",
            f"    score:    mean={np.mean(scores):.4f}  median={np.median(scores):.4f}  std={np.std(scores):.4f}" if scores else "    score:    no data",
            f"    duration: mean={np.mean(durations):.1f}s  median={np.median(durations):.1f}s  max={np.max(durations):.1f}s" if durations else "    duration: no data",
            f"    agent_calls: mean={np.mean(calls):.1f}   comms: mean={np.mean(comms):.1f}",
            f"    errors: {n_err}/{len(recs)} ({100*n_err/len(recs):.1f}%)",
            "",
        ]

    lines += ["Per-domain summary:", "-" * 40]
    for d in domains:
        recs = by_domain[d]
        scores = [safe_float(r.get("score")) for r in recs if r.get("score") is not None]
        lines += [
            f"  {d} (n={len(recs)}):",
            f"    score: mean={np.mean(scores):.4f}  median={np.median(scores):.4f}" if scores else "    score: no data",
            "",
        ]

    out = out_dir / "results_summary.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Saved: {out}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Analyse MARBLE benchmark results across topologies/domains.")
    parser.add_argument(
        "--results-dir",
        type=Path,
        required=True,
        help="Directory containing <domain>_<topology>.jsonl result files",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to write plots into (default: <results-dir>/../plots/results/)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    results_dir = args.results_dir
    if not results_dir.is_dir():
        sys.exit(f"ERROR: results directory not found: {results_dir}")

    out_dir = args.output_dir or (results_dir.parent / "plots" / "results")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[marble-results] Loading results from {results_dir} ...", file=sys.stderr)
    records = load_results(results_dir)
    if not records:
        sys.exit("ERROR: no records found in results directory")
    print(f"[marble-results] {len(records)} records loaded", file=sys.stderr)

    plot_score_by_topology(records, out_dir)
    plot_score_by_domain(records, out_dir)
    plot_duration_heatmap(records, out_dir)
    plot_fanout(records, out_dir)
    plot_comms(records, out_dir)
    plot_error_rate(records, out_dir)
    write_summary(records, out_dir)

    print(f"[marble-results] Done. Output: {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
