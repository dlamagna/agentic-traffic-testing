#!/usr/bin/env python3
"""
analyse_vertical_raw_vs_aggregated.py
======================================
Focused comparison of vertical-raw IATs vs vertical-aggregated IATs.

Vertical-raw:        every individual LLM request timestamp (includes the
                     near-zero burst from parallel reviewer fan-out).
Vertical-aggregated: each parallel reviewer batch is collapsed to its minimum
                     timestamp so the unit of analysis becomes a discussion
                     *stage* rather than an individual request.

This script answers the question:
  "Is the near-zero burst spike purely a fan-out artefact, or does sequential-
   level burstiness survive after aggregation?"

Outputs:
  vertical_raw_vs_aggregated.png  — 3×2 figure: histogram, ECDF, box, bar,
                                    stats table, test table
  vertical_raw_vs_aggregated.txt  — plain-text summary

Usage:
    python analyse_vertical_raw_vs_aggregated.py \\
        --experiment-dir <dir> [--output-dir <dir>]

Defaults:
    --output-dir  <experiment-dir>/plots/iat_analysis/
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
    print("WARNING: scipy not available — KS/MW tests and KDE will be skipped",
          file=sys.stderr)

# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------
DARK_BG  = "white"
PANEL_BG = "#f7f7f7"
GRID_COL = "#cccccc"
TEXT_COL = "#222222"
IAT_MAX_S = 100

V_COLOR  = "#ff7f0e"   # vertical raw   — orange
VA_COLOR = "#2ca02c"   # vertical agg   — green

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
    "vertical_solver",
    "vertical_reviewer",
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _is_vertical(data: dict) -> bool:
    """Return True if run labels indicate a vertical discussion structure."""
    labels = {req.get("label", "") for req in data.get("llm_requests", [])}
    return any(l.startswith(("vertical_solver", "vertical_reviewer")) for l in labels)


def _aggregate_vertical_run(timestamps: list[float], requests: list[dict]) -> list[float]:
    """Collapse simultaneous reviewer requests within a vertical run.

    Groups:
      ("solver",    round_N)  — vertical_solver_iterN
      ("reviewers", round_N)  — all vertical_reviewer_*_iterN → collapsed to min ts
      ("other",     seq)      — anything else
    """
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


def load_vertical_pools(experiment_dir: Path) -> tuple[
    np.ndarray,   # raw IATs
    np.ndarray,   # aggregated IATs
    int,          # n vertical runs
]:
    raw_iats: list[np.ndarray] = []
    agg_iats: list[np.ndarray] = []
    n_runs = 0

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

        if not _is_vertical(data):
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

        n_runs += 1
        sorted_ts = sorted(timestamps)
        raw_iats.append(np.diff(np.array(sorted_ts)))

        agg = _aggregate_vertical_run(timestamps, requests)
        if len(agg) >= 2:
            agg_iats.append(np.diff(np.array(agg)))

    raw = np.concatenate(raw_iats) if raw_iats else np.array([])
    agg = np.concatenate(agg_iats) if agg_iats else np.array([])
    return raw, agg, n_runs


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def descriptive_stats(arr: np.ndarray) -> dict:
    if len(arr) == 0:
        return {}
    mean = float(arr.mean())
    std  = float(arr.std())
    return {
        "n":            len(arr),
        "mean":         mean,
        "std":          std,
        "cv":           std / mean if mean > 0 else float("nan"),
        "median":       float(np.percentile(arr, 50)),
        "p25":          float(np.percentile(arr, 25)),
        "p75":          float(np.percentile(arr, 75)),
        "p95":          float(np.percentile(arr, 95)),
        "p99":          float(np.percentile(arr, 99)),
        "min":          float(arr.min()),
        "max":          float(arr.max()),
        "skewness":     float(scipy_stats.skew(arr))     if SCIPY_AVAILABLE else float("nan"),
        "kurtosis":     float(scipy_stats.kurtosis(arr)) if SCIPY_AVAILABLE else float("nan"),
        "frac_lt_1s":   float(np.mean(arr < 1.0)),
        "frac_lt_10ms": float(np.mean(arr < 0.01)),
        "burst_frac":   float(np.mean(arr < 0.1)),
    }


def run_tests(raw: np.ndarray, agg: np.ndarray) -> dict:
    if not SCIPY_AVAILABLE or len(raw) < 2 or len(agg) < 2:
        nan = float("nan")
        return dict(ks_stat=nan, ks_p=nan, mw_stat=nan, mw_p=nan,
                    cohen_d=nan, cliff_delta=nan)

    ks_stat, ks_p = scipy_stats.ks_2samp(raw, agg)
    mw_stat, mw_p = scipy_stats.mannwhitneyu(raw, agg, alternative="two-sided")

    pooled_std = np.sqrt((raw.var() + agg.var()) / 2)
    cohen_d    = (raw.mean() - agg.mean()) / pooled_std if pooled_std > 0 else float("nan")

    u_gt, _    = scipy_stats.mannwhitneyu(raw, agg, alternative="greater")
    cliff      = (2 * u_gt / (len(raw) * len(agg))) - 1

    return dict(
        ks_stat=float(ks_stat), ks_p=float(ks_p),
        mw_stat=float(mw_stat), mw_p=float(mw_p),
        cohen_d=float(cohen_d), cliff_delta=float(cliff),
    )


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------

def build_text_report(
    raw: np.ndarray,
    agg: np.ndarray,
    n_runs: int,
    tests: dict,
) -> str:
    buf = StringIO()

    def w(line: str = "") -> None:
        buf.write(line + "\n")

    SEP  = "=" * 72
    SEP2 = "-" * 72

    w(SEP)
    w("  VERTICAL RAW vs VERTICAL AGGREGATED — IAT COMPARISON")
    w(f"  (discussion stage only, within-run IATs, {n_runs} vertical runs)")
    w(SEP)
    w()
    w("  Unit definitions:")
    w("    vertical-raw:        every individual LLM request (solver + each reviewer)")
    w("    vertical-aggregated: each parallel reviewer batch collapsed to min timestamp")
    w("                         → unit becomes a discussion stage, not a request")
    w()

    s_raw = descriptive_stats(raw)
    s_agg = descriptive_stats(agg)

    w("DESCRIPTIVE STATISTICS")
    w(SEP2)

    col_w = 26
    header = f"  {'Metric':<22}  {'vertical-raw':<{col_w}}  {'vertical-aggregated':<{col_w}}"
    w(header)
    w(SEP2)

    rows = [
        ("n",             "n",             lambda v: f"{v:.0f}"),
        ("mean",          "mean (s)",      lambda v: f"{v:.4f}"),
        ("std",           "std (s)",       lambda v: f"{v:.4f}"),
        ("cv",            "CV (std/mean)", lambda v: f"{v:.4f}"),
        ("median",        "median (s)",    lambda v: f"{v:.4f}"),
        ("p25",           "p25 (s)",       lambda v: f"{v:.4f}"),
        ("p75",           "p75 (s)",       lambda v: f"{v:.4f}"),
        ("p95",           "p95 (s)",       lambda v: f"{v:.4f}"),
        ("p99",           "p99 (s)",       lambda v: f"{v:.4f}"),
        ("min",           "min (s)",       lambda v: f"{v:.6f}"),
        ("max",           "max (s)",       lambda v: f"{v:.4f}"),
        ("skewness",      "skewness",      lambda v: f"{v:.4f}"),
        ("kurtosis",      "kurtosis",      lambda v: f"{v:.4f}"),
        ("frac_lt_1s",    "frac < 1 s",   lambda v: f"{v:.4f}  ({v*100:.1f}%)"),
        ("frac_lt_10ms",  "frac < 10 ms", lambda v: f"{v:.4f}  ({v*100:.1f}%)"),
        ("burst_frac",    "frac < 100 ms",lambda v: f"{v:.4f}  ({v*100:.1f}%)"),
    ]
    for key, label, fmt in rows:
        v_raw = s_raw.get(key, float("nan"))
        v_agg = s_agg.get(key, float("nan"))
        w(f"  {label:<22}  {fmt(v_raw):<{col_w}}  {fmt(v_agg):<{col_w}}")

    w()
    w("  CV note: CV≈1.0 → exponential/Poisson arrivals; >1 → over-dispersed (bursty)")
    w("  The ~62 % near-zero fraction in vertical-raw comes from parallel fan-out.")
    w("  Aggregation collapses this spike; if CV drops toward 1.0, fan-out is the")
    w("  dominant — not sequential — source of burstiness.")
    w()

    w(SEP)
    w("STATISTICAL TESTS  (two-sample, two-sided)")
    w(SEP2)

    def pstr(p: float) -> str:
        if p != p: return "n/a"
        if p < 1e-300: return "< 1e-300"
        return f"{p:.3e}"

    ks_verdict = "REJECT H0  (distributions differ)" if tests["ks_p"] < 0.05 else "fail to reject H0"
    mw_verdict = "REJECT H0  (medians differ)"       if tests["mw_p"] < 0.05 else "fail to reject H0"

    w("  Kolmogorov-Smirnov (two-sample)")
    w(f"    KS statistic = {tests['ks_stat']:.6f}")
    w(f"    p-value      = {pstr(tests['ks_p'])}")
    w(f"    conclusion   = {ks_verdict}")
    w()
    w("  Mann-Whitney U")
    w(f"    U statistic  = {tests['mw_stat']:.2f}")
    w(f"    p-value      = {pstr(tests['mw_p'])}")
    w(f"    conclusion   = {mw_verdict}")
    w()
    w("  Effect sizes  (|Cohen d|: 0.2=small 0.5=medium 0.8=large)")
    w(f"    Cohen d      = {tests['cohen_d']:+.4f}")
    w(f"    Cliff delta  = {tests['cliff_delta']:+.4f}")
    w()

    # Interpretation block
    w(SEP)
    w("INTERPRETATION")
    w(SEP2)
    cv_raw = s_raw.get("cv", float("nan"))
    cv_agg = s_agg.get("cv", float("nan"))
    bf     = s_raw.get("burst_frac", float("nan"))
    cv_drop = cv_raw - cv_agg if (cv_raw == cv_raw and cv_agg == cv_agg) else float("nan")
    w(f"  Burst fraction (IAT < 100 ms) in raw stream : {bf*100:.1f}%")
    w(f"  CV vertical-raw                              : {cv_raw:.3f}")
    w(f"  CV vertical-aggregated                       : {cv_agg:.3f}")
    w(f"  CV drop after aggregation                    : {cv_drop:.3f}")
    w()
    if cv_agg == cv_agg and cv_agg <= 1.15:
        w("  After aggregation CV ≈ 1.0 — the sequential inter-stage timing is")
        w("  approximately exponential/Poisson.  The parallel fan-out burst is")
        w("  the dominant (and nearly sole) source of over-dispersion in the raw stream.")
    elif cv_agg == cv_agg and cv_agg <= 1.5:
        w("  After aggregation CV is moderately above 1.0 — the sequential timing")
        w("  retains some over-dispersion beyond what fan-out alone explains.")
        w("  Both fan-out burst and variable LLM response times contribute.")
    else:
        w("  After aggregation CV remains well above 1.0 — significant sequential-")
        w("  level burstiness persists even after collapsing parallel fan-out.")
    w()
    w(SEP)

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Plot helpers
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
    tbl.set_fontsize(8.0)
    for (row_idx, _col_idx), cell in tbl.get_celld().items():
        cell.set_edgecolor(GRID_COL)
        if row_idx == 0:
            cell.set_facecolor("#d0d8e8")
            cell.set_text_props(fontweight="bold", fontsize=8.0)
        elif row_idx % 2 == 0:
            cell.set_facecolor("#eeeeee")
        else:
            cell.set_facecolor("white")


def generate_plot(
    raw: np.ndarray,
    agg: np.ndarray,
    n_runs: int,
    tests: dict,
    output_path: Path,
) -> None:
    fig = plt.figure(figsize=(16, 14))
    fig.patch.set_facecolor(DARK_BG)

    gs = gridspec.GridSpec(
        3, 2,
        figure=fig,
        hspace=0.44,
        wspace=0.30,
        top=0.93, bottom=0.04, left=0.05, right=0.97,
    )
    axes = [[fig.add_subplot(gs[r, c]) for c in range(2)] for r in range(3)]

    fig.suptitle(
        f"Vertical IAT: Raw vs Aggregated  "
        f"({n_runs} vertical runs — discussion stage only)",
        fontsize=11, color=TEXT_COL, fontweight="bold",
    )

    series = [
        (raw, V_COLOR,  f"vertical-raw  (n={len(raw)} IATs)"),
        (agg, VA_COLOR, f"vertical-aggregated  (n={len(agg)} events)"),
    ]

    # ── [0,0] Histogram + KDE ────────────────────────────────────────────
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
        ax.hist(clipped, bins=50, density=True, alpha=0.35, color=color, label=lbl)
        if SCIPY_AVAILABLE and len(clipped) > 5:
            kde      = scipy_stats.gaussian_kde(clipped)
            xs       = np.linspace(0, IAT_MAX_S, 500)
            kde_vals = kde(xs)
            kde_vals[xs < clipped.min()] = 0.0
            ax.plot(xs, kde_vals, color=color, linewidth=2.2)
    ax.set_xlim(0, IAT_MAX_S)
    ax.legend(fontsize=7.5)

    # ── [0,1] ECDF ───────────────────────────────────────────────────────
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
            ax.text(pval, 0.02 + i * 0.09,
                    f"p{pct}={pval:.1f}s", color=color, fontsize=6, ha="left")
    ax.legend(fontsize=7.5)

    # ── [1,0] Box plot ────────────────────────────────────────────────────
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
        bp = ax.boxplot(
            bp_data, positions=range(1, len(bp_data) + 1),
            vert=False, patch_artist=True, widths=0.5,
            flierprops=dict(marker=".", markersize=2, alpha=0.25),
        )
        for patch, color in zip(bp["boxes"], bp_colors):
            patch.set_facecolor(color)
            patch.set_alpha(0.55)
        for median in bp["medians"]:
            median.set_color(TEXT_COL)
            median.set_linewidth(1.5)
        ax.set_yticks(range(1, len(bp_data) + 1))
        ax.set_yticklabels(bp_labels, fontsize=8)
        ax.set_xlim(0, IAT_MAX_S)

    # ── [1,1] Near-zero fraction bars ────────────────────────────────────
    ax = axes[1][1]
    ax.set_title("Near-zero IAT Fractions", loc="left")
    ax.set_ylabel("fraction")
    ax.grid(True, axis="y")
    thresholds = [("< 10 ms", 0.01), ("< 100 ms", 0.1), ("< 1 s", 1.0)]
    x = np.arange(len(thresholds))
    width = 0.35
    for i, (arr, color, label) in enumerate(series):
        if len(arr) == 0:
            continue
        fracs = [float(np.mean(arr < thr)) for _, thr in thresholds]
        bars  = ax.bar(x + (i - 0.5) * width, fracs, width,
                       color=color, alpha=0.75, label=label.split("  (")[0])
        for bar, frac in zip(bars, fracs):
            if frac > 0.01:
                ax.text(bar.get_x() + bar.get_width() / 2, frac + 0.01,
                        f"{frac:.1%}", ha="center", va="bottom", fontsize=7)
    ax.set_xticks(x)
    ax.set_xticklabels([t for t, _ in thresholds])
    ax.set_ylim(0, 1.05)
    ax.legend(fontsize=7.5)

    # ── [2,0] Descriptive stats table ────────────────────────────────────
    ax = axes[2][0]
    ax.set_title("Descriptive Statistics", loc="left")
    s_raw = descriptive_stats(raw)
    s_agg = descriptive_stats(agg)
    stat_rows_def = [
        ("n",            "n",              lambda v: f"{int(v)}"),
        ("mean",         "mean (s)",       lambda v: f"{v:.2f}"),
        ("std",          "std (s)",        lambda v: f"{v:.2f}"),
        ("cv",           "CV",             lambda v: f"{v:.3f}"),
        ("median",       "median (s)",     lambda v: f"{v:.2f}"),
        ("p95",          "p95 (s)",        lambda v: f"{v:.2f}"),
        ("skewness",     "skewness",       lambda v: f"{v:.2f}"),
        ("kurtosis",     "kurtosis",       lambda v: f"{v:.1f}"),
        ("frac_lt_1s",   "frac < 1 s",    lambda v: f"{v:.1%}"),
        ("burst_frac",   "frac < 100 ms", lambda v: f"{v:.1%}"),
    ]
    col_labels_d = ["Metric", "Vertical\n(raw)", "Vertical\n(aggregated)"]
    tbl_rows = []
    for key, label, fmt in stat_rows_def:
        v_raw = s_raw.get(key, float("nan"))
        v_agg = s_agg.get(key, float("nan"))
        def _fmt(v: float, f=fmt) -> str:
            return f(v) if v == v else "—"
        tbl_rows.append([label, _fmt(v_raw), _fmt(v_agg)])
    _table_ax(ax, col_labels_d, tbl_rows, col_widths=[0.40, 0.30, 0.30])

    # ── [2,1] Statistical tests table ────────────────────────────────────
    ax = axes[2][1]
    ax.set_title("Statistical Tests (raw vs aggregated)", loc="left")

    def pstr(p: float) -> str:
        if p != p: return "n/a"
        if p < 1e-300: return "< 1e-300"
        return f"{p:.2e}"

    sig_ks = " *" if tests["ks_p"] < 0.05 else ""
    sig_mw = " *" if tests["mw_p"] < 0.05 else ""
    test_rows = [
        ["KS statistic",  f"{tests['ks_stat']:.4f}",                ""],
        ["KS p-value",    pstr(tests["ks_p"]) + sig_ks,             ""],
        ["MW U statistic",f"{tests['mw_stat']:.2f}",                ""],
        ["MW p-value",    pstr(tests["mw_p"]) + sig_mw,             ""],
        ["Cohen d",       f"{tests['cohen_d']:+.3f}",               "0.2/0.5/0.8"],
        ["Cliff delta",   f"{tests['cliff_delta']:+.3f}",           "0.15/0.33/0.47"],
        ["* p < 0.05",    "",                                        ""],
    ]
    col_labels_t = ["Test", "Value", "Ref thresholds"]
    _table_ax(ax, col_labels_t, test_rows, col_widths=[0.40, 0.30, 0.30])

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
    parser.add_argument(
        "--experiment-dir", required=True, metavar="DIR",
        help="Experiment root directory containing per-run subdirectories",
    )
    parser.add_argument(
        "--output-dir", metavar="DIR",
        help="Directory to write outputs (default: <experiment-dir>/plots/iat_analysis/)",
    )
    args = parser.parse_args()

    experiment_dir = Path(args.experiment_dir)
    if not experiment_dir.is_dir():
        sys.exit(f"ERROR: experiment directory not found: {experiment_dir}")

    output_dir = (
        Path(args.output_dir) if args.output_dir
        else experiment_dir / "plots" / "iat_analysis"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[info] loading vertical runs from {experiment_dir} …")
    raw, agg, n_runs = load_vertical_pools(experiment_dir)

    if n_runs == 0:
        sys.exit("ERROR: no vertical runs found — check response.json files exist")

    print(f"[info] loaded {n_runs} vertical runs  "
          f"(raw IATs={len(raw)}, aggregated events={len(agg)})")

    tests  = run_tests(raw, agg)
    report = build_text_report(raw, agg, n_runs, tests)

    print(report)

    txt_path = output_dir / "vertical_raw_vs_aggregated.txt"
    txt_path.write_text(report)
    print(f"  saved  {txt_path}")

    if not SCIPY_AVAILABLE:
        print("  WARN  scipy not available — plot will be missing KDE and test results")

    generate_plot(raw, agg, n_runs, tests, output_dir / "vertical_raw_vs_aggregated.png")


if __name__ == "__main__":
    main()
