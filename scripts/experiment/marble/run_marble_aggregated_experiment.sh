#!/usr/bin/env bash
# =============================================================================
# run_marble_aggregated_experiment.sh
# =============================================================================
# Outer orchestrator for a long-running MARBLE experiment with cron-based
# crash recovery. Wraps run_marble_experiment.sh and adds:
#   - Kill any stale experiment processes
#   - Remove existing monitor cron jobs
#   - Reset + redeploy the testbed
#   - Wait 5 minutes for system stabilisation
#   - Install the monitor cron job (fires every 5 minutes)
#   - Launch run_marble_experiment.sh in the background via nohup
#
# Usage:
#   ./run_marble_aggregated_experiment.sh -n <tasks> [options]
#
# Options:
#   -n <int>   Tasks per domain/topology combo (required)
#   -d <csv>   Domains  (default: research,coding,bargaining)
#   -t <csv>   Topologies (default: star,chain,tree,graph)
#   -w <int>   Seconds to wait between runs (default: 20)
#   -j         Skip LLM-as-judge scoring
#
# Examples:
#   ./run_marble_aggregated_experiment.sh -n 20          # ~8 hour overnight run
#   ./run_marble_aggregated_experiment.sh -n 5 -j        # quick test, no scoring
#   ./run_marble_aggregated_experiment.sh -n 10 -d research,coding
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Activate venv
if [[ -f "$REPO_ROOT/.venv/bin/activate" ]]; then
    # shellcheck source=/dev/null
    source "$REPO_ROOT/.venv/bin/activate"
fi

RUN_SCRIPT="$SCRIPT_DIR/run_marble_experiment.sh"
MONITOR_SCRIPT="$SCRIPT_DIR/monitor_marble_experiment.sh"
STATE_FILE="$SCRIPT_DIR/.marble_experiment_state"
UNINSTALL_SCRIPT="$REPO_ROOT/scripts/deploy/uninstall_testbed.sh"
DEPLOY_SCRIPT="$REPO_ROOT/scripts/deploy/deploy.sh"

CRON_TAG="# marble-experiment-monitor"

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
TASKS_PER_COMBO=""
DOMAINS_FLAG=""
TOPOLOGIES_FLAG=""
WAIT_FLAG=""
SKIP_JUDGE_FLAG=""

usage() {
    sed -n '3,26p' "$0" | sed 's/^# //' | sed 's/^#//'
    exit 0
}

while getopts "n:d:t:w:jh" opt; do
    case $opt in
        n) TASKS_PER_COMBO="$OPTARG" ;;
        d) DOMAINS_FLAG="-d $OPTARG" ;;
        t) TOPOLOGIES_FLAG="-t $OPTARG" ;;
        w) WAIT_FLAG="-w $OPTARG" ;;
        j) SKIP_JUDGE_FLAG="-j" ;;
        h) usage ;;
        *) echo "ERROR: Unknown option -$OPTARG"; usage ;;
    esac
done

if [[ -z "$TASKS_PER_COMBO" ]]; then
    echo "ERROR: -n <tasks> is required"
    usage
fi

# ---------------------------------------------------------------------------
# Kill stale processes
# ---------------------------------------------------------------------------
echo "================================="
echo "[aggregated] Checking for stale processes"
echo "================================="

STALE=$(pgrep -f "run_marble_experiment.sh\|run_marble_aggregated\|benchmarks.marble.runner" 2>/dev/null \
        | grep -v "^$$\$" || true)
if [[ -n "$STALE" ]]; then
    echo "[aggregated] Killing: $STALE"
    for PID in $STALE; do kill -TERM "$PID" 2>/dev/null || true; done
    sleep 5
    for PID in $STALE; do ps -p "$PID" > /dev/null 2>&1 && kill -KILL "$PID" 2>/dev/null || true; done
fi

# ---------------------------------------------------------------------------
# Remove existing monitor cron jobs and stale state
# ---------------------------------------------------------------------------
echo ""
echo "================================="
echo "[aggregated] Cleaning existing monitor cron jobs"
echo "================================="

