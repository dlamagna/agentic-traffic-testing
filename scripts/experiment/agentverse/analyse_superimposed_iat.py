#!/usr/bin/env python3
"""
analyse_superimposed_iat.py
===========================
Reconstruct the superimposed LLM arrival process — treating all experiment
runs as if they executed simultaneously — and analyse the resulting arrival
rate and resource constraints.

The existing IAT analysis pools *within-run* IATs across runs, which gives a
better statistical estimate of a single run's traffic pattern but does NOT
model what happens when N runs hit a shared LLM endpoint in parallel.  This
script reconstructs that superimposed process by:

  1. Anchoring each run at its first disc-stage request (t=0 per run).
  2. Shifting all timestamps so every run starts at t=0.
  3. Merging all shifted timestamps into a single sorted timeline.
  4. Computing IATs on this merged stream.

Outputs (in <experiment-dir>/plots/iat_analysis/ by default):
  superimposed_iat.png   — 3×2 figure: IAT comparison, arrival rate,
                           concurrency, queue wait, Little's Law
  superimposed_iat.txt   — plain-text summary

Usage:
    python analyse_superimposed_iat.py <experiment_dir>
           [--structure horizontal|vertical|both]
           [--window SECONDS]
           [--capacity N]
           [--output-dir DIR]
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
    print("WARNING: scipy not available — KDE and KS tests will be skipped", file=sys.stderr)

# ---------------------------------------------------------------------------
# Style — matches the rest of the project
# ---------------------------------------------------------------------------
DARK_BG  = "white"
PANEL_BG = "#f7f7f7"
GRID_COL = "#cccccc"
TEXT_COL = "#222222"
IAT_MAX_S = 100

H_COLOR  = "#1f77b4"   # horizontal within-run
V_COLOR  = "#ff7f0e"   # vertical within-run
SI_COLOR = "#d62728"   # superimposed (merged)

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

DISC_PREFIXES = (
    "horizontal_discussion",
    "synthesize_discussion",
    "vertical_solver",
    "vertical_reviewer",
)

# ---------------------------------------------------------------------------
# Inlined utilities (self-contained — no cross-script imports)
# ---------------------------------------------------------------------------

TASKS_SUBDIR = "tasks"


def _tasks_dir(experiment_dir: Path) -> Path:
    tasks = experiment_dir / TASKS_SUBDIR
    return tasks if tasks.is_dir() else experiment_dir


def _parse_ts(ts_str: str) -> float | None:
    try:
        return datetime.fromisoformat(ts_str).timestamp()
    except Exception:
        return None


def _infer_structure(data: dict) -> str:
    """Infer discussion structure from request labels (label-based wins over metadata)."""
    labels = {req.get("label", "") for req in data.get("llm_requests", [])}
    has_h = any(lb.startswith("horizontal_discussion") for lb in labels)
    has_v = any(lb.startswith(("vertical_solver", "vertical_reviewer")) for lb in labels)

    if has_h and not has_v:
        return "horizontal"
    if has_v:
        return "vertical"

    # Fall back to metadata
    stages = data.get("stages", {})
    meta = (
        stages.get("recruitment", {}).get("communication_structure")
        or stages.get("decision", {}).get("structure_used", "")
    )
    return (meta or "").lower()


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
    }


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_all_runs(
    experiment_dir: Path,
    structure_filter: str = "both",
) -> list[dict]:
    """Walk run dirs and return per-run records with disc-stage request timestamps.

    Each record:
      {run_id, structure, requests: [{abs_ts, duration_s, queue_wait_s, label}]}
    """
    runs: list[dict] = []

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
        if structure_filter != "both" and structure != structure_filter:
            continue

        requests: list[dict] = []
        for req in data.get("llm_requests", []):
            label  = req.get("label", "")
            ts_str = req.get("start_time_utc", "")
            dur    = req.get("duration_seconds")
            if not ts_str or not any(label.startswith(p) for p in DISC_PREFIXES):
                continue
            if dur is None or dur <= 0:
                continue
            abs_ts = _parse_ts(ts_str)
            if abs_ts is None:
                continue
            llm = req.get("llm_meta") or {}
            qw  = llm.get("queue_wait_s")
            requests.append({
                "abs_ts":      abs_ts,
                "duration_s":  float(dur),
                "queue_wait_s": float(qw) if qw is not None else None,
                "label":       label,
            })

        if len(requests) < 2:
            continue

        runs.append({
            "run_id":    run_dir.name,
            "structure": structure,
            "requests":  requests,
        })

    return runs


# ---------------------------------------------------------------------------
# Core computations
# ---------------------------------------------------------------------------

def compute_within_run_iats(runs: list[dict]) -> np.ndarray:
    """Pool within-run IATs across all runs (reproduces existing load_pools logic)."""
    all_iats: list[np.ndarray] = []
    for run in runs:
        ts = np.array(sorted(r["abs_ts"] for r in run["requests"]))
        if len(ts) >= 2:
            all_iats.append(np.diff(ts))
    return np.concatenate(all_iats) if all_iats else np.array([])


def build_superimposed_timestamps(
    runs: list[dict],
) -> tuple[np.ndarray, np.ndarray, list[float | None]]:
    """Construct the superimposed process by anchoring each run at its first request.

    Each run is shifted so its first disc-stage request lands at t=0.  All
    shifted timestamps are merged into a single sorted stream — equivalent to
    all N runs starting simultaneously.

    Returns:
        super_ts   — sorted 1-D array of shifted arrival times (s from t=0)
        super_dur  — corresponding service durations (aligned to super_ts)
        super_qw   — corresponding queue_wait_s values (may contain None)
    """
    all_ts:  list[float] = []
    all_dur: list[float] = []
    all_qw:  list[float | None] = []

    for run in runs:
        reqs   = run["requests"]
        anchor = min(r["abs_ts"] for r in reqs)
        for r in reqs:
            all_ts.append(r["abs_ts"] - anchor)
            all_dur.append(r["duration_s"])
            all_qw.append(r["queue_wait_s"])

    order    = np.argsort(all_ts)
    super_ts  = np.array(all_ts,  dtype=float)[order]
    super_dur = np.array(all_dur, dtype=float)[order]
    super_qw  = [all_qw[i] for i in order]
    return super_ts, super_dur, super_qw


def compute_arrival_rate(
    super_ts: np.ndarray,
    window_s: float = 60.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Rolling-window arrival rate (requests/s) over the merged timeline."""
    if len(super_ts) == 0:
        return np.array([]), np.array([])
    t_max  = float(super_ts[-1])
    t_axis = np.arange(0.0, t_max + 1.0, 1.0)
    left   = np.maximum(t_axis - window_s, 0.0)
    counts = (
        np.searchsorted(super_ts, t_axis, side="right")
        - np.searchsorted(super_ts, left,   side="left")
    )
    return t_axis, counts.astype(float) / window_s


