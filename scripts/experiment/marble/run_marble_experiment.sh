#!/usr/bin/env bash
# =============================================================================
# run_marble_experiment.sh
# =============================================================================
# End-to-end MARBLE experiment runner.
#
# Resets the testbed, iterates over all domain × topology combinations,
# collects per-call IAT and task-level metrics, then runs analysis and
# generates plots. Modelled on scripts/experiment/agentverse/run_aggregated_experiment.sh.
#
# Usage:
#   ./run_marble_experiment.sh [options]
#
# Options:
#   -n <int>         Tasks per domain/topology combo (default: 5)
#   -d <csv>         Comma-separated domains (default: research,coding,bargaining)
#   -t <csv>         Comma-separated topologies (default: star,chain,tree,graph)
#   -w <int>         Seconds to wait between runs (default: 20)
#   -o <dir>         Output directory (required for -r; default: <repo>/data/marble/marble_experiment_<ts>)
#   -j               Skip LLM-as-judge scoring (faster)
#   -s               Skip testbed reset/redeploy (for reruns)
#   -r               Resume an interrupted run (requires -o pointing at existing dir;
#                    skips combos whose output JSONL already has enough records)
#   -h               Show this help
#
# Examples:
#   ./run_marble_experiment.sh -n 5
#   ./run_marble_experiment.sh -n 3 -d research,coding -t star,graph -j
#   ./run_marble_experiment.sh -n 5 -s          # skip reset, reuse running testbed
#   ./run_marble_experiment.sh -r -o data/marble/marble_experiment_2026-03-26_19-28-42  # resume
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Activate venv
if [[ -f "$REPO_ROOT/.venv/bin/activate" ]]; then
    # shellcheck source=/dev/null
    source "$REPO_ROOT/.venv/bin/activate"
fi

if [[ -x "$REPO_ROOT/.venv/bin/python3" ]]; then
    PYTHON="$REPO_ROOT/.venv/bin/python3"
else
    PYTHON="python3"
fi

UNINSTALL_SCRIPT="$REPO_ROOT/scripts/deploy/uninstall_testbed.sh"
DEPLOY_SCRIPT="$REPO_ROOT/scripts/deploy/deploy.sh"

IAT_SCRIPT="$SCRIPT_DIR/analyse_marble_iat.py"
RESULTS_SCRIPT="$SCRIPT_DIR/analyse_marble_results.py"
TOKENS_CONCURRENCY_SCRIPT="$SCRIPT_DIR/analyse_marble_tokens_concurrency.py"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
TASKS_PER_COMBO=5
DOMAINS="research,coding,bargaining"
TOPOLOGIES="star,chain,tree,graph"
WAIT_BETWEEN_RUNS=20
OUTPUT_DIR_OVERRIDE=""
SKIP_JUDGE_FLAG=""
SKIP_RESET="${SKIP_RESET:-0}"
RESUME_MODE=0

usage() {
    sed -n '3,38p' "$0" | sed 's/^# //' | sed 's/^#//'
    exit 0
}

while getopts "n:d:t:w:o:jsrh" opt; do
    case $opt in
        n) TASKS_PER_COMBO="$OPTARG" ;;
        d) DOMAINS="$OPTARG" ;;
        t) TOPOLOGIES="$OPTARG" ;;
        w) WAIT_BETWEEN_RUNS="$OPTARG" ;;
        o) OUTPUT_DIR_OVERRIDE="$OPTARG" ;;
        j) SKIP_JUDGE_FLAG="--skip-judge" ;;
        s) SKIP_RESET=1 ;;
        r) RESUME_MODE=1 ;;
        h) usage ;;
        *) echo "ERROR: Unknown option -$OPTARG"; usage ;;
    esac
done

if [[ $RESUME_MODE -eq 1 ]]; then
    if [[ -z "$OUTPUT_DIR_OVERRIDE" ]]; then
        echo "ERROR: -r (resume) requires -o <existing-experiment-dir>"
        exit 1
    fi
    if [[ ! -d "$OUTPUT_DIR_OVERRIDE" ]]; then
        echo "ERROR: Experiment directory not found: $OUTPUT_DIR_OVERRIDE"
        exit 1
    fi
    # Read original params from summary.txt
    SUMMARY_TMP="$OUTPUT_DIR_OVERRIDE/summary.txt"
    if [[ -f "$SUMMARY_TMP" ]]; then
        _n=$(grep -oP "Tasks/combo\s*:\s*\K\d+" "$SUMMARY_TMP" 2>/dev/null || true)
        _d=$(grep -oP "Domains\s*:\s*\K\S+" "$SUMMARY_TMP" 2>/dev/null || true)
        _t=$(grep -oP "Topologies\s*:\s*\K\S+" "$SUMMARY_TMP" 2>/dev/null || true)
        [[ -n "$_n" ]] && TASKS_PER_COMBO="$_n"
        [[ -n "$_d" ]] && DOMAINS="$_d"
        [[ -n "$_t" ]] && TOPOLOGIES="$_t"
    fi
    SKIP_RESET=1
