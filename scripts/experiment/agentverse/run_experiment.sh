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
#   ./run_experiment.sh -n <iterations> -b [options]
#   ./run_experiment.sh -n <iterations> -s [options]
#   ./run_experiment.sh -c -o <existing-experiment-dir> [options]
#
# Options:
#   -n <int>     Number of iterations per task (required for fresh runs)
#   -b           Balanced mode: force exactly 50 % horizontal + 50 % vertical runs
#                (passes force_structure to Agent A; incompatible with -c and -s)
#   -s           Sweep mode: run every combination of discussion structure
#                (horizontal, vertical) × agent count (1–5) plus solo (0 agents)
#                — 11 combos total.
#                Each combo is run -n times per task. Incompatible with -b and -c.
#   -A <int>     Force a fixed number of sub-agents (experts) for every run.
#                0–5 (capped at MAX_PARALLEL_WORKERS / available AGENT_B_URLS).
#                0 = solo mode (Agent A only, no sub-agents).
#                Ignored when -s is active (sweep covers all counts 1–5).
#                Omit to let the LLM decide (default behaviour).
#   -c           Continue/resume an interrupted experiment (requires -o)
#   -o <dir>     Output directory — new dir for fresh runs, existing dir for -c
#                (default for fresh runs: <repo>/data/agentverse/experiment_<ts>)
#                (balanced default:       <repo>/data/agentverse/balanced_experiment_<ts>)
#                (sweep default:          <repo>/data/agentverse/sweep_experiment_<ts>)
#   -a <url>     Agent A base URL  (default: http://localhost:8101)
#   -p <url>     Prometheus URL    (default: http://localhost:9090)
#   -w <int>     Seconds to wait after each run for metrics to propagate
#                (default: 20)
#   -h           Show this help
#
# Examples:
#   ./run_experiment.sh -n 50
#   ./run_experiment.sh -n 25 -b          # 25 H + 25 V per task = 50 * tasks total
#   ./run_experiment.sh -n 25 -b -A 3     # balanced, always 3 agents
#   ./run_experiment.sh -n 10 -A 1        # single-agent baseline
#   ./run_experiment.sh -n 10 -s          # sweep: 10 iters × 10 combos × tasks
#   ./run_experiment.sh -n 10 -a http://192.168.1.100:8101 -w 30
#   ./run_experiment.sh -c -o data/agentverse/experiment_2026-03-10_17-42-47
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Activate the repo's virtualenv so all Python packages are available
if [[ -f "$REPO_ROOT/.venv/bin/activate" ]]; then
    # shellcheck source=/dev/null
    source "$REPO_ROOT/.venv/bin/activate"
fi

UNINSTALL_SCRIPT="$REPO_ROOT/scripts/deploy/uninstall_testbed.sh"
DEPLOY_SCRIPT="$REPO_ROOT/scripts/deploy/deploy.sh"

DASHBOARD_JSON="$REPO_ROOT/infra/monitoring/grafana/provisioning/dashboards/agentic-traffic.json"
SCRAPE_SCRIPT="$SCRIPT_DIR/scrape_metrics.py"
PLOT_SCRIPT="$SCRIPT_DIR/plot_results.py"
STRUCTURE_COMPARE_SCRIPT="$SCRIPT_DIR/compare_discussion_structures.py"
STRUCTURE_CORRELATE_SCRIPT="$SCRIPT_DIR/correlate_structure_metrics.py"
IAT_STATS_SCRIPT="$SCRIPT_DIR/analyse_iat_statistics.py"
BURST_AGENTS_SCRIPT="$SCRIPT_DIR/analyse_burst_removed_agents.py"
VERTICAL_RAW_AGG_SCRIPT="$SCRIPT_DIR/analyse_vertical_raw_vs_aggregated.py"
CONCURRENCY_PERF_SCRIPT="$SCRIPT_DIR/analyse_concurrency_performance.py"
CORRELATE_METRICS_SCRIPT="$SCRIPT_DIR/correlate_metrics.py"
WORKFLOW_TEMPLATE="$REPO_ROOT/agents/templates/agentverse_workflow.json"

