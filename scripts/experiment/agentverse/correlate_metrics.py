#!/usr/bin/env python3
"""
correlate_metrics.py
====================
Joins per-call LLM metrics (logs/llm_calls.jsonl) with Prometheus TCP
telemetry to produce a merged per-task CSV dataset.

For each task_id in the call log the script:
  1. Derives the task time window from the earliest/latest call timestamps.
  2. Optionally enriches task metadata from persisted agentverse JSON files.
  3. Queries Prometheus for TCP bytes, packets, SYN count, flow-duration
     quantiles, and RTT quantiles over that window by service pair.
  4. Writes one row per task to the output CSV.

Usage:
    python scripts/experiment/correlate_metrics.py [options]

    --call-log PATH       Path to llm_calls.jsonl (default: logs/llm_calls.jsonl)
    --agentverse-dir DIR  Optional: logs/agentverse/ for richer task metadata
    --prometheus URL      Prometheus base URL (default: http://localhost:9090)
    --output PATH         Output CSV path (default: data/correlated.csv)
    --min-window-s FLOAT  Minimum lookback window for Prometheus queries (default: 15)

Correlation methodology:
    Application logs provide per-call timestamps (timestamp_start, timestamp_end)
    written by MetricsLogger (agents/common/metrics_logger.py). The task window
    is defined as [min(timestamp_start), max(timestamp_end)] across all calls
    sharing the same task_id.

    TCP telemetry is queried via the Prometheus HTTP API using increase() with a
    lookback window equal to the task duration. This captures bytes, packets, and
    connection events on the inter-agent network bridge that occurred during the
    task. All tasks propagate X-Task-ID headers so application logs can be
    filtered per task_id; the Prometheus data is time-windowed, not per-task-id.

Limitations:
    - Prometheus scrape interval (default 15s) limits resolution for short tasks.
      Tasks shorter than ~15s may have null TCP fields; a warning is printed.
    - TCP metrics are counter-based; increase() may return NaN or 0 for tasks
      that generate no traffic on the monitored bridge.
    - Service pair labels (src_service, dst_service) require tcp_metrics_collector
      and docker_mapping_exporter to have been running during the task.
    - The script does NOT filter TCP metrics by task_id (Prometheus has no
      application-level awareness). Concurrent tasks sharing a time window will
      have overlapping TCP metric windows. Run experiments serially for cleanest
      per-task attribution.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ── Prometheus helpers ─────────────────────────────────────────────────────────

def _prom_get(url: str, timeout: int = 20) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode()[:200]}") from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def _prom_instant(prom_url: str, query: str, time_s: float) -> List[Dict[str, Any]]:
    """Run a Prometheus instant query. Returns list of {metric, value} dicts."""
    params = urllib.parse.urlencode({"query": query, "time": f"{time_s:.3f}"})
    url = f"{prom_url.rstrip('/')}/api/v1/query?{params}"
    try:
        data = _prom_get(url)
    except RuntimeError as exc:
        print(f"  WARN  prometheus query failed [{query[:80]}]: {exc}", file=sys.stderr)
        return []
    if data.get("status") != "success":
        print(f"  WARN  prometheus non-success [{query[:80]}]: {data.get('error', '')}", file=sys.stderr)
        return []
    results = []
    for item in data.get("data", {}).get("result", []):
        ts_val = item.get("value")
        if ts_val:
            _, raw_val = ts_val
            if raw_val not in ("NaN", "+Inf", "-Inf"):
                results.append({"metric": item.get("metric", {}), "value": float(raw_val)})
    return results


def _prom_scalar(prom_url: str, query: str, time_s: float) -> Optional[float]:
    """Return sum of all series values from a Prometheus instant query."""
    results = _prom_instant(prom_url, query, time_s)
    if not results:
        return None
    total = sum(r["value"] for r in results)
    return round(total, 6)


def _prom_quantile(prom_url: str, query: str, time_s: float) -> Optional[float]:
    """Return a single quantile value from a histogram_quantile instant query."""
    results = _prom_instant(prom_url, query, time_s)
    if not results:
        return None
    return round(results[0]["value"], 6)


# ── TCP metric queries ─────────────────────────────────────────────────────────

def query_tcp_metrics(prom_url: str, end_s: float, window_s: float) -> Dict[str, Any]:
    """
    Query TCP telemetry over [end_s - window_s, end_s] using increase().

    All queries are Prometheus instant queries at end_s with a lookback window
    equal to the task duration. Returns a flat dict of metric_name -> value.

    Service pair selectors match the labels produced by tcp_metrics_collector.py
    and annotated by docker_mapping_exporter.py.
    """
    # Prometheus requires a minimum range of at least one scrape interval.
    # Use max(window_s, 15) to avoid empty ranges; warn if window is short.
    ws = f"{max(window_s, 15.0):.0f}s"

    out: Dict[str, Any] = {}

    # Bytes and packets: agents → LLM backend (all agent variants summed)
    sel_to_llm   = '{src_service=~"agent_a|agent_b.*",dst_service="llm_backend"}'
    sel_from_llm = '{src_service="llm_backend",dst_service=~"agent_a|agent_b.*"}'
    sel_a_to_b   = '{src_service="agent_a",dst_service=~"agent_b.*"}'

    out["tcp_bytes_to_llm"] = _prom_scalar(
        prom_url, f"sum(increase(tcp_bytes_total{sel_to_llm}[{ws}]))", end_s
    )
    out["tcp_bytes_from_llm"] = _prom_scalar(
        prom_url, f"sum(increase(tcp_bytes_total{sel_from_llm}[{ws}]))", end_s
    )
    out["tcp_packets_to_llm"] = _prom_scalar(
        prom_url, f"sum(increase(tcp_packets_total{sel_to_llm}[{ws}]))", end_s
    )

    # Agent-to-agent bytes and packets (fan-out traffic)
    out["tcp_bytes_a_to_b"] = _prom_scalar(
        prom_url, f"sum(increase(tcp_bytes_total{sel_a_to_b}[{ws}]))", end_s
    )
    out["tcp_packets_a_to_b"] = _prom_scalar(
        prom_url, f"sum(increase(tcp_packets_total{sel_a_to_b}[{ws}]))", end_s
    )

    # New TCP connections (SYN count ≈ flow count) over the window
    out["tcp_syn_count"] = _prom_scalar(
        prom_url, f"sum(increase(tcp_syn_total[{ws}]))", end_s
    )

    # Flow duration quantiles: agent_a → llm_backend
    dur_sel = f'src_service="agent_a",dst_service="llm_backend"'
    dur_inc = f"increase(tcp_flow_duration_seconds_bucket{{{dur_sel}}}[{ws}])"
    out["tcp_flow_duration_p50_s"] = _prom_quantile(
        prom_url, f"histogram_quantile(0.5, sum by (le) ({dur_inc}))", end_s
    )
    out["tcp_flow_duration_p95_s"] = _prom_quantile(
        prom_url, f"histogram_quantile(0.95, sum by (le) ({dur_inc}))", end_s
    )

    # SYN RTT quantiles: agent_a → llm_backend
    rtt_sel = f'src_service="agent_a",dst_service="llm_backend"'
    rtt_inc = f"increase(tcp_rtt_handshake_seconds_bucket{{{rtt_sel}}}[{ws}])"
    out["tcp_rtt_p50_s"] = _prom_quantile(
        prom_url, f"histogram_quantile(0.5, sum by (le) ({rtt_inc}))", end_s
    )
    out["tcp_rtt_p95_s"] = _prom_quantile(
        prom_url, f"histogram_quantile(0.95, sum by (le) ({rtt_inc}))", end_s
    )

    return out


# ── Call log loading ───────────────────────────────────────────────────────────

def load_call_log(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Load llm_calls.jsonl and group records by task_id."""
    records: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    if not path.exists():
        print(f"WARN call log not found: {path}", file=sys.stderr)
        return records
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"WARN skipping malformed line {i}: {exc}", file=sys.stderr)
                continue
            task_id = rec.get("task_id")
            if task_id:
                records[task_id].append(rec)
    return records


