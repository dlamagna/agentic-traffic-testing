#!/usr/bin/env python3
"""
analyse_iat_statistics.py
=========================
Statistical analysis of LLM inter-arrival time (IAT) distributions across
discussion structures (horizontal, vertical, vertical-aggregated).

Loads every response.json in an experiment directory, infers the actual
discussion structure from request labels (overriding mislabelled metadata),
and produces:

  iat_statistics.png   — 3×2 figure: distributions, statistical tests, tables
  iat_statistics.txt   — plain-text summary of all statistics

The "vertical-aggregated" view collapses simultaneous reviewer requests within
each vertical round into a single virtual request (min timestamp) to test the
hypothesis that concurrent fan-out is the sole driver of the near-zero IAT spike.

Usage:
    python analyse_iat_statistics.py --experiment-dir <dir> [--output-dir <dir>]

Defaults:
    --output-dir  <experiment-dir>/plots/
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from io import StringIO

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
# Style — matches the rest of the project
# ---------------------------------------------------------------------------
DARK_BG  = "white"
PANEL_BG = "#f7f7f7"
GRID_COL = "#cccccc"
TEXT_COL = "#222222"
IAT_MAX_S = 100

H_COLOR  = "#1f77b4"   # horizontal
V_COLOR  = "#ff7f0e"   # vertical raw
VA_COLOR = "#2ca02c"   # vertical aggregated

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
# Data loading
# ---------------------------------------------------------------------------

def _infer_structure(data: dict) -> tuple[str, bool]:
    """Infer structure from request labels; fall back to metadata.

    Returns (structure, mislabeled). mislabeled=True when labels contradict
    the metadata field — the label-based value is always used.
    """
    labels = {req.get("label", "") for req in data.get("llm_requests", [])}
    has_h = any(l.startswith("horizontal_discussion") for l in labels)
    has_v = any(l.startswith(("vertical_solver", "vertical_reviewer")) for l in labels)

    if has_h and not has_v:
        inferred = "horizontal"
    elif has_v:
        inferred = "vertical"   # vertical-only or mixed: treat as vertical
    else:
        inferred = ""

    stages = data.get("stages", {})
    meta = (
        stages.get("recruitment", {}).get("communication_structure")
        or stages.get("decision",   {}).get("structure_used", "")
    )
    meta = (meta or "").lower()

    if inferred:
        return inferred, (meta != inferred and bool(meta))
    return meta, False


def _aggregate_vertical_run(timestamps: list[float], requests: list[dict]) -> list[float]:
    """Collapse simultaneous reviewer requests within a vertical run.

    Groups:
      ("solver",    round_N)  — vertical_solver_iterN          (kept as-is)
      ("reviewers", round_N)  — all vertical_reviewer_*_iterN  (collapsed to min ts)
      ("other",     seq)      — anything else                  (kept as-is)
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


