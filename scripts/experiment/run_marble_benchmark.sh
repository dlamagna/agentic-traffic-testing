#!/usr/bin/env bash
# run_marble_benchmark.sh
#
# Wrapper script for the MARBLE (MultiAgentBench) benchmark runner.
# Loads configuration from infra/.env.experiment (if present), validates the
# MARBLE repo is available, then delegates to benchmarks/marble/runner.py.
#
# Usage
# -----
#   ./scripts/experiment/run_marble_benchmark.sh [runner args...]
#
# Examples
# --------
#   # Run 5 research tasks with graph topology (default)
#   ./scripts/experiment/run_marble_benchmark.sh \
#       --domain research --max-tasks 5 --verbose
#
#   # Run all coding tasks with star topology
#   ./scripts/experiment/run_marble_benchmark.sh \
#       --domain coding --topology star
#
#   # Run bargaining tasks with tree topology, skip scoring
#   ./scripts/experiment/run_marble_benchmark.sh \
#       --domain bargaining --topology tree --skip-judge --max-tasks 3
#
#   # Run specific task IDs across all topologies
#   for topo in star chain tree graph; do
#       ./scripts/experiment/run_marble_benchmark.sh \
#           --domain research --topology "$topo" --task-ids 1,2,3 \
#           --output "logs/benchmarks/marble_research_${topo}.jsonl"
#   done
#
# Environment variables (set in infra/.env.experiment or shell)
# --------------------------------------------------------------
#   AGENT_A_URL              Base URL for Agent A (default: http://localhost:8101)
#   AGENT_B_URLS             Comma-separated Agent B endpoints
#   MARBLE_ROOT              Path to cloned MARBLE repo (default: ../MARBLE)
#   MARBLE_DOMAIN            Default domain (default: research)
#   MARBLE_TOPOLOGY          Force topology (default: from task config)
#   MARBLE_MAX_ITERATIONS    Max coordination iterations (default: 3)
#   MARBLE_TIMEOUT_SECONDS   Per-request timeout (default: 300)

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
    echo "[run_marble_benchmark] Loaded ${ENV_FILE}"
fi

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
AGENT_A_URL="${AGENT_A_URL:-http://localhost:8101}"
MARBLE_ROOT="${MARBLE_ROOT:-$(cd "${REPO_ROOT}/.." && pwd)/MARBLE}"

export AGENT_A_URL
export MARBLE_ROOT

# ---------------------------------------------------------------------------
# Validate MARBLE repo
# ---------------------------------------------------------------------------
if [[ ! -d "${MARBLE_ROOT}" ]]; then
    echo "[run_marble_benchmark] ERROR: MARBLE repo not found at ${MARBLE_ROOT}"
    echo "  Clone it with: git clone https://github.com/ulab-uiuc/MARBLE.git ${MARBLE_ROOT}"
    exit 1
fi

if [[ ! -d "${MARBLE_ROOT}/multiagentbench" ]]; then
    echo "[run_marble_benchmark] ERROR: MARBLE repo at ${MARBLE_ROOT} looks incomplete (no multiagentbench/)."
    exit 1
fi

# List available domains
AVAILABLE_DOMAINS=""
for domain_dir in "${MARBLE_ROOT}"/multiagentbench/*/; do
    domain_name="$(basename "${domain_dir}")"
    jsonl="${domain_dir}${domain_name}_main.jsonl"
    if [[ -f "${jsonl}" ]]; then
        count=$(wc -l < "${jsonl}" | tr -d ' ')
        AVAILABLE_DOMAINS="${AVAILABLE_DOMAINS}  ${domain_name}: ${count} tasks\n"
    fi
done

echo "[run_marble_benchmark] MARBLE repo: ${MARBLE_ROOT}"
echo "[run_marble_benchmark] Agent A: ${AGENT_A_URL}"
echo "[run_marble_benchmark] Available domains:"
echo -e "${AVAILABLE_DOMAINS}"
echo "[run_marble_benchmark] Args: $*"
echo ""

# ---------------------------------------------------------------------------
# Check Agent A is reachable
# ---------------------------------------------------------------------------
if command -v curl &>/dev/null; then
    if ! curl -sf --max-time 5 "${AGENT_A_URL}/task" -X OPTIONS >/dev/null 2>&1; then
        echo "[run_marble_benchmark] WARNING: Agent A at ${AGENT_A_URL} may not be reachable."
        echo "  Ensure the stack is running: cd infra && docker compose up -d"
        echo "  Continuing anyway..."
        echo ""
    fi
fi

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
cd "${REPO_ROOT}"
python -m benchmarks.marble.runner "$@"
