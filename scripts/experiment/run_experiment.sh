#!/usr/bin/env bash
# =============================================================================
# run_experiment.sh
# =============================================================================
# Repeatable experiment runner for AgentVerse traffic characterisation.
#
# Runs every task defined in agents/templates/agentverse_workflow.json N times, saving:
#   - AgentVerse JSON response
#   - Per-run and aggregate Prometheus metrics (CSV)
#   - Matplotlib plots mirroring the Grafana dashboard
#
# Usage:
#   ./run_experiment.sh -n <iterations> [options]
#   ./run_experiment.sh -c -o <existing-experiment-dir> [options]
#
# Options:
#   -n <int>     Number of iterations per task (required for fresh runs)
#   -c           Continue/resume an interrupted experiment (requires -o)
#   -o <dir>     Output directory — new dir for fresh runs, existing dir for -c
#                (default for fresh runs: <repo>/data/runs/experiment_<ts>)
#   -a <url>     Agent A base URL  (default: http://localhost:8101)
#   -p <url>     Prometheus URL    (default: http://localhost:9090)
#   -w <int>     Seconds to wait after each run for metrics to propagate
#                (default: 20)
#   -h           Show this help
#
# Examples:
#   ./run_experiment.sh -n 50
#   ./run_experiment.sh -n 10 -a http://192.168.1.100:8101 -w 30
#   ./run_experiment.sh -c -o data/runs/experiment_2026-03-10_17-42-47
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DASHBOARD_JSON="$REPO_ROOT/infra/monitoring/grafana/provisioning/dashboards/agentic-traffic.json"
SCRAPE_SCRIPT="$SCRIPT_DIR/scrape_metrics.py"
PLOT_SCRIPT="$SCRIPT_DIR/plot_results.py"
WORKFLOW_TEMPLATE="$REPO_ROOT/agents/templates/agentverse_workflow.json"

# Defaults
AGENT_A_URL="${AGENT_A_URL:-http://localhost:8101}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
ITERATIONS=""
WAIT_AFTER_RUN=20
OUTPUT_DIR_OVERRIDE=""
CONTINUE_MODE=0

# -------------------------------------------------------------------------
# Load tasks from template
# -------------------------------------------------------------------------
load_tasks_from_template() {
python3 - "$WORKFLOW_TEMPLATE" <<'PYEOF'
import json, sys, re

path = sys.argv[1]

with open(path) as f:
    data = json.load(f)

# Load every example_task — no filtering so new tasks added to the
# template are automatically picked up on the next experiment run.
for t in data.get("example_tasks", []):
    name = t.get("name", "")
    task = t.get("task", "").strip()
    if not task:
        continue
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    print(f"{slug}|||{task}")
PYEOF
}

TASK_NAMES=()
TASK_SLUGS=()

while IFS="|||" read -r slug task; do
    TASK_SLUGS+=("$slug")
    TASK_NAMES+=("$task")
done < <(load_tasks_from_template)

NUM_TASKS="${#TASK_NAMES[@]}"

echo ""
echo "Loaded tasks from template:"
for i in "${!TASK_NAMES[@]}"; do
    echo "  ${TASK_SLUGS[$i]} -> ${TASK_NAMES[$i]:0:80}..."
done
echo ""

# -------------------------------------------------------------------------
# Parse options
# -------------------------------------------------------------------------
usage() {
    sed -n '3,32p' "$0" | sed 's/^# //' | sed 's/^#//'
    exit 0
}

while getopts "n:a:p:w:o:ch" opt; do
    case $opt in
        n) ITERATIONS="$OPTARG" ;;
        c) CONTINUE_MODE=1 ;;
        a) AGENT_A_URL="$OPTARG" ;;
        p) PROMETHEUS_URL="$OPTARG" ;;
        w) WAIT_AFTER_RUN="$OPTARG" ;;
        o) OUTPUT_DIR_OVERRIDE="$OPTARG" ;;
        h) usage ;;
        *) echo "ERROR: Unknown option -$OPTARG"; usage ;;
    esac
