#!/usr/bin/env bash
# run_rlm_benchmark.sh
#
# Wrapper script for the RLM benchmark runner.
# Loads configuration from infra/.env.experiment (if present), ensures the
# RLM repo is available, then delegates to benchmarks/rlm/runner.py.
#
# Usage
# -----
#   ./scripts/experiment/run_rlm_benchmark.sh [runner args...]
#
# Examples
# --------
#   # Baseline: no REPL, plain LLM call
#   ./scripts/experiment/run_rlm_benchmark.sh --scenario rlm_simple --max-tasks 10
#
#   # Recursive REPL with up to 3 Agent B workers available as tools
#   ./scripts/experiment/run_rlm_benchmark.sh \
#       --scenario rlm_recursive \
#       --max-depth 1 \
#       --agent-count 3 \
#       --max-tasks 50
#
#   # Parallel fan-out scenario
#   ./scripts/experiment/run_rlm_benchmark.sh \
#       --scenario rlm_parallel \
#       --agent-count 5 \
#       --max-tasks 50
#
#   # Custom task file
#   ./scripts/experiment/run_rlm_benchmark.sh \
#       --tasks-file data/my_tasks.jsonl \
#       --no-oolong-scorer \
#       --output logs/benchmarks/rlm_custom.jsonl
#
# Environment variables (set in infra/.env.experiment or shell)
# --------------------------------------------------------------
#   AGENT_A_URL          Base URL for Agent A (default: http://localhost:8101)
#   RLM_URL              Full /rlm endpoint (default: ${AGENT_A_URL}/rlm)
#   RLM_ROOT             Path to the cloned rlm repo (default: /home/dlamagna/projects/rlm)
#   LLM_BASE_URL         vLLM OpenAI-compatible base URL (default: derived from LLM_SERVER_URL)
#   MODEL_NAME           Model name served by vLLM
#   RLM_MAX_DEPTH        Default max recursion depth (default: 1)
#   RLM_MAX_ITERATIONS   Default max REPL iterations (default: 30)
#   RLM_AGENT_COUNT      Default number of Agent B workers to expose (default: 0)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# ---------------------------------------------------------------------------
# Load experiment config
# ---------------------------------------------------------------------------
ENV_FILE="${REPO_ROOT}/infra/.env.experiment"
if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "${ENV_FILE}"
    set +a
    echo "[run_rlm_benchmark] Loaded ${ENV_FILE}"
fi

# ---------------------------------------------------------------------------
# Defaults (can be overridden by .env.experiment or shell)
# ---------------------------------------------------------------------------
AGENT_A_URL="${AGENT_A_URL:-http://localhost:8101}"
RLM_URL="${RLM_URL:-${AGENT_A_URL}/rlm}"
RLM_ROOT="${RLM_ROOT:-/home/dlamagna/projects/rlm}"

export RLM_ROOT
export RLM_URL

# ---------------------------------------------------------------------------
# Validate RLM repo
# ---------------------------------------------------------------------------
if [[ ! -d "${RLM_ROOT}" ]]; then
    echo "[run_rlm_benchmark] ERROR: RLM repo not found at ${RLM_ROOT}"
    echo "  Clone it with: git clone https://github.com/alexzhang13/rlm.git ${RLM_ROOT}"
    exit 1
fi

if [[ ! -f "${RLM_ROOT}/rlm/core/rlm.py" ]]; then
    echo "[run_rlm_benchmark] ERROR: RLM repo at ${RLM_ROOT} looks incomplete."
    exit 1
fi

echo "[run_rlm_benchmark] RLM repo: ${RLM_ROOT}"
echo "[run_rlm_benchmark] /rlm endpoint: ${RLM_URL}"
echo "[run_rlm_benchmark] Args: $*"
echo ""

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
cd "${REPO_ROOT}"
python -m benchmarks.rlm.runner --rlm-url "${RLM_URL}" "$@"