# Use the repo's .venv python if available (has matplotlib/pandas/etc.)
if [[ -x "$REPO_ROOT/.venv/bin/python3" ]]; then
    PYTHON="$REPO_ROOT/.venv/bin/python3"
else
    PYTHON="python3"
fi

# Defaults
AGENT_A_URL="${AGENT_A_URL:-http://localhost:8101}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
ITERATIONS=""
WAIT_AFTER_RUN=20
OUTPUT_DIR_OVERRIDE=""
CONTINUE_MODE=0
BALANCED_MODE=0
SWEEP_MODE=0
# Optional: force a fixed number of sub-agents (experts) for every run.
# Empty string = let the LLM decide (default). Ignored when SWEEP_MODE=1.
FORCE_AGENT_COUNT=""
# Set to 1 (or export SKIP_RESET=1) to skip the reset+deploy at startup.
# run_aggregated_experiment.sh sets this when it manages its own reset.
SKIP_RESET="${SKIP_RESET:-0}"

# AgentVerse request defaults (mirrors UI payload fields)
# These are sent to Agent A (/agentverse) and propagate into LLM prompts.
AGENTVERSE_MAX_ITERATIONS="${AGENTVERSE_MAX_ITERATIONS:-3}"
AGENTVERSE_SUCCESS_THRESHOLD="${AGENTVERSE_SUCCESS_THRESHOLD:-90}"

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
    sed -n '3,50p' "$0" | sed 's/^# //' | sed 's/^#//'
    exit 0
}

while getopts "n:A:a:p:w:o:csbh" opt; do
    case $opt in
        n) ITERATIONS="$OPTARG" ;;
        b) BALANCED_MODE=1 ;;
        s) SWEEP_MODE=1 ;;
        A) FORCE_AGENT_COUNT="$OPTARG" ;;
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
if [[ $BALANCED_MODE -eq 1 && $SWEEP_MODE -eq 1 ]]; then
    echo "ERROR: -b (balanced) and -s (sweep) cannot be used together"
    exit 1
fi
if [[ $BALANCED_MODE -eq 1 && $CONTINUE_MODE -eq 1 ]]; then
    echo "ERROR: -b (balanced) and -c (continue) cannot be used together"
    exit 1
fi
if [[ $SWEEP_MODE -eq 1 && $CONTINUE_MODE -eq 1 ]]; then
    echo "ERROR: -s (sweep) and -c (continue) cannot be used together"
    exit 1
