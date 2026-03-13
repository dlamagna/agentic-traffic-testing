# Experiment Runner

Automated, repeatable experiment pipeline for collecting and analysing AgentVerse traffic data at scale.

## Overview

`scripts/experiment/run_experiment.sh` orchestrates a batch of AgentVerse runs, scrapes all Prometheus metrics that are defined in the Grafana dashboard, and produces matching matplotlib plots.  Changing a panel in the dashboard JSON automatically updates what the scraper collects on the next run.

---

## Quick start

```bash
# 5 iterations of each task (math + coding = 10 total AgentVerse calls)
./scripts/experiment/run_experiment.sh -n 5

# Custom Agent A / Prometheus endpoints
./scripts/experiment/run_experiment.sh -n 10 \
  -a http://192.168.1.100:8101 \
  -p http://192.168.1.100:9090

# Longer wait for metrics propagation (useful with slow Prometheus scrape intervals)
./scripts/experiment/run_experiment.sh -n 5 -w 30
```

> **Prerequisites**
> - Agent A must be running (`http://localhost:8101` by default)
> - Prometheus must be running (`http://localhost:9090` by default) — start via `ENABLE_MONITORING=1` in `infra/.env`
> - Python packages: `pip install matplotlib pandas numpy scipy`

---

## Script reference

### `run_experiment.sh`

```
Usage: run_experiment.sh -n <iterations> [options]

  -n <int>   Iterations per task (required)
  -a <url>   Agent A base URL     (default: http://localhost:8101,
                                   or $AGENT_A_URL env var)
  -p <url>   Prometheus base URL  (default: http://localhost:9090,
                                   or $PROMETHEUS_URL env var)
  -w <int>   Seconds to wait after each run before scraping metrics
             (default: 20 — allows Prometheus to ingest the data)
  -o <dir>   Override output directory
  -h         Show help
```

Tasks run per iteration — read directly from `agents/templates/agentverse_workflow.json` (`example_tasks[]`), so adding a task to the template is the only change needed:

| Slug | Name |
|------|------|
| `mathematical-problem` | Mathematical Problem |
| `research-task`        | Research Task        |
| `software-development` | Software Development |
| `consulting`           | Consulting           |

### `scrape_metrics.py`

Reads every `targets[].expr` (PromQL) from the Grafana dashboard JSON and queries the Prometheus range API.  Called automatically by `run_experiment.sh`; can also be run standalone.

```bash
python3 scripts/experiment/scrape_metrics.py \
  --dashboard-json infra/monitoring/grafana/provisioning/dashboards/agentic-traffic.json \
  --output-dir     data/runs/my_run \
  --prometheus-url http://localhost:9090 \
  --start-ms       1700000000000 \
  --end-ms         1700003600000 \
  --step           5 \
  --task-slug      math-problem \
  --task-id        abc123 \
  --iteration      1
```

Output: `<output-dir>/metrics.csv`

### `plot_results.py`

Reads the aggregate and per-run `metrics.csv` files and generates Grafana-style dark-theme plots.  Called automatically by `run_experiment.sh`; can also be run standalone after data collection.

```bash
python3 scripts/experiment/plot_results.py \
  --experiment-dir data/runs/experiment_2026-03-09_14-30-00 \
  --dashboard-json infra/monitoring/grafana/provisioning/dashboards/agentic-traffic.json
```

---

## Output directory layout

Each experiment produces a self-contained directory under `data/runs/`:

```
data/runs/experiment_<YYYY-MM-DD_HH-MM-SS>/
│
├── runs.jsonl                    # One JSON record per completed run
├── summary.txt                   # Full console output log
├── metrics.csv                   # Aggregate metrics, full experiment window
│
├── plots/
│   ├── 01_Overview.png
│   ├── 02_Network_Traffic.png
│   ├── 03_Resource_Usage.png
│   ├── 04_Service-level_Network_(TCP).png
│   ├── 05_AI_Performance_(LLM).png
│   ├── 06_LLM_Configuration.png
│   ├── 07_Interarrival_Interpretation.png
│   ├── interarrival_distribution.png   # histogram + KDE per task type
│   ├── interarrival_ecdf.png           # empirical CDF with p50/p95 markers
│   ├── per_run_summary.png             # duration bar chart per iteration
│   └── statistics.txt                  # mean / p50 / p95 / max table
│
├── <RUN_TS>_math-problem_<task_id>/
│   ├── meta.json                 # task text, timing, task_id, URLs
│   ├── response.json             # full AgentVerse JSON response
│   └── metrics.csv               # metrics for this run's time window only
│
├── <RUN_TS>_coding-task_<task_id>/
│   ├── meta.json
│   ├── response.json
│   └── metrics.csv
│
└── ...
```

### Run directory naming

Each per-run subdirectory is named:

```
<YYYY-MM-DD_HH-MM-SS>_<task-slug>_<task_id>
```

