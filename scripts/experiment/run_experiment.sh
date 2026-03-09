#!/usr/bin/env bash
# =============================================================================
# run_experiment.sh
# =============================================================================
# Repeatable experiment runner for AgentVerse traffic characterisation.
#
# Runs "Math problem" and "Coding task" N times each, saving:
#   - AgentVerse JSON response
#   - Per-run and aggregate Prometheus metrics (CSV)
#   - Matplotlib plots mirroring the Grafana dashboard
#
# Usage:
#   ./run_experiment.sh -n <iterations> [options]
#
# Options:
#   -n <int>     Number of iterations per task (required)
#   -a <url>     Agent A base URL  (default: http://localhost:8101)
#   -p <url>     Prometheus URL    (default: http://localhost:9090)
#   -w <int>     Seconds to wait after each run for metrics to propagate
#                (default: 20)
#   -o <dir>     Override output directory (default: <repo>/data/runs/experiment_<ts>)
#   -h           Show this help
#
# Example:
#   ./run_experiment.sh -n 5
#   ./run_experiment.sh -n 10 -a http://192.168.1.100:8101 -w 30
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DASHBOARD_JSON="$REPO_ROOT/infra/monitoring/grafana/provisioning/dashboards/agentic-traffic.json"
SCRAPE_SCRIPT="$SCRIPT_DIR/scrape_metrics.py"
PLOT_SCRIPT="$SCRIPT_DIR/plot_results.py"

# Defaults
AGENT_A_URL="${AGENT_A_URL:-http://localhost:8101}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
ITERATIONS=""
WAIT_AFTER_RUN=20
OUTPUT_DIR_OVERRIDE=""

usage() {
    sed -n '3,30p' "$0" | sed 's/^# //' | sed 's/^#//'
    exit 0
}

while getopts "n:a:p:w:o:h" opt; do
    case $opt in
        n) ITERATIONS="$OPTARG" ;;
        a) AGENT_A_URL="$OPTARG" ;;
        p) PROMETHEUS_URL="$OPTARG" ;;
        w) WAIT_AFTER_RUN="$OPTARG" ;;
        o) OUTPUT_DIR_OVERRIDE="$OPTARG" ;;
        h) usage ;;
        *) echo "ERROR: Unknown option -$OPTARG"; usage ;;
    esac
done

if [[ -z "$ITERATIONS" ]]; then
    echo "ERROR: -n <iterations> is required"
    usage
fi

if ! [[ "$ITERATIONS" =~ ^[0-9]+$ ]] || [[ "$ITERATIONS" -lt 1 ]]; then
    echo "ERROR: -n must be a positive integer, got: $ITERATIONS"
    exit 1
fi

# -------------------------------------------------------------------------
# Tasks to run (name → slug pairs)
# -------------------------------------------------------------------------
TASK_NAMES=(
    "Solve the equation: 2x + 5 = 17, showing all steps. Then verify the answer."
    "Write a Python function to find the nth Fibonacci number using dynamic programming. Include docstring, type hints, and example usage."
)
TASK_SLUGS=(
    "math-problem"
    "coding-task"
)

# -------------------------------------------------------------------------
# Set up experiment output directory
# -------------------------------------------------------------------------
EXPERIMENT_TS=$(date +%Y-%m-%d_%H-%M-%S)

if [[ -n "$OUTPUT_DIR_OVERRIDE" ]]; then
    EXPERIMENT_DIR="$OUTPUT_DIR_OVERRIDE"
else
    EXPERIMENT_DIR="$REPO_ROOT/data/runs/experiment_${EXPERIMENT_TS}"
fi

mkdir -p "$EXPERIMENT_DIR"

RUN_LOG="$EXPERIMENT_DIR/runs.jsonl"
SUMMARY_LOG="$EXPERIMENT_DIR/summary.txt"

EXPERIMENT_START_S=$(date +%s)
EXPERIMENT_START_MS=$(python3 -c "import time; print(int(time.time() * 1000))")

echo "================================================================"        | tee -a "$SUMMARY_LOG"
echo "  Agentic Traffic Experiment"                                             | tee -a "$SUMMARY_LOG"
echo "  Timestamp  : $EXPERIMENT_TS"                                           | tee -a "$SUMMARY_LOG"
echo "  Iterations : $ITERATIONS per task (${#TASK_NAMES[@]} tasks total)"    | tee -a "$SUMMARY_LOG"
echo "  Agent A    : $AGENT_A_URL"                                             | tee -a "$SUMMARY_LOG"
echo "  Prometheus : $PROMETHEUS_URL"                                          | tee -a "$SUMMARY_LOG"
echo "  Output     : $EXPERIMENT_DIR"                                          | tee -a "$SUMMARY_LOG"
echo "================================================================"        | tee -a "$SUMMARY_LOG"