fi

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
    # Reset testbed: tear down any running deployment, then redeploy fresh.
    # Skip only when called from run_aggregated_experiment.sh (SKIP_RESET=1),
    # which manages its own reset before spawning this script.
    if [[ "${SKIP_RESET}" -ne 1 ]]; then
        echo "[*] Resetting testbed before experiment..."
        if [[ -x "$UNINSTALL_SCRIPT" ]]; then
            "$UNINSTALL_SCRIPT" --keep-logs
        else
            echo "[!] uninstall_testbed.sh not found at $UNINSTALL_SCRIPT — skipping reset"
        fi
        if [[ -x "$DEPLOY_SCRIPT" ]]; then
            "$DEPLOY_SCRIPT"
        else
            echo "[!] deploy.sh not found at $DEPLOY_SCRIPT — skipping deploy"
        fi
    fi

    EXPERIMENT_TS=$(date +%Y-%m-%d_%H-%M-%S)
    if [[ -n "$OUTPUT_DIR_OVERRIDE" ]]; then
        EXPERIMENT_DIR="$OUTPUT_DIR_OVERRIDE"
    else
        # Build directory prefix from active options
        _DIR_PREFIX=""
        if [[ $SWEEP_MODE -eq 1 ]]; then
            _DIR_PREFIX="sweep_"
        elif [[ $BALANCED_MODE -eq 1 ]]; then
            _DIR_PREFIX="balanced_"
        fi
        if [[ $SWEEP_MODE -eq 0 && -n "$FORCE_AGENT_COUNT" ]]; then
            _DIR_PREFIX="${_DIR_PREFIX}agents${FORCE_AGENT_COUNT}_"
        fi
        EXPERIMENT_DIR="$REPO_ROOT/data/agentverse/${_DIR_PREFIX}experiment_${EXPERIMENT_TS}"
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
# Finalization (idempotent): plots + DONE marker
# -------------------------------------------------------------------------
finalize_experiment() {
    echo ""
    echo "Generating plots..."
    if "$PYTHON" "$PLOT_SCRIPT" \
        --experiment-dir  "$EXPERIMENT_DIR" \
        --dashboard-json  "$DASHBOARD_JSON" 2>&1; then
        echo "  Plots saved → $EXPERIMENT_DIR/plots/"
    else
        echo "  WARNING: Plotting failed (check dependencies: pip install matplotlib pandas)"
    fi

    echo ""
    echo "Generating discussion structure comparison plots..."
    if "$PYTHON" "$STRUCTURE_COMPARE_SCRIPT" \
        "$EXPERIMENT_DIR" \
        --output-dir "$EXPERIMENT_DIR/plots/discussion_structure/" 2>&1; then
        echo "  Structure plots saved → $EXPERIMENT_DIR/plots/discussion_structure/"
    else
        echo "  WARNING: Discussion structure comparison plotting failed"
    fi

    echo ""
    echo "Generating discussion structure vs network metrics correlation..."
    if "$PYTHON" "$STRUCTURE_CORRELATE_SCRIPT" \
        "$EXPERIMENT_DIR" \
        --output-dir "$EXPERIMENT_DIR/plots/structure_correlation/" 2>&1; then
        echo "  Correlation plots saved → $EXPERIMENT_DIR/plots/structure_correlation/"
    else
        echo "  WARNING: Discussion structure correlation failed"
    fi

    echo ""
    echo "Generating IAT statistical analysis (KS tests, effect sizes)..."
    if "$PYTHON" "$IAT_STATS_SCRIPT" \
        --experiment-dir "$EXPERIMENT_DIR" \
        --output-dir     "$EXPERIMENT_DIR/plots/iat_analysis/" 2>&1; then
        echo "  IAT statistics saved → $EXPERIMENT_DIR/plots/iat_analysis/iat_statistics.{png,txt}"
    else
        echo "  WARNING: IAT statistical analysis failed"
    fi

    echo ""
    echo "Generating burst-removal and agent-count IAT analysis..."
    if "$PYTHON" "$BURST_AGENTS_SCRIPT" \
        --experiment-dir "$EXPERIMENT_DIR" \
        --output-dir     "$EXPERIMENT_DIR/plots/iat_analysis/" 2>&1; then
        echo "  Burst analysis saved → $EXPERIMENT_DIR/plots/iat_analysis/"
        echo "    burst_comparison.{png,txt}                   — H / V-agg / V-burst-removed comparison"
        echo "    vertical/"
        echo "      agent_count_iat.{png,txt}                  — vertical IAT by n_experts (raw + burst-removed)"
        echo "      burst_removed_ks_pairwise.{png,txt}        — vertical pairwise KS tests across n_experts"
        echo "      exponential_fit_nN.{png,txt}               — vertical exponential GOF for largest n_experts"
        echo "    horizontal/"
        echo "      h_agent_count_iat.{png,txt}                — horizontal IAT by n_experts (raw, no burst removal)"
        echo "      h_ks_pairwise.{png,txt}                    — horizontal pairwise KS tests across n_experts"
        echo "      exponential_fit_h_nN.{png,txt}             — horizontal exponential GOF for largest n_experts"
    else
        echo "  WARNING: Burst / agent-count IAT analysis failed"
    fi

    echo ""
    echo "Generating vertical raw vs aggregated IAT comparison..."
    if "$PYTHON" "$VERTICAL_RAW_AGG_SCRIPT" \
        --experiment-dir "$EXPERIMENT_DIR" \
        --output-dir     "$EXPERIMENT_DIR/plots/iat_analysis/" 2>&1; then
        echo "  Vertical comparison saved → $EXPERIMENT_DIR/plots/iat_analysis/vertical_raw_vs_aggregated.{png,txt}"
    else
        echo "  WARNING: Vertical raw vs aggregated analysis failed"
    fi

    echo ""
    echo "Generating LLM concurrency performance analysis..."
    if "$PYTHON" "$CONCURRENCY_PERF_SCRIPT" \
        --experiment-dir "$EXPERIMENT_DIR" \
        --output-dir     "$EXPERIMENT_DIR/plots/concurrency/" 2>&1; then
        echo "  Concurrency analysis saved → $EXPERIMENT_DIR/plots/concurrency/concurrency_performance.{png,txt}"
    else
        echo "  WARNING: Concurrency performance analysis failed"
    fi

    echo ""
    echo "Correlating LLM call log with Prometheus TCP telemetry..."
    if "$PYTHON" "$CORRELATE_METRICS_SCRIPT" \
        --call-log       "$REPO_ROOT/logs/llm_calls.jsonl" \
        --agentverse-dir "$REPO_ROOT/logs/agentverse" \
        --prometheus     "$PROMETHEUS_URL" \
        --output         "$EXPERIMENT_DIR/correlated.csv" 2>&1; then
        echo "  Correlated metrics saved → $EXPERIMENT_DIR/correlated.csv"
    else
        echo "  WARNING: Metric correlation failed (check logs/llm_calls.jsonl exists)"
    fi

    echo ""
    echo "================================================================"
    echo "  DONE"
    echo "  Results: $EXPERIMENT_DIR"
    echo "  Plots layout:"
    echo "    plots/dashboard/            — Grafana section PNGs + statistics.txt"
    echo "    plots/iat_analysis/         — IAT timing, fit, stats, burst, vertical analysis"
    echo "    plots/concurrency/          — throughput / latency / queue-wait vs agent count"
    echo "    plots/discussion_structure/ — horizontal vs vertical IAT comparisons"
    echo "    plots/structure_correlation/— structure vs network metric correlations"
    echo "================================================================"

    # Uninstall testbed so the LLM backend is not left running idle.
    echo ""
    echo "[*] Uninstalling testbed (keeping logs)..."
    if [[ -x "$UNINSTALL_SCRIPT" ]]; then
        "$UNINSTALL_SCRIPT" --keep-logs
    else
        echo "[!] uninstall_testbed.sh not found at $UNINSTALL_SCRIPT — skipping"
    fi
}

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

    # Continue mode is always single-structure (resume doesn't support balanced/sweep)
    COMBO_STRUCTURES=("")
    COMBO_AGENT_COUNTS=("")

    if [[ $START_ITER -gt $ITERATIONS ]]; then
        echo ""
        echo "Experiment already complete! All $TOTAL_RUNS runs finished."
        finalize_experiment
        exit 0
    fi