fi

# ---------------------------------------------------------------------------
# Kill stale experiment processes
# ---------------------------------------------------------------------------
echo "================================="
echo "[marble] Checking for stale experiment processes"
echo "================================="

STALE_PIDS=$(pgrep -f "run_marble_experiment.sh\|benchmarks.marble.runner" 2>/dev/null | grep -v "^$$\$" || true)
if [[ -n "$STALE_PIDS" ]]; then
    echo "[marble] Killing stale processes: $STALE_PIDS"
    for PID in $STALE_PIDS; do
        kill -TERM "$PID" 2>/dev/null || true
    done
    sleep 3
    for PID in $STALE_PIDS; do
        ps -p "$PID" > /dev/null 2>&1 && kill -KILL "$PID" 2>/dev/null || true
    done
fi

# ---------------------------------------------------------------------------
# Reset testbed
# ---------------------------------------------------------------------------
if [[ "${SKIP_RESET}" -ne 1 ]]; then
    echo ""
    echo "================================="
    echo "[marble] Resetting testbed"
    echo "================================="

    if [[ -x "$UNINSTALL_SCRIPT" ]]; then
        echo "[marble] Uninstalling existing deployment (keeping logs)..."
        "$UNINSTALL_SCRIPT" --keep-logs
    else
        echo "[marble] WARNING: uninstall_testbed.sh not found — skipping"
    fi

    if [[ -x "$DEPLOY_SCRIPT" ]]; then
        echo "[marble] Deploying fresh testbed..."
        "$DEPLOY_SCRIPT"
    else
        echo "[marble] WARNING: deploy.sh not found — skipping"
    fi

    echo ""
    echo "[marble] Waiting 5 minutes for system stabilisation..."
    sleep 300
    echo "[marble] Stabilisation complete"
else
    echo "[marble] Skipping testbed reset (SKIP_RESET=1)"
fi

# ---------------------------------------------------------------------------
# Create experiment directory
# ---------------------------------------------------------------------------
EXPERIMENT_TS=$(date +%Y-%m-%d_%H-%M-%S)
if [[ -n "$OUTPUT_DIR_OVERRIDE" ]]; then
    EXPERIMENT_DIR="$OUTPUT_DIR_OVERRIDE"
else
    EXPERIMENT_DIR="$REPO_ROOT/data/marble/marble_experiment_${EXPERIMENT_TS}"
fi
mkdir -p "$EXPERIMENT_DIR/results"
SUMMARY_LOG="$EXPERIMENT_DIR/summary.txt"

# Tee all output to summary.txt from here on
exec > >(tee -a "$SUMMARY_LOG") 2>&1

trap 'echo ""; echo "================================================================"; echo "  FATAL: Script interrupted at line $LINENO (exit code $?)"; echo "  Time: $(date +%Y-%m-%d_%H-%M-%S)"; echo "================================================================"' ERR

echo ""
echo "================================================================"
echo "  MARBLE End-to-End Experiment"
echo "  Time        : $(date +%Y-%m-%d_%H-%M-%S)"
echo "  Experiment  : $EXPERIMENT_DIR"
echo "  Domains     : $DOMAINS"
echo "  Topologies  : $TOPOLOGIES"
echo "  Tasks/combo : $TASKS_PER_COMBO"
echo "  Wait between: ${WAIT_BETWEEN_RUNS}s"
echo "  Skip judge  : ${SKIP_JUDGE_FLAG:-no}"
echo "================================================================"
echo ""