def compute_concurrency(
    super_ts: np.ndarray,
    super_dur: np.ndarray,
    resolution_s: float = 0.5,
) -> tuple[np.ndarray, np.ndarray]:
    """Number of simultaneously in-flight requests at each query point.

    conc(t) = |{ i : super_ts[i] ≤ t ≤ super_ts[i] + super_dur[i] }|

    Uses chunked broadcasting to avoid large memory allocations.
    """
    if len(super_ts) == 0:
        return np.array([]), np.array([])

    ends   = super_ts + super_dur
    t_max  = float(ends.max())
    query_t = np.arange(0.0, t_max + resolution_s, resolution_s)
    conc    = np.zeros(len(query_t), dtype=int)

    CHUNK = 500
    for i in range(0, len(query_t), CHUNK):
        q      = query_t[i : i + CHUNK, np.newaxis]          # (chunk, 1)
        starts = super_ts[np.newaxis, :]                      # (1, N)
        finish = ends[np.newaxis, :]                          # (1, N)
        conc[i : i + CHUNK] = np.sum((q >= starts) & (q <= finish), axis=1)

    return query_t, conc


def littles_law(
    super_ts: np.ndarray,
    queue_waits: list[float],
) -> dict:
    """Estimate mean queue depth via Little's Law: L = λ × W."""
    if len(super_ts) < 2 or not queue_waits:
        return {"lambda_rps": float("nan"), "mean_wait_s": float("nan"), "L": float("nan")}
    span = float(super_ts[-1] - super_ts[0])
    lam  = len(super_ts) / span if span > 0 else float("nan")
    W    = float(np.mean(queue_waits))
    return {"lambda_rps": lam, "mean_wait_s": W, "L": lam * W}


