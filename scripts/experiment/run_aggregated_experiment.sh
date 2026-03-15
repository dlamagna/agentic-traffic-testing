#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RUN_SCRIPT="$SCRIPT_DIR/run_experiment.sh"
MONITOR_SCRIPT="$SCRIPT_DIR/monitor_experiment.sh"
STATE_FILE="$SCRIPT_DIR/.experiment_state"

CRON_TAG="# agentic-experiment-monitor"

ITERATIONS="${1:-}"

if [[ -z "$ITERATIONS" ]]; then
    echo "Usage:"
    echo "  ./run_aggregated_experiment.sh <iterations>"
    exit 1
fi

echo "================================="
echo "[runner] Checking for existing experiment processes"
echo "================================="

# --------------------------------------------------
# find existing run_experiment processes
# --------------------------------------------------

PIDS=$(pgrep -f "$RUN_SCRIPT" || true)

if [[ -n "$PIDS" ]]; then

    echo "[runner] Found existing run_experiment.sh processes:"
    echo "$PIDS"
    echo ""

    echo "[runner] Killing existing experiment processes..."

    for PID in $PIDS; do
        echo "[runner] Killing PID $PID"
        kill -TERM "$PID" 2>/dev/null || true
    done

    echo "[runner] Waiting for processes to terminate..."
    sleep 5

    # force kill if still alive
    for PID in $PIDS; do
        if ps -p "$PID" > /dev/null 2>&1; then
            echo "[runner] PID $PID still alive — sending SIGKILL"
            kill -KILL "$PID" 2>/dev/null || true
        fi
    done

    echo "[runner] Existing experiment processes terminated"

else
    echo "[runner] No existing run_experiment.sh processes found"
fi


echo ""
echo "================================="
echo "[runner] Cleaning existing monitor cron jobs"
echo "================================="

# --------------------------------------------------
# remove existing monitor cron jobs
# --------------------------------------------------

CURRENT_CRON=$(crontab -l 2>/dev/null || true)

echo "[runner] Current cron entries:"
echo "---------------------------------"
echo "$CURRENT_CRON"
echo "---------------------------------"

CLEAN_CRON=$(echo "$CURRENT_CRON" | grep -v "$CRON_TAG" || true)

echo "$CLEAN_CRON" | crontab -

echo "[runner] Existing monitor cron entries removed (if any)"


# --------------------------------------------------
# remove stale state file
# --------------------------------------------------

if [[ -f "$STATE_FILE" ]]; then
    echo "[runner] Removing stale state file"
    rm -f "$STATE_FILE"
fi


# --------------------------------------------------
# allow system to stabilise
# --------------------------------------------------

echo ""
echo "[runner] Waiting 5 minutes for system stabilisation..."
echo "[runner] This allows metrics pipelines and services to settle"
echo ""

sleep 300

echo "[runner] Stabilisation wait complete"


echo "================================="
echo "Starting experiment"
echo "Iterations: $ITERATIONS"
echo "================================="


# --------------------------------------------------
# install cron monitor
# --------------------------------------------------

CURRENT_CRON=$(crontab -l 2>/dev/null || true)

NEW_CRON=$(echo "$CURRENT_CRON" | grep -v "$CRON_TAG" || true)

NEW_CRON="${NEW_CRON}
*/5 * * * * $MONITOR_SCRIPT $CRON_TAG"

echo "$NEW_CRON" | crontab -

echo "[runner] Monitor cron installed"


# --------------------------------------------------
# start experiment
# --------------------------------------------------

nohup "$RUN_SCRIPT" -n "$ITERATIONS" > "$SCRIPT_DIR/experiment.log" 2>&1 &

PID=$!

echo "[runner] Experiment process started"
echo "[runner] PID: $PID"

sleep 3

EXPERIMENT_DIR=$(ls -td "$SCRIPT_DIR/../../data/runs"/experiment_* 2>/dev/null | head -n1 || true)

if [[ -z "$EXPERIMENT_DIR" ]]; then
    echo "[runner] ERROR: Could not detect experiment directory"
    exit 1
fi

echo "[runner] Experiment directory:"
echo "  $EXPERIMENT_DIR"

cat > "$STATE_FILE" <<EOF
PID=$PID
EXPERIMENT_DIR=$EXPERIMENT_DIR
EOF

echo "[runner] State file written:"
cat "$STATE_FILE"

echo "[runner] Setup complete — monitor cron will handle crashes"