# ---------------------------------------------------------------------------
# Finalization function (called at end or on EXIT if set)
# ---------------------------------------------------------------------------
finalize_experiment() {
    echo ""
    echo "================================================================"
    echo "  Generating analysis and plots"
    echo "================================================================"

    mkdir -p "$EXPERIMENT_DIR/plots/iat" \
             "$EXPERIMENT_DIR/plots/results" \
             "$EXPERIMENT_DIR/plots/tokens_concurrency"

    echo ""
    echo "Generating IAT analysis (per-topology inter-arrival time distributions)..."
    if "$PYTHON" "$IAT_SCRIPT" \
        --call-log "$REPO_ROOT/logs/marble_llm_calls.jsonl" \
        --output-dir "$EXPERIMENT_DIR/plots/iat" 2>&1; then
        echo "  IAT plots saved → $EXPERIMENT_DIR/plots/iat/"
    else
        echo "  WARNING: IAT analysis failed (check logs/marble_llm_calls.jsonl exists)"
    fi

    echo ""
    echo "Generating results analysis (score/duration/fan-out per topology × domain)..."
    if "$PYTHON" "$RESULTS_SCRIPT" \
        --results-dir "$EXPERIMENT_DIR/results" \
        --output-dir "$EXPERIMENT_DIR/plots/results" 2>&1; then
        echo "  Results plots saved → $EXPERIMENT_DIR/plots/results/"
    else
        echo "  WARNING: Results analysis failed"
    fi

    echo ""
    echo "Generating tokens and concurrency analysis..."
    if "$PYTHON" "$TOKENS_CONCURRENCY_SCRIPT" \
        --results-dir "$EXPERIMENT_DIR/results" \
        --call-log    "$REPO_ROOT/logs/marble_llm_calls.jsonl" \
        --output-dir  "$EXPERIMENT_DIR/plots/tokens_concurrency" 2>&1; then
        echo "  Tokens/concurrency plots saved → $EXPERIMENT_DIR/plots/tokens_concurrency/"
    else
        echo "  WARNING: Tokens/concurrency analysis failed"
    fi

    echo ""
    echo "================================================================"
    echo "  DONE"
    echo "  Experiment : $EXPERIMENT_DIR"
    echo "  Plots:"
    echo "    plots/iat/                 — IAT histograms, ECDF, box plots, KS tests"
    echo "    plots/results/             — score, duration, fan-out, comms per topology × domain"
    echo "    plots/tokens_concurrency/  — token usage, call latency, concurrency"
    echo "================================================================"

    # Uninstall testbed so LLM backend is not left idle
    if [[ "${SKIP_RESET}" -ne 1 ]]; then
        echo ""
        echo "[marble] Uninstalling testbed (keeping logs)..."
        if [[ -x "$UNINSTALL_SCRIPT" ]]; then
            "$UNINSTALL_SCRIPT" --keep-logs
        fi
    fi
}

# ---------------------------------------------------------------------------
# Main experiment loop
# ---------------------------------------------------------------------------

# Parse csv lists into arrays
IFS=',' read -ra DOMAIN_LIST <<< "$DOMAINS"
IFS=',' read -ra TOPO_LIST <<< "$TOPOLOGIES"

TOTAL_COMBOS=$(( ${#DOMAIN_LIST[@]} * ${#TOPO_LIST[@]} ))
COMBO_NUM=0

for DOMAIN in "${DOMAIN_LIST[@]}"; do
    for TOPOLOGY in "${TOPO_LIST[@]}"; do
        COMBO_NUM=$(( COMBO_NUM + 1 ))
        OUTPUT_FILE="$EXPERIMENT_DIR/results/${DOMAIN}_${TOPOLOGY}.jsonl"

        echo ""
        echo "--- Combo $COMBO_NUM / $TOTAL_COMBOS : domain=$DOMAIN  topology=$TOPOLOGY ---"
        echo "    Output: $OUTPUT_FILE"
        echo "    Time: $(date +%Y-%m-%d_%H-%M-%S)"

        # Resume mode: skip combos that already have enough records
        if [[ $RESUME_MODE -eq 1 && -f "$OUTPUT_FILE" ]]; then
            EXISTING=$(wc -l < "$OUTPUT_FILE" | tr -d ' ')
            if [[ "$EXISTING" -ge "$TASKS_PER_COMBO" ]]; then
                echo "    [SKIP] Already complete ($EXISTING records found)"
                continue
            else
                echo "    [PARTIAL] $EXISTING / $TASKS_PER_COMBO records — rerunning combo"
                rm -f "$OUTPUT_FILE"
            fi
        fi

        cd "$REPO_ROOT"
        # shellcheck disable=SC2086
        if "$PYTHON" -m benchmarks.marble.runner \
            --domain "$DOMAIN" \
            --topology "$TOPOLOGY" \
            --max-tasks "$TASKS_PER_COMBO" \
            --output "$OUTPUT_FILE" \
            --tasks-dir "$EXPERIMENT_DIR/tasks" \
            --verbose \
            $SKIP_JUDGE_FLAG; then
            echo "    [OK] Combo $COMBO_NUM complete"
        else
            echo "    [WARN] Combo $COMBO_NUM exited with error — continuing"
        fi

        if [[ $COMBO_NUM -lt $TOTAL_COMBOS ]]; then
            echo "    Waiting ${WAIT_BETWEEN_RUNS}s before next combo..."
            sleep "$WAIT_BETWEEN_RUNS"
        fi
    done
done

echo ""
echo "All $TOTAL_COMBOS combos complete."

finalize_experiment
