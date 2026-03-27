#!/usr/bin/env bash
# monitor_marble_experiment.sh
# Installed as a cron job by run_marble_aggregated_experiment.sh.
# Checks every invocation whether the experiment process is alive;
# if it died without writing DONE, restarts it in resume mode.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_SCRIPT="$SCRIPT_DIR/run_marble_experiment.sh"
STATE_FILE="$SCRIPT_DIR/.marble_experiment_state"
CRON_TAG="# marble-experiment-monitor"

echo "[marble-monitor] =================================="
echo "[marble-monitor] $(date)"

if [[ ! -f "$STATE_FILE" ]]; then
    echo "[marble-monitor] No state file — nothing to monitor"
    exit 0
fi

# shellcheck source=/dev/null
source "$STATE_FILE"

echo "[marble-monitor] PID=$PID"
echo "[marble-monitor] DIR=$EXPERIMENT_DIR"

# Still running — nothing to do
if ps -p "$PID" > /dev/null 2>&1; then
    echo "[marble-monitor] Process still running"
    exit 0
fi

echo "[marble-monitor] Process not running"

# Completed normally
if grep -q "DONE" "$EXPERIMENT_DIR/summary.txt" 2>/dev/null; then
    echo "[marble-monitor] Experiment completed normally — removing cron and state"
    rm -f "$STATE_FILE" 2>/dev/null || true
    crontab -l 2>/dev/null | grep -v "$CRON_TAG" | crontab -
    echo "[marble-monitor] Cron job removed"
    exit 0
fi

# Crashed — restart in resume mode (skip completed combos)
echo "[marble-monitor] Experiment crashed — restarting in resume mode"

REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
if [[ -f "$REPO_ROOT/.venv/bin/activate" ]]; then
    # shellcheck source=/dev/null
    source "$REPO_ROOT/.venv/bin/activate"
fi

export SKIP_RESET=1
nohup "$RUN_SCRIPT" -r -s -o "$EXPERIMENT_DIR" >> "$EXPERIMENT_DIR/restart.log" 2>&1 &
NEW_PID=$!

cat > "$STATE_FILE" <<EOF
PID=$NEW_PID
EXPERIMENT_DIR=$EXPERIMENT_DIR
EOF

echo "[marble-monitor] Restarted: PID=$NEW_PID"
