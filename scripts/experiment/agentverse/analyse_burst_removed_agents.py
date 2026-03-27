#!/usr/bin/env python3
"""
analyse_burst_removed_agents.py
================================
Extended IAT analysis with two additional views:

  1. vertical-burst-removed  — drops every IAT below a burst threshold
     (default 0.1 s) rather than collapsing parallel fan-out into one event.
     The remaining samples represent only the sequential gaps between
     genuinely sequential requests, showing what the distribution looks like
     when concurrent dispatch is simply excised.

  2. sub-agent count analysis — shows how the number of recruited sub-agents
     (n_experts) per vertical run affects the IAT distribution, near-zero
     fraction, and CV.

Outputs (all written to <output-dir>/):
  burst_comparison.png     — Figure 1: H / V-aggregated / V-burst-removed
  burst_comparison.txt     — Text report for Figure 1
  agent_count_iat.png      — Figure 2: IAT by n_experts for vertical runs
  agent_count_iat.txt      — Text report for Figure 2

Usage:
    python analyse_burst_removed_agents.py \\
        --experiment-dir <dir> \\
        [--output-dir <dir>] \\
        [--burst-threshold 0.1]

Defaults:
    --output-dir       <experiment-dir>/plots/
    --burst-threshold  0.1   (seconds; IATs below this are treated as bursts)
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import defaultdict
from datetime import datetime
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import _tasks_dir  # noqa: E402

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
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
# Style
# ---------------------------------------------------------------------------
DARK_BG  = "white"
PANEL_BG = "#f7f7f7"
GRID_COL = "#cccccc"
TEXT_COL = "#222222"
IAT_MAX_S = 100

H_COLOR  = "#1f77b4"   # horizontal
VA_COLOR = "#2ca02c"   # vertical aggregated
VR_COLOR = "#d62728"   # vertical burst-removed

# sub-agent count palette (1–5 experts) — ColorBrewer Set1 for max distinctness
AGENT_COLORS = {
    1: "#e41a1c",   # red
    2: "#377eb8",   # blue
    3: "#4daf4a",   # green
    4: "#ff7f00",   # orange
    5: "#984ea3",   # purple
}

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
    "legend.edgecolor": GRID_COL,
    "figure.dpi":       120,
    "font.size":        9,
    "axes.titlesize":   9,
})

DISC_PREFIXES = (
    "horizontal_discussion",
    "synthesize_discussion",
    "vertical_solver",
    "vertical_reviewer",
)


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def _infer_structure(data: dict) -> str:
    """Infer horizontal/vertical from request labels; fall back to metadata."""
    labels = {req.get("label", "") for req in data.get("llm_requests", [])}
    has_h = any(l.startswith("horizontal_discussion") for l in labels)
    has_v = any(l.startswith(("vertical_solver", "vertical_reviewer")) for l in labels)
    if has_h and not has_v:
        return "horizontal"
    if has_v:
        return "vertical"
    stages = data.get("stages", {})
    return (
        stages.get("recruitment", {}).get("communication_structure")
        or stages.get("decision", {}).get("structure_used", "")
        or ""
    ).lower()


def _aggregate_vertical_run(timestamps: list[float], requests: list[dict]) -> list[float]:
    """Collapse simultaneous reviewer requests within a vertical run."""
    groups: dict[tuple, list[float]] = defaultdict(list)
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


def _burst_remove(iats: np.ndarray, threshold: float) -> np.ndarray:
    """Return only IATs >= threshold (i.e., drop the near-zero burst events)."""
    return iats[iats >= threshold]


def _n_experts(data: dict) -> int:
    """Return the number of recruited experts for this run."""
    experts = data.get("stages", {}).get("recruitment", {}).get("experts", [])
    return len(experts)


# ---------------------------------------------------------------------------
# Main loader
# ---------------------------------------------------------------------------

def load_data(
    experiment_dir: Path,
    burst_threshold: float,
) -> tuple[
    dict[str, np.ndarray],          # pools: horizontal / vertical_agg / vertical_br
    dict[str, int],                 # run counts: horizontal / vertical
    dict[int, list[np.ndarray]],    # per-agent raw IATs for vertical runs (keyed by n)
    dict[int, list[np.ndarray]],    # per-agent burst-removed IATs
    dict[int, list[np.ndarray]],    # per-agent raw IATs for horizontal runs (keyed by n)
]:
    pools_lists: dict[str, list[np.ndarray]] = {
        "horizontal": [], "vertical_agg": [], "vertical_br": [],
    }
    run_counts = {"horizontal": 0, "vertical": 0}
    per_agent_raw: dict[int, list[np.ndarray]] = defaultdict(list)
    per_agent_br:  dict[int, list[np.ndarray]] = defaultdict(list)
    per_agent_h_raw: dict[int, list[np.ndarray]] = defaultdict(list)

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

        structure = _infer_structure(data)
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
                timestamps.append(datetime.fromisoformat(ts_str).timestamp())
                requests.append(req)
            except Exception:
                continue

        if len(timestamps) < 2:
            continue

        sorted_ts = sorted(timestamps)
        raw_iats  = np.diff(np.array(sorted_ts))
        run_counts[structure] += 1

        if structure == "horizontal":
            pools_lists["horizontal"].append(raw_iats)
            n = _n_experts(data)
            if n > 0:
                per_agent_h_raw[n].append(raw_iats)

        elif structure == "vertical":
            # aggregated view
            agg = _aggregate_vertical_run(timestamps, requests)
            if len(agg) >= 2:
                pools_lists["vertical_agg"].append(np.diff(np.array(agg)))

            # burst-removed view
            br_iats = _burst_remove(raw_iats, burst_threshold)
            if len(br_iats) > 0:
                pools_lists["vertical_br"].append(br_iats)

            # per-agent breakdown
            n = _n_experts(data)
            if n > 0:
                per_agent_raw[n].append(raw_iats)
                if len(br_iats) > 0:
                    per_agent_br[n].append(br_iats)

    pools = {
        k: np.concatenate(v) if v else np.array([])
        for k, v in pools_lists.items()
    }
    return pools, run_counts, per_agent_raw, per_agent_br, per_agent_h_raw


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def descriptive_stats(arr: np.ndarray) -> dict:
    if len(arr) == 0:
        return {}
    mean = float(arr.mean())
    std  = float(arr.std())
    return {
        "n":             len(arr),
        "mean":          mean,
        "std":           std,
        "cv":            std / mean if mean > 0 else float("nan"),
        "median":        float(np.percentile(arr, 50)),
        "p25":           float(np.percentile(arr, 25)),
        "p75":           float(np.percentile(arr, 75)),
        "p95":           float(np.percentile(arr, 95)),
        "p99":           float(np.percentile(arr, 99)),
        "min":           float(arr.min()),
        "max":           float(arr.max()),
        "skewness":      float(scipy_stats.skew(arr))     if SCIPY_AVAILABLE else float("nan"),
        "kurtosis":      float(scipy_stats.kurtosis(arr)) if SCIPY_AVAILABLE else float("nan"),
        "frac_lt_1s":    float(np.mean(arr < 1.0)),
        "frac_lt_10ms":  float(np.mean(arr < 0.01)),
    }


def pairwise_ks_mw(a: np.ndarray, b: np.ndarray) -> dict:
    if not SCIPY_AVAILABLE or len(a) < 2 or len(b) < 2:
        nan = float("nan")
        return dict(ks_stat=nan, ks_p=nan, mw_stat=nan, mw_p=nan,
                    cohen_d=nan, cliff_delta=nan)
    ks_stat, ks_p = scipy_stats.ks_2samp(a, b)
    mw_stat, mw_p = scipy_stats.mannwhitneyu(a, b, alternative="two-sided")
    pooled_std = np.sqrt((a.var() + b.var()) / 2)
    cohen_d    = (a.mean() - b.mean()) / pooled_std if pooled_std > 0 else float("nan")
    u_gt, _    = scipy_stats.mannwhitneyu(a, b, alternative="greater")
    cliff      = (2 * u_gt / (len(a) * len(b))) - 1
    return dict(ks_stat=float(ks_stat), ks_p=float(ks_p),
                mw_stat=float(mw_stat), mw_p=float(mw_p),
                cohen_d=float(cohen_d), cliff_delta=float(cliff))


# ---------------------------------------------------------------------------
# Text reports
# ---------------------------------------------------------------------------

def _pstr(p: float) -> str:
    if p != p:        return "—"
    if p < 1e-300:    return "< 1e-300"
    return f"{p:.2e}"


def build_comparison_report(
    pools: dict[str, np.ndarray],
    run_counts: dict[str, int],
    burst_threshold: float,
) -> str:
    buf = StringIO()

    def w(line=""):
        buf.write(line + "\n")

    SEP = "=" * 76
    w(SEP)
    w("  BURST-REMOVAL COMPARISON  (discussion stage, within-run IATs)")
    w(f"  burst threshold = {burst_threshold:.3f} s")
    w(SEP)

    keys  = ("horizontal", "vertical_agg", "vertical_br")
    names = {
        "horizontal":   f"horizontal        ({run_counts.get('horizontal', 0)} runs)",
        "vertical_agg": f"vertical-aggregated",
        "vertical_br":  f"vertical-burst-removed  (IAT ≥ {burst_threshold:.3f} s)",
    }

    stats_all = {k: descriptive_stats(pools[k]) for k in keys}

    w()
    w("DESCRIPTIVE STATISTICS")
    w("-" * 76)
    header = f"  {'Metric':<22}"
    for k in keys:
        header += f"  {names[k]:<26}"
    w(header)
    w("-" * 76)

    rows = [
        ("n",            "n",              lambda v: f"{int(v)}"),
        ("mean",         "mean (s)",       lambda v: f"{v:.4f}"),
        ("std",          "std (s)",        lambda v: f"{v:.4f}"),
        ("cv",           "CV (std/mean)",  lambda v: f"{v:.4f}"),
        ("median",       "median (s)",     lambda v: f"{v:.4f}"),
        ("p25",          "p25 (s)",        lambda v: f"{v:.4f}"),
        ("p75",          "p75 (s)",        lambda v: f"{v:.4f}"),
        ("p95",          "p95 (s)",        lambda v: f"{v:.4f}"),
        ("p99",          "p99 (s)",        lambda v: f"{v:.4f}"),
        ("skewness",     "skewness",       lambda v: f"{v:.4f}"),
        ("kurtosis",     "kurtosis",       lambda v: f"{v:.4f}"),
        ("frac_lt_1s",   "frac < 1 s",    lambda v: f"{v:.4f}  ({v*100:.1f}%)"),
        ("frac_lt_10ms", "frac < 10 ms",  lambda v: f"{v:.4f}  ({v*100:.1f}%)"),
    ]
    for key, label, fmt in rows:
        line = f"  {label:<22}"
        for k in keys:
            val = stats_all.get(k, {}).get(key, float("nan"))
            line += f"  {fmt(val):<26}"
        w(line)

    w()
    w("  CV note: 1.0 = exponential/Poisson;  >1 = over-dispersed (bursty)")
    w()
    w("INTERPRETATION")
    w("-" * 76)
    w("  vertical-aggregated:   collapses each parallel fan-out into a single")
    w("                         event — changes unit from request to stage.")
    w("  vertical-burst-removed: removes the burst IATs entirely — keeps only")
    w("                         sequential gaps; sample size decreases but the")
    w("                         unit remains individual LLM requests.")
    w("  If both transformations converge on similar CV/shape, the parallel")
    w("  dispatch is truly the dominant source of burstiness.")
    w("  If they differ, additional sequential-level burstiness is present.")
    w()

    # pairwise tests
    pairs = [
        ("horizontal",   "vertical_agg", "H vs V-aggregated"),
        ("horizontal",   "vertical_br",  "H vs V-burst-removed"),
        ("vertical_agg", "vertical_br",  "V-aggregated vs V-burst-removed"),
    ]
    w(SEP)
    w("KOLMOGOROV-SMIRNOV + MANN-WHITNEY TESTS")
    w("-" * 76)
    for a, b, label in pairs:
        t = pairwise_ks_mw(pools.get(a, np.array([])), pools.get(b, np.array([])))
        verdict_ks = "REJECT H0 *" if t["ks_p"] < 0.05 else "fail to reject"
        verdict_mw = "REJECT H0 *" if t["mw_p"] < 0.05 else "fail to reject"
        w(f"  {label}")
        w(f"    KS stat={t['ks_stat']:.4f}  p={_pstr(t['ks_p'])}  → {verdict_ks}")
        w(f"    MW p={_pstr(t['mw_p'])}  Cohen d={t['cohen_d']:+.3f}  Cliff δ={t['cliff_delta']:+.3f}  → {verdict_mw}")
        w()

    w(SEP)
    return buf.getvalue()


def build_agent_count_report(
    per_agent_raw: dict[int, list[np.ndarray]],
    per_agent_br:  dict[int, list[np.ndarray]],
    burst_threshold: float,
) -> str:
    buf = StringIO()

    def w(line=""):
        buf.write(line + "\n")

    SEP = "=" * 76
    w(SEP)
    w("  VERTICAL: SUB-AGENT COUNT ANALYSIS  (burst-removed IATs by n_experts)")
    w(f"  burst threshold = {burst_threshold:.3f} s")
    w(SEP)

    agent_counts = sorted(set(list(per_agent_raw.keys()) + list(per_agent_br.keys())))

    # --- raw IATs by n_agents ---
    w()
    w("RAW IATS BY N_AGENTS")
    w("-" * 76)
    header = f"  {'Metric':<22}" + "".join(f"  {'n='+str(n):<14}" for n in agent_counts)
    w(header)
    w("-" * 76)
    pools_raw = {n: np.concatenate(per_agent_raw[n]) for n in agent_counts if n in per_agent_raw}
    pools_br  = {n: np.concatenate(per_agent_br[n])  for n in agent_counts if n in per_agent_br}

    rows = [
        ("n",            "n",             lambda v: f"{int(v)}"),
        ("mean",         "mean (s)",      lambda v: f"{v:.4f}"),
        ("cv",           "CV",            lambda v: f"{v:.4f}"),
        ("median",       "median (s)",    lambda v: f"{v:.4f}"),
        ("p95",          "p95 (s)",       lambda v: f"{v:.4f}"),
        ("skewness",     "skewness",      lambda v: f"{v:.4f}"),
        ("frac_lt_1s",   "frac < 1 s",   lambda v: f"{v:.4f} ({v*100:.1f}%)"),
        ("frac_lt_10ms", "frac < 10 ms", lambda v: f"{v:.4f} ({v*100:.1f}%)"),
    ]
    for key, label, fmt in rows:
        line = f"  {label:<22}"
        for n in agent_counts:
            s   = descriptive_stats(pools_raw.get(n, np.array([])))
            val = s.get(key, float("nan"))
            line += f"  {fmt(val):<14}"
        w(line)

    # --- burst-removed IATs by n_agents ---
    w()
    w("BURST-REMOVED IATS BY N_AGENTS")
    w("-" * 76)
    w(header)
    w("-" * 76)
    for key, label, fmt in rows:
        line = f"  {label:<22}"
        for n in agent_counts:
            s   = descriptive_stats(pools_br.get(n, np.array([])))
            val = s.get(key, float("nan"))
            line += f"  {fmt(val):<14}"
        w(line)

    # --- burst fraction by n_agents (raw: frac < threshold) ---
    w()
    w("BURST FRACTION VS N_AGENTS  (fraction of raw IATs < threshold)")
    w("-" * 76)
    for n in agent_counts:
        arr = pools_raw.get(n, np.array([]))
        if len(arr) == 0:
            continue
        runs    = len(per_agent_raw.get(n, []))
        frac    = float(np.mean(arr < burst_threshold))
        n_burst = int(np.sum(arr < burst_threshold))
        w(f"  n_experts={n}  runs={runs:3d}  "
          f"raw_n={len(arr):4d}  burst_n={n_burst:4d}  "
          f"burst_frac={frac:.3f} ({frac*100:.1f}%)")

    w()
    w("INTERPRETATION")
    w("-" * 76)
    w("  In a vertical run with N experts, each discussion round triggers N")
    w("  simultaneous reviewer requests. The burst fraction should therefore")
    w("  scale roughly as (N-1)/total_requests_per_round — higher N means")
    w("  more simultaneous requests and a larger near-zero spike.")
    w("  If CV-burst-removed is stable across n_experts values, the sequential")
    w("  inter-stage timing is independent of team size (only the burst size")
    w("  changes). If CV-burst-removed grows with N, larger teams also produce")
    w("  more variable sequential pacing.")

    w()
    w(SEP)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Table helper
# ---------------------------------------------------------------------------

def _table_ax(ax, col_labels, rows, col_widths=None):
    ax.axis("off")
    tbl = ax.table(
        cellText=rows,
        colLabels=col_labels,
        cellLoc="center",
        loc="center",
        bbox=[0.0, 0.0, 1.0, 1.0],
        colWidths=col_widths,
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(7.5)
    for (row_idx, _col_idx), cell in tbl.get_celld().items():
        cell.set_edgecolor(GRID_COL)
        if row_idx == 0:
            cell.set_facecolor("#d0d8e8")
            cell.set_text_props(fontweight="bold", fontsize=7.5)
        elif row_idx % 2 == 0:
            cell.set_facecolor("#eeeeee")
        else:
            cell.set_facecolor("white")


# ---------------------------------------------------------------------------
# Figure 1: burst comparison
# ---------------------------------------------------------------------------

def plot_comparison(
    pools: dict[str, np.ndarray],
    run_counts: dict[str, int],
    burst_threshold: float,
    output_path: Path,
) -> None:
    series = [
        (pools["horizontal"],   H_COLOR,  f"horizontal  (n={len(pools['horizontal'])} IATs, {run_counts.get('horizontal',0)} runs)"),
        (pools["vertical_agg"], VA_COLOR, f"vertical-aggregated  (n={len(pools['vertical_agg'])} IATs)"),
        (pools["vertical_br"],  VR_COLOR, f"vertical-burst-removed  (n={len(pools['vertical_br'])} IATs, threshold={burst_threshold:.2f}s)"),
    ]

    fig = plt.figure(figsize=(18, 16))
    fig.patch.set_facecolor(DARK_BG)
    gs  = gridspec.GridSpec(3, 2, figure=fig, hspace=0.42, wspace=0.30,
                            top=0.93, bottom=0.04, left=0.05, right=0.97)
    axes = [[fig.add_subplot(gs[r, c]) for c in range(2)] for r in range(3)]

    fig.suptitle(
        f"IAT Comparison: Horizontal · Vertical-Aggregated · Vertical-Burst-Removed  (threshold={burst_threshold:.3f} s)",
        fontsize=11, color=TEXT_COL, fontweight="bold",
    )

    # [0,0] Histogram + KDE
    ax = axes[0][0]
    ax.set_title("IAT Histogram + KDE  (clipped at 100 s)", loc="left")
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("density")
    ax.grid(True)
    for arr, color, label in series:
        if len(arr) == 0:
            continue
        clipped = arr[arr <= IAT_MAX_S]
        n_over  = len(arr) - len(clipped)
        lbl     = label if n_over == 0 else f"{label}  ({n_over} clipped)"
        ax.hist(clipped, bins=40, density=True, alpha=0.30, color=color, label=lbl)
        if SCIPY_AVAILABLE and len(clipped) > 5:
            kde = scipy_stats.gaussian_kde(clipped)
            xs  = np.linspace(0, IAT_MAX_S, 400)
            ax.plot(xs, kde(xs), color=color, linewidth=2.2)
    ax.set_xlim(0, IAT_MAX_S)
    ax.legend(fontsize=6.5)

    # [0,0] inset: zoom on 0–2 s to show the burst spike
    ax_inset = ax.inset_axes([0.38, 0.35, 0.60, 0.60])
    ax_inset.set_facecolor(PANEL_BG)
    ax_inset.set_title("zoom: 0–2 s", fontsize=7)
    for arr, color, _ in series:
        if len(arr) == 0:
            continue
        clipped_z = arr[arr <= 2.0]
        if len(clipped_z) > 2:
            ax_inset.hist(clipped_z, bins=30, density=True, alpha=0.35, color=color)
            if SCIPY_AVAILABLE:
                kde_z = scipy_stats.gaussian_kde(clipped_z)
                xs_z  = np.linspace(0, 2.0, 200)
                ax_inset.plot(xs_z, kde_z(xs_z), color=color, linewidth=1.5)
    ax_inset.set_xlim(0, 2.0)
    ax_inset.tick_params(labelsize=6)
    ax.axvline(burst_threshold, color="black", linestyle=":", linewidth=1.0,
               alpha=0.6, label=f"threshold {burst_threshold:.2f}s")

    # [0,1] ECDF
    ax = axes[0][1]
    ax.set_title("ECDF  (dashed=p50, dotted=p95)", loc="left")
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("P(X ≤ x)")
    ax.set_ylim(0, 1.05)
    ax.set_xlim(0, IAT_MAX_S)
    ax.grid(True)
    for i, (arr, color, label) in enumerate(series):
        if len(arr) == 0:
            continue
        clipped = np.sort(arr[arr <= IAT_MAX_S])
        y = np.arange(1, len(clipped) + 1) / len(clipped)
        ax.plot(clipped, y, color=color, linewidth=2, label=label)
        for pct, ls in [(50, "--"), (95, ":")]:
            pval = np.percentile(clipped, pct)
            ax.axvline(pval, color=color, linestyle=ls, alpha=0.6, linewidth=1)
            ax.text(pval, 0.02 + i * 0.07,
                    f"p{pct}={pval:.1f}s", color=color, fontsize=5.5, ha="left")
    ax.legend(fontsize=6.5)

    # [1,0] Box plot
    ax = axes[1][0]
    ax.set_title("IAT Box Plot  (clipped at 100 s)", loc="left")
    ax.set_xlabel("interarrival time (s)")
    ax.grid(True, axis="x")
    bp_data, bp_labels, bp_colors = [], [], []
    for arr, color, label in series:
        if len(arr):
            bp_data.append(arr[arr <= IAT_MAX_S])
            bp_labels.append(label.split("  (")[0])
            bp_colors.append(color)
    if bp_data:
        bp = ax.boxplot(bp_data, positions=range(1, len(bp_data) + 1),
                        vert=False, patch_artist=True, widths=0.5,
                        flierprops=dict(marker=".", markersize=2, alpha=0.25))
        for patch, color in zip(bp["boxes"], bp_colors):
            patch.set_facecolor(color)
            patch.set_alpha(0.55)
        for median in bp["medians"]:
            median.set_color(TEXT_COL)
            median.set_linewidth(1.5)
        ax.set_yticks(range(1, len(bp_data) + 1))
        ax.set_yticklabels(bp_labels, fontsize=7.5)
        ax.set_xlim(0, IAT_MAX_S)

    # [1,1] Near-zero fraction bar
    ax = axes[1][1]
    ax.set_title("Near-Zero IAT Fraction  (< 1 s)", loc="left")
    ax.set_ylabel("fraction of IATs < 1 s")
    ax.grid(True, axis="y")
    bar_names, bar_fracs, bar_colors = [], [], []
    for arr, color, label in series:
        if len(arr):
            bar_names.append(label.split("  (")[0])
            bar_fracs.append(float(np.mean(arr < 1.0)))
            bar_colors.append(color)
    if bar_names:
        xpos = np.arange(len(bar_names))
        bars = ax.bar(xpos, bar_fracs, color=bar_colors, alpha=0.75, width=0.5)
        ax.set_xticks(xpos)
        ax.set_xticklabels(bar_names, fontsize=7)
        ax.set_ylim(0, 1.05)
        for bar, frac in zip(bars, bar_fracs):
            ax.text(bar.get_x() + bar.get_width() / 2,
                    frac + 0.02, f"{frac:.1%}",
                    ha="center", va="bottom", fontsize=8)

    # [2,0] Descriptive stats table
    ax = axes[2][0]
    ax.set_title("Descriptive Statistics", loc="left")
    stats_all = {k: descriptive_stats(pools[k]) for k in ("horizontal", "vertical_agg", "vertical_br")}
    stat_rows_def = [
        ("n",            "n",            lambda v: f"{int(v)}"),
        ("mean",         "mean (s)",     lambda v: f"{v:.2f}"),
        ("std",          "std (s)",      lambda v: f"{v:.2f}"),
        ("cv",           "CV",           lambda v: f"{v:.3f}"),
        ("median",       "median (s)",   lambda v: f"{v:.2f}"),
        ("p95",          "p95 (s)",      lambda v: f"{v:.2f}"),
        ("skewness",     "skewness",     lambda v: f"{v:.2f}"),
        ("kurtosis",     "kurtosis",     lambda v: f"{v:.2f}"),
        ("frac_lt_1s",   "frac < 1 s",  lambda v: f"{v:.1%}"),
        ("frac_lt_10ms", "frac < 10 ms",lambda v: f"{v:.1%}"),
    ]
    col_labels_d = ["Metric", "Horizontal", "V-aggregated", "V-burst-removed"]
    tbl_rows_d = []
    for key, label, fmt in stat_rows_def:
        row = [label]
        for k in ("horizontal", "vertical_agg", "vertical_br"):
            val = stats_all.get(k, {}).get(key, float("nan"))
            row.append(fmt(val) if val == val else "—")
        tbl_rows_d.append(row)
    _table_ax(ax, col_labels_d, tbl_rows_d, col_widths=[0.28, 0.24, 0.24, 0.24])

    # [2,1] Test results table
    ax = axes[2][1]
    ax.set_title("Statistical Tests (KS · Mann-Whitney · Effect sizes)", loc="left")
    pairs = [
        ("horizontal",   "vertical_agg", "H vs V-agg"),
        ("horizontal",   "vertical_br",  "H vs V-burst-rm"),
        ("vertical_agg", "vertical_br",  "V-agg vs V-burst-rm"),
    ]
    col_labels_t = ["Pair", "KS stat", "KS p", "MW p", "Cohen d", "Cliff δ"]
    tbl_rows_t = []
    for a, b, label in pairs:
        t = pairwise_ks_mw(pools.get(a, np.array([])), pools.get(b, np.array([])))
        tbl_rows_t.append([
            label,
            f"{t['ks_stat']:.4f}",
            _pstr(t["ks_p"]) + (" *" if t["ks_p"] < 0.05 else ""),
            _pstr(t["mw_p"]) + (" *" if t["mw_p"] < 0.05 else ""),
            f"{t['cohen_d']:+.3f}",
            f"{t['cliff_delta']:+.3f}",
        ])
    tbl_rows_t.append(["* p < 0.05", "", "", "", "", ""])
    _table_ax(ax, col_labels_t, tbl_rows_t, col_widths=[0.28, 0.12, 0.15, 0.15, 0.14, 0.14])

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        fig.savefig(output_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"  saved  {output_path}")


# ---------------------------------------------------------------------------
# Figure 2: sub-agent count analysis
# ---------------------------------------------------------------------------

def plot_agent_count(
    per_agent_raw: dict[int, list[np.ndarray]],
    per_agent_br:  dict[int, list[np.ndarray]],
    burst_threshold: float,
    output_path: Path,
) -> None:
    agent_counts = sorted(set(list(per_agent_raw.keys()) + list(per_agent_br.keys())))
    if not agent_counts:
        print("  [warn] no vertical runs with agent count data — skipping agent_count_iat.png")
        return

    pools_raw = {n: np.concatenate(per_agent_raw[n]) for n in agent_counts if n in per_agent_raw}
    pools_br  = {n: np.concatenate(per_agent_br[n])  for n in agent_counts if n in per_agent_br}

    fig = plt.figure(figsize=(18, 20))
    fig.patch.set_facecolor(DARK_BG)
    gs  = gridspec.GridSpec(4, 2, figure=fig, hspace=0.48, wspace=0.30,
                            top=0.94, bottom=0.04, left=0.05, right=0.97)
    axes = [[fig.add_subplot(gs[r, c]) for c in range(2)] for r in range(4)]

    fig.suptitle(
        "Vertical: IAT by Number of Sub-Agents — raw and burst-removed",
        fontsize=11, color=TEXT_COL, fontweight="bold",
    )

    # [0,0] Raw IAT histograms by n_agents
    ax = axes[0][0]
    ax.set_title("Raw IAT distribution by n_experts  (clipped at 100 s)", loc="left")
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("density")
    ax.grid(True)
    for n in agent_counts:
        arr = pools_raw.get(n, np.array([]))
        if len(arr) == 0:
            continue
        color  = AGENT_COLORS.get(n, "#7f7f7f")
        clipped = arr[arr <= IAT_MAX_S]
        ax.hist(clipped, bins=40, density=True, alpha=0.25, color=color)
        if SCIPY_AVAILABLE and len(clipped) > 5:
            kde = scipy_stats.gaussian_kde(clipped)
            xs  = np.linspace(0, IAT_MAX_S, 400)
            ax.plot(xs, kde(xs), color=color, linewidth=2.2,
                    label=f"n={n}  (IATs={len(arr)}, runs={len(per_agent_raw.get(n,[]))})")
    ax.set_xlim(0, IAT_MAX_S)
    ax.legend(fontsize=7)

    # [0,1] Burst-removed IAT histograms by n_agents
    ax = axes[0][1]
    ax.set_title("Burst-removed IAT distribution by n_experts  (clipped at 100 s)", loc="left")
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("density")
    ax.grid(True)
    for n in agent_counts:
        arr = pools_br.get(n, np.array([]))
        if len(arr) == 0:
            continue
        color   = AGENT_COLORS.get(n, "#7f7f7f")
        clipped = arr[arr <= IAT_MAX_S]
        ax.hist(clipped, bins=40, density=True, alpha=0.25, color=color)
        if SCIPY_AVAILABLE and len(clipped) > 5:
            kde = scipy_stats.gaussian_kde(clipped)
            xs  = np.linspace(0, IAT_MAX_S, 400)
            ax.plot(xs, kde(xs), color=color, linewidth=2.2,
                    label=f"n={n}  (IATs={len(arr)})")
    ax.set_xlim(0, IAT_MAX_S)
    ax.legend(fontsize=7)

    # [1,0] ECDF raw by n_agents
    ax = axes[1][0]
    ax.set_title("ECDF — raw IATs by n_experts", loc="left")
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("P(X ≤ x)")
    ax.set_xlim(0, IAT_MAX_S)
    ax.set_ylim(0, 1.05)
    ax.grid(True)
    for n in agent_counts:
        arr = pools_raw.get(n, np.array([]))
        if len(arr) == 0:
            continue
        clipped = np.sort(arr[arr <= IAT_MAX_S])
        y = np.arange(1, len(clipped) + 1) / len(clipped)
        ax.plot(clipped, y, color=AGENT_COLORS.get(n, "#7f7f7f"),
                linewidth=2, label=f"n={n}")
    ax.legend(fontsize=8)

    # [1,1] ECDF burst-removed by n_agents
    ax = axes[1][1]
    ax.set_title("ECDF — burst-removed IATs by n_experts", loc="left")
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("P(X ≤ x)")
    ax.set_xlim(0, IAT_MAX_S)
    ax.set_ylim(0, 1.05)
    ax.grid(True)
    for n in agent_counts:
        arr = pools_br.get(n, np.array([]))
        if len(arr) == 0:
            continue
        clipped = np.sort(arr[arr <= IAT_MAX_S])
        y = np.arange(1, len(clipped) + 1) / len(clipped)
        ax.plot(clipped, y, color=AGENT_COLORS.get(n, "#7f7f7f"),
                linewidth=2, label=f"n={n}")
    ax.legend(fontsize=8)

    # [2,0] CV vs n_agents  (raw and burst-removed)
    ax = axes[2][0]
    ax.set_title("CV vs n_experts  (raw vs burst-removed)", loc="left")
    ax.set_xlabel("n_experts")
    ax.set_ylabel("CV (std / mean)")
    ax.grid(True)
    ns       = agent_counts
    cvs_raw  = [descriptive_stats(pools_raw.get(n, np.array([]))).get("cv", float("nan")) for n in ns]
    cvs_br   = [descriptive_stats(pools_br.get(n,  np.array([]))).get("cv", float("nan")) for n in ns]
    ax.plot(ns, cvs_raw, "o-", color=V_COLOR if "V_COLOR" in dir() else "#ff7f0e",
            linewidth=2, markersize=8, label="raw")
    ax.plot(ns, cvs_br,  "s--", color=VR_COLOR, linewidth=2, markersize=8, label="burst-removed")
    ax.axhline(1.0, color="black", linestyle=":", linewidth=1, alpha=0.6, label="CV=1 (Poisson)")
    ax.set_xticks(ns)
    ax.legend(fontsize=8)

    # annotate
    for n, cv_r, cv_b in zip(ns, cvs_raw, cvs_br):
        if cv_r == cv_r:
            ax.annotate(f"{cv_r:.2f}", (n, cv_r), textcoords="offset points",
                        xytext=(5, 6), fontsize=7)
        if cv_b == cv_b:
            ax.annotate(f"{cv_b:.2f}", (n, cv_b), textcoords="offset points",
                        xytext=(5, -12), fontsize=7, color=VR_COLOR)

    # [2,1] Burst fraction vs n_agents — line + linear fit
    ax = axes[2][1]
    ax.set_title(f"Burst fraction vs n_experts  (IAT < {burst_threshold:.2f} s)", loc="left")
    ax.set_xlabel("n_experts")
    ax.set_ylabel(f"fraction of IATs < {burst_threshold:.2f} s")
    ax.grid(True)
    burst_fracs = []
    for n in ns:
        arr = pools_raw.get(n, np.array([]))
        frac = float(np.mean(arr < burst_threshold)) if len(arr) > 0 else float("nan")
        burst_fracs.append(frac)
    colors_n = [AGENT_COLORS.get(n, "#7f7f7f") for n in ns]
    # scatter points coloured by n_agents
    for n, frac, color in zip(ns, burst_fracs, colors_n):
        if frac == frac:
            ax.plot(n, frac, "o", color=color, markersize=9, zorder=3)
            ax.text(n, frac + 0.03, f"{frac:.1%}", ha="center", va="bottom", fontsize=8)
    # connect with a line
    valid = [(n, f) for n, f in zip(ns, burst_fracs) if f == f]
    if valid:
        vns, vfs = zip(*valid)
        ax.plot(vns, vfs, "-", color="#555555", linewidth=1.5, zorder=2)
        # linear fit
        if SCIPY_AVAILABLE and len(vns) >= 2:
            slope, intercept, r, _, _ = scipy_stats.linregress(vns, vfs)
            xs_fit = np.linspace(min(vns), max(vns), 200)
            ax.plot(xs_fit, intercept + slope * xs_fit, "--", color="#222222",
                    linewidth=1.5, zorder=1,
                    label=f"linear fit: y = {slope:.3f}x + {intercept:.3f}  (R²={r**2:.3f})")
            ax.legend(fontsize=7.5)
    ax.set_xticks(ns)
    ax.set_ylim(0, 1.05)

    # [3,0] Stats table — raw
    ax = axes[3][0]
    ax.set_title("Raw IAT stats by n_experts", loc="left")
    stat_rows_def = [
        ("n",            "n",            lambda v: f"{int(v)}"),
        ("mean",         "mean (s)",     lambda v: f"{v:.2f}"),
        ("cv",           "CV",           lambda v: f"{v:.3f}"),
        ("median",       "median (s)",   lambda v: f"{v:.2f}"),
        ("p95",          "p95 (s)",      lambda v: f"{v:.2f}"),
        ("skewness",     "skewness",     lambda v: f"{v:.2f}"),
        ("frac_lt_10ms", "frac < 10 ms",lambda v: f"{v:.1%}"),
    ]
    col_labels = ["Metric"] + [f"n={n}" for n in ns]
    tbl_rows = []
    for key, label, fmt in stat_rows_def:
        row = [label]
        for n in ns:
            val = descriptive_stats(pools_raw.get(n, np.array([]))).get(key, float("nan"))
            row.append(fmt(val) if val == val else "—")
        tbl_rows.append(row)
    col_w = [0.30] + [0.70 / len(ns)] * len(ns)
    _table_ax(ax, col_labels, tbl_rows, col_widths=col_w)

    # [3,1] Stats table — burst-removed
    ax = axes[3][1]
    ax.set_title("Burst-removed IAT stats by n_experts", loc="left")
    tbl_rows = []
    for key, label, fmt in stat_rows_def:
        row = [label]
        for n in ns:
            val = descriptive_stats(pools_br.get(n, np.array([]))).get(key, float("nan"))
            row.append(fmt(val) if val == val else "—")
        tbl_rows.append(row)
    _table_ax(ax, col_labels, tbl_rows, col_widths=col_w)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        fig.savefig(output_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"  saved  {output_path}")


# ---------------------------------------------------------------------------
# Figure 3: pairwise KS tests between burst-removed IAT distributions
# ---------------------------------------------------------------------------

def build_ks_pairwise_report(
    per_agent_br: dict[int, list[np.ndarray]],
    label: str = "Vertical burst-removed",
) -> str:
    buf = StringIO()

    def w(line=""):
        buf.write(line + "\n")

    agent_counts = sorted(per_agent_br.keys())
    pools = {n: np.concatenate(per_agent_br[n]) for n in agent_counts}

    SEP = "=" * 76
    w(SEP)
    w(f"  PAIRWISE KS TESTS — {label} IAT distributions by n_experts")
    w(SEP)
    w()
    w("  H0: the two samples are drawn from the same distribution.")
    w("  * = significant at p < 0.05   ** = p < 0.001   *** = p < 1e-6")
    w()

    pairs = [(a, b) for i, a in enumerate(agent_counts) for b in agent_counts[i+1:]]
    w(f"  {'Pair':<14}  {'n_a':>6}  {'n_b':>6}  {'KS stat':>9}  {'p-value':>12}  {'conclusion'}")
    w("  " + "-" * 72)
    for a, b in pairs:
        xa, xb = pools[a], pools[b]
        if not SCIPY_AVAILABLE or len(xa) < 2 or len(xb) < 2:
            w(f"  n={a} vs n={b}    — scipy unavailable")
            continue
        ks_stat, ks_p = scipy_stats.ks_2samp(xa, xb)
        if ks_p < 1e-6:   stars = "***"
        elif ks_p < 0.001: stars = "** "
        elif ks_p < 0.05:  stars = "*  "
        else:              stars = "   "
        verdict = f"REJECT H0 {stars}" if ks_p < 0.05 else "fail to reject"
        w(f"  n={a} vs n={b}    {len(xa):>6}  {len(xb):>6}  {ks_stat:>9.4f}  {_pstr(ks_p):>12}  {verdict}")
    w()
    w(SEP)
    return buf.getvalue()


def plot_ks_pairwise(
    per_agent_br: dict[int, list[np.ndarray]],
    output_path: Path,
    label: str = "Vertical burst-removed",
) -> None:
    if not SCIPY_AVAILABLE:
        print("  [warn] scipy unavailable — skipping ks_pairwise.png")
        return

    agent_counts = sorted(per_agent_br.keys())
    pools = {n: np.concatenate(per_agent_br[n]) for n in agent_counts}
    k = len(agent_counts)

    # Build KS stat and p-value matrices
    ks_mat  = np.full((k, k), np.nan)
    p_mat   = np.full((k, k), np.nan)
    for i, a in enumerate(agent_counts):
        for j, b in enumerate(agent_counts):
            if i == j:
                ks_mat[i, j] = 0.0
                p_mat[i, j]  = 1.0
            elif i < j:
                stat, pval = scipy_stats.ks_2samp(pools[a], pools[b])
                ks_mat[i, j] = ks_mat[j, i] = stat
                p_mat[i, j]  = p_mat[j, i]  = pval

    labels = [f"n={n}" for n in agent_counts]

    fig, axes = plt.subplots(1, 3, figsize=(18, 6))
    fig.patch.set_facecolor(DARK_BG)
    fig.suptitle(
        f"Pairwise KS Tests — {label} IAT distributions by n_experts",
        fontsize=11, color=TEXT_COL, fontweight="bold",
    )

    # [0] KS statistic heatmap
    ax = axes[0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("KS statistic", loc="left")
    im = ax.imshow(ks_mat, vmin=0, vmax=1, cmap="YlOrRd", aspect="auto")
    ax.set_xticks(range(k)); ax.set_xticklabels(labels)
    ax.set_yticks(range(k)); ax.set_yticklabels(labels)
    for i in range(k):
        for j in range(k):
            v = ks_mat[i, j]
            if v == v:
                ax.text(j, i, f"{v:.3f}", ha="center", va="center", fontsize=8,
                        color="white" if v > 0.5 else TEXT_COL)
    fig.colorbar(im, ax=ax, fraction=0.046)

    # [1] p-value heatmap (log scale)
    ax = axes[1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("KS p-value  (log scale, red = significant)", loc="left")
    log_p = np.where(p_mat > 0, -np.log10(np.clip(p_mat, 1e-300, 1)), 0)
    im2 = ax.imshow(log_p, vmin=0, cmap="RdYlGn_r", aspect="auto")
    ax.set_xticks(range(k)); ax.set_xticklabels(labels)
    ax.set_yticks(range(k)); ax.set_yticklabels(labels)
    for i in range(k):
        for j in range(k):
            p = p_mat[i, j]
            if p == p:
                lbl = "1.00" if p >= 1.0 else _pstr(p)
                ax.text(j, i, lbl, ha="center", va="center", fontsize=7,
                        color="white" if log_p[i, j] > 3 else TEXT_COL)
    fig.colorbar(im2, ax=ax, fraction=0.046, label="-log10(p)")

    # [2] ECDF overlay
    ax = axes[2]
    ax.set_facecolor(PANEL_BG)
    ax.set_title(f"ECDF overlay — {label} distributions", loc="left")
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("P(X ≤ x)")
    ax.set_xlim(0, IAT_MAX_S)
    ax.set_ylim(0, 1.05)
    ax.grid(True)
    for n in agent_counts:
        arr = np.sort(pools[n][pools[n] <= IAT_MAX_S])
        y = np.arange(1, len(arr) + 1) / len(arr)
        ax.plot(arr, y, color=AGENT_COLORS.get(n, "#7f7f7f"),
                linewidth=2, label=f"n={n}  ({len(pools[n])} IATs)")
    ax.legend(fontsize=8)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        fig.savefig(output_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"  saved  {output_path}")


# ---------------------------------------------------------------------------
# Figure 4: exponential goodness-of-fit for n=5 burst-removed IATs
# ---------------------------------------------------------------------------

def build_exponential_fit_report(
    per_agent_br: dict[int, list[np.ndarray]],
    n_target: int = 5,
    n_bins: int = 20,
    label: str = "vertical",
) -> str:
    buf = StringIO()

    def w(line=""):
        buf.write(line + "\n")

    SEP = "=" * 76
    arr = np.concatenate(per_agent_br.get(n_target, []))
    if len(arr) == 0:
        w(f"No burst-removed data for n={n_target}")
        return buf.getvalue()

    lam = 1.0 / arr.mean()   # MLE for exponential rate

    w(SEP)
    w(f"  EXPONENTIAL GOODNESS-OF-FIT  — {label} IATs, n_experts={n_target}")
    w(SEP)
    w()
    w(f"  Sample size : {len(arr)}")
    w(f"  Mean IAT    : {arr.mean():.4f} s")
    w(f"  Std IAT     : {arr.std():.4f} s")
    w(f"  CV          : {arr.std()/arr.mean():.4f}  (1.0 => exponential)")
    w(f"  MLE λ̂      : {lam:.5f} s⁻¹  (mean service rate)")
    w(f"  1/λ̂        : {1/lam:.4f} s  (expected IAT under fitted exponential)")
    w()

    if not SCIPY_AVAILABLE:
        w("  scipy unavailable — statistical tests skipped")
        w(SEP)
        return buf.getvalue()

    # KS test against fitted exponential
    ks_stat, ks_p = scipy_stats.kstest(arr, "expon", args=(0, 1/lam))
    w("KS TEST vs fitted exponential  (H0: data ~ Exp(λ̂))")
    w(f"  KS statistic : {ks_stat:.5f}")
    w(f"  p-value      : {_pstr(ks_p)}")
    w(f"  conclusion   : {'REJECT H0 *' if ks_p < 0.05 else 'fail to reject H0'}")
    w()

    # Chi-square goodness-of-fit using equiprobable bins
    # Expected counts per bin = n / n_bins; merge tail bins if needed
    probs = np.linspace(0, 1, n_bins + 1)
    bin_edges = scipy_stats.expon.ppf(probs, scale=1/lam)
    bin_edges[-1] = np.inf
    observed, _ = np.histogram(arr, bins=bin_edges)
    expected = np.full(n_bins, len(arr) / n_bins, dtype=float)

    # Merge bins with expected < 5 from right
    obs_m, exp_m = list(observed), list(expected)
    while len(obs_m) > 1 and exp_m[-1] < 5:
        obs_m[-2] += obs_m[-1]; obs_m.pop()
        exp_m[-2] += exp_m[-1]; exp_m.pop()
    obs_m, exp_m = np.array(obs_m), np.array(exp_m)
    chi2_stat = float(np.sum((obs_m - exp_m) ** 2 / exp_m))
    df = len(obs_m) - 2   # k bins - 1 constraint - 1 estimated parameter
    chi2_p = float(scipy_stats.chi2.sf(chi2_stat, df))

    w(f"CHI-SQUARE GOF  ({len(obs_m)} equiprobable bins after merging, df={df})")
    w(f"  χ² statistic : {chi2_stat:.4f}")
    w(f"  p-value      : {_pstr(chi2_p)}")
    w(f"  conclusion   : {'REJECT H0 *' if chi2_p < 0.05 else 'fail to reject H0'}")
    w()

    # Anderson-Darling (exponential)
    ad_result = scipy_stats.anderson(arr, dist="expon")
    w("ANDERSON-DARLING TEST vs exponential")
    w(f"  A² statistic : {ad_result.statistic:.5f}")
    for cv, sl in zip(ad_result.critical_values, ad_result.significance_level):
        marker = " <-- reject" if ad_result.statistic > cv else ""
        w(f"  critical val  {cv:.3f}  @ {sl}% sig{marker}")
    w()
    w(SEP)
    return buf.getvalue()


def plot_exponential_fit(
    per_agent_br: dict[int, list[np.ndarray]],
    n_target: int = 5,
    burst_threshold: float = 0.1,
    n_bins: int = 20,
    output_path: Path = Path("exp_fit.png"),
    label: str = "vertical",
) -> None:
    arr = np.concatenate(per_agent_br.get(n_target, []))
    if len(arr) == 0:
        print(f"  [warn] no burst-removed data for n={n_target} — skipping")
        return

    lam = 1.0 / arr.mean()
    color = AGENT_COLORS.get(n_target, "#7f7f7f")

    fig = plt.figure(figsize=(18, 14))
    fig.patch.set_facecolor(DARK_BG)
    gs = gridspec.GridSpec(2, 3, figure=fig, hspace=0.42, wspace=0.32,
                           top=0.93, bottom=0.06, left=0.06, right=0.97)
    axes = [fig.add_subplot(gs[r, c]) for r in range(2) for c in range(3)]

    cv = arr.std() / arr.mean()
    fig.suptitle(
        f"Exponential GOF — {label} IATs, n_experts={n_target}   "
        f"λ̂ = {lam:.4f} s⁻¹   (CV={cv:.3f}, 1.0 = perfect exponential)",
        fontsize=11, color=TEXT_COL, fontweight="bold",
    )

    xs = np.linspace(0, min(arr.max(), IAT_MAX_S), 400)
    pdf_fitted = scipy_stats.expon.pdf(xs, scale=1/lam) if SCIPY_AVAILABLE else None
    cdf_fitted = scipy_stats.expon.cdf(xs, scale=1/lam) if SCIPY_AVAILABLE else None

    # [0] Histogram + fitted PDF
    ax = axes[0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title(f"Histogram + fitted Exp(λ̂={lam:.4f}) PDF", loc="left")
    ax.set_xlabel("IAT (s)")
    ax.set_ylabel("density")
    ax.grid(True)
    clipped = arr[arr <= IAT_MAX_S]
    ax.hist(clipped, bins=40, density=True, alpha=0.45, color=color,
            label=f"n={n_target} burst-removed  (n={len(arr)})")
    if pdf_fitted is not None:
        ax.plot(xs, pdf_fitted, color="#222222", linewidth=2.2,
                label=f"Exp(λ̂={lam:.4f})")
    if SCIPY_AVAILABLE:
        kde = scipy_stats.gaussian_kde(clipped)
        ax.plot(xs, kde(xs), color=color, linewidth=1.8, linestyle="--",
                label="KDE (empirical)")
    ax.set_xlim(0, IAT_MAX_S)
    ax.legend(fontsize=7.5)

    # [1] ECDF + fitted CDF
    ax = axes[1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("ECDF vs fitted Exp CDF", loc="left")
    ax.set_xlabel("IAT (s)")
    ax.set_ylabel("P(X ≤ x)")
    ax.set_xlim(0, IAT_MAX_S)
    ax.set_ylim(0, 1.05)
    ax.grid(True)
    sorted_arr = np.sort(arr[arr <= IAT_MAX_S])
    ecdf_y = np.arange(1, len(sorted_arr) + 1) / len(sorted_arr)
    ax.plot(sorted_arr, ecdf_y, color=color, linewidth=2, label="empirical ECDF")
    if cdf_fitted is not None:
        ax.plot(xs, cdf_fitted, color="#222222", linewidth=2, linestyle="--",
                label=f"Exp(λ̂={lam:.4f}) CDF")
    ax.legend(fontsize=8)

    # [2] QQ plot
    ax = axes[2]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Q-Q plot vs Exp(λ̂)", loc="left")
    ax.set_xlabel("theoretical quantiles  (s)")
    ax.set_ylabel("empirical quantiles  (s)")
    ax.grid(True)
    n = len(arr)
    probs = (np.arange(1, n + 1) - 0.5) / n
    theoretical_q = scipy_stats.expon.ppf(probs, scale=1/lam) if SCIPY_AVAILABLE else probs
    empirical_q = np.sort(arr)
    ax.scatter(theoretical_q, empirical_q, s=6, alpha=0.4, color=color, label="data")
    # reference line through 25th–75th percentile
    if SCIPY_AVAILABLE:
        q25, q75 = np.percentile(empirical_q, [25, 75])
        tq25 = scipy_stats.expon.ppf(0.25, scale=1/lam)
        tq75 = scipy_stats.expon.ppf(0.75, scale=1/lam)
        slope_ref = (q75 - q25) / (tq75 - tq25) if tq75 != tq25 else 1.0
        intercept_ref = q25 - slope_ref * tq25
        ref_x = np.linspace(theoretical_q.min(), theoretical_q.max(), 200)
        ax.plot(ref_x, intercept_ref + slope_ref * ref_x, "--",
                color="#222222", linewidth=1.5, label="ref line (Q25–Q75)")
    ax.legend(fontsize=8)

    # [3] Chi-square observed vs expected bar chart
    ax = axes[3]
    ax.set_facecolor(PANEL_BG)
    ax.set_title(f"Chi-square GOF — equiprobable bins (n={n_bins})", loc="left")
    ax.set_xlabel("bin index")
    ax.set_ylabel("count")
    ax.grid(True, axis="y")
    if SCIPY_AVAILABLE:
        probs_bins = np.linspace(0, 1, n_bins + 1)
        bin_edges = scipy_stats.expon.ppf(probs_bins, scale=1/lam)
        bin_edges[-1] = np.inf
        observed, _ = np.histogram(arr, bins=bin_edges)
        expected_val = len(arr) / n_bins
        xpos = np.arange(n_bins)
        ax.bar(xpos - 0.2, observed, width=0.4, color=color, alpha=0.75, label="observed")
        ax.bar(xpos + 0.2, np.full(n_bins, expected_val), width=0.4,
               color="#222222", alpha=0.5, label=f"expected ({expected_val:.1f})")
        ax.set_xticks(xpos[::2])
        ax.legend(fontsize=8)
        chi2_stat = float(np.sum((observed - expected_val) ** 2 / expected_val))
        df = n_bins - 2
        chi2_p = float(scipy_stats.chi2.sf(chi2_stat, df))
        ax.set_title(
            f"Chi-square GOF  χ²={chi2_stat:.2f}  df={df}  p={_pstr(chi2_p)}",
            loc="left", fontsize=8,
        )

    # [4] Residuals: empirical CDF - fitted CDF
    ax = axes[4]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Residuals: ECDF − Exp CDF", loc="left")
    ax.set_xlabel("IAT (s)")
    ax.set_ylabel("ECDF(x) − F_exp(x)")
    ax.grid(True)
    ax.axhline(0, color="#222222", linewidth=1, linestyle="--")
    if SCIPY_AVAILABLE:
        fit_cdf_at_data = scipy_stats.expon.cdf(sorted_arr, scale=1/lam)
        residuals = ecdf_y - fit_cdf_at_data
        ax.plot(sorted_arr, residuals, color=color, linewidth=1.5)
        ax.fill_between(sorted_arr, residuals, 0, alpha=0.25, color=color)

    # [5] Summary stats table
    ax = axes[5]
    ax.set_title("Summary statistics", loc="left")
    rows = []
    if SCIPY_AVAILABLE:
        ks_stat, ks_p = scipy_stats.kstest(arr, "expon", args=(0, 1/lam))
        ad_result = scipy_stats.anderson(arr, dist="expon")
        probs_bins2 = np.linspace(0, 1, n_bins + 1)
        be2 = scipy_stats.expon.ppf(probs_bins2, scale=1/lam); be2[-1] = np.inf
        obs2, _ = np.histogram(arr, bins=be2)
        exp2 = np.full(n_bins, len(arr) / n_bins, dtype=float)
        obs_m2, exp_m2 = list(obs2), list(exp2)
        while len(obs_m2) > 1 and exp_m2[-1] < 5:
            obs_m2[-2] += obs_m2[-1]; obs_m2.pop()
            exp_m2[-2] += exp_m2[-1]; exp_m2.pop()
        obs_m2, exp_m2 = np.array(obs_m2), np.array(exp_m2)
        chi2_s = float(np.sum((obs_m2 - exp_m2) ** 2 / exp_m2))
        df2 = len(obs_m2) - 2
        chi2_p2 = float(scipy_stats.chi2.sf(chi2_s, df2))
        rows = [
            ["n (burst-removed)", str(len(arr))],
            ["mean IAT (s)", f"{arr.mean():.4f}"],
            ["std IAT (s)", f"{arr.std():.4f}"],
            ["CV", f"{cv:.4f}"],
            ["MLE λ̂ (s⁻¹)", f"{lam:.5f}"],
            ["1/λ̂ (s)", f"{1/lam:.4f}"],
            ["KS stat", f"{ks_stat:.5f}"],
            ["KS p-value", _pstr(ks_p) + (" *" if ks_p < 0.05 else "")],
            [f"χ² stat (df={df2})", f"{chi2_s:.4f}"],
            ["χ² p-value", _pstr(chi2_p2) + (" *" if chi2_p2 < 0.05 else "")],
            ["A-D stat", f"{ad_result.statistic:.5f}"],
        ]
    _table_ax(ax, ["Metric", "Value"], rows, col_widths=[0.65, 0.35])

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        fig.savefig(output_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"  saved  {output_path}")


# ---------------------------------------------------------------------------
# Figure 5: horizontal sub-agent count analysis
# ---------------------------------------------------------------------------

def build_h_agent_count_report(
    per_agent_h_raw: dict[int, list[np.ndarray]],
) -> str:
    buf = StringIO()

    def w(line=""):
        buf.write(line + "\n")

    SEP = "=" * 76
    w(SEP)
    w("  HORIZONTAL: SUB-AGENT COUNT ANALYSIS  (raw IATs by n_experts)")
    w(SEP)

    agent_counts = sorted(per_agent_h_raw.keys())
    pools_raw = {n: np.concatenate(per_agent_h_raw[n]) for n in agent_counts if n in per_agent_h_raw}

    w()
    w("RAW IATS BY N_AGENTS")
    w("-" * 76)
    header = f"  {'Metric':<22}" + "".join(f"  {'n='+str(n):<14}" for n in agent_counts)
    w(header)
    w("-" * 76)

    rows = [
        ("n",            "n",             lambda v: f"{int(v)}"),
        ("mean",         "mean (s)",      lambda v: f"{v:.4f}"),
        ("cv",           "CV",            lambda v: f"{v:.4f}"),
        ("median",       "median (s)",    lambda v: f"{v:.4f}"),
        ("p95",          "p95 (s)",       lambda v: f"{v:.4f}"),
        ("skewness",     "skewness",      lambda v: f"{v:.4f}"),
        ("frac_lt_1s",   "frac < 1 s",   lambda v: f"{v:.4f} ({v*100:.1f}%)"),
        ("frac_lt_10ms", "frac < 10 ms", lambda v: f"{v:.4f} ({v*100:.1f}%)"),
    ]
    for key, label, fmt in rows:
        line = f"  {label:<22}"
        for n in agent_counts:
            s   = descriptive_stats(pools_raw.get(n, np.array([])))
            val = s.get(key, float("nan"))
            line += f"  {fmt(val):<14}"
        w(line)

    w()
    w("INTERPRETATION")
    w("-" * 76)
    w("  Horizontal discussion dispatches requests sequentially — no parallel")
    w("  fan-out occurs, so burst fraction is 0% at all agent counts and CV < 2.")
    w("  No burst removal is needed or applied.")

    w()
    w(SEP)
    return buf.getvalue()


def plot_h_agent_count(
    per_agent_h_raw: dict[int, list[np.ndarray]],
    output_path: Path,
) -> None:
    agent_counts = sorted(per_agent_h_raw.keys())
    if not agent_counts:
        print("  [warn] no horizontal runs with agent count data — skipping h_agent_count_iat.png")
        return

    pools_raw = {n: np.concatenate(per_agent_h_raw[n]) for n in agent_counts if n in per_agent_h_raw}

    fig = plt.figure(figsize=(18, 18))
    fig.patch.set_facecolor(DARK_BG)
    gs  = gridspec.GridSpec(3, 2, figure=fig, hspace=0.48, wspace=0.30,
                            top=0.94, bottom=0.04, left=0.05, right=0.97)
    axes = [[fig.add_subplot(gs[r, c]) for c in range(2)] for r in range(3)]

    fig.suptitle(
        "Horizontal: IAT by Number of Sub-Agents — raw (no burst removal needed)",
        fontsize=11, color=TEXT_COL, fontweight="bold",
    )

    # [0,0] Raw IAT histogram + KDE by n_agents
    ax = axes[0][0]
    ax.set_title("Raw IAT distribution by n_experts  (clipped at 100 s)", loc="left")
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("density")
    ax.grid(True)
    for n in agent_counts:
        arr = pools_raw.get(n, np.array([]))
        if len(arr) == 0:
            continue
        color   = AGENT_COLORS.get(n, "#7f7f7f")
        clipped = arr[arr <= IAT_MAX_S]
        ax.hist(clipped, bins=40, density=True, alpha=0.25, color=color)
        if SCIPY_AVAILABLE and len(clipped) > 5:
            kde = scipy_stats.gaussian_kde(clipped)
            xs  = np.linspace(0, IAT_MAX_S, 400)
            ax.plot(xs, kde(xs), color=color, linewidth=2.2,
                    label=f"n={n}  (IATs={len(arr)}, runs={len(per_agent_h_raw.get(n,[]))})")
    ax.set_xlim(0, IAT_MAX_S)
    ax.legend(fontsize=7)

    # [0,1] ECDF by n_agents
    ax = axes[0][1]
    ax.set_title("ECDF by n_experts", loc="left")
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("P(X ≤ x)")
    ax.set_xlim(0, IAT_MAX_S)
    ax.set_ylim(0, 1.05)
    ax.grid(True)
    for n in agent_counts:
        arr = pools_raw.get(n, np.array([]))
        if len(arr) == 0:
            continue
        clipped = np.sort(arr[arr <= IAT_MAX_S])
        y = np.arange(1, len(clipped) + 1) / len(clipped)
        ax.plot(clipped, y, color=AGENT_COLORS.get(n, "#7f7f7f"),
                linewidth=2, label=f"n={n}")
    ax.legend(fontsize=8)

    # [1,0] CV vs n_agents (raw only)
    ax = axes[1][0]
    ax.set_title("CV vs n_experts  (raw)", loc="left")
    ax.set_xlabel("n_experts")
    ax.set_ylabel("CV (std / mean)")
    ax.grid(True)
    ns      = agent_counts
    cvs_raw = [descriptive_stats(pools_raw.get(n, np.array([]))).get("cv", float("nan")) for n in ns]
    ax.plot(ns, cvs_raw, "o-", color="#1f77b4", linewidth=2, markersize=8, label="raw")
    ax.axhline(1.0, color="black", linestyle=":", linewidth=1, alpha=0.6, label="CV=1 (Poisson)")
    ax.set_xticks(ns)
    ax.legend(fontsize=8)
    for n, cv_r in zip(ns, cvs_raw):
        if cv_r == cv_r:
            ax.annotate(f"{cv_r:.2f}", (n, cv_r), textcoords="offset points",
                        xytext=(5, 6), fontsize=7)

    # [1,1] Mean / median IAT vs n_agents
    ax = axes[1][1]
    ax.set_title("Mean and median IAT vs n_experts", loc="left")
    ax.set_xlabel("n_experts")
    ax.set_ylabel("IAT (s)")
    ax.grid(True)
    means   = [descriptive_stats(pools_raw.get(n, np.array([]))).get("mean",   float("nan")) for n in ns]
    medians = [descriptive_stats(pools_raw.get(n, np.array([]))).get("median", float("nan")) for n in ns]
    ax.plot(ns, means,   "o-", color="#1f77b4", linewidth=2, markersize=8, label="mean")
    ax.plot(ns, medians, "s-", color="#ff7f0e", linewidth=2, markersize=8, label="median")
    ax.set_xticks(ns)
    ax.legend(fontsize=8)

    # [2,0] Stats table
    ax = axes[2][0]
    ax.set_title("Raw IAT stats by n_experts", loc="left")
    stat_rows_def = [
        ("n",            "n",            lambda v: f"{int(v)}"),
        ("mean",         "mean (s)",     lambda v: f"{v:.2f}"),
        ("cv",           "CV",           lambda v: f"{v:.3f}"),
        ("median",       "median (s)",   lambda v: f"{v:.2f}"),
        ("p95",          "p95 (s)",      lambda v: f"{v:.2f}"),
        ("skewness",     "skewness",     lambda v: f"{v:.2f}"),
        ("frac_lt_10ms", "frac < 10 ms",lambda v: f"{v:.1%}"),
    ]
    col_labels = ["Metric"] + [f"n={n}" for n in ns]
    tbl_rows = []
    for key, lbl, fmt in stat_rows_def:
        row = [lbl]
        for n in ns:
            val = descriptive_stats(pools_raw.get(n, np.array([]))).get(key, float("nan"))
            row.append(fmt(val) if val == val else "—")
        tbl_rows.append(row)
    col_w = [0.30] + [0.70 / len(ns)] * len(ns)
    _table_ax(ax, col_labels, tbl_rows, col_widths=col_w)

    # [2,1] Blank explanatory panel
    ax = axes[2][1]
    ax.axis("off")
    ax.set_facecolor(PANEL_BG)
    ax.text(0.5, 0.5,
            "No burst removal: horizontal discussion dispatches\n"
            "requests sequentially — CV < 2, burst fraction = 0%\n"
            "at all agent counts",
            ha="center", va="center", fontsize=10, color=TEXT_COL,
            transform=ax.transAxes, wrap=True)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        fig.savefig(output_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"  saved  {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--experiment-dir", required=True, metavar="DIR")
    parser.add_argument("--output-dir",     metavar="DIR")
    parser.add_argument("--burst-threshold", type=float, default=0.1,
                        metavar="SEC",
                        help="IATs below this (seconds) are treated as burst artefacts (default: 0.1)")
    args = parser.parse_args()

    experiment_dir = Path(args.experiment_dir)
    if not experiment_dir.is_dir():
        sys.exit(f"ERROR: experiment directory not found: {experiment_dir}")

    output_dir = Path(args.output_dir) if args.output_dir else experiment_dir / "plots"
    output_dir.mkdir(parents=True, exist_ok=True)
    v_dir = output_dir / "vertical"
    h_dir = output_dir / "horizontal"
    v_dir.mkdir(parents=True, exist_ok=True)
    h_dir.mkdir(parents=True, exist_ok=True)

    print(f"[info] loading runs from {experiment_dir} …")
    print(f"[info] burst threshold = {args.burst_threshold:.3f} s")

    pools, run_counts, per_agent_raw, per_agent_br, per_agent_h_raw = load_data(
        experiment_dir, args.burst_threshold
    )

    total = sum(run_counts.values())
    if total == 0:
        sys.exit("ERROR: no valid runs found")

    print(f"[info] loaded {total} runs  "
          f"(horizontal={run_counts.get('horizontal',0)}, "
          f"vertical={run_counts.get('vertical',0)})")
    print(f"[info] pool sizes: horizontal={len(pools['horizontal'])}, "
          f"vertical_agg={len(pools['vertical_agg'])}, "
          f"vertical_br={len(pools['vertical_br'])}")

    # Figure 1 + text report  (written to output_dir root — spans both structures)
    report1 = build_comparison_report(pools, run_counts, args.burst_threshold)
    print(report1)
    txt1 = output_dir / "burst_comparison.txt"
    txt1.write_text(report1)
    print(f"  saved  {txt1}")
    plot_comparison(pools, run_counts, args.burst_threshold,
                    output_dir / "burst_comparison.png")

    # --- Vertical outputs ---

    # Figure 2 + text report
    report2 = build_agent_count_report(per_agent_raw, per_agent_br, args.burst_threshold)
    print(report2)
    txt2 = v_dir / "agent_count_iat.txt"
    txt2.write_text(report2)
    print(f"  saved  {txt2}")
    plot_agent_count(per_agent_raw, per_agent_br, args.burst_threshold,
                     v_dir / "agent_count_iat.png")

    # Figure 3: pairwise KS tests between burst-removed distributions
    report3 = build_ks_pairwise_report(per_agent_br, label="Vertical burst-removed")
    print(report3)
    txt3 = v_dir / "burst_removed_ks_pairwise.txt"
    txt3.write_text(report3)
    print(f"  saved  {txt3}")
    plot_ks_pairwise(per_agent_br, v_dir / "burst_removed_ks_pairwise.png",
                     label="Vertical burst-removed")

    # Figure 4: exponential GOF for n=5 burst-removed
    n_target = max(per_agent_br.keys()) if per_agent_br else 5
    report4 = build_exponential_fit_report(per_agent_br, n_target=n_target, label="vertical")
    print(report4)
    txt4 = v_dir / f"exponential_fit_n{n_target}.txt"
    txt4.write_text(report4)
    print(f"  saved  {txt4}")
    plot_exponential_fit(per_agent_br, n_target=n_target,
                         burst_threshold=args.burst_threshold,
                         output_path=v_dir / f"exponential_fit_n{n_target}.png",
                         label="vertical")

    # --- Horizontal outputs ---

    # H-Figure 1: agent count IAT
    report_h1 = build_h_agent_count_report(per_agent_h_raw)
    print(report_h1)
    txt_h1 = h_dir / "h_agent_count_iat.txt"
    txt_h1.write_text(report_h1)
    print(f"  saved  {txt_h1}")
    plot_h_agent_count(per_agent_h_raw, h_dir / "h_agent_count_iat.png")

    # H-Figure 2: pairwise KS tests on raw horizontal distributions
    report_h2 = build_ks_pairwise_report(per_agent_h_raw, label="Horizontal raw")
    print(report_h2)
    txt_h2 = h_dir / "h_ks_pairwise.txt"
    txt_h2.write_text(report_h2)
    print(f"  saved  {txt_h2}")
    plot_ks_pairwise(per_agent_h_raw, h_dir / "h_ks_pairwise.png",
                     label="Horizontal raw")

    # H-Figure 3: exponential GOF for horizontal raw
    n_target_h = max(per_agent_h_raw.keys()) if per_agent_h_raw else 5
    report_h3 = build_exponential_fit_report(per_agent_h_raw, n_target=n_target_h, label="horizontal")
    print(report_h3)
    txt_h3 = h_dir / f"exponential_fit_h_n{n_target_h}.txt"
    txt_h3.write_text(report_h3)
    print(f"  saved  {txt_h3}")
    plot_exponential_fit(per_agent_h_raw, n_target=n_target_h,
                         burst_threshold=args.burst_threshold,
                         output_path=h_dir / f"exponential_fit_h_n{n_target_h}.png",
                         label="horizontal")


if __name__ == "__main__":
    main()
