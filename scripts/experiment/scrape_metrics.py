#!/usr/bin/env python3
"""
scrape_metrics.py
=================
Scrape Prometheus metrics for an experiment run.

Reads PromQL expressions from the Grafana dashboard JSON so that any
edits to the dashboard are automatically reflected in the next scrape.

Output: <output-dir>/metrics.csv with columns:
  panel_id, panel_title, panel_type, row_section, unit,
  ref_id, legend_format, expr, labels,
  timestamp, datetime, value,
  task_slug, task_id, iteration
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Dashboard parsing
# ---------------------------------------------------------------------------

def load_dashboard_panels(dashboard_json_path: str | Path) -> list[dict]:
    """Return a list of panel dicts with their PromQL targets.

    Panels are sorted by gridPos.y so that row headers are assigned
    correctly even when the JSON is not in display order.
    """
    with open(dashboard_json_path) as f:
        dashboard = json.load(f)

    raw = dashboard.get("panels", [])
    # Sort all panels by their y position so rows are encountered before
    # the panels they contain.
    raw_sorted = sorted(raw, key=lambda p: (p.get("gridPos", {}).get("y", 0),
                                            p.get("gridPos", {}).get("x", 0)))

    panels: list[dict] = []
    current_row = "General"

    for panel in raw_sorted:
        ptype = panel.get("type", "")

        if ptype == "row":
            current_row = panel.get("title", "General")
            continue

        targets = panel.get("targets", [])
        if not targets:
            continue

        field_cfg = panel.get("fieldConfig", {}).get("defaults", {})
        panel_info: dict = {
            "id":      panel.get("id"),
            "title":   panel.get("title", f"Panel {panel.get('id')}"),
            "type":    ptype,
            "row":     current_row,
            "gridPos": panel.get("gridPos", {}),
            "unit":    field_cfg.get("unit", "short"),
            "targets": [],
        }

        for target in targets:
            expr = target.get("expr", "").strip()
            if not expr:
                continue
            panel_info["targets"].append({
                "refId":        target.get("refId", "A"),
                "expr":         expr,
                "legendFormat": target.get("legendFormat", ""),
            })

        if panel_info["targets"]:
            panels.append(panel_info)

    return panels


# ---------------------------------------------------------------------------
# Prometheus queries
# ---------------------------------------------------------------------------

def _prom_get(url: str, timeout: int = 30) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        raise RuntimeError(f"HTTP {exc.code}: {body[:200]}") from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def query_prometheus_range(
    prom_url: str,
    expr: str,
    start_s: float,
    end_s: float,
    step_s: int,
) -> list[dict]:
    """Query Prometheus range API.

    Returns a list of series dicts:
        {"labels": {...}, "values": [(timestamp_float, value_float), ...]}
    """
    params = urllib.parse.urlencode({
        "query": expr,
        "start": f"{start_s:.3f}",
        "end":   f"{end_s:.3f}",
        "step":  str(step_s),
    })
    url = f"{prom_url.rstrip('/')}/api/v1/query_range?{params}"

    try:
        data = _prom_get(url)
    except RuntimeError as exc:
        print(f"  WARN  prometheus query failed [{expr[:60]}...]: {exc}",
              file=sys.stderr)
        return []

    if data.get("status") != "success":
        print(f"  WARN  prometheus non-success status for [{expr[:60]}...]: "
              f"{data.get('status')} / {data.get('error', '')}",
              file=sys.stderr)
        return []

    results: list[dict] = []
    for series in data.get("data", {}).get("result", []):
        labels = series.get("metric", {})
        raw_values = series.get("values", [])
        values = [
            (float(ts), float(val))
            for ts, val in raw_values
            if val not in ("NaN", "+Inf", "-Inf")
        ]
        if values:
            results.append({"labels": labels, "values": values})
    return results


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

FIELDNAMES = [
    "panel_id", "panel_title", "panel_type", "row_section", "unit",
    "ref_id", "legend_format", "expr", "labels",
    "timestamp", "datetime", "value",
    "task_slug", "task_id", "iteration",
]


def scrape_to_csv(
    panels: list[dict],
    prom_url: str,
    start_ms: int,
    end_ms: int,
    step_s: int,
    output_path: Path,
    meta: dict,
) -> int:
    """Scrape all panels and write results to *output_path*.

    Returns the total number of data rows written.
    """
    # Add a 1-minute buffer on each side so that Prometheus scrape intervals
    # (typically 15 s) don't clip the boundary.
    start_s = (start_ms - 60_000) / 1000.0
    end_s   = (end_ms   + 60_000) / 1000.0

    rows: list[dict] = []

    for panel in panels:
        print(f"  scraping  [{panel['row']}] {panel['title']}", file=sys.stderr)

        for target in panel["targets"]:
            series_list = query_prometheus_range(
                prom_url, target["expr"], start_s, end_s, step_s
            )

            for series in series_list:
                labels_json = json.dumps(series["labels"], sort_keys=True)
                for ts, val in series["values"]:
                    rows.append({
                        "panel_id":     panel["id"],
                        "panel_title":  panel["title"],
                        "panel_type":   panel["type"],
                        "row_section":  panel["row"],
                        "unit":         panel["unit"],
                        "ref_id":       target["refId"],
                        "legend_format": target["legendFormat"],
                        "expr":         target["expr"],
                        "labels":       labels_json,
                        "timestamp":    ts,
                        "datetime":     datetime.fromtimestamp(ts, tz=timezone.utc)
                                               .strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                        "value":        val,
                        **meta,
                    })

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    n = len(rows)
    print(f"  written   {n} rows → {output_path}", file=sys.stderr)
    return n


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scrape Prometheus metrics using queries from a Grafana dashboard JSON."
    )
    p.add_argument("--dashboard-json",  required=True,
                   help="Path to Grafana dashboard JSON file")
    p.add_argument("--output-dir",      required=True,
                   help="Directory where metrics.csv will be written")
    p.add_argument("--prometheus-url",  default="http://localhost:9090",
                   help="Prometheus base URL (default: http://localhost:9090)")
    p.add_argument("--start-ms",        required=True, type=int,
                   help="Run start time, Unix milliseconds")
    p.add_argument("--end-ms",          required=True, type=int,
                   help="Run end time, Unix milliseconds")
    p.add_argument("--step",            default=5, type=int,
                   help="Query step in seconds (default: 5)")
    p.add_argument("--task-slug",       default="unknown")
    p.add_argument("--task-id",         default="unknown")
    p.add_argument("--iteration",       default=0, type=int)
    return p


def main() -> None:
    args = build_parser().parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    panels = load_dashboard_panels(args.dashboard_json)
    print(f"  loaded    {len(panels)} panels from {args.dashboard_json}",
          file=sys.stderr)

    meta = {
        "task_slug": args.task_slug,
        "task_id":   args.task_id,
        "iteration": args.iteration,
    }

    n = scrape_to_csv(
        panels      = panels,
        prom_url    = args.prometheus_url,
        start_ms    = args.start_ms,
        end_ms      = args.end_ms,
        step_s      = args.step,
        output_path = output_dir / "metrics.csv",
        meta        = meta,
    )

    if n == 0:
        print("  WARN  no data rows scraped – is Prometheus reachable and "
              "are services running?", file=sys.stderr)
        sys.exit(0)   # non-fatal; the experiment can still save the response JSON


if __name__ == "__main__":
    main()