CURRENT_CRON=$(crontab -l 2>/dev/null || true)
echo "$CURRENT_CRON" | grep -v "$CRON_TAG" | crontab -
echo "[aggregated] Existing monitor cron removed (if any)"

rm -f "$STATE_FILE"

# ---------------------------------------------------------------------------
# Reset + redeploy testbed
# ---------------------------------------------------------------------------
echo ""
echo "================================="
echo "[aggregated] Resetting testbed"
echo "================================="

if [[ -x "$UNINSTALL_SCRIPT" ]]; then
    "$UNINSTALL_SCRIPT" --keep-logs
else
    echo "[aggregated] WARNING: uninstall_testbed.sh not found — skipping"
fi

if [[ -x "$DEPLOY_SCRIPT" ]]; then
    "$DEPLOY_SCRIPT"
else
    echo "[aggregated] WARNING: deploy.sh not found — skipping"
fi

echo ""
echo "[aggregated] Waiting 5 minutes for system stabilisation..."
sleep 300
echo "[aggregated] Stabilisation complete"

# ---------------------------------------------------------------------------
# Install monitor cron (every 5 minutes)
# ---------------------------------------------------------------------------
CURRENT_CRON=$(crontab -l 2>/dev/null || true)
NEW_CRON=$(echo "$CURRENT_CRON" | grep -v "$CRON_TAG" || true)
NEW_CRON="${NEW_CRON}
*/5 * * * * $MONITOR_SCRIPT $CRON_TAG"
echo "$NEW_CRON" | crontab -
echo "[aggregated] Monitor cron installed (every 5 min)"

# ---------------------------------------------------------------------------
# Launch experiment
# ---------------------------------------------------------------------------
echo ""
echo "================================="
echo "[aggregated] Starting experiment"
echo "  Tasks/combo : $TASKS_PER_COMBO"
echo "  Domains     : ${DOMAINS_FLAG:-(default)}"
echo "  Topologies  : ${TOPOLOGIES_FLAG:-(default)}"
echo "  Skip judge  : ${SKIP_JUDGE_FLAG:-(no)}"
echo "================================="

export SKIP_RESET=1
LOG_FILE="$SCRIPT_DIR/experiment.log"

# shellcheck disable=SC2086
nohup "$RUN_SCRIPT" \
    -n "$TASKS_PER_COMBO" \
    -s \
    ${DOMAINS_FLAG} \
    ${TOPOLOGIES_FLAG} \
    ${WAIT_FLAG} \
    ${SKIP_JUDGE_FLAG} \
    > "$LOG_FILE" 2>&1 &

PID=$!
echo "[aggregated] Experiment PID: $PID"
echo "[aggregated] Log: $LOG_FILE"

sleep 5

# Detect experiment directory from the log
EXPERIMENT_DIR=$(grep -oP "Experiment\s*:\s*\K\S+" "$LOG_FILE" 2>/dev/null | head -1 || true)

if [[ -z "$EXPERIMENT_DIR" ]]; then
    # Fallback: find most recently created marble experiment dir
    EXPERIMENT_DIR=$(ls -td "$REPO_ROOT/data/marble/marble_experiment_"* 2>/dev/null | head -1 || true)
fi

if [[ -z "$EXPERIMENT_DIR" ]]; then
    echo "[aggregated] WARNING: Could not detect experiment directory"
else
    echo "[aggregated] Experiment directory: $EXPERIMENT_DIR"
fi

cat > "$STATE_FILE" <<EOF
PID=$PID
EXPERIMENT_DIR=${EXPERIMENT_DIR:-unknown}
EOF

echo "[aggregated] State written to $STATE_FILE"
echo "[aggregated] Monitor cron will restart the experiment if it crashes"
echo ""
echo "  Monitor with:  tail -f $LOG_FILE"
echo "  Cancel cron:   crontab -e  (remove line tagged '$CRON_TAG')"
echo "  State file:    $STATE_FILE"