- `YYYY-MM-DD_HH-MM-SS` — wall-clock timestamp when the request was sent
- `task-slug` — `math-problem` or `coding-task`
- `task_id` — the `task_id` field returned by Agent A in the AgentVerse response, correlatable with Jaeger traces and Prometheus labels

---

## Metrics CSV schema

Both the per-run and aggregate `metrics.csv` files share the same column set:

| Column | Description |
|--------|-------------|
| `panel_id` | Grafana panel numeric ID |
| `panel_title` | Panel title as shown in the dashboard |
| `panel_type` | `timeseries` or `stat` |
| `row_section` | Dashboard row heading the panel belongs to |
| `unit` | Grafana unit string (e.g. `s`, `Bps`, `short`) |
| `ref_id` | Target ref ID within the panel (`A`, `B`, …) |
| `legend_format` | Legend template string from the panel definition |
| `expr` | PromQL expression that produced this data |
| `labels` | JSON-encoded Prometheus label set for this series |
| `timestamp` | Unix timestamp (float seconds) |
| `datetime` | ISO-8601 UTC datetime string |
| `value` | Metric value at this timestamp |
| `task_slug` | `math-problem`, `coding-task`, or `all` (aggregate) |
| `task_id` | Agent A task ID for this run |
| `iteration` | Iteration number (1-based); 0 for the aggregate scrape |

---

## How dashboard JSON drives scraping

`scrape_metrics.py` calls `load_dashboard_panels()` which:

1. Loads `agentic-traffic.json`
2. Sorts all panels by `gridPos.y` (then `x`) so that row-header panels are always encountered before the panels they contain
3. Assigns each data panel to the last-seen row header title
4. Extracts every `targets[].expr` field

This means **editing a panel in Grafana and exporting the JSON** is the only change needed to update what the experiment scraper collects.  No changes to any Python or bash files are required.

---

## Plots produced

### Dashboard section plots

One PNG per Grafana row section, with subplots matching the panel layout:

| File | Contents |
|------|----------|
| `01_Overview.png` | Active containers, Docker TX/RX rate, LLM request rate |
| `02_Network_Traffic.png` | Per-interface TX/RX bytes and packets |
| `03_Resource_Usage.png` | CPU core-equivalents and memory per container |
| `04_Service-level_Network_(TCP).png` | TCP bytes by service pair, RTT and flow duration |
| `05_AI_Performance_(LLM).png` | E2E latency, TTFT, token rates, in-flight requests |
| `06_LLM_Configuration.png` | KV-cache concurrency, max tokens, GPU utilisation |
| `07_Interarrival_Interpretation.png` | Interarrival time, arrivals histogram, burst signature |

### Interarrival-focused plots

**`interarrival_distribution.png`**
- Left: interarrival time as a time-series for each task type
- Right: histogram with KDE overlay (requires `scipy`) per task type

**`interarrival_ecdf.png`**
- Empirical CDF of interarrival times per task type
- p50 (dashed) and p95 (dotted) vertical markers

**`per_run_summary.png`**
- Bar chart of AgentVerse round-trip duration per iteration, one panel per task type
- Mean duration shown as a dashed horizontal line

**`statistics.txt`**
- Plain-text table of n / mean / p50 / p95 / max for:
  - LLM Interarrival Time
  - LLM End-to-end Latency
  - Time-to-First-Token
  - In-flight Requests
  - LLM Request Rate

---

## Correlating with Jaeger traces

Every per-run directory name embeds the `task_id` returned by Agent A.  The same ID is emitted as an OpenTelemetry span attribute (`app.task_id`) and appears in Jaeger under the `agent-a` service.

To look up a specific run in Jaeger:

1. Open `http://localhost:16686`
2. Service: `agent-a`, Operation: `agent_a.agentverse_workflow`
3. Tags: `app.task_id=<task_id from directory name>`

---

## Extending the experiment

### Adding tasks

Add an entry to `example_tasks[]` in `agents/templates/agentverse_workflow.json`:

```json
{
  "name": "Theory Question",
  "task": "Explain the CAP theorem in one paragraph.",
  "recommended_structure": "horizontal",
  "recommended_experts": ["researcher", "summarizer"]
}
```

The slug is derived automatically from `name` (lowercased, non-alphanumeric chars replaced with `-`).  No changes to any script are needed.

> **Note on `recommended_structure` / `recommended_experts`:** These fields are **not sent to the agent**. The `/agentverse` endpoint only accepts `task`, `max_iterations`, `success_threshold`, and `stream`. The orchestrator calls the LLM independently during the recruitment stage to decide structure and expert composition from the task text alone, so these fields introduce no bias — they exist only as human-readable documentation inside the template.

### Adding metrics

Add a new panel to `infra/monitoring/grafana/provisioning/dashboards/agentic-traffic.json` (either by editing the file directly or by editing via the Grafana UI and re-exporting).  The scraper picks it up automatically on the next run.

### Changing the scrape window buffer

By default `scrape_metrics.py` adds ±60 seconds around the reported run window to avoid clipping at Prometheus scrape boundaries.  Adjust the constant at the top of the `scrape_to_csv` function if needed.