def _extract_queue_waits(runs: list[dict], structure: str | None = None) -> list[float]:
    vals: list[float] = []
    for run in runs:
        if structure and run["structure"] != structure:
            continue
        for r in run["requests"]:
            if r["queue_wait_s"] is not None:
                vals.append(r["queue_wait_s"])
    return vals


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------

def build_text_report(
    runs_h: list[dict],
    runs_v: list[dict],
    within_h: np.ndarray,
    within_v: np.ndarray,
    super_ts: np.ndarray,
    super_dur: np.ndarray,
    super_qw: list[float | None],
    t_axis: np.ndarray,
    rates: np.ndarray,
    query_t: np.ndarray,
    conc: np.ndarray,
    experiment_dir: Path,
    window_s: float,
    capacity: int,
    structure_filter: str,
) -> str:
    buf = StringIO()

    def w(line: str = "") -> None:
        buf.write(line + "\n")

    SEP = "=" * 72

    n_runs_h = len(runs_h)
    n_runs_v = len(runs_v)
    n_runs   = n_runs_h + n_runs_v
    n_req    = len(super_ts)
    t_span   = float(super_ts[-1]) if len(super_ts) else 0.0

    super_iats = np.diff(super_ts) if len(super_ts) >= 2 else np.array([])
    qw_vals    = [x for x in super_qw if x is not None]
    qw_h       = _extract_queue_waits(runs_h)
    qw_v       = _extract_queue_waits(runs_v)

    w(SEP)
    w("  SUPERIMPOSED IAT ANALYSIS  (discussion-stage requests)")
    w(SEP)
    w(f"  Experiment dir:       {experiment_dir}")
    w(f"  Structures analysed:  {structure_filter}")
    w(f"  Runs loaded:          {n_runs}  (horizontal={n_runs_h}, vertical={n_runs_v})")
    w(f"  Total requests:       {n_req}")
    w(f"  Merged timeline span: {t_span:.1f} s  (anchor = first request per run → t=0)")
    w(f"  Rolling window:       {window_s:.0f} s")
    w()

    # ── Within-run IATs ──────────────────────────────────────────────────
    w(SEP)
    w("  WITHIN-RUN POOLED IATs  (current approach)")
    w("-" * 72)
    keys = []
    arrs = []
    if len(within_h):
        keys.append(f"horizontal ({n_runs_h} runs)")
        arrs.append(within_h)
    if len(within_v):
        keys.append(f"vertical   ({n_runs_v} runs)")
        arrs.append(within_v)

    def _stat_table(col_keys: list[str], col_arrs: list[np.ndarray]) -> None:
        header = f"  {'Metric':<22}" + "".join(f"  {k:<26}" for k in col_keys)
        w(header)
        w("-" * 72)
        rows = [
            ("n",            "n",             lambda v: f"{int(v)}"),
            ("mean",         "mean (s)",      lambda v: f"{v:.4f}"),
            ("std",          "std (s)",       lambda v: f"{v:.4f}"),
            ("cv",           "CV (std/mean)", lambda v: f"{v:.4f}"),
            ("median",       "median (s)",    lambda v: f"{v:.4f}"),
            ("p95",          "p95 (s)",       lambda v: f"{v:.4f}"),
            ("p99",          "p99 (s)",       lambda v: f"{v:.4f}"),
            ("min",          "min (s)",       lambda v: f"{v:.6f}"),
            ("max",          "max (s)",       lambda v: f"{v:.4f}"),
            ("skewness",     "skewness",      lambda v: f"{v:.4f}"),
            ("frac_lt_1s",   "frac < 1 s",   lambda v: f"{v:.4f}  ({v*100:.1f}%)"),
            ("frac_lt_10ms", "frac < 10 ms", lambda v: f"{v:.4f}  ({v*100:.1f}%)"),
        ]
        stats_list = [descriptive_stats(a) for a in col_arrs]
        for key, label, fmt in rows:
            line = f"  {label:<22}"
            for s in stats_list:
                val = s.get(key, float("nan"))
                if isinstance(val, float) and val != val:
                    line += f"  {'—':<26}"
                else:
                    line += f"  {fmt(val):<26}"
            w(line)
        w()
        w("  CV note: ≈1.0 → Poisson arrivals; >1 → over-dispersed (bursty)")

    if keys:
        _stat_table(keys, arrs)
    w()

    # ── Superimposed IATs ────────────────────────────────────────────────
    w(SEP)
    w("  SUPERIMPOSED IATs  (all runs anchored to t=0 and merged)")
    w("-" * 72)
    if len(super_iats):
        _stat_table([f"superimposed ({n_runs} runs)"], [super_iats])

        # Rate-ratio note
        mean_within = float(np.concatenate([within_h, within_v]).mean()) if (len(within_h) or len(within_v)) else float("nan")
        mean_super  = float(super_iats.mean())
        if mean_within > 0 and mean_super > 0:
            ratio = mean_within / mean_super
            w()
            w(f"  Mean IAT ratio (within/superimposed): {ratio:.2f}×")
            w(f"  → superimposed process is ≈{ratio:.1f}× more frequent than a single run")
            w(f"    (theoretical max for {n_runs} i.i.d. runs: {n_runs}×)")
    w()

    # ── Arrival rate ─────────────────────────────────────────────────────
    w(SEP)
    w(f"  ARRIVAL RATE ANALYSIS  (rolling {window_s:.0f}s window)")
    w("-" * 72)
    if len(rates):
        mean_rate = float(rates[rates > 0].mean()) if np.any(rates > 0) else 0.0
        peak_rate = float(rates.max())
        peak_t    = float(t_axis[np.argmax(rates)])
        # Single-run arrival rate: total requests / total time span
        single_rates = []
        for run in runs_h + runs_v:
            ts_run = sorted(r["abs_ts"] for r in run["requests"])
            span_r = ts_run[-1] - ts_run[0]
            if span_r > 0:
                single_rates.append(len(ts_run) / span_r)
        mean_single = float(np.mean(single_rates)) if single_rates else float("nan")
        w(f"  Mean arrival rate (λ):   {mean_rate:.4f} req/s")
        w(f"  Peak arrival rate:       {peak_rate:.4f} req/s  (at t={peak_t:.1f}s)")
        w(f"  Mean single-run rate:    {mean_single:.4f} req/s")
        if mean_single > 0:
            w(f"  Superposition factor:    {mean_rate / mean_single:.2f}×  (of max {n_runs}×)")
    w()

    # ── Concurrency ───────────────────────────────────────────────────────
    w(SEP)
    w("  CONCURRENCY ANALYSIS")
    w("-" * 72)
    if len(conc):
        active = conc[conc > 0]
        if len(active):
            p50  = float(np.percentile(active, 50))
            p95  = float(np.percentile(active, 95))
            p99  = float(np.percentile(active, 99))
            peak = int(active.max())
            mean = float(active.mean())
            w(f"  Query resolution:  0.5 s")
            w(f"  Mean concurrency:  {mean:.2f}")
            w(f"  p50 concurrency:   {p50:.1f}")
            w(f"  p95 concurrency:   {p95:.1f}")
            w(f"  p99 concurrency:   {p99:.1f}")
            w(f"  Peak concurrency:  {peak}  (at t={float(query_t[np.argmax(conc)]):.1f}s)")
    w()

    # ── Little's Law ──────────────────────────────────────────────────────
    w(SEP)
    w("  QUEUE DEPTH ANALYSIS  (Little's Law: L = λ × W)")
    w("-" * 72)
    w("  λ = arrival rate (req/s), W = mean queue_wait_s, L = predicted mean queue depth")
    w()

    def _ll_row(label: str, ts_sub: np.ndarray, qw_sub: list[float]) -> None:
        ll = littles_law(ts_sub, qw_sub)
        w(f"  {label}")
        w(f"    λ           = {ll['lambda_rps']:.4f} req/s")
        w(f"    W (mean qw) = {ll['mean_wait_s']:.4f} s")
        w(f"    L           = {ll['L']:.4f}  (predicted mean queue depth)")
        w()

    if len(runs_h):
        ts_h = build_superimposed_timestamps(runs_h)[0]
        _ll_row(f"Horizontal  ({n_runs_h} runs)", ts_h, qw_h)
    if len(runs_v):
        ts_v = build_superimposed_timestamps(runs_v)[0]
        _ll_row(f"Vertical    ({n_runs_v} runs)", ts_v, qw_v)
    _ll_row("Combined superimposed", super_ts, qw_vals)

    # ── Saturation ────────────────────────────────────────────────────────
    w(SEP)
    w(f"  RESOURCE SATURATION  (capacity threshold = {capacity} concurrent requests)")
    w("-" * 72)
    if len(conc):
        frac_sat = float(np.mean(conc >= capacity))
        # Estimate single-run saturation (average concurrency / capacity)
        single_conc_vals = []
        for run in runs_h + runs_v:
            reqs = run["requests"]
            ts_r  = np.array([r["abs_ts"] for r in reqs])
            dur_r = np.array([r["duration_s"] for r in reqs])
            if len(ts_r) < 2:
                continue
            ts_r = ts_r - ts_r.min()  # anchor to t=0 to avoid huge arange
            _, c_r = compute_concurrency(ts_r, dur_r, resolution_s=0.5)
            single_conc_vals.extend(c_r[c_r > 0].tolist())
        frac_sat_single = float(np.mean(np.array(single_conc_vals) >= capacity)) if single_conc_vals else float("nan")
        w(f"  Fraction of time ≥ {capacity} concurrent requests:")
        w(f"    Superimposed:  {frac_sat:.1%}")
        w(f"    Typical run:   {frac_sat_single:.1%}")
        w()
        w("  Note: saturation computed from concurrency time series (0.5s resolution)")

    w()
    w(SEP)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Table helper (shared with plot)
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
# Plot
# ---------------------------------------------------------------------------