done

# -------------------------------------------------------------------------
# Validate options and set up experiment directory
# -------------------------------------------------------------------------
if [[ $CONTINUE_MODE -eq 1 ]]; then
    # -c mode: must have -o pointing at an existing experiment
    if [[ -z "$OUTPUT_DIR_OVERRIDE" ]]; then
        echo "ERROR: -c requires -o <existing-experiment-dir>"
        exit 1
    fi
    EXPERIMENT_DIR="$OUTPUT_DIR_OVERRIDE"
    if [[ ! -d "$EXPERIMENT_DIR" ]]; then
        echo "ERROR: Experiment directory not found: $EXPERIMENT_DIR"
        exit 1
    fi
    RUN_LOG="$EXPERIMENT_DIR/runs.jsonl"
    SUMMARY_LOG="$EXPERIMENT_DIR/summary.txt"
    if [[ ! -f "$SUMMARY_LOG" ]]; then
        echo "ERROR: summary.txt not found in $EXPERIMENT_DIR"
        exit 1
    fi
    if [[ ! -f "$RUN_LOG" ]]; then
        echo "ERROR: runs.jsonl not found in $EXPERIMENT_DIR"
        exit 1
    fi
else
    # Fresh run: -n is required
    if [[ -z "$ITERATIONS" ]]; then
        echo "ERROR: -n <iterations> is required (or use -c to resume)"
        usage
    fi
    if ! [[ "$ITERATIONS" =~ ^[0-9]+$ ]] || [[ "$ITERATIONS" -lt 1 ]]; then
        echo "ERROR: -n must be a positive integer, got: $ITERATIONS"
        exit 1
    fi
    EXPERIMENT_TS=$(date +%Y-%m-%d_%H-%M-%S)
    if [[ -n "$OUTPUT_DIR_OVERRIDE" ]]; then
        EXPERIMENT_DIR="$OUTPUT_DIR_OVERRIDE"
    else
        EXPERIMENT_DIR="$REPO_ROOT/data/runs/experiment_${EXPERIMENT_TS}"
    fi
    mkdir -p "$EXPERIMENT_DIR"
    RUN_LOG="$EXPERIMENT_DIR/runs.jsonl"
    SUMMARY_LOG="$EXPERIMENT_DIR/summary.txt"
fi

# -------------------------------------------------------------------------
# Tee ALL stdout+stderr to summary.txt from here on
# -------------------------------------------------------------------------
exec > >(tee -a "$SUMMARY_LOG") 2>&1

# Trap unexpected errors (set -e triggers ERR before EXIT)
trap 'echo ""; echo "================================================================"; echo "  FATAL: Script interrupted at line $LINENO (exit code $?)"; echo "  Time: $(date +%Y-%m-%d_%H-%M-%S)"; echo "================================================================"' ERR

# -------------------------------------------------------------------------
# Resume mode: read original params and detect where to continue from
# -------------------------------------------------------------------------
if [[ $CONTINUE_MODE -eq 1 ]]; then
    # Use python to safely parse summary.txt (avoids grep -oP / set -e issues)
    read -r ITERATIONS TOTAL_RUNS LAST_RUN_COUNT <<< "$(python3 - "$SUMMARY_LOG" "$RUN_LOG" <<'PYEOF'
import json, re, sys

summary_path = sys.argv[1]
run_log_path  = sys.argv[2]

iterations  = ""
total_runs  = ""
last_run_count = "0"

with open(summary_path) as f:
    for line in f:
        m = re.match(r"^\s+Iterations\s+:\s+(\d+)", line)
        if m:
            iterations = m.group(1)
        m = re.match(r"^--- Run (\d+) / (\d+)", line)
        if m:
            last_run_count = m.group(1)
            total_runs     = m.group(2)

