#!/usr/bin/env python3
"""
correlate_structure_metrics.py
================================
Correlate AgentVerse discussion structure (horizontal / vertical) with
lower-level network and LLM metrics stored in per-run metrics.csv files.

For each run this script extracts:
  - Structure label        (response.json → stages.recruitment.communication_structure)
  - App-level stats        (response.json → llm_requests, discussion-stage filtered)
  - Network / LLM metrics  (metrics.csv  → aggregated to per-run scalars)

Outputs (all saved to --output-dir):
  network_metrics_by_structure.png   — violin/box plots, one panel per metric
  correlation_heatmap.png            — Spearman correlation matrix
  per_run_metrics.csv                — raw per-run dataframe (for further analysis)

Usage:
    python correlate_structure_metrics.py [DATA_DIR ...] [--output-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
except ImportError as exc:
    sys.exit(f"ERROR: missing dependency – {exc}\nInstall with: pip install matplotlib numpy pandas")

try:
    from scipy import stats as scipy_stats
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

# ---------------------------------------------------------------------------
# Styling (matches compare_discussion_structures.py)
# ---------------------------------------------------------------------------
DARK_BG  = "white"
PANEL_BG = "#f7f7f7"
GRID_COL = "#cccccc"
TEXT_COL = "#222222"
H_COLOR  = "#1f77b4"
V_COLOR  = "#ff7f0e"
PALETTE  = [H_COLOR, V_COLOR]

plt.rcParams.update({
    "figure.facecolor": DARK_BG,
    "axes.facecolor":   PANEL_BG,
    "axes.edgecolor":   GRID_COL,
    "axes.labelcolor":  TEXT_COL,
    "xtick.color":      TEXT_COL,
    "ytick.color":      TEXT_COL,
    "text.color":       TEXT_COL,
})

# Discussion-stage label prefixes (Stage 2 only)
DISCUSSION_LABEL_PREFIXES = (
    "horizontal_discussion",
    "synthesize_discussion",
    "vertical_solver",
    "vertical_reviewer",
)

# ---------------------------------------------------------------------------
# Metric extraction config
# ---------------------------------------------------------------------------
# Each entry: (column_name, panel_title, agg_fn, label_filter)
# label_filter: None = no filter on the 'labels' column JSON;
#               dict  = all key/value pairs must match the parsed labels JSON
METRIC_DEFS: list[tuple[str, str, str, dict | None]] = [
    # ── LLM performance ──────────────────────────────────────────────────
    ("llm_latency_p50_mean_s",   "LLM End-to-end Latency (p50/p95)",         "mean", {"ref_id": "A"}),
    ("llm_latency_p95_mean_s",   "LLM End-to-end Latency (p50/p95)",         "mean", {"ref_id": "B"}),
    ("llm_ttft_p50_mean_s",      "LLM Time-to-First-Token (TTFT p50/p95)",   "mean", {"ref_id": "A"}),
    ("llm_ttft_p95_mean_s",      "LLM Time-to-First-Token (TTFT p50/p95)",   "mean", {"ref_id": "B"}),
    ("llm_inflight_mean",        "In-flight LLM Requests",                   "mean", None),
    ("llm_inflight_peak",        "In-flight LLM Requests",                   "max",  None),
    ("llm_queue_wait_p50_mean_s","Queue Wait Distribution (p50/p95/p99) + In-flight", "mean", {"ref_id": "A"}),
    ("llm_queue_wait_p95_mean_s","Queue Wait Distribution (p50/p95/p99) + In-flight", "mean", {"ref_id": "B"}),
    # ── Traffic characterisation ─────────────────────────────────────────
    ("iat_mean_s",               "LLM Interarrival Time (30s rolling avg)",   "mean", None),
    ("burstiness_mean",          "Burstiness Coefficient (peak 10s / avg 5m)","mean", None),
    ("burstiness_max",           "Burstiness Coefficient (peak 10s / avg 5m)","max",  None),
    ("iat_jitter_p50_mean_s",    "Interarrival Jitter (p95 − p50)",           "mean", {"ref_id": "A"}),
    ("iat_jitter_p95_mean_s",    "Interarrival Jitter (p95 − p50)",           "mean", {"ref_id": "B"}),
    # ── TCP service-level ────────────────────────────────────────────────
    ("tcp_rtt_p50_mean_s",       "TCP RTT (SYN/SYN-ACK Agent A → LLM)",      "mean", {"ref_id": "A"}),
    ("tcp_rtt_p95_mean_s",       "TCP RTT (SYN/SYN-ACK Agent A → LLM)",      "mean", {"ref_id": "B"}),
    ("tcp_flow_dur_p50_mean_s",  "TCP Flow Duration (Agent A → LLM)",         "mean", {"ref_id": "A"}),
    ("tcp_flow_dur_p95_mean_s",  "TCP Flow Duration (Agent A → LLM)",         "mean", {"ref_id": "B"}),
    ("tcp_bytes_llm_mean_Bps",   "TCP Bytes/s from LLM Backend",              "mean", None),
    ("tcp_bytes_llm_peak_Bps",   "TCP Bytes/s from LLM Backend",              "max",  None),
    # ── Throughput ───────────────────────────────────────────────────────
    ("prompt_tokens_per_s_mean", "Prompt Tokens / s",                         "mean", None),
    ("completion_tokens_per_s_mean", "Completion Tokens / s",                 "mean", None),
]

# TCP Bytes/s by Service Pair — these need label filtering by src/dst
TCP_PAIR_METRICS: list[tuple[str, dict]] = [
    ("tcp_bytes_a_to_llm_mean_Bps",
     {"src_service": "agent_a", "dst_service": "llm_backend"}),
    ("tcp_bytes_b_to_llm_mean_Bps",
     None),  # sum all agent_b* → llm_backend, handled specially
    ("tcp_bytes_a_to_b_mean_Bps",
     None),  # sum all agent_a → agent_b*, handled specially
]

# Human-readable labels for plots
METRIC_LABELS: dict[str, str] = {
    "llm_latency_p50_mean_s":        "LLM Latency p50 (s)",
    "llm_latency_p95_mean_s":        "LLM Latency p95 (s)",
    "llm_ttft_p50_mean_s":           "TTFT p50 (s)",
    "llm_ttft_p95_mean_s":           "TTFT p95 (s)",
    "llm_inflight_mean":             "In-flight LLM Req (mean)",
    "llm_inflight_peak":             "In-flight LLM Req (peak)",
    "llm_queue_wait_p50_mean_s":     "Queue Wait p50 (s)",
    "llm_queue_wait_p95_mean_s":     "Queue Wait p95 (s)",
    "iat_mean_s":                    "IAT Mean (s)",
    "burstiness_mean":               "Burstiness (mean)",
    "burstiness_max":                "Burstiness (max)",
    "iat_jitter_p50_mean_s":         "IAT Jitter p50 (s)",
    "iat_jitter_p95_mean_s":         "IAT Jitter p95 (s)",
    "tcp_rtt_p50_mean_s":            "TCP RTT p50 (s)",
    "tcp_rtt_p95_mean_s":            "TCP RTT p95 (s)",
    "tcp_flow_dur_p50_mean_s":       "TCP Flow Dur p50 (s)",
    "tcp_flow_dur_p95_mean_s":       "TCP Flow Dur p95 (s)",
    "tcp_bytes_llm_mean_Bps":        "TCP Bytes/s from LLM (mean)",
    "tcp_bytes_llm_peak_Bps":        "TCP Bytes/s from LLM (peak)",
    "prompt_tokens_per_s_mean":      "Prompt Tokens/s",
    "completion_tokens_per_s_mean":  "Completion Tokens/s",
    "tcp_bytes_a_to_llm_mean_Bps":   "TCP Bytes/s A→LLM",
    "tcp_bytes_b_to_llm_mean_Bps":   "TCP Bytes/s B→LLM (sum)",
    "tcp_bytes_a_to_b_mean_Bps":     "TCP Bytes/s A→B (sum)",
    # app-level
    "disc_n_requests":               "Discussion Requests (count)",
    "disc_duration_s":               "Discussion Duration (s)",
    "disc_total_tokens":             "Discussion Tokens (total)",
    "disc_mean_latency_s":           "Discussion LLM Latency Mean (s)",
    "disc_mean_iat_s":               "Discussion IAT Mean (s)",
}

# Groups for subplots
METRIC_GROUPS: list[tuple[str, list[str]]] = [
    ("LLM Performance", [
        "llm_latency_p50_mean_s", "llm_latency_p95_mean_s",
        "llm_ttft_p50_mean_s", "llm_ttft_p95_mean_s",
        "llm_inflight_mean", "llm_inflight_peak",
        "llm_queue_wait_p50_mean_s", "llm_queue_wait_p95_mean_s",
    ]),
    ("Traffic Characterisation", [
        "iat_mean_s", "burstiness_mean", "burstiness_max",
        "iat_jitter_p50_mean_s", "iat_jitter_p95_mean_s",
    ]),
    ("TCP Service-level", [
        "tcp_rtt_p50_mean_s", "tcp_rtt_p95_mean_s",
        "tcp_flow_dur_p50_mean_s", "tcp_flow_dur_p95_mean_s",
        "tcp_bytes_llm_mean_Bps", "tcp_bytes_llm_peak_Bps",
        "tcp_bytes_a_to_llm_mean_Bps", "tcp_bytes_b_to_llm_mean_Bps",
        "tcp_bytes_a_to_b_mean_Bps",
    ]),
    ("Application (Discussion Stage)", [
        "disc_n_requests", "disc_duration_s",
        "disc_total_tokens", "disc_mean_latency_s", "disc_mean_iat_s",
    ]),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_data_dirs(cli_dirs: list[str]) -> list[Path]:
    if cli_dirs:
        paths = [Path(d) for d in cli_dirs]
        for p in paths:
            if not p.is_dir():
                sys.exit(f"ERROR: directory not found: {p}")
        return paths
    repo_root = Path(__file__).resolve().parents[3]
    runs_root = repo_root / "data" / "runs"
    candidates = sorted(runs_root.glob("100_RUNS_*"), reverse=True)
    if not candidates:
        candidates = sorted(runs_root.glob("experiment_*"), reverse=True)
    if not candidates:
        sys.exit(f"ERROR: no experiment directories found under {runs_root}")
    chosen = candidates[0]
    print(f"[info] auto-selected dataset: {chosen}")
    return [chosen]


def _parse_labels(label_str: str) -> dict:
    """Parse the JSON labels string from metrics.csv into a dict."""
    try:
        return json.loads(label_str) if label_str and label_str.strip() != "{}" else {}
    except Exception:
        return {}


def _matches(row_labels: dict, filter_dict: dict) -> bool:
    """Check if all key/value pairs in filter_dict are present in row_labels."""
    return all(row_labels.get(k) == v for k, v in filter_dict.items())


def _agg(series: pd.Series, fn: str) -> float:
    s = series.dropna()
    if s.empty:
        return float("nan")
    if fn == "mean":
        return float(s.mean())
    if fn == "max":
        return float(s.max())
    if fn == "p95":
        return float(s.quantile(0.95))
    return float(s.mean())


# ---------------------------------------------------------------------------
# Per-run extraction
# ---------------------------------------------------------------------------

def extract_run(run_dir: Path) -> dict | None:
    resp_path = run_dir / "response.json"
    metrics_path = run_dir / "metrics.csv"
    if not resp_path.exists():
        return None

    try:
        data = json.loads(resp_path.read_text())
    except Exception:
        return None

    stages = data.get("stages", {})
    structure = (
        stages.get("recruitment", {}).get("communication_structure")
        or stages.get("decision", {}).get("structure_used")
    )
    if not isinstance(structure, str):
        return None
    structure = structure.lower()
    if structure not in ("horizontal", "vertical"):
        return None

    # --- task slug ---
    task_slug = "unknown"
    meta_path = run_dir / "meta.json"
    if meta_path.exists():
        try:
            task_slug = json.loads(meta_path.read_text()).get("task_slug", "unknown") or "unknown"
        except Exception:
            pass

    row: dict = {"run_dir": str(run_dir), "structure": structure, "task_slug": task_slug}

    # --- app-level: discussion-stage requests ---
    disc_ts: list[float] = []
    disc_durations: list[float] = []
    disc_tokens: list[int] = []
    for req in data.get("llm_requests", []):
        label = req.get("label", "")
        if not any(label.startswith(p) for p in DISCUSSION_LABEL_PREFIXES):
            continue
        ts_str = req.get("start_time_utc")
        if ts_str:
            try:
                disc_ts.append(datetime.fromisoformat(ts_str).timestamp())
            except Exception:
                pass
        dur = req.get("duration_seconds")
        if dur is not None:
            disc_durations.append(float(dur))
        meta = req.get("llm_meta") or {}
        tokens = (meta.get("prompt_tokens", 0) or 0) + (meta.get("completion_tokens", 0) or 0)
        disc_tokens.append(int(tokens))

    row["disc_n_requests"] = len(disc_ts)
    if len(disc_ts) >= 2:
        sorted_ts = sorted(disc_ts)
        row["disc_duration_s"] = sorted_ts[-1] - sorted_ts[0]
        row["disc_mean_iat_s"] = float(np.mean(np.diff(sorted_ts)))
    else:
        row["disc_duration_s"] = float("nan")
        row["disc_mean_iat_s"] = float("nan")
    row["disc_total_tokens"] = sum(disc_tokens)
    row["disc_mean_latency_s"] = float(np.mean(disc_durations)) if disc_durations else float("nan")

    # --- metrics.csv ---
    if not metrics_path.exists():
        return row  # return what we have without CSV metrics

    try:
        df = pd.read_csv(metrics_path)
    except Exception:
        return row

    # Parse labels column once
    df["_labels"] = df["labels"].apply(_parse_labels)
    # Add ref_id from labels if present (some panels use ref_id in labels column)
    # Actually ref_id is a separate column in the CSV
    if "ref_id" not in df.columns:
        df["ref_id"] = ""

    for col_name, panel_title, agg_fn, label_filter in METRIC_DEFS:
        subset = df[df["panel_title"] == panel_title]
        if label_filter:
            # ref_id filter goes against the column directly; other keys against parsed labels
            ref_id_filter = label_filter.get("ref_id")
            other_filters = {k: v for k, v in label_filter.items() if k != "ref_id"}
            if ref_id_filter:
                subset = subset[subset["ref_id"] == ref_id_filter]
            if other_filters:
                subset = subset[subset["_labels"].apply(lambda lb: _matches(lb, other_filters))]
        row[col_name] = _agg(subset["value"], agg_fn)

    # TCP Bytes/s by Service Pair — specialised extraction
    tcp_pair = df[df["panel_title"] == "TCP Bytes/s by Service Pair"].copy()
    if not tcp_pair.empty:
        # agent_a → llm_backend
        mask = tcp_pair["_labels"].apply(
            lambda lb: lb.get("src_service") == "agent_a" and lb.get("dst_service") == "llm_backend"
        )
        row["tcp_bytes_a_to_llm_mean_Bps"] = _agg(tcp_pair[mask]["value"], "mean")

        # sum all agent_b* → llm_backend
        mask_b_llm = tcp_pair["_labels"].apply(
            lambda lb: lb.get("src_service", "").startswith("agent_b")
                       and lb.get("dst_service") == "llm_backend"
        )
        b_llm = tcp_pair[mask_b_llm].groupby("timestamp")["value"].sum()
        row["tcp_bytes_b_to_llm_mean_Bps"] = float(b_llm.mean()) if not b_llm.empty else float("nan")

        # sum agent_a → all agent_b*
        mask_a_b = tcp_pair["_labels"].apply(
            lambda lb: lb.get("src_service") == "agent_a"
                       and lb.get("dst_service", "").startswith("agent_b")
        )
        a_b = tcp_pair[mask_a_b].groupby("timestamp")["value"].sum()
        row["tcp_bytes_a_to_b_mean_Bps"] = float(a_b.mean()) if not a_b.empty else float("nan")

    return row


# ---------------------------------------------------------------------------
# Load all runs
# ---------------------------------------------------------------------------

def load_all_runs(data_dirs: list[Path]) -> pd.DataFrame:
    rows = []
    n_skipped = 0
    for data_dir in data_dirs:
        for run_dir in sorted(data_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            result = extract_run(run_dir)
            if result is None:
                n_skipped += 1
            else:
                rows.append(result)

    print(f"[info] extracted {len(rows)} runs ({n_skipped} skipped)")
    df = pd.DataFrame(rows)
    df["structure"] = pd.Categorical(df["structure"], categories=["horizontal", "vertical"])
    return df


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def _violin_or_box(ax: plt.Axes, h_vals: np.ndarray, v_vals: np.ndarray,
                   col: str) -> None:
    """Draw a violin (if scipy available) or box plot for the two groups."""
    groups = [(h_vals, H_COLOR, "horiz."), (v_vals, V_COLOR, "vert.")]
    data, colors, labels = [], [], []
    for vals, color, label in groups:
        clean = vals[np.isfinite(vals)]
        if len(clean):
            data.append(clean)
            colors.append(color)
            labels.append(f"{label}\n(n={len(clean)})")

    if not data:
        ax.text(0.5, 0.5, "no data", ha="center", va="center", transform=ax.transAxes)
        return

    positions = list(range(1, len(data) + 1))

    if SCIPY_AVAILABLE and all(len(d) > 4 for d in data):
        parts = ax.violinplot(data, positions=positions, showmedians=True,
                              showextrema=True, widths=0.6)
        for i, (pc, color) in enumerate(zip(parts["bodies"], colors)):
            pc.set_facecolor(color)
            pc.set_alpha(0.55)
        for part in ("cmedians", "cmins", "cmaxes", "cbars"):
            if part in parts:
                parts[part].set_color(TEXT_COL)
                parts[part].set_linewidth(1.2)
    else:
        bp = ax.boxplot(data, positions=positions, patch_artist=True, widths=0.5,
                        flierprops=dict(marker=".", markersize=2, alpha=0.3))
        for patch, color in zip(bp["boxes"], colors):
            patch.set_facecolor(color)
            patch.set_alpha(0.55)
        for median in bp["medians"]:
            median.set_color(TEXT_COL)

    ax.set_xticks(positions)
    ax.set_xticklabels(labels, fontsize=7)
    # annotate medians
    for i, (d, color) in enumerate(zip(data, colors)):
        med = np.median(d)
        ax.text(positions[i], ax.get_ylim()[1] if ax.get_ylim()[1] > 0 else 1,
                f"med={med:.3g}", ha="center", va="bottom", fontsize=6, color=color)

    # Mann-Whitney U p-value if both groups have data
    if SCIPY_AVAILABLE and len(data) == 2 and len(data[0]) > 1 and len(data[1]) > 1:
        _, pval = scipy_stats.mannwhitneyu(data[0], data[1], alternative="two-sided")
        pstr = f"p={pval:.3g}" if pval >= 0.001 else "p<0.001"
        ax.set_title(METRIC_LABELS.get(col, col), fontsize=8, color=TEXT_COL)
        ax.text(0.98, 0.97, pstr, transform=ax.transAxes, ha="right", va="top",
                fontsize=7, color="#555555",
                bbox=dict(boxstyle="round,pad=0.2", facecolor=PANEL_BG, edgecolor=GRID_COL))
    else:
        ax.set_title(METRIC_LABELS.get(col, col), fontsize=8, color=TEXT_COL)


def plot_metric_groups(df: pd.DataFrame, output_dir: Path) -> None:
    h = df[df["structure"] == "horizontal"]
    v = df[df["structure"] == "vertical"]

    for group_name, cols in METRIC_GROUPS:
        # filter to columns that actually have non-NaN data
        valid_cols = [c for c in cols if c in df.columns and df[c].notna().any()]
        if not valid_cols:
            print(f"[warn] no data for group '{group_name}' — skipping")
            continue

        ncols = min(4, len(valid_cols))
        nrows = (len(valid_cols) + ncols - 1) // ncols
        fig, axes = plt.subplots(nrows, ncols, figsize=(4.5 * ncols, 4 * nrows))
        fig.patch.set_facecolor(DARK_BG)

        h_count = len(h)
        v_count = len(v)
        fig.suptitle(
            f"Structure Comparison — {group_name}\n"
            f"horizontal: {h_count} runs  |  vertical: {v_count} runs  "
            f"({'Mann-Whitney U p-value shown' if SCIPY_AVAILABLE else 'install scipy for p-values'})",
            fontsize=10, color=TEXT_COL, fontweight="bold",
        )

        axes_flat = np.array(axes).flatten() if nrows * ncols > 1 else [axes]

        for i, col in enumerate(valid_cols):
            ax = axes_flat[i]
            ax.set_facecolor(PANEL_BG)
            ax.grid(True, color=GRID_COL, linewidth=0.4, axis="y")
            h_vals = h[col].to_numpy(dtype=float, na_value=float("nan"))
            v_vals = v[col].to_numpy(dtype=float, na_value=float("nan"))
            _violin_or_box(ax, h_vals, v_vals, col)

        # hide unused axes
        for j in range(len(valid_cols), len(axes_flat)):
            axes_flat[j].set_visible(False)

        slug = group_name.lower().replace(" ", "_").replace("/", "_")
        out_path = output_dir / f"structure_metrics_{slug}.png"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            plt.tight_layout(rect=[0, 0, 1, 0.93])
        fig.savefig(out_path, bbox_inches="tight", facecolor=DARK_BG)
        plt.close(fig)
        print(f"[info] saved → {out_path}")


def plot_correlation_heatmap(df: pd.DataFrame, output_dir: Path) -> None:
    # numeric columns only, drop identifier columns
    drop_cols = {"run_dir", "structure", "task_slug"}
    num_cols = [c for c in df.columns if c not in drop_cols and pd.api.types.is_numeric_dtype(df[c])]

    # need at least some data
    sub = df[num_cols].dropna(axis=1, how="all")
    if sub.shape[1] < 2:
        print("[warn] insufficient numeric columns for heatmap — skipping")
        return

    # Spearman correlation
    corr = sub.corr(method="spearman")
    labels = [METRIC_LABELS.get(c, c) for c in corr.columns]

    n = len(labels)
    fig_size = max(10, n * 0.55)
    fig, ax = plt.subplots(figsize=(fig_size, fig_size * 0.85))
    fig.patch.set_facecolor(DARK_BG)
    ax.set_facecolor(PANEL_BG)

    try:
        import matplotlib.colors as mcolors
        cmap = plt.cm.RdYlBu_r
    except Exception:
        cmap = "coolwarm"

    im = ax.imshow(corr.values, cmap=cmap, vmin=-1, vmax=1, aspect="auto")
    plt.colorbar(im, ax=ax, shrink=0.6, label="Spearman ρ")

    ax.set_xticks(range(n))
    ax.set_yticks(range(n))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=7)
    ax.set_yticklabels(labels, fontsize=7)

    # annotate cells
    for i in range(n):
        for j in range(n):
            val = corr.values[i, j]
            ax.text(j, i, f"{val:.2f}", ha="center", va="center",
                    fontsize=5.5, color="black" if abs(val) < 0.7 else "white")

    ax.set_title("Spearman Correlation — all per-run metrics", fontsize=10,
                 color=TEXT_COL, fontweight="bold")

    out_path = output_dir / "correlation_heatmap.png"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        plt.tight_layout()
    fig.savefig(out_path, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)
    print(f"[info] saved → {out_path}")


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def print_summary(df: pd.DataFrame) -> None:
    all_metric_cols = [c for grp in METRIC_GROUPS for c in grp[1] if c in df.columns]
    print()
    print("=" * 90)
    print("  Per-metric summary: median (horizontal) vs median (vertical)")
    print("=" * 90)
    print(f"  {'Metric':<40} {'H median':>10} {'V median':>10} {'H n':>6} {'V n':>6}  p-val")
    print("-" * 90)
    h = df[df["structure"] == "horizontal"]
    v = df[df["structure"] == "vertical"]
    for col in all_metric_cols:
        if col not in df.columns:
            continue
        hv = h[col].dropna()
        vv = v[col].dropna()
        if hv.empty and vv.empty:
            continue
        h_med = f"{hv.median():.3g}" if not hv.empty else "—"
        v_med = f"{vv.median():.3g}" if not vv.empty else "—"
        if SCIPY_AVAILABLE and len(hv) > 1 and len(vv) > 1:
            _, pval = scipy_stats.mannwhitneyu(hv, vv, alternative="two-sided")
            pstr = f"{pval:.3g}" if pval >= 0.001 else "<0.001"
        else:
            pstr = "—"
        label = METRIC_LABELS.get(col, col)
        print(f"  {label:<40} {h_med:>10} {v_med:>10} {len(hv):>6} {len(vv):>6}  {pstr}")
    print("=" * 90)
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "data_dirs", nargs="*", metavar="DATA_DIR",
        help="Experiment root dir(s) (defaults to most recent data/runs/100_RUNS_*)",
    )
    parser.add_argument(
        "--output-dir", metavar="DIR",
        help="Where to write output PNGs + CSV (defaults to first DATA_DIR/plots/)",
    )
    args = parser.parse_args()

    data_dirs = _resolve_data_dirs(args.data_dirs)
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = data_dirs[0] / "plots"
    output_dir.mkdir(parents=True, exist_ok=True)

    df = load_all_runs(data_dirs)

    h_count = (df["structure"] == "horizontal").sum()
    v_count = (df["structure"] == "vertical").sum()
    print(f"[info] horizontal={h_count}  vertical={v_count}")

    if h_count == 0 or v_count == 0:
        print("[warn] only one structure present — comparison plots will be limited")

    # Save raw dataframe
    csv_path = output_dir / "per_run_metrics.csv"
    df.drop(columns=["run_dir"], errors="ignore").to_csv(csv_path, index=False)
    print(f"[info] saved raw data → {csv_path}")

    print_summary(df)
    plot_metric_groups(df, output_dir)
    plot_correlation_heatmap(df, output_dir)


if __name__ == "__main__":
    main()