# -------------------------------------------------------------------------
# Helper: send one AgentVerse request via Python (avoids shell quoting issues)
# -------------------------------------------------------------------------
send_agentverse_request() {
    local task="$1"
    local url="$2"
    python3 - "$task" "$url" <<'PYEOF'
import json, sys, urllib.request, urllib.error

task_text = sys.argv[1]
base_url  = sys.argv[2].rstrip("/")

payload = json.dumps({"task": task_text, "stream": False, "max_iterations": 2}).encode()
req = urllib.request.Request(
    f"{base_url}/agentverse",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=600) as resp:
        body = resp.read().decode()
    print(body)
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(body, file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(json.dumps({"error": str(e)}), file=sys.stderr)
    sys.exit(1)
PYEOF
}

# -------------------------------------------------------------------------
# Helper: extract task_id from response JSON
# -------------------------------------------------------------------------
extract_task_id() {
    python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('task_id') or d.get('taskId') or 'unknown')
except Exception:
    print('unknown')
"
}

# -------------------------------------------------------------------------
# Main experiment loop
# -------------------------------------------------------------------------
TOTAL_RUNS=$(( ITERATIONS * ${#TASK_NAMES[@]} ))
RUN_COUNT=0
FAILED_RUNS=0

for ITER in $(seq 1 "$ITERATIONS"); do
    for TASK_IDX in "${!TASK_NAMES[@]}"; do
        TASK="${TASK_NAMES[$TASK_IDX]}"
        TASK_SLUG="${TASK_SLUGS[$TASK_IDX]}"
        RUN_COUNT=$(( RUN_COUNT + 1 ))

        RUN_TS=$(date +%Y-%m-%d_%H-%M-%S)
        RUN_START_S=$(date +%s)
        RUN_START_MS=$(python3 -c "import time; print(int(time.time() * 1000))")

        echo "" | tee -a "$SUMMARY_LOG"
        echo "--- Run $RUN_COUNT / $TOTAL_RUNS  |  iter=$ITER  task=$TASK_SLUG ---" | tee -a "$SUMMARY_LOG"
        echo "  Time  : $RUN_TS" | tee -a "$SUMMARY_LOG"
        echo "  Task  : ${TASK:0:80}..." | tee -a "$SUMMARY_LOG"

        # Send request
        RESPONSE=""
        if RESPONSE=$(send_agentverse_request "$TASK" "$AGENT_A_URL" 2>/tmp/agentverse_err.txt); then
            :
        else
            ERR=$(cat /tmp/agentverse_err.txt 2>/dev/null || true)
            echo "  ERROR: Request failed: $ERR" | tee -a "$SUMMARY_LOG"
            FAILED_RUNS=$(( FAILED_RUNS + 1 ))
            continue
        fi

        RUN_END_S=$(date +%s)
        RUN_END_MS=$(python3 -c "import time; print(int(time.time() * 1000))")
        DURATION_S=$(( RUN_END_S - RUN_START_S ))

        # Extract task_id
        TASK_ID=$(echo "$RESPONSE" | extract_task_id)

        echo "  Task ID  : $TASK_ID" | tee -a "$SUMMARY_LOG"
        echo "  Duration : ${DURATION_S}s"  | tee -a "$SUMMARY_LOG"

        # Create run directory: <timestamp>_<task-slug>_<task-id>
        RUN_DIR="$EXPERIMENT_DIR/${RUN_TS}_${TASK_SLUG}_${TASK_ID}"
        mkdir -p "$RUN_DIR"

        # Save metadata
        python3 - <<PYEOF
import json
meta = {
    "task":        "$TASK",
    "task_slug":   "$TASK_SLUG",
    "iteration":   $ITER,
    "task_id":     "$TASK_ID",
    "run_ts":      "$RUN_TS",
    "run_start_ms": $RUN_START_MS,
    "run_end_ms":   $RUN_END_MS,
    "duration_s":   $DURATION_S,
    "agent_a_url":  "$AGENT_A_URL",
    "prometheus_url": "$PROMETHEUS_URL",
}
with open("$RUN_DIR/meta.json", "w") as f:
    json.dump(meta, f, indent=2)
PYEOF

        # Save response JSON (pretty-printed if possible)
        if ! echo "$RESPONSE" | python3 -m json.tool > "$RUN_DIR/response.json" 2>/dev/null; then
            echo "$RESPONSE" > "$RUN_DIR/response.json"
        fi

        # Append to run log
        python3 - <<PYEOF
import json
record = {
    "run_dir":      "$RUN_DIR",
    "task_slug":    "$TASK_SLUG",
    "iteration":    $ITER,
    "task_id":      "$TASK_ID",
    "run_start_ms": $RUN_START_MS,
    "run_end_ms":   $RUN_END_MS,
    "duration_s":   $DURATION_S,
}
with open("$RUN_LOG", "a") as f:
    f.write(json.dumps(record) + "\n")
PYEOF

        echo "  Saved response → $RUN_DIR/response.json"

        # Wait for metrics to propagate into Prometheus
        echo "  Waiting ${WAIT_AFTER_RUN}s for metrics to propagate..."
        sleep "$WAIT_AFTER_RUN"

        # Scrape per-run Prometheus metrics
        echo "  Scraping per-run metrics..."
        if python3 "$SCRAPE_SCRIPT" \
            --dashboard-json "$DASHBOARD_JSON" \
            --output-dir     "$RUN_DIR" \
            --prometheus-url "$PROMETHEUS_URL" \
            --start-ms       "$RUN_START_MS" \
            --end-ms         "$RUN_END_MS" \
            --step           5 \
            --task-slug      "$TASK_SLUG" \
            --task-id        "$TASK_ID" \
            --iteration      "$ITER" 2>&1 | tee -a "$SUMMARY_LOG"; then
            echo "  Metrics saved → $RUN_DIR/metrics.csv"
        else
            echo "  WARNING: Per-run metrics scrape failed (Prometheus may not be running)"
        fi

    done
done

# -------------------------------------------------------------------------
# Aggregate scrape for full experiment window
# -------------------------------------------------------------------------
EXPERIMENT_END_MS=$(python3 -c "import time; print(int(time.time() * 1000))")
EXPERIMENT_END_S=$(date +%s)

echo "" | tee -a "$SUMMARY_LOG"
echo "================================================================" | tee -a "$SUMMARY_LOG"
echo "  All runs complete: $RUN_COUNT total, $FAILED_RUNS failed"      | tee -a "$SUMMARY_LOG"
echo "  Experiment duration: $(( EXPERIMENT_END_S - EXPERIMENT_START_S ))s" | tee -a "$SUMMARY_LOG"
echo "  Scraping full experiment window..."                             | tee -a "$SUMMARY_LOG"
echo "================================================================" | tee -a "$SUMMARY_LOG"

if python3 "$SCRAPE_SCRIPT" \
    --dashboard-json "$DASHBOARD_JSON" \
    --output-dir     "$EXPERIMENT_DIR" \
    --prometheus-url "$PROMETHEUS_URL" \
    --start-ms       "$EXPERIMENT_START_MS" \
    --end-ms         "$EXPERIMENT_END_MS" \
    --step           15 \
    --task-slug      "all" \
    --task-id        "aggregate" \
    --iteration      0 2>&1 | tee -a "$SUMMARY_LOG"; then
    echo "  Aggregate metrics saved → $EXPERIMENT_DIR/metrics.csv"
else
    echo "  WARNING: Aggregate metrics scrape failed"
fi

# -------------------------------------------------------------------------
# Generate plots
# -------------------------------------------------------------------------
echo ""
echo "Generating plots..."
if python3 "$PLOT_SCRIPT" \
    --experiment-dir  "$EXPERIMENT_DIR" \
    --dashboard-json  "$DASHBOARD_JSON" 2>&1 | tee -a "$SUMMARY_LOG"; then
    echo "  Plots saved → $EXPERIMENT_DIR/plots/"
else
    echo "  WARNING: Plotting failed (check dependencies: pip install matplotlib pandas)"
fi

echo "" | tee -a "$SUMMARY_LOG"
echo "================================================================" | tee -a "$SUMMARY_LOG"
echo "  DONE"                                                           | tee -a "$SUMMARY_LOG"
echo "  Results: $EXPERIMENT_DIR"                                       | tee -a "$SUMMARY_LOG"
echo "================================================================" | tee -a "$SUMMARY_LOG"