print(iterations, total_runs, last_run_count)
PYEOF
)"

    # Allow -a / -p overrides; otherwise fall back to values in original summary
    if [[ -z "$AGENT_A_URL" || "$AGENT_A_URL" == "http://localhost:8101" ]]; then
        AGENT_A_URL_FROM_LOG=$(python3 - "$SUMMARY_LOG" <<'PYEOF'
import re, sys
with open(sys.argv[1]) as f:
    for line in f:
        m = re.match(r"^\s+Agent A\s+:\s+(\S+)", line)
        if m:
            print(m.group(1)); break
PYEOF
)
        [[ -n "$AGENT_A_URL_FROM_LOG" ]] && AGENT_A_URL="$AGENT_A_URL_FROM_LOG"
    fi

    if [[ -z "$PROMETHEUS_URL" || "$PROMETHEUS_URL" == "http://localhost:9090" ]]; then
        PROM_FROM_LOG=$(python3 - "$SUMMARY_LOG" <<'PYEOF'
import re, sys
with open(sys.argv[1]) as f:
    for line in f:
        m = re.match(r"^\s+Prometheus\s+:\s+(\S+)", line)
        if m:
            print(m.group(1)); break
PYEOF
)
        [[ -n "$PROM_FROM_LOG" ]] && PROMETHEUS_URL="$PROM_FROM_LOG"
    fi

    # Detect last completed run from runs.jsonl
    read -r LAST_ITER LAST_TASK_SLUG <<< "$(python3 - "$RUN_LOG" <<'PYEOF'
import json, sys
last = None
with open(sys.argv[1]) as f:
    for line in f:
        line = line.strip()
        if line:
            last = json.loads(line)
print(last["iteration"] if last else 0, last["task_slug"] if last else "")
PYEOF
)"

    # Find last task's index in the template
    LAST_TASK_IDX=-1
    for i in "${!TASK_SLUGS[@]}"; do
        if [[ "${TASK_SLUGS[$i]}" == "$LAST_TASK_SLUG" ]]; then
            LAST_TASK_IDX=$i
            break
        fi
    done

    if [[ $LAST_TASK_IDX -eq -1 ]]; then
        echo "ERROR: Last completed task '$LAST_TASK_SLUG' not found in template"
        exit 1
    fi

    START_ITER=$LAST_ITER
    START_TASK_IDX=$(( LAST_TASK_IDX + 1 ))

    # If the last task was the final one in the iteration, advance to next iter
    if [[ $START_TASK_IDX -ge $NUM_TASKS ]]; then
        START_ITER=$(( START_ITER + 1 ))
        START_TASK_IDX=0
    fi

    RUN_COUNT=$LAST_RUN_COUNT

    # Original experiment start time (from first run in runs.jsonl)
    EXPERIMENT_START_MS=$(python3 - "$RUN_LOG" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    first = json.loads(f.readline())
print(first["run_start_ms"])
PYEOF
)

    echo ""
    echo "================================================================"
    echo "  RESUMED at $(date +%Y-%m-%d_%H-%M-%S)"
    echo "  Experiment  : $EXPERIMENT_DIR"
    echo "  Iterations  : $ITERATIONS per task ($NUM_TASKS tasks)"
    echo "  Total Runs  : $TOTAL_RUNS"
    echo "  Last Run    : $LAST_RUN_COUNT (iter=$LAST_ITER, task=$LAST_TASK_SLUG)"
    echo "  Resuming at : iter=$START_ITER, task=${TASK_SLUGS[$START_TASK_IDX]:-none}"
    echo "  Remaining   : $(( TOTAL_RUNS - RUN_COUNT )) runs"
    echo "  Agent A     : $AGENT_A_URL"
    echo "  Prometheus  : $PROMETHEUS_URL"
    echo "================================================================"

    if [[ $START_ITER -gt $ITERATIONS ]]; then
        echo ""
        echo "Experiment already complete! All $TOTAL_RUNS runs finished."
        exit 0
    fi

