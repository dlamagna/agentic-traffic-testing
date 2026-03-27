#!/usr/bin/env bash
# =============================================================================
# run_aggregated_experiment.sh
# =============================================================================
# Outer orchestrator for a long-running experiment with cron-based crash
# recovery.  Wraps run_experiment.sh and adds:
#   - Kill any stale run_experiment.sh processes
#   - Remove existing monitor cron jobs
#   - Reset + redeploy the testbed
#   - Wait 5 minutes for system stabilisation
#   - Install the monitor cron job
#   - Launch run_experiment.sh in the background via nohup
#
# Usage:
#   ./run_aggregated_experiment.sh <iterations> [-b] [-s] [-A <int>]
#
# Options:
#   <iterations>   Iterations per task (required)
#   -b             Balanced mode: 50 % horizontal + 50 % vertical
#                  (forwarded to run_experiment.sh -b; incompatible with -s)
#   -s             Sweep mode: solo (0 agents) + all combos of structure × agent
#                  count 1–5 — 11 combos (forwarded to run_experiment.sh -s;
#                  incompatible with -b)
#   -A <int>       Force a fixed number of sub-agents (experts) per run.
#                  0–5 (forwarded to run_experiment.sh -A <int>).
#                  0 = solo mode (Agent A only, no sub-agents).
#                  Ignored when -s is active. Omit to let the LLM decide.
#
# Examples:
#   ./run_aggregated_experiment.sh 50
#   ./run_aggregated_experiment.sh 25 -b
#   ./run_aggregated_experiment.sh 25 -b -A 3
#   ./run_aggregated_experiment.sh 50 -A 1
#   ./run_aggregated_experiment.sh 10 -s
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Activate the repo's virtualenv so all Python packages are available
if [[ -f "$REPO_ROOT/.venv/bin/activate" ]]; then
    # shellcheck source=/dev/null
    source "$REPO_ROOT/.venv/bin/activate"
fi

RUN_SCRIPT="$SCRIPT_DIR/run_experiment.sh"
MONITOR_SCRIPT="$SCRIPT_DIR/monitor_experiment.sh"
STATE_FILE="$SCRIPT_DIR/.experiment_state"
UNINSTALL_SCRIPT="$REPO_ROOT/scripts/deploy/uninstall_testbed.sh"
DEPLOY_SCRIPT="$REPO_ROOT/scripts/deploy/deploy.sh"

CRON_TAG="# agentic-experiment-monitor"

ITERATIONS="${1:-}"
BALANCED_FLAG=""
SWEEP_FLAG=""
AGENT_COUNT_FLAG=""

# Parse optional flags from remaining args (can appear in any order after <iterations>)
_NEXT_IS_A=0
for arg in "${@:2}"; do
    if [[ $_NEXT_IS_A -eq 1 ]]; then
        AGENT_COUNT_FLAG="-A $arg"
        _NEXT_IS_A=0
    elif [[ "$arg" == "-b" ]]; then
        BALANCED_FLAG="-b"
    elif [[ "$arg" == "-s" ]]; then
        SWEEP_FLAG="-s"
    elif [[ "$arg" == "-A" ]]; then
        _NEXT_IS_A=1
    elif [[ "$arg" =~ ^-A([0-9]+)$ ]]; then
        AGENT_COUNT_FLAG="-A ${BASH_REMATCH[1]}"
    fi
done

if [[ -n "$BALANCED_FLAG" && -n "$SWEEP_FLAG" ]]; then
    echo "ERROR: -b (balanced) and -s (sweep) cannot be used together"
    exit 1
fi

if [[ -z "$ITERATIONS" ]]; then
    echo "Usage:"
    echo "  ./run_aggregated_experiment.sh <iterations> [-b] [-s] [-A <int>]"
    echo ""
    echo "  -b       Balanced mode: 50 % horizontal + 50 % vertical"
    echo "  -s       Sweep mode: all combos of structure × agent count 1–5"
    echo "  -A <int> Force a fixed number of sub-agents per run (ignored with -s)"
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
echo "================================="
echo "[runner] Resetting testbed"
echo "================================="

if [[ -x "$UNINSTALL_SCRIPT" ]]; then
    echo "[runner] Uninstalling existing deployment (keeping logs)..."
    "$UNINSTALL_SCRIPT" --keep-logs
else
    echo "[runner] WARNING: uninstall_testbed.sh not found at $UNINSTALL_SCRIPT"
fi

if [[ -x "$DEPLOY_SCRIPT" ]]; then
    echo "[runner] Deploying fresh testbed..."
    "$DEPLOY_SCRIPT"
else
    echo "[runner] WARNING: deploy.sh not found at $DEPLOY_SCRIPT"
fi

echo ""
echo "[runner] Waiting 5 minutes for system stabilisation..."
echo "[runner] This allows metrics pipelines and services to settle"
echo ""

sleep 300

echo "[runner] Stabilisation wait complete"


echo "================================="
echo "Starting experiment"
_EXP_MODE=""
[[ -n "$BALANCED_FLAG" ]] && _EXP_MODE=" (balanced 50/50)"
[[ -n "$SWEEP_FLAG" ]]    && _EXP_MODE=" (sweep: solo + H+V × agents 1–5)"
[[ -n "$AGENT_COUNT_FLAG" && -z "$SWEEP_FLAG" ]] && _EXP_MODE="${_EXP_MODE} (fixed agents: ${AGENT_COUNT_FLAG#-A })"
echo "Iterations: $ITERATIONS${_EXP_MODE}"
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

# Tell run_experiment.sh not to reset again — we already reset above.
export SKIP_RESET=1
# shellcheck disable=SC2086
nohup "$RUN_SCRIPT" -n "$ITERATIONS" $BALANCED_FLAG $SWEEP_FLAG $AGENT_COUNT_FLAG > "$SCRIPT_DIR/experiment.log" 2>&1 &

PID=$!

echo "[runner] Experiment process started"
echo "[runner] PID: $PID"

sleep 3

# Detect the most-recently created experiment dir (any prefix variant)
EXPERIMENT_DIR=$(ls -td "$REPO_ROOT/data/agentverse/"*experiment_* 2>/dev/null | head -n1 || true)

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