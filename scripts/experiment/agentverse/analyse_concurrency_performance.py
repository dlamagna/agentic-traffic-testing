#!/usr/bin/env python3
"""
analyse_concurrency_performance.py
====================================
Analyse how LLM backend throughput, latency, and TTFT degrade as the
number of concurrently executing agents increases.

For every run directory the script reads:
  response.json  — per-request fields: completion_tokens, duration_seconds,
                   queue_wait_s (= TTFT), latency_ms, start_time_utc, label, source
  meta.json      — run-level fields: forced_agent_count, forced_structure,
                   duration_s

Key computed metrics
--------------------
  tokens_per_s   = completion_tokens / duration_seconds      (per request)
  latency_s      = latency_ms / 1000
  ttft_ms        = llm_meta.queue_wait_s * 1000  — time from vLLM submission to
                   first token in milliseconds; subsecond unless there is queuing
  inst_concurr   = number of other in-run requests overlapping this one
                   (computed from start_time_utc + duration_seconds)

Outputs (in --output-dir, default <experiment-dir>/plots/concurrency/):
  concurrency_performance.png
  concurrency_performance.txt

Usage:
    python analyse_concurrency_performance.py --experiment-dir <dir> \\
                                              [--output-dir <dir>]
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
    print("WARNING: scipy not available — KDE and KW tests will be skipped", file=sys.stderr)

# ---------------------------------------------------------------------------
# Style — matches the rest of the project
# ---------------------------------------------------------------------------
DARK_BG  = "white"
PANEL_BG = "#f7f7f7"
GRID_COL = "#cccccc"
TEXT_COL = "#222222"

PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
]

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

# Only consider discussion-stage requests — same filter as other analysis scripts
DISC_PREFIXES = (
    "horizontal_discussion",
    "synthesize_discussion",
    "vertical_solver",
    "vertical_reviewer",
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str) -> float | None:
    try:
        return datetime.fromisoformat(ts_str).timestamp()
    except Exception:
        return None


def _inst_concurrency(requests: list[dict]) -> list[int]:
    """For each request compute how many *other* requests in the same run overlap it.

    Overlap condition: other.start < this.start + this.dur  AND
                       other.start + other.dur > this.start
    """
    intervals: list[tuple[float, float]] = []
    for r in requests:
        ts = _parse_ts(r.get("start_time_utc", ""))
        dur = r.get("duration_seconds") or 0.0
        if ts is not None and dur > 0:
            intervals.append((ts, ts + dur))
        else:
            intervals.append((float("nan"), float("nan")))

    counts = []
    for i, (s_i, e_i) in enumerate(intervals):
        if s_i != s_i:  # nan
            counts.append(0)
            continue
        c = 0
        for j, (s_j, e_j) in enumerate(intervals):
            if i == j or s_j != s_j:
                continue
            if s_j < e_i and e_j > s_i:
                c += 1
        counts.append(c)
    return counts


def load_data(experiment_dir: Path) -> list[dict]:
    """Walk run dirs and return a flat list of per-request records.

    Each record contains request-level metrics plus run-level metadata.
    Only discussion-stage requests are included.
    """
    records: list[dict] = []

    for run_dir in sorted(_tasks_dir(experiment_dir).iterdir()):
        if not run_dir.is_dir():
            continue
        resp_path = run_dir / "response.json"
        meta_path = run_dir / "meta.json"
        if not resp_path.exists() or not meta_path.exists():
            continue

        try:
            resp = json.loads(resp_path.read_text())
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue

        agent_count  = meta.get("forced_agent_count")
        structure    = meta.get("forced_structure", "")
        run_duration = meta.get("duration_s")
        task_slug    = meta.get("task_slug", "")

        if agent_count is None:
            continue  # only analyse forced-count runs

        # Filter to discussion-stage requests
        disc_requests = [
            r for r in resp.get("llm_requests", [])
            if any(r.get("label", "").startswith(p) for p in DISC_PREFIXES)
        ]
        if not disc_requests:
            continue

        inst_counts = _inst_concurrency(disc_requests)

        for req, inst_c in zip(disc_requests, inst_counts):
            llm = req.get("llm_meta") or {}
            comp_tokens  = llm.get("completion_tokens")
            dur_s        = req.get("duration_seconds")
            lat_ms       = llm.get("latency_ms")
            qwait_s      = llm.get("queue_wait_s")
            prompt_tok   = llm.get("prompt_tokens")

            if comp_tokens is None or dur_s is None or dur_s <= 0:
                continue
            if comp_tokens <= 0:
                continue

            records.append({
                "agent_count":   int(agent_count),
                "structure":     structure,
                "task_slug":     task_slug,
                "run_duration":  run_duration,
                "label":         req.get("label", ""),
                "source":        req.get("source", ""),
                "tokens_per_s":  comp_tokens / dur_s,
                "latency_s":     lat_ms / 1000.0 if lat_ms is not None else None,
                "ttft_ms":       qwait_s * 1000.0 if qwait_s is not None else None,
                "comp_tokens":   comp_tokens,
                "prompt_tokens": prompt_tok,
                "dur_s":         dur_s,
                "inst_concurr":  inst_c,
            })

    return records


# ---------------------------------------------------------------------------
# Per-agent-count aggregation
# ---------------------------------------------------------------------------

def group_by_agent_count(
    records: list[dict],
    field: str,
) -> dict[int, np.ndarray]:
    """Group numeric field values by agent_count, returning sorted arrays."""
    groups: dict[int, list[float]] = defaultdict(list)
    for r in records:
        val = r.get(field)
        if val is not None and not (isinstance(val, float) and val != val):
            groups[r["agent_count"]].append(float(val))
    return {k: np.array(v) for k, v in sorted(groups.items())}


def run_durations_by_agent_count(records: list[dict]) -> dict[int, np.ndarray]:
    """Deduplicate run durations (one per run) grouped by agent_count."""
    seen: set[tuple] = set()
    groups: dict[int, list[float]] = defaultdict(list)
    for r in records:
        key = (r["agent_count"], r["task_slug"], r.get("run_duration"))
        if key in seen or r.get("run_duration") is None:
            continue
        seen.add(key)
        groups[r["agent_count"]].append(float(r["run_duration"]))
    return {k: np.array(v) for k, v in sorted(groups.items())}


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _stat(arr: np.ndarray) -> dict:
    if len(arr) == 0:
        return {}
    return {
        "n":      len(arr),
        "mean":   float(arr.mean()),
        "std":    float(arr.std()),
        "median": float(np.percentile(arr, 50)),
        "p25":    float(np.percentile(arr, 25)),
        "p75":    float(np.percentile(arr, 75)),
        "p95":    float(np.percentile(arr, 95)),
        "min":    float(arr.min()),
        "max":    float(arr.max()),
    }


def kruskal_wallis(groups: dict[int, np.ndarray]) -> dict:
    """Kruskal-Wallis H test across all agent-count groups."""
    arrays = [v for v in groups.values() if len(v) >= 2]
    if len(arrays) < 2 or not SCIPY_AVAILABLE:
        return {"H": float("nan"), "p": float("nan")}
    H, p = scipy_stats.kruskal(*arrays)
    return {"H": float(H), "p": float(p)}


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------

def build_text_report(records: list[dict]) -> str:
    buf = StringIO()

    def w(line: str = "") -> None:
        buf.write(line + "\n")

    SEP = "=" * 72
    w(SEP)
    w("  LLM CONCURRENCY PERFORMANCE ANALYSIS")
    w("  (discussion-stage requests only)")
    w(SEP)
    w(f"  Total requests analysed: {len(records)}")
    agent_counts = sorted({r['agent_count'] for r in records})
    w(f"  Agent counts found: {agent_counts}")
    w()

    NULL_HYPOTHESES = {
        "tokens_per_s": (
            "H₀: the distribution of per-request throughput (tokens/s) is identical "
            "across all agent-count groups (concurrency has no effect on throughput)."
        ),
        "latency_s": (
            "H₀: the distribution of end-to-end request latency is identical "
            "across all agent-count groups (concurrency has no effect on latency)."
        ),
        "ttft_ms": (
            "H₀: the distribution of TTFT (time to first token, ms) is identical "
            "across all agent-count groups (concurrency has no effect on TTFT)."
        ),
    }

    for field, label, unit in [
        ("tokens_per_s",  "Throughput",              "tok/s"),
        ("latency_s",     "Latency",                 "s"),
        ("ttft_ms",       "TTFT (time to first token)", "ms"),
    ]:
        groups = group_by_agent_count(records, field)
        kw = kruskal_wallis(groups)

        w(SEP)
        w(f"  {label.upper()}  ({unit})")
        w(f"  Null hypothesis: {NULL_HYPOTHESES[field]}")
        w("-" * 72)
        w(f"  {'agents':<8}  {'n':>6}  {'mean':>8}  {'median':>8}  "
          f"{'p95':>8}  {'std':>8}  {'min':>8}  {'max':>8}")
        w("-" * 72)
        baseline_mean = None
        for ac, arr in sorted(groups.items()):
            s = _stat(arr)
            if not s:
                continue
            if baseline_mean is None and ac == min(groups):
                baseline_mean = s["mean"]
            eff = ""
            if baseline_mean and baseline_mean > 0 and field == "tokens_per_s":
                actual_ratio   = s["mean"] / baseline_mean
                ideal_ratio    = min(groups) / ac if ac > 0 else 1.0
                eff = f"  efficiency={actual_ratio / ideal_ratio:.2f}" if ideal_ratio > 0 else ""
            w(f"  {ac:<8}  {s['n']:>6}  {s['mean']:>8.3f}  {s['median']:>8.3f}  "
              f"{s['p95']:>8.3f}  {s['std']:>8.3f}  {s['min']:>8.3f}  {s['max']:>8.3f}{eff}")
        w()
        kw_verdict = "REJECT H₀ (p < 0.05)" if kw["p"] < 0.05 else "FAIL TO REJECT H₀ (p ≥ 0.05)"
        w(f"  Kruskal-Wallis H = {kw['H']:.4f},  p = {kw['p']:.3e}  → {kw_verdict}")

    # Throughput degradation vs ideal
    w()
    w(SEP)
    w("  THROUGHPUT DEGRADATION vs IDEAL 1/N SCALING")
    w("-" * 72)
    tps_groups = group_by_agent_count(records, "tokens_per_s")
    if tps_groups:
        min_ac = min(tps_groups)
        baseline = tps_groups[min_ac].mean() if len(tps_groups[min_ac]) else None
        w(f"  Baseline (agents={min_ac}): {baseline:.2f} tok/s" if baseline else "  no baseline")
        w(f"  {'agents':<8}  {'actual_tps':>12}  {'ideal_tps':>12}  {'efficiency':>12}")
        w("-" * 72)
        for ac, arr in sorted(tps_groups.items()):
            if not len(arr) or baseline is None:
                continue
            actual = arr.mean()
            ideal  = baseline * (min_ac / ac) if ac > 0 else float("nan")
            eff    = actual / ideal if ideal > 0 else float("nan")
            w(f"  {ac:<8}  {actual:>12.3f}  {ideal:>12.3f}  {eff:>12.3f}")

    # Run duration
    dur_groups = run_durations_by_agent_count(records)
    w()
    w(SEP)
    w("  RUN DURATION vs AGENT COUNT  (seconds)")
    w("-" * 72)
    w(f"  {'agents':<8}  {'n_runs':>8}  {'mean_s':>8}  {'median_s':>8}  {'std_s':>8}")
    w("-" * 72)
    for ac, arr in sorted(dur_groups.items()):
        s = _stat(arr)
        if not s:
            continue
        w(f"  {ac:<8}  {s['n']:>8}  {s['mean']:>8.1f}  {s['median']:>8.1f}  {s['std']:>8.1f}")

    # TTFT vs instantaneous concurrency
    inst_ttft: dict[int, list[float]] = defaultdict(list)
    for r in records:
        ic   = r.get("inst_concurr")
        ttft = r.get("ttft_ms")
        if ic is not None and ttft is not None:
            inst_ttft[ic].append(float(ttft))

    inst_ttft_arr = {k: np.array(v) for k, v in sorted(inst_ttft.items())}

    w()
    w(SEP)
    w("  TTFT vs INSTANTANEOUS CONCURRENCY  (exact per-request correlation)")
    w("  inst_concurr = number of other requests overlapping this one in the same run")
    w("  Null hypothesis: H₀: TTFT is uncorrelated with instantaneous concurrency")
    w("  (i.e. slope of OLS regression of TTFT on inst_concurr is zero).")
    w("-" * 72)
    w(f"  {'overlap':>8}  {'n':>6}  {'mean_ms':>9}  {'median_ms':>10}  {'p95_ms':>9}  {'std_ms':>9}")
    w("-" * 72)
    for ic, arr in sorted(inst_ttft_arr.items()):
        s = _stat(arr)
        if not s:
            continue
        w(f"  {ic:>8}  {s['n']:>6}  {s['mean']:>9.1f}  {s['median']:>10.1f}  "
          f"{s['p95']:>9.1f}  {s['std']:>9.1f}")

    # Linear regression slope
    all_ic   = np.array([ic   for ic, v in inst_ttft_arr.items() for _ in v])
    all_ttft = np.array([ttft for ic, v in inst_ttft_arr.items() for ttft in v])
    if len(all_ic) > 2:
        slope, intercept = np.polyfit(all_ic, all_ttft, 1)
        w()
        w(f"  Linear regression: TTFT = {intercept:.1f} ms + {slope:+.1f} ms × inst_concurr")
        w(f"  → each additional overlapping request adds {slope:.1f} ms to TTFT")

    w()
    w(SEP)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Table helper
# ---------------------------------------------------------------------------

def _table_ax(
    ax: plt.Axes,
    col_labels: list[str],
    rows: list[list[str]],
    col_widths: list[float] | None = None,
) -> None:
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
    for (row_idx, _), cell in tbl.get_celld().items():
        cell.set_edgecolor(GRID_COL)
        if row_idx == 0:
            cell.set_facecolor("#d0d8e8")
            cell.set_text_props(fontweight="bold", fontsize=7.5)
        elif row_idx % 2 == 0:
            cell.set_facecolor("#eeeeee")
        else:
            cell.set_facecolor("white")


# ---------------------------------------------------------------------------
# Box-plot helper
# ---------------------------------------------------------------------------

def _boxplot(
    ax: plt.Axes,
    groups: dict[int, np.ndarray],
    title: str,
    xlabel: str,
    ylabel: str,
    palette: list[str],
) -> None:
    ax.set_facecolor(PANEL_BG)
    ax.set_title(title, loc="left", fontsize=9)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(True, axis="y")

    sorted_items = sorted(groups.items())
    if not sorted_items:
        return

    data   = [arr for _, arr in sorted_items]
    labels = [str(ac) for ac, _ in sorted_items]
    colors = [palette[i % len(palette)] for i in range(len(sorted_items))]

    bp = ax.boxplot(
        data,
        positions=range(1, len(data) + 1),
        patch_artist=True,
        widths=0.5,
        flierprops=dict(marker=".", markersize=3, alpha=0.35),
    )
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.60)
    for median in bp["medians"]:
        median.set_color(TEXT_COL)
        median.set_linewidth(1.5)

    ax.set_xticks(range(1, len(data) + 1))
    ax.set_xticklabels(labels)

    # Annotate medians
    for pos, (_, arr) in enumerate(sorted_items, start=1):
        if len(arr):
            med = float(np.median(arr))
            ax.text(pos, med, f"{med:.1f}",
                    ha="center", va="bottom", fontsize=7, color=TEXT_COL,
                    fontweight="bold")


# ---------------------------------------------------------------------------
# Main plot
# ---------------------------------------------------------------------------

def generate_plot(records: list[dict], output_path: Path) -> None:
    tps_groups  = group_by_agent_count(records, "tokens_per_s")
    lat_groups  = group_by_agent_count(records, "latency_s")
    qw_groups   = group_by_agent_count(records, "ttft_ms")
    dur_groups  = run_durations_by_agent_count(records)
    inst_groups = group_by_agent_count(records, "inst_concurr")

    fig = plt.figure(figsize=(18, 16))
    fig.patch.set_facecolor(DARK_BG)
    gs = gridspec.GridSpec(
        3, 2,
        figure=fig,
        hspace=0.42, wspace=0.30,
        top=0.93, bottom=0.04, left=0.06, right=0.97,
    )
    axes = [[fig.add_subplot(gs[r, c]) for c in range(2)] for r in range(3)]

    n_req = len(records)
    agent_counts = sorted(tps_groups.keys())
    fig.suptitle(
        f"LLM Concurrency Performance Analysis  "
        f"({n_req} discussion-stage requests, agent counts: {agent_counts})",
        fontsize=11, color=TEXT_COL, fontweight="bold",
    )

    # ── [0,0] Throughput (tokens/s) vs agent count ───────────────────────
    _boxplot(
        axes[0][0], tps_groups,
        "Throughput per Request (tokens/s) vs Agent Count",
        "number of concurrent agents", "tokens / s",
        PALETTE,
    )

    # ── [0,1] End-to-end latency vs agent count ──────────────────────────
    _boxplot(
        axes[0][1], lat_groups,
        "End-to-end Latency (s) vs Agent Count",
        "number of concurrent agents", "latency (s)",
        PALETTE,
    )

    # ── [1,0] TTFT vs agent count ─────────────────────────────────────────
    TTFT_YMAX = 1500
    _boxplot(
        axes[1][0], qw_groups,
        "TTFT (Time to First Token) vs Agent Count",
        "number of concurrent agents", "TTFT (ms)",
        PALETTE,
    )
    n_clipped_box = sum(int((arr > TTFT_YMAX).sum()) for arr in qw_groups.values())
    n_total_box   = sum(len(arr) for arr in qw_groups.values())
    axes[1][0].set_ylim(bottom=0, top=TTFT_YMAX)
    if n_clipped_box:
        axes[1][0].text(
            0.99, 0.98,
            f"{n_clipped_box}/{n_total_box} values clipped (>{TTFT_YMAX} ms)",
            transform=axes[1][0].transAxes,
            ha="right", va="top", fontsize=7, color="#cc3333",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor="#cc3333", alpha=0.8),
        )

    # ── [1,1] Throughput degradation curve ───────────────────────────────
    ax = axes[1][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Throughput Degradation: Actual vs Ideal 1/N Scaling", loc="left", fontsize=9)
    ax.set_xlabel("number of concurrent agents")
    ax.set_ylabel("normalised tokens/s  (1.0 = single-agent baseline)")
    ax.grid(True)

    if tps_groups:
        min_ac = min(tps_groups)
        baseline_mean = tps_groups[min_ac].mean() if len(tps_groups[min_ac]) else None

        if baseline_mean and baseline_mean > 0:
            acs        = sorted(tps_groups.keys())
            act_ratios = [tps_groups[ac].mean() / baseline_mean for ac in acs]
            act_std    = [tps_groups[ac].std()  / baseline_mean for ac in acs]
            ideal_ratios = [min_ac / ac if ac > 0 else 1.0 for ac in acs]

            ax.plot(acs, ideal_ratios, color="#888888", linewidth=1.8,
                    linestyle="--", label="ideal 1/N")
            ax.errorbar(acs, act_ratios, yerr=act_std,
                        color=PALETTE[0], linewidth=2, marker="o", markersize=6,
                        capsize=4, label="actual (mean ± std)")

            # Efficiency annotations
            for ac, act, ideal in zip(acs, act_ratios, ideal_ratios):
                if ideal > 0:
                    eff = act / ideal
                    ax.annotate(
                        f"eff={eff:.2f}",
                        xy=(ac, act), xytext=(0, 10),
                        textcoords="offset points",
                        ha="center", fontsize=7, color=TEXT_COL,
                    )

        ax.set_ylim(bottom=0)
        ax.axhline(1.0, color=PALETTE[2], linewidth=1, linestyle=":", alpha=0.7)
        ax.legend(fontsize=7)

    # ── [2,0] TTFT vs instantaneous concurrency (box plots + regression) ─
    ax = axes[2][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title(
        "TTFT vs Instantaneous Concurrency  (exact per-request)",
        loc="left", fontsize=9,
    )
    ax.set_xlabel("overlapping requests at moment of submission (inst_concurr)")
    ax.set_ylabel("TTFT (ms)")
    ax.grid(True, axis="y")

    # Build per-inst_concurr pools
    inst_ttft_plot: dict[int, list[float]] = defaultdict(list)
    for r in records:
        ic   = r.get("inst_concurr")
        ttft = r.get("ttft_ms")
        if ic is not None and ttft is not None:
            inst_ttft_plot[ic].append(float(ttft))

    all_ic_plot   = []
    all_ttft_plot = []

    sorted_ics = sorted(inst_ttft_plot.keys())
    if sorted_ics:
        data   = [np.array(inst_ttft_plot[ic]) for ic in sorted_ics]
        colors = [PALETTE[i % len(PALETTE)] for i in range(len(sorted_ics))]

        bp = ax.boxplot(
            data,
            positions=sorted_ics,
            patch_artist=True,
            widths=0.5,
            flierprops=dict(marker=".", markersize=3, alpha=0.35),
        )
        for patch, color in zip(bp["boxes"], colors):
            patch.set_facecolor(color)
            patch.set_alpha(0.60)
        for median in bp["medians"]:
            median.set_color(TEXT_COL)
            median.set_linewidth(1.5)

        # Annotate medians and sample counts
        for ic, arr in zip(sorted_ics, data):
            if len(arr):
                med = float(np.median(arr))
                ax.text(ic, med, f"{med:.0f}",
                        ha="center", va="bottom", fontsize=7,
                        color=TEXT_COL, fontweight="bold")
                ax.text(ic, ax.get_ylim()[0] if ax.get_ylim()[0] > 0 else 0,
                        f"n={len(arr)}", ha="center", va="top",
                        fontsize=6, color=TEXT_COL, alpha=0.7)

        ax.set_xticks(sorted_ics)
        ax.set_xticklabels([str(ic) for ic in sorted_ics])

        for ic, vals in zip(sorted_ics, data):
            all_ic_plot.extend([ic] * len(vals))
            all_ttft_plot.extend(vals.tolist())

    # Regression trend line overlaid on boxes
    if len(all_ic_plot) > 2:
        xs   = np.array(all_ic_plot)
        ys   = np.array(all_ttft_plot)
        slope, intercept = np.polyfit(xs, ys, 1)
        x_range = np.linspace(min(sorted_ics), max(sorted_ics), 100)
        ax.plot(x_range, intercept + slope * x_range,
                color="#cc3333", linewidth=1.8, linestyle="--", zorder=6,
                label=f"trend: +{slope:.0f} ms / overlap")
        ax.legend(fontsize=7, loc="upper left")

    n_clipped_scatter = int(np.sum(np.array(all_ttft_plot) > TTFT_YMAX)) if all_ttft_plot else 0
    ax.set_ylim(bottom=0, top=TTFT_YMAX)
    if n_clipped_scatter:
        ax.text(
            0.99, 0.98,
            f"{n_clipped_scatter}/{len(all_ttft_plot)} values clipped (>{TTFT_YMAX} ms)",
            transform=ax.transAxes,
            ha="right", va="top", fontsize=7, color="#cc3333",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor="#cc3333", alpha=0.8),
        )

    # ── [2,1] Summary table ───────────────────────────────────────────────
    ax = axes[2][1]
    ax.set_title("Summary Statistics by Agent Count", loc="left", fontsize=9)

    col_labels = ["Agents", "n req", "tps mean", "tps p50",
                  "lat mean (s)", "TTFT mean (ms)", "run dur mean (s)"]
    tbl_rows = []
    for ac in sorted(set(r["agent_count"] for r in records)):
        def _m(g: dict, k: int) -> str:
            arr = g.get(k, np.array([]))
            return f"{arr.mean():.2f}" if len(arr) else "—"
        def _med(g: dict, k: int) -> str:
            arr = g.get(k, np.array([]))
            return f"{np.median(arr):.2f}" if len(arr) else "—"
        n_req_ac = len([r for r in records if r["agent_count"] == ac])
        tbl_rows.append([
            str(ac),
            str(n_req_ac),
            _m(tps_groups, ac),
            _med(tps_groups, ac),
            _m(lat_groups, ac),
            _m(qw_groups, ac),
            _m(dur_groups, ac),
        ])

    kw_tps = kruskal_wallis(tps_groups)
    kw_lat = kruskal_wallis(lat_groups)
    tbl_rows.append(["KW H", "—",
                     f"H={kw_tps['H']:.1f}" if kw_tps['H'] == kw_tps['H'] else "—",
                     f"p={kw_tps['p']:.2e}" if kw_tps['p'] == kw_tps['p'] else "—",
                     f"H={kw_lat['H']:.1f}" if kw_lat['H'] == kw_lat['H'] else "—",
                     f"p={kw_lat['p']:.2e}" if kw_lat['p'] == kw_lat['p'] else "—",
                     ""])

    _table_ax(ax, col_labels, tbl_rows,
              col_widths=[0.09, 0.08, 0.12, 0.12, 0.16, 0.18, 0.18])

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        fig.savefig(output_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"  saved  {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", required=True, metavar="DIR")
    parser.add_argument("--output-dir",     metavar="DIR")
    args = parser.parse_args()

    experiment_dir = Path(args.experiment_dir)
    if not experiment_dir.is_dir():
        sys.exit(f"ERROR: experiment directory not found: {experiment_dir}")

    output_dir = Path(args.output_dir) if args.output_dir else experiment_dir / "plots" / "concurrency"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[info] loading runs from {experiment_dir} …")
    records = load_data(experiment_dir)

    if not records:
        sys.exit(
            "ERROR: no discussion-stage requests with forced_agent_count found.\n"
            "       Run with -s (sweep) or -A <n> to produce forced-count runs."
        )

    agent_counts = sorted({r["agent_count"] for r in records})
    print(f"[info] loaded {len(records)} requests  "
          f"(agent counts: {agent_counts})")

    report = build_text_report(records)
    print(report)

    txt_path = output_dir / "concurrency_performance.txt"
    txt_path.write_text(report)
    print(f"  saved  {txt_path}")

    generate_plot(records, output_dir / "concurrency_performance.png")


if __name__ == "__main__":
    main()