def generate_plot(
    runs_h: list[dict],
    runs_v: list[dict],
    within_h: np.ndarray,
    within_v: np.ndarray,
    super_ts: np.ndarray,
    super_dur: np.ndarray,
    super_qw: list[float | None],
    t_axis: np.ndarray,
    rates: np.ndarray,
    query_t: np.ndarray,
    conc: np.ndarray,
    window_s: float,
    capacity: int,
    structure_filter: str,
    output_path: Path,
) -> None:
    super_iats = np.diff(super_ts) if len(super_ts) >= 2 else np.array([])
    qw_h = _extract_queue_waits(runs_h)
    qw_v = _extract_queue_waits(runs_v)
    n_runs = len(runs_h) + len(runs_v)

    fig = plt.figure(figsize=(18, 14))
    fig.patch.set_facecolor(DARK_BG)
    gs = gridspec.GridSpec(
        3, 2,
        figure=fig,
        hspace=0.45, wspace=0.30,
        top=0.93, bottom=0.04, left=0.06, right=0.97,
    )
    axes = [[fig.add_subplot(gs[r, c]) for c in range(2)] for r in range(3)]

    fig.suptitle(
        f"Superimposed LLM Arrival Process  "
        f"({n_runs} runs merged, structure={structure_filter})",
        fontsize=11, color=TEXT_COL, fontweight="bold",
    )

    # ── [0,0] IAT Histogram + KDE ────────────────────────────────────────
    ax = axes[0][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title(f"IAT Distribution: Within-run vs Superimposed  (clipped at {IAT_MAX_S}s)",
                 loc="left", fontsize=9)
    ax.set_xlabel("interarrival time (s)")
    ax.set_ylabel("density")
    ax.grid(True)

    series_iat = []
    if len(within_h):
        series_iat.append((within_h, H_COLOR,
                           f"within-run horizontal  (n={len(within_h)}, {len(runs_h)} runs)"))
    if len(within_v):
        series_iat.append((within_v, V_COLOR,
                           f"within-run vertical  (n={len(within_v)}, {len(runs_v)} runs)"))
    if len(super_iats):
        series_iat.append((super_iats, SI_COLOR,
                           f"superimposed  (n={len(super_iats)}, {n_runs} runs)"))

    for arr, color, label in series_iat:
        clipped = arr[arr <= IAT_MAX_S]
        n_over  = len(arr) - len(clipped)
        lbl     = label if n_over == 0 else f"{label}  ({n_over} clipped)"
        ax.hist(clipped, bins=40, density=True, alpha=0.25, color=color, label=lbl)
        if SCIPY_AVAILABLE and len(clipped) > 5:
            kde  = scipy_stats.gaussian_kde(clipped)
            xs   = np.linspace(0, IAT_MAX_S, 400)
            kv   = kde(xs)
            kv[xs < clipped.min()] = 0.0
            ax.plot(xs, kv, color=color, linewidth=2.2)

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
    for i, (arr, color, label) in enumerate(series_iat):
        clipped = np.sort(arr[arr <= IAT_MAX_S])
        if len(clipped) == 0:
            continue
        y = np.arange(1, len(clipped) + 1) / len(clipped)
        ax.plot(clipped, y, color=color, linewidth=2, label=label)
        for pct, ls in [(50, "--"), (95, ":")]:
            pval = float(np.percentile(clipped, pct))
            ax.axvline(pval, color=color, linestyle=ls, alpha=0.6, linewidth=1)
            ax.text(pval, 0.02 + i * 0.08,
                    f"p{pct}={pval:.1f}s", color=color, fontsize=5.5, ha="left")
    ax.legend(fontsize=6.5)

    # ── [1,0] Arrival rate over time ──────────────────────────────────────
    ax = axes[1][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title(f"Superimposed Arrival Rate  (rolling {window_s:.0f}s window)",
                 loc="left", fontsize=9)
    ax.set_xlabel("time since first request (s)")
    ax.set_ylabel("requests / second")
    ax.grid(True)
    if len(rates):
        ax.plot(t_axis, rates, color=SI_COLOR, linewidth=1.8, label="arrival rate")
        mean_rate = float(rates[rates > 0].mean()) if np.any(rates > 0) else 0.0
        ax.axhline(mean_rate, color=SI_COLOR, linestyle="--", alpha=0.6, linewidth=1)
        ax.text(t_axis[-1] * 0.98, mean_rate * 1.05,
                f"mean λ = {mean_rate:.3f} req/s",
                ha="right", va="bottom", fontsize=8, color=SI_COLOR)
        ax.set_xlim(0, t_axis[-1])
        ax.set_ylim(bottom=0)
    ax.legend(fontsize=7)

    # ── [1,1] Concurrency CDF ─────────────────────────────────────────────
    ax = axes[1][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("CDF of Superimposed Concurrency Level", loc="left", fontsize=9)
    ax.set_xlabel("simultaneous in-flight requests")
    ax.set_ylabel("P(concurrency ≤ x)")
    ax.grid(True)
    if len(conc):
        active = conc[conc > 0]
        if len(active):
            sorted_c = np.sort(active)
            y        = np.arange(1, len(sorted_c) + 1) / len(sorted_c)
            ax.plot(sorted_c, y, color=SI_COLOR, linewidth=2, label="superimposed")
            p50  = float(np.percentile(active, 50))
            p95  = float(np.percentile(active, 95))
            p99  = float(np.percentile(active, 99))
            peak = int(active.max())
            for pval, ls, lbl in [(p50, "--", "p50"), (p95, ":", "p95")]:
                ax.axvline(pval, color=SI_COLOR, linestyle=ls, alpha=0.7, linewidth=1.2)
                ax.text(pval + 0.05, 0.05, f"{lbl}={pval:.1f}",
                        color=SI_COLOR, fontsize=7, va="bottom")
            ax.axvline(capacity, color="#888888", linestyle="-.", linewidth=1.2,
                       label=f"capacity={capacity}")
            ax.text(0.05, 0.80,
                    f"p50={p50:.0f}  p95={p95:.0f}  p99={p99:.0f}  peak={peak}",
                    transform=ax.transAxes, fontsize=7.5, color=TEXT_COL,
                    bbox=dict(facecolor=PANEL_BG, edgecolor=GRID_COL, pad=3))
            ax.set_ylim(0, 1.05)
    ax.legend(fontsize=7)

    # ── [2,0] Queue wait distribution by structure ────────────────────────
    ax = axes[2][0]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Queue Wait (TTFT) Distribution by Structure  (clipped at 20s)",
                 loc="left", fontsize=9)
    ax.set_xlabel("queue_wait_s (s)")
    ax.set_ylabel("density")
    ax.grid(True)
    CLIP_QW = 20.0
    qw_series = []
    if qw_h:
        qw_series.append((np.array(qw_h), H_COLOR, f"horizontal  (n={len(qw_h)})"))
    if qw_v:
        qw_series.append((np.array(qw_v), V_COLOR, f"vertical  (n={len(qw_v)})"))

    for arr, color, label in qw_series:
        clipped = arr[arr <= CLIP_QW]
        if len(clipped) < 2:
            continue
        ax.hist(clipped, bins=30, density=True, alpha=0.30, color=color, label=label)
        if SCIPY_AVAILABLE and len(clipped) > 5:
            kde = scipy_stats.gaussian_kde(clipped)
            xs  = np.linspace(0, CLIP_QW, 300)
            kv  = kde(xs)
            kv[xs < clipped.min()] = 0.0
            ax.plot(xs, kv, color=color, linewidth=2.2)
        mean_v = float(arr.mean())
        p95_v  = float(np.percentile(arr, 95))
        ax.axvline(mean_v, color=color, linestyle="--", alpha=0.7, linewidth=1)
        ax.text(mean_v, ax.get_ylim()[1] * 0.9 if ax.get_ylim()[1] > 0 else 1,
                f"μ={mean_v:.2f}s", color=color, fontsize=7, ha="left", va="top")

    ax.set_xlim(0, CLIP_QW)
    ax.legend(fontsize=7)

    # ── [2,1] Little's Law bar chart ─────────────────────────────────────
    ax = axes[2][1]
    ax.set_facecolor(PANEL_BG)
    ax.set_title("Little's Law: Predicted Mean Queue Depth  (L = λ × W)",
                 loc="left", fontsize=9)
    ax.set_ylabel("predicted queue depth (L)")
    ax.grid(True, axis="y")

    bar_labels: list[str] = []
    bar_L:      list[float] = []
    bar_colors: list[str] = []
    bar_annots: list[str] = []

    if len(runs_h):
        ts_h = build_superimposed_timestamps(runs_h)[0]
        ll_h = littles_law(ts_h, qw_h)
        bar_labels.append("horizontal")
        bar_L.append(ll_h["L"])
        bar_colors.append(H_COLOR)
        bar_annots.append(f"λ={ll_h['lambda_rps']:.3f}\nW={ll_h['mean_wait_s']:.2f}s")

    if len(runs_v):
        ts_v = build_superimposed_timestamps(runs_v)[0]
        ll_v = littles_law(ts_v, qw_v)
        bar_labels.append("vertical")
        bar_L.append(ll_v["L"])
        bar_colors.append(V_COLOR)
        bar_annots.append(f"λ={ll_v['lambda_rps']:.3f}\nW={ll_v['mean_wait_s']:.2f}s")

    qw_all = [x for x in super_qw if x is not None]
    ll_all = littles_law(super_ts, qw_all)
    bar_labels.append("combined")
    bar_L.append(ll_all["L"])
    bar_colors.append(SI_COLOR)
    bar_annots.append(f"λ={ll_all['lambda_rps']:.3f}\nW={ll_all['mean_wait_s']:.2f}s")

    if bar_labels:
        xpos = np.arange(len(bar_labels))
        bars = ax.bar(xpos, bar_L, color=bar_colors, alpha=0.75, width=0.5)
        ax.set_xticks(xpos)
        ax.set_xticklabels(bar_labels)

        # Saturation fraction from concurrency time series
        if len(conc):
            frac_sat = float(np.mean(conc >= capacity))
            ax.axhline(capacity, color="#888888", linestyle="-.", linewidth=1.2,
                       label=f"capacity={capacity}")
            ax.text(xpos[-1] + 0.4, capacity + 0.02,
                    f"cap={capacity}\n({frac_sat:.1%} sat.)",
                    fontsize=7, color="#888888", va="bottom")

        for bar, L_val, annot in zip(bars, bar_L, bar_annots):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                L_val + 0.01 * max(bar_L + [0.01]),
                f"L={L_val:.3f}\n{annot}",
                ha="center", va="bottom", fontsize=7, color=TEXT_COL,
            )
        ax.set_ylim(bottom=0)

    ax.legend(fontsize=7)

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
        "experiment_dir",
        metavar="EXPERIMENT_DIR",
        help="Experiment root directory",
    )
    parser.add_argument(
        "--structure",
        choices=["horizontal", "vertical", "both"],
        default="both",
        help="Filter to a specific structure (default: both)",
    )
    parser.add_argument(
        "--window",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="Rolling window size (s) for arrival rate plot (default: 60)",
    )
    parser.add_argument(
        "--capacity",
        type=int,
        default=10,
        metavar="N",
        help="Hypothetical max concurrent requests for saturation estimate (default: 10)",
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        help="Output directory (default: <experiment_dir>/plots/iat_analysis/)",
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

    print(f"[info] loading runs from {experiment_dir} …")
    runs = load_all_runs(experiment_dir, args.structure)

    if not runs:
        sys.exit("ERROR: no valid runs found — check response.json files exist")

    runs_h = [r for r in runs if r["structure"] == "horizontal"]
    runs_v = [r for r in runs if r["structure"] == "vertical"]

    print(f"[info] loaded {len(runs)} runs  "
          f"(horizontal={len(runs_h)}, vertical={len(runs_v)})")

    print("[info] computing within-run pooled IATs …")
    within_h = compute_within_run_iats(runs_h)
    within_v = compute_within_run_iats(runs_v)

    print("[info] building superimposed timeline …")
    super_ts, super_dur, super_qw = build_superimposed_timestamps(runs)

    print("[info] computing arrival rate …")
    t_axis, rates = compute_arrival_rate(super_ts, window_s=args.window)

    print("[info] computing concurrency time series …")
    query_t, conc = compute_concurrency(super_ts, super_dur)

    report = build_text_report(
        runs_h, runs_v,
        within_h, within_v,
        super_ts, super_dur, super_qw,
        t_axis, rates,
        query_t, conc,
        experiment_dir,
        window_s=args.window,
        capacity=args.capacity,
        structure_filter=args.structure,
    )
    print(report)

    txt_path = output_dir / "superimposed_iat.txt"
    txt_path.write_text(report)
    print(f"  saved  {txt_path}")

    print("[info] generating plot …")
    generate_plot(
        runs_h, runs_v,
        within_h, within_v,
        super_ts, super_dur, super_qw,
        t_axis, rates,
        query_t, conc,
        window_s=args.window,
        capacity=args.capacity,
        structure_filter=args.structure,
        output_path=output_dir / "superimposed_iat.png",
    )


if __name__ == "__main__":
    main()