def task_window(calls: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    """Return (start_unix_s, end_unix_s) for a task from its call records."""
    starts, ends = [], []
    for c in calls:
        for val in [c.get("timestamp_start")]:
            if val:
                try:
                    starts.append(datetime.fromisoformat(val).timestamp())
                except ValueError:
                    pass
        for val in [c.get("timestamp_end")]:
            if val:
                try:
                    ends.append(datetime.fromisoformat(val).timestamp())
                except ValueError:
                    pass
    if not starts or not ends:
        return None, None
    return min(starts), max(ends)


def load_agentverse_meta(av_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Load persisted agentverse JSON files (logs/agentverse/*.json) by task_id."""
    meta: Dict[str, Dict[str, Any]] = {}
    if not av_dir.is_dir():
        return meta
    for json_path in av_dir.glob("*.json"):
        try:
            with open(json_path, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        task_id = rec.get("task_id")
        if task_id:
            meta[task_id] = rec
    return meta


# ── Row building ───────────────────────────────────────────────────────────────

def _env_float(key: str) -> float:
    try:
        return float(os.environ.get(key, "0.0"))
    except ValueError:
        return 0.0


def build_app_row(
    task_id: str,
    calls: List[Dict[str, Any]],
    av_meta: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build application-level fields for one task row from per-call records."""
    prompt_tokens      = sum((c.get("prompt_tokens") or 0) for c in calls)
    completion_tokens  = sum((c.get("completion_tokens") or 0) for c in calls)
    total_tokens       = sum((c.get("total_tokens") or 0) for c in calls)
    total_llm_latency  = sum((c.get("latency_ms") or 0) for c in calls)

    input_rate  = _env_float("COST_PER_INPUT_TOKEN_USD")
    output_rate = _env_float("COST_PER_OUTPUT_TOKEN_USD")
    cost = (
        round(prompt_tokens * input_rate + completion_tokens * output_rate, 8)
        if (input_rate or output_rate) else None
    )

    # Try to get scenario from agentverse result or call type
    scenario = None
    if av_meta:
        result = av_meta.get("result") or {}
        scenario = result.get("scenario") or av_meta.get("scenario")

    return {
        "task_id":               task_id,
        "scenario":              scenario,
        "total_llm_calls":       len(calls),
        "agent_a_calls":         sum(1 for c in calls if c.get("agent_id") == "AgentA"),
        "agent_b_calls":         sum(1 for c in calls if c.get("agent_id") == "AgentB"),
        "total_prompt_tokens":   prompt_tokens,
        "total_completion_tokens": completion_tokens,
        "total_tokens":          total_tokens,
        "total_llm_latency_ms":  total_llm_latency,
        "cost_estimate_usd":     cost,
        "model_name":            calls[0].get("model_name") if calls else None,
    }


# ── CSV schema ─────────────────────────────────────────────────────────────────

FIELDNAMES = [
    # Task identity / timing
    "task_id", "scenario", "task_start", "task_end", "window_s",
    # Application-level aggregates (from llm_calls.jsonl)
    "total_llm_calls", "agent_a_calls", "agent_b_calls",
    "total_prompt_tokens", "total_completion_tokens", "total_tokens",
    "total_llm_latency_ms", "cost_estimate_usd", "model_name",
    # TCP telemetry (from Prometheus / tcp_metrics_collector.py)
    "tcp_bytes_to_llm", "tcp_bytes_from_llm", "tcp_packets_to_llm",
    "tcp_bytes_a_to_b", "tcp_packets_a_to_b",
    "tcp_syn_count",
    "tcp_flow_duration_p50_s", "tcp_flow_duration_p95_s",
    "tcp_rtt_p50_s", "tcp_rtt_p95_s",
]


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Correlate LLM call logs with Prometheus TCP telemetry.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--call-log", default="logs/llm_calls.jsonl",
        help="Path to llm_calls.jsonl written by MetricsLogger (default: logs/llm_calls.jsonl)",
    )
    p.add_argument(
        "--agentverse-dir", default="logs/agentverse",
        help="Directory of persisted agentverse JSON files for richer metadata (default: logs/agentverse)",
    )
    p.add_argument(
        "--prometheus", default="http://localhost:9090",
        help="Prometheus base URL (default: http://localhost:9090)",
    )
    p.add_argument(
        "--output", default="data/correlated.csv",
        help="Output CSV path (default: data/correlated.csv)",
    )
    p.add_argument(
        "--min-window-s", type=float, default=15.0,
        help="Minimum Prometheus lookback window in seconds (default: 15, matching scrape interval)",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()

    call_log_path = Path(args.call_log)
    av_dir        = Path(args.agentverse_dir)
    output_path   = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Loading call log:          {call_log_path}", file=sys.stderr)
    calls_by_task = load_call_log(call_log_path)
    if not calls_by_task:
        print("ERROR no task records found in call log.", file=sys.stderr)
        sys.exit(1)
    print(f"Tasks found:               {len(calls_by_task)}", file=sys.stderr)

    print(f"Loading agentverse meta:   {av_dir}", file=sys.stderr)
    av_meta_by_task = load_agentverse_meta(av_dir)
    print(f"Agentverse files loaded:   {len(av_meta_by_task)}", file=sys.stderr)

    rows: List[Dict[str, Any]] = []

    for task_id, calls in sorted(calls_by_task.items()):
        start_s, end_s = task_window(calls)
        if start_s is None or end_s is None:
            print(f"  WARN  skipping {task_id[:12]}: no timestamps in call records", file=sys.stderr)
            continue

        window_s = max(end_s - start_s, args.min_window_s)
        start_iso = datetime.fromtimestamp(start_s, tz=timezone.utc).isoformat()
        end_iso   = datetime.fromtimestamp(end_s,   tz=timezone.utc).isoformat()

        print(
            f"  task {task_id[:12]}...  window=[{start_iso}, {end_iso}]  ({window_s:.1f}s)",
            file=sys.stderr,
        )
        if window_s < 15:
            print(
                f"    WARN  window {window_s:.1f}s < 15s scrape interval; "
                "TCP metrics may be null or imprecise",
                file=sys.stderr,
            )

        app_row = build_app_row(task_id, calls, av_meta_by_task.get(task_id))
        app_row["task_start"] = start_iso
        app_row["task_end"]   = end_iso
        app_row["window_s"]   = round(window_s, 3)

        tcp_row = query_tcp_metrics(args.prometheus, end_s, window_s)
        rows.append({**app_row, **tcp_row})

    if not rows:
        print("ERROR no rows to write.", file=sys.stderr)
        sys.exit(1)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWritten {len(rows)} rows → {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