def load_pools(experiment_dir: Path) -> tuple[
    dict[str, np.ndarray],   # pooled within-run IATs keyed by structure name
    dict[str, int],          # run counts
    list[str],               # mislabeled run names
]:
    """Walk run dirs and return pooled IAT arrays for each structure."""
    run_iats: dict[str, list[np.ndarray]] = {
        "horizontal": [], "vertical": [], "vertical_agg": [],
    }
    run_counts: dict[str, int] = {"horizontal": 0, "vertical": 0}
    mislabeled: list[str] = []

    for run_dir in sorted(experiment_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        resp_path = run_dir / "response.json"
        if not resp_path.exists():
            continue
        try:
            data = json.loads(resp_path.read_text())
        except Exception:
            continue

        structure, is_mislabeled = _infer_structure(data)
        if is_mislabeled:
            mislabeled.append(run_dir.name)
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
        run_iats[structure].append(np.diff(np.array(sorted_ts)))
        run_counts[structure] += 1

        if structure == "vertical":
            agg = _aggregate_vertical_run(timestamps, requests)
            if len(agg) >= 2:
                run_iats["vertical_agg"].append(np.diff(np.array(agg)))

    pools = {
        k: np.concatenate(v) if v else np.array([])
        for k, v in run_iats.items()
    }
    return pools, run_counts, mislabeled


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def descriptive_stats(arr: np.ndarray) -> dict:
    if len(arr) == 0:
        return {}
    mean = float(arr.mean())
    std  = float(arr.std())
    return {
        "n":           len(arr),
        "mean":        mean,
        "std":         std,
        "cv":          std / mean if mean > 0 else float("nan"),
        "median":      float(np.percentile(arr, 50)),
        "p25":         float(np.percentile(arr, 25)),
        "p75":         float(np.percentile(arr, 75)),
        "p95":         float(np.percentile(arr, 95)),
        "p99":         float(np.percentile(arr, 99)),
        "min":         float(arr.min()),
        "max":         float(arr.max()),
        "skewness":    float(scipy_stats.skew(arr))    if SCIPY_AVAILABLE else float("nan"),
        "kurtosis":    float(scipy_stats.kurtosis(arr)) if SCIPY_AVAILABLE else float("nan"),
        "frac_lt_1s":  float(np.mean(arr < 1.0)),
        "frac_lt_10ms": float(np.mean(arr < 0.01)),
    }


def pairwise_tests(pools: dict[str, np.ndarray]) -> list[dict]:
    """Run KS, Mann-Whitney, and effect-size tests for all relevant pairs."""
    pairs = [
        ("horizontal",   "vertical",     "H vs V-raw"),
        ("horizontal",   "vertical_agg", "H vs V-aggregated"),
        ("vertical",     "vertical_agg", "V-raw vs V-aggregated"),
    ]
    results = []
    for a, b, label in pairs:
        xa, xb = pools.get(a, np.array([])), pools.get(b, np.array([]))
        entry: dict = {"label": label, "a": a, "b": b}
        if len(xa) < 2 or len(xb) < 2 or not SCIPY_AVAILABLE:
            entry.update({"ks_stat": float("nan"), "ks_p": float("nan"),
                          "mw_stat": float("nan"), "mw_p": float("nan"),
                          "cohen_d": float("nan"), "cliff_delta": float("nan")})
            results.append(entry)
            continue

        ks_stat, ks_p = scipy_stats.ks_2samp(xa, xb)
        mw_stat, mw_p = scipy_stats.mannwhitneyu(xa, xb, alternative="two-sided")

        pooled_std = np.sqrt((xa.var() + xb.var()) / 2)
        cohen_d    = (xa.mean() - xb.mean()) / pooled_std if pooled_std > 0 else float("nan")

        u_gt, _    = scipy_stats.mannwhitneyu(xa, xb, alternative="greater")
        cliff      = (2 * u_gt / (len(xa) * len(xb))) - 1

        entry.update({
            "ks_stat":    float(ks_stat),
            "ks_p":       float(ks_p),
            "mw_stat":    float(mw_stat),
            "mw_p":       float(mw_p),
            "cohen_d":    float(cohen_d),
            "cliff_delta": float(cliff),
        })
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------

def build_text_report(
    pools: dict[str, np.ndarray],
    run_counts: dict[str, int],
    mislabeled: list[str],
    tests: list[dict],
) -> str:
    buf = StringIO()

    def w(line: str = "") -> None:
        buf.write(line + "\n")

    SEP = "=" * 72
    w(SEP)
    w("  IAT STATISTICAL ANALYSIS  (discussion stage only, within-run IATs)")
    w(SEP)

    if mislabeled:
        w(f"\n  [{len(mislabeled)} mislabeled run(s) reclassified from metadata to label-inferred structure]")
        for name in mislabeled:
            w(f"    {name}")

    display_names = {
        "horizontal":   f"horizontal        ({run_counts.get('horizontal', 0)} runs)",
        "vertical":     f"vertical raw      ({run_counts.get('vertical',   0)} runs)",
        "vertical_agg": f"vertical aggregated",
    }

    w()
    w("DESCRIPTIVE STATISTICS")
    w("-" * 72)
    header = f"  {'Metric':<20}"
    for k in ("horizontal", "vertical", "vertical_agg"):
        header += f"  {display_names[k]:<26}"
    w(header)
    w("-" * 72)

    stats_all = {k: descriptive_stats(pools[k]) for k in ("horizontal", "vertical", "vertical_agg")}

    rows = [
        ("n",            "n",              lambda v: f"{v:.0f}"),
        ("mean",         "mean (s)",       lambda v: f"{v:.4f}"),
        ("std",          "std (s)",        lambda v: f"{v:.4f}"),
        ("cv",           "CV (std/mean)",  lambda v: f"{v:.4f}"),
        ("median",       "median (s)",     lambda v: f"{v:.4f}"),
        ("p25",          "p25 (s)",        lambda v: f"{v:.4f}"),
        ("p75",          "p75 (s)",        lambda v: f"{v:.4f}"),
        ("p95",          "p95 (s)",        lambda v: f"{v:.4f}"),
        ("p99",          "p99 (s)",        lambda v: f"{v:.4f}"),
        ("min",          "min (s)",        lambda v: f"{v:.6f}"),
        ("max",          "max (s)",        lambda v: f"{v:.4f}"),
        ("skewness",     "skewness",       lambda v: f"{v:.4f}"),
        ("kurtosis",     "kurtosis",       lambda v: f"{v:.4f}"),
        ("frac_lt_1s",   "frac < 1 s",    lambda v: f"{v:.4f}  ({v*100:.1f}%)"),
        ("frac_lt_10ms", "frac < 10 ms",  lambda v: f"{v:.4f}  ({v*100:.1f}%)"),
    ]
    for key, label, fmt in rows:
        line = f"  {label:<20}"
        for k in ("horizontal", "vertical", "vertical_agg"):
            s = stats_all.get(k, {})
            val = s.get(key, float("nan"))
            line += f"  {fmt(val):<26}"
        w(line)

    w()
    w("  CV note: CV≈1.0 => exponential/Poisson arrivals; >1 => over-dispersed")

    w()
    w(SEP)
    w("KOLMOGOROV-SMIRNOV TESTS  (two-sample, two-sided)")
    w("-" * 72)
    for t in tests:
        ks_p = t["ks_p"]
        verdict = "REJECT H0  (distributions differ)" if ks_p < 0.05 else "fail to reject H0"
        w(f"  {t['label']}")
        w(f"    KS statistic  = {t['ks_stat']:.6f}")
        w(f"    p-value       = {ks_p:.3e}")
        w(f"    conclusion    = {verdict}")
        w()

    w(SEP)
    w("MANN-WHITNEY U TESTS  (non-parametric, two-sided)")
    w("-" * 72)
    for t in tests:
        mw_p = t["mw_p"]
        verdict = "REJECT H0  (medians differ)" if mw_p < 0.05 else "fail to reject H0"
        w(f"  {t['label']}")
        w(f"    U statistic   = {t['mw_stat']:.2f}")
        w(f"    p-value       = {mw_p:.3e}")
        w(f"    conclusion    = {verdict}")
        w()

    w(SEP)
    w("EFFECT SIZES")
    w("-" * 72)
    w("  |Cohen d|:    0.2=small  0.5=medium  0.8=large")
    w("  |Cliff delta|: 0.15=small  0.33=medium  0.47=large")
    w()
    for t in tests:
        w(f"  {t['label']}")
        w(f"    Cohen d       = {t['cohen_d']:+.4f}")
        w(f"    Cliff delta   = {t['cliff_delta']:+.4f}")
        w()

    w(SEP)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Plot
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
    for (row_idx, _col_idx), cell in tbl.get_celld().items():
        cell.set_edgecolor(GRID_COL)
        if row_idx == 0:
            cell.set_facecolor("#d0d8e8")
            cell.set_text_props(fontweight="bold", fontsize=7.5)
        elif row_idx % 2 == 0:
            cell.set_facecolor("#eeeeee")
        else:
            cell.set_facecolor("white")


def generate_plot(
    pools: dict[str, np.ndarray],
    run_counts: dict[str, int],
    tests: list[dict],
    output_path: Path,
    mislabeled: list[str],
) -> None:
    fig = plt.figure(figsize=(18, 16))
    fig.patch.set_facecolor(DARK_BG)

    gs = gridspec.GridSpec(
        3, 2,
        figure=fig,
        hspace=0.42,
        wspace=0.30,
        top=0.93, bottom=0.04, left=0.05, right=0.97,
    )

    axes = [[fig.add_subplot(gs[r, c]) for c in range(2)] for r in range(3)]

    n_mis = len(mislabeled)
    mis_note = (f"  [{n_mis} mislabeled run(s) reclassified]" if n_mis else "")
    fig.suptitle(
        "LLM IAT Statistical Analysis — discussion stage, within-run"
        + mis_note,
        fontsize=11, color=TEXT_COL, fontweight="bold",
    )

    series = [
        (pools["horizontal"],   H_COLOR,  f"horizontal  (n={len(pools['horizontal'])} IATs, {run_counts.get('horizontal',0)} runs)"),
        (pools["vertical"],     V_COLOR,  f"vertical raw  (n={len(pools['vertical'])} IATs, {run_counts.get('vertical',0)} runs)"),
        (pools["vertical_agg"], VA_COLOR, f"vertical aggregated  (n={len(pools['vertical_agg'])} IATs)"),
    ]

    # ── [0,0] Histogram + KDE ────────────────────────────────────────────
    ax = axes[0][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("IAT Histogram + KDE  (clipped at 180 s)", loc="left", fontsize=9)
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
    ax.legend(fontsize=6.5, loc="upper right")

    # ── [0,1] ECDF ───────────────────────────────────────────────────────
    ax = axes[0][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("ECDF  (dashed=p50, dotted=p95)", loc="left", fontsize=9)
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

    # ── [1,0] Box plot ────────────────────────────────────────────────────
    ax = axes[1][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("IAT Box Plot  (clipped at 180 s)", loc="left", fontsize=9)
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
        ax.set_yticklabels(bp_labels, fontsize=7.5)
        ax.set_xlim(0, IAT_MAX_S)

    # ── [1,1] Near-zero fraction bar + descriptive stats table ───────────
    ax = axes[1][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Fraction of Near-Zero IATs  (< 1 s)", loc="left", fontsize=9)
    ax.set_ylabel("fraction")
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
        ax.set_xticklabels(bar_names, fontsize=7.5)
        ax.set_ylim(0, 1.05)
        for bar, frac in zip(bars, bar_fracs):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                frac + 0.02,
                f"{frac:.1%}",
                ha="center", va="bottom", fontsize=8, color=TEXT_COL,
            )

    # ── [2,0] Descriptive statistics table ───────────────────────────────
    ax = axes[2][0]
    ax.set_title("Descriptive Statistics", loc="left", fontsize=9)
    stats_all = {k: descriptive_stats(pools[k]) for k in ("horizontal", "vertical", "vertical_agg")}
    stat_rows = [
        ("n",             "n",             lambda v: f"{int(v)}"),
        ("mean",          "mean (s)",      lambda v: f"{v:.2f}"),
        ("std",           "std (s)",       lambda v: f"{v:.2f}"),
        ("cv",            "CV",            lambda v: f"{v:.3f}"),
        ("median",        "median (s)",    lambda v: f"{v:.2f}"),
        ("p95",           "p95 (s)",       lambda v: f"{v:.2f}"),
        ("skewness",      "skewness",      lambda v: f"{v:.2f}"),
        ("kurtosis",      "kurtosis",      lambda v: f"{v:.2f}"),
        ("frac_lt_1s",    "frac < 1 s",   lambda v: f"{v:.1%}"),
        ("frac_lt_10ms",  "frac < 10 ms", lambda v: f"{v:.1%}"),
    ]
    col_labels_d = ["Metric", "Horizontal", "Vertical\n(raw)", "Vertical\n(aggregated)"]
    tbl_rows_d = []
    for key, label, fmt in stat_rows:
        row = [label]
        for k in ("horizontal", "vertical", "vertical_agg"):
            val = stats_all.get(k, {}).get(key, float("nan"))
            row.append(fmt(val) if not (isinstance(val, float) and val != val) else "—")
        tbl_rows_d.append(row)
    _table_ax(ax, col_labels_d, tbl_rows_d, col_widths=[0.30, 0.23, 0.23, 0.24])

    # ── [2,1] Statistical tests table ────────────────────────────────────
    ax = axes[2][1]
    ax.set_title("Statistical Tests  (KS · Mann-Whitney · Effect Sizes)", loc="left", fontsize=9)
    col_labels_t = ["Pair", "KS stat", "KS p-val", "MW p-val", "Cohen d", "Cliff δ"]
    tbl_rows_t = []
    for t in tests:
        ks_p  = t["ks_p"]
        mw_p  = t["mw_p"]
        def _pstr(p: float) -> str:
            if p != p: return "—"         # nan
            if p < 1e-300: return "< 1e-300"
            return f"{p:.2e}"
        sig_ks = " *" if ks_p < 0.05 else ""
        sig_mw = " *" if mw_p < 0.05 else ""
        tbl_rows_t.append([
            t["label"],
            f"{t['ks_stat']:.4f}",
            _pstr(ks_p) + sig_ks,
            _pstr(mw_p) + sig_mw,
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
        help="Directory to write outputs (default: <experiment-dir>/plots/)",
    )
    args = parser.parse_args()

    experiment_dir = Path(args.experiment_dir)
    if not experiment_dir.is_dir():
        sys.exit(f"ERROR: experiment directory not found: {experiment_dir}")

    output_dir = Path(args.output_dir) if args.output_dir else experiment_dir / "plots"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[info] loading runs from {experiment_dir} …")
    pools, run_counts, mislabeled = load_pools(experiment_dir)

    if mislabeled:
        print(f"[warn] {len(mislabeled)} mislabeled run(s) reclassified by request labels:")
        for name in mislabeled:
            print(f"       {name}")

    total = sum(run_counts.values())
    if total == 0:
        sys.exit("ERROR: no valid runs found — check response.json files exist")

    print(f"[info] loaded {total} runs  "
          f"(horizontal={run_counts.get('horizontal',0)}, "
          f"vertical={run_counts.get('vertical',0)})")

    tests = pairwise_tests(pools)
    report = build_text_report(pools, run_counts, mislabeled, tests)

    print(report)

    txt_path = output_dir / "iat_statistics.txt"
    txt_path.write_text(report)
    print(f"  saved  {txt_path}")

    if not SCIPY_AVAILABLE:
        print("  WARN  scipy not available — plot will be missing KDE and test results")

    generate_plot(pools, run_counts, tests, output_dir / "iat_statistics.png", mislabeled)


if __name__ == "__main__":
    main()