else
    # Fresh run
    START_ITER=1
    START_TASK_IDX=0
    RUN_COUNT=0
    TOTAL_RUNS=$(( ITERATIONS * NUM_TASKS ))
    EXPERIMENT_START_MS=$(python3 -c "import time; print(int(time.time() * 1000))")
    EXPERIMENT_TS=$(date +%Y-%m-%d_%H-%M-%S)

    echo "================================================================"
    echo "  Agentic Traffic Experiment"
    echo "  Timestamp  : $EXPERIMENT_TS"
    echo "  Iterations : $ITERATIONS per task ($NUM_TASKS tasks total)"
    echo "  Agent A    : $AGENT_A_URL"
    echo "  Prometheus : $PROMETHEUS_URL"
    echo "  Output     : $EXPERIMENT_DIR"
    echo "================================================================"
fi

EXPERIMENT_START_S=$(date +%s)

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
FAILED_RUNS=0

for ITER in $(seq "$START_ITER" "$ITERATIONS"); do
    # On the first iteration of a resumed run, start mid-task; otherwise from task 0
    if [[ $ITER -eq $START_ITER ]]; then
        FIRST_TASK_IDX=$START_TASK_IDX
    else
        FIRST_TASK_IDX=0
    fi

    for TASK_IDX in $(seq "$FIRST_TASK_IDX" $(( NUM_TASKS - 1 ))); do
        TASK="${TASK_NAMES[$TASK_IDX]}"
        TASK_SLUG="${TASK_SLUGS[$TASK_IDX]}"
        RUN_COUNT=$(( RUN_COUNT + 1 ))

        RUN_TS=$(date +%Y-%m-%d_%H-%M-%S)
        RUN_START_S=$(date +%s)
        RUN_START_MS=$(python3 -c "import time; print(int(time.time() * 1000))")

        echo ""
        echo "--- Run $RUN_COUNT / $TOTAL_RUNS  |  iter=$ITER  task=$TASK_SLUG ---"
        echo "  Time  : $RUN_TS"
        echo "  Task  : ${TASK:0:80}..."

        # Send request
        RESPONSE=""
        if RESPONSE=$(send_agentverse_request "$TASK" "$AGENT_A_URL" 2>/tmp/agentverse_err.txt); then
            :
        else
            ERR=$(cat /tmp/agentverse_err.txt 2>/dev/null || true)
            echo "  ERROR: Request failed: $ERR"
            FAILED_RUNS=$(( FAILED_RUNS + 1 ))
            continue
        fi

        RUN_END_S=$(date +%s)
        RUN_END_MS=$(python3 -c "import time; print(int(time.time() * 1000))")
        DURATION_S=$(( RUN_END_S - RUN_START_S ))

        # Extract task_id
        TASK_ID=$(echo "$RESPONSE" | extract_task_id)

        echo "  Task ID  : $TASK_ID"
        echo "  Duration : ${DURATION_S}s"

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
            --iteration      "$ITER" 2>&1; then
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

echo ""
echo "================================================================"
echo "  All runs complete: $RUN_COUNT total, $FAILED_RUNS failed"
echo "  Experiment duration: $(( EXPERIMENT_END_S - EXPERIMENT_START_S ))s"
echo "  Scraping full experiment window..."
echo "================================================================"

if python3 "$SCRAPE_SCRIPT" \
    --dashboard-json "$DASHBOARD_JSON" \
    --output-dir     "$EXPERIMENT_DIR" \
    --prometheus-url "$PROMETHEUS_URL" \
    --start-ms       "$EXPERIMENT_START_MS" \
    --end-ms         "$EXPERIMENT_END_MS" \
    --step           15 \
    --task-slug      "all" \
    --task-id        "aggregate" \
    --iteration      0 2>&1; then
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
    --dashboard-json  "$DASHBOARD_JSON" 2>&1; then
    echo "  Plots saved → $EXPERIMENT_DIR/plots/"
else
    echo "  WARNING: Plotting failed (check dependencies: pip install matplotlib pandas)"
fi

echo ""
echo "================================================================"
echo "  DONE"
echo "  Results: $EXPERIMENT_DIR"
echo "================================================================"
