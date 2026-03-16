#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RUN_SCRIPT="$SCRIPT_DIR/run_experiment.sh"
STATE_FILE="$SCRIPT_DIR/.experiment_state"

echo "[monitor] =================================="
echo "[monitor] $(date)"

if [[ ! -f "$STATE_FILE" ]]; then
    echo "[monitor] No state file found"
    exit 0
fi

source "$STATE_FILE"

echo "[monitor] Monitoring experiment:"
echo "PID=$PID"
echo "DIR=$EXPERIMENT_DIR"

# --------------------------------------------------
# if still running
# --------------------------------------------------

if ps -p "$PID" > /dev/null 2>&1; then
    echo "[monitor] Process still running"
    exit 0
fi

echo "[monitor] Process not running"

# --------------------------------------------------
# detect normal completion
# --------------------------------------------------

if grep -q "DONE" "$EXPERIMENT_DIR/summary.txt" 2>/dev/null; then
    echo "[monitor] Experiment completed normally"
    rm -f "$STATE_FILE" 2>/dev/null || true
    # Remove the cron job now that the experiment is done.
    crontab -l 2>/dev/null | grep -v "# agentic-experiment-monitor" | crontab -
    echo "[monitor] Cron job removed"
    exit 0
fi

# --------------------------------------------------
# restart crashed experiment
# --------------------------------------------------

echo "[monitor] Experiment appears to have crashed"
echo "[monitor] Restarting with resume mode..."

nohup "$RUN_SCRIPT" -c -o "$EXPERIMENT_DIR" >> "$EXPERIMENT_DIR/restart.log" 2>&1 &

NEW_PID=$!

cat > "$STATE_FILE" <<EOF
PID=$NEW_PID
EXPERIMENT_DIR=$EXPERIMENT_DIR
EOF

echo "[monitor] Restarted experiment"
echo "NEW_PID=$NEW_PID"