else
    # Fresh run
    START_ITER=1
    START_TASK_IDX=0
    RUN_COUNT=0

    # Build the list of (structure, agent_count) combos to cycle through per
    # iteration.  Using two parallel arrays because bash lacks tuples.
    COMBO_STRUCTURES=()
    COMBO_AGENT_COUNTS=()

    if [[ $SWEEP_MODE -eq 1 ]]; then
        # Solo baseline (0 agents — Agent A only, no discussion structure)
        COMBO_STRUCTURES+=("solo")
        COMBO_AGENT_COUNTS+=("0")
        # Full grid: horizontal × {1..5} then vertical × {1..5} = 10 combos
        # Total with solo: 11 combos
        for _s in horizontal vertical; do
            for _n in 1 2 3 4 5; do
                COMBO_STRUCTURES+=("$_s")
                COMBO_AGENT_COUNTS+=("$_n")
            done
        done
    elif [[ $BALANCED_MODE -eq 1 ]]; then
        COMBO_STRUCTURES=("horizontal" "vertical")
        COMBO_AGENT_COUNTS=("$FORCE_AGENT_COUNT" "$FORCE_AGENT_COUNT")
    else
        COMBO_STRUCTURES=("")
        COMBO_AGENT_COUNTS=("$FORCE_AGENT_COUNT")
    fi

    NUM_COMBOS=${#COMBO_STRUCTURES[@]}
    TOTAL_RUNS=$(( ITERATIONS * NUM_TASKS * NUM_COMBOS ))

    EXPERIMENT_START_MS=$(python3 -c "import time; print(int(time.time() * 1000))")
    EXPERIMENT_TS=$(date +%Y-%m-%d_%H-%M-%S)

    echo "================================================================"
    echo "  Agentic Traffic Experiment"
    if [[ $SWEEP_MODE -eq 1 ]]; then
        echo "  Mode       : SWEEP (solo + horizontal+vertical × agents 1–5 = 11 combos)"
        echo "  Combos     : ${COMBO_STRUCTURES[*]} / counts ${COMBO_AGENT_COUNTS[*]}"
    elif [[ $BALANCED_MODE -eq 1 ]]; then
        echo "  Mode       : BALANCED (50 % horizontal + 50 % vertical)"
        if [[ -n "$FORCE_AGENT_COUNT" ]]; then
            echo "  Agents     : FIXED = $FORCE_AGENT_COUNT sub-agent(s) per run"
        else
            echo "  Agents     : LLM-decided (up to MAX_PARALLEL_WORKERS)"
        fi
    else
        if [[ -n "$FORCE_AGENT_COUNT" ]]; then
            echo "  Agents     : FIXED = $FORCE_AGENT_COUNT sub-agent(s) per run"
        else
            echo "  Agents     : LLM-decided (up to MAX_PARALLEL_WORKERS)"
        fi
    fi
    echo "  Timestamp  : $EXPERIMENT_TS"
    echo "  Iterations : $ITERATIONS per task ($NUM_TASKS tasks, $NUM_COMBOS combo(s))"
    echo "  Total Runs : $TOTAL_RUNS"
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
    local max_iterations="$3"
    local success_threshold="$4"
    local force_structure="${5:-}"    # optional; "horizontal" or "vertical" for balanced mode
    local force_agent_count="${6:-}"  # optional; positive integer to fix sub-agent count
    python3 - "$task" "$url" "$max_iterations" "$success_threshold" "$force_structure" "$force_agent_count" <<'PYEOF'
import json, sys, urllib.request, urllib.error

task_text         = sys.argv[1]
base_url          = sys.argv[2].rstrip("/")
max_iterations    = sys.argv[3]
success_threshold = sys.argv[4]
force_structure   = sys.argv[5] if len(sys.argv) > 5 else ""
force_agent_count = sys.argv[6] if len(sys.argv) > 6 else ""

try:
    max_iterations = int(max_iterations)
except Exception:
    max_iterations = 3

try:
    success_threshold = int(float(success_threshold))
except Exception:
    success_threshold = 90

payload = {
    "task": task_text,
    "stream": False,
    "max_iterations": max_iterations,
    "success_threshold": success_threshold,
}
if force_structure in ("horizontal", "vertical"):
    payload["force_structure"] = force_structure
if force_agent_count not in ("", None):
    try:
        n = int(force_agent_count)
        if n >= 0:
            payload["force_agent_count"] = n
    except (TypeError, ValueError):
        pass

req = urllib.request.Request(
    f"{base_url}/agentverse",
    data=json.dumps(payload).encode(),
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

    for COMBO_IDX in "${!COMBO_STRUCTURES[@]}"; do
        FORCE_STRUCTURE="${COMBO_STRUCTURES[$COMBO_IDX]}"
        FORCE_AGENT_COUNT="${COMBO_AGENT_COUNTS[$COMBO_IDX]}"
        for TASK_IDX in $(seq "$FIRST_TASK_IDX" $(( NUM_TASKS - 1 ))); do
        TASK="${TASK_NAMES[$TASK_IDX]}"
        TASK_SLUG="${TASK_SLUGS[$TASK_IDX]}"
        RUN_COUNT=$(( RUN_COUNT + 1 ))

        RUN_TS=$(date +%Y-%m-%d_%H-%M-%S)
        RUN_START_S=$(date +%s)
        RUN_START_MS=$(python3 -c "import time; print(int(time.time() * 1000))")

        echo ""
        _RUN_LABEL="iter=$ITER"
        [[ -n "$FORCE_STRUCTURE" ]] && _RUN_LABEL="$_RUN_LABEL  structure=$FORCE_STRUCTURE"
        [[ -n "$FORCE_AGENT_COUNT" ]] && _RUN_LABEL="$_RUN_LABEL  agents=$FORCE_AGENT_COUNT"
        _RUN_LABEL="$_RUN_LABEL  task=$TASK_SLUG"
        echo "--- Run $RUN_COUNT / $TOTAL_RUNS  |  $_RUN_LABEL ---"
        echo "  Time  : $RUN_TS"
        echo "  Task  : ${TASK:0:80}..."

        # Send request
        RESPONSE=""
        if RESPONSE=$(send_agentverse_request "$TASK" "$AGENT_A_URL" "$AGENTVERSE_MAX_ITERATIONS" "$AGENTVERSE_SUCCESS_THRESHOLD" "$FORCE_STRUCTURE" "$FORCE_AGENT_COUNT" 2>/tmp/agentverse_err.txt); then
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

        # Create run directory: tasks/<timestamp>_<task-slug>_<task-id>
        RUN_DIR="$EXPERIMENT_DIR/tasks/${RUN_TS}_${TASK_SLUG}_${TASK_ID}"
        mkdir -p "$RUN_DIR"

        # Save response JSON (pretty-printed if possible)
        if ! echo "$RESPONSE" | python3 -m json.tool > "$RUN_DIR/response.json" 2>/dev/null; then
            echo "$RESPONSE" > "$RUN_DIR/response.json"
        fi

        # Save metadata (including per-iteration scores)
        python3 - "$RUN_DIR/response.json" <<PYEOF
import json
import sys

response_path = sys.argv[1]

try:
    with open(response_path) as f:
        response = json.load(f)
except Exception:
    response = {}

scores = []
for h in (response.get("iteration_history") or []):
    try:
        scores.append(int(h.get("evaluation", {}).get("score")))
    except Exception:
        scores.append(None)

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
    "forced_structure": "$FORCE_STRUCTURE" or None,
    "forced_agent_count": int("$FORCE_AGENT_COUNT") if "$FORCE_AGENT_COUNT" else None,
    "agentverse": {
        "max_iterations": int("$AGENTVERSE_MAX_ITERATIONS"),
        "success_threshold": int("$AGENTVERSE_SUCCESS_THRESHOLD"),
        "iteration_scores": scores,
    },
}

with open("$RUN_DIR/meta.json", "w") as f:
    json.dump(meta, f, indent=2)
PYEOF

        # Append to run log
        python3 - <<PYEOF
import json
record = {
    "run_dir":           "$RUN_DIR",
    "task_slug":         "$TASK_SLUG",
    "iteration":         $ITER,
    "task_id":           "$TASK_ID",
    "forced_structure":  "$FORCE_STRUCTURE" or None,
    "forced_agent_count": int("$FORCE_AGENT_COUNT") if "$FORCE_AGENT_COUNT" else None,
    "run_start_ms":      $RUN_START_MS,
    "run_end_ms":        $RUN_END_MS,
    "duration_s":        $DURATION_S,
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

        done  # TASK_IDX
        # After the first combo pass, subsequent passes start from task 0
        FIRST_TASK_IDX=0
    done  # COMBO_IDX
done  # ITER

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

finalize_experiment
