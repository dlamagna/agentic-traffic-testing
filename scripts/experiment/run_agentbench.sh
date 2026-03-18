#!/usr/bin/env bash
# Run an AgentBench task type against Agent A.
#
# Two modes:
#   Standalone (default): loads tasks from local AgentBench data files,
#     sends to Agent A as single-shot prompts, scores offline.
#     No AgentBench task servers required.
#
#   Controller mode (AGENTBENCH_URL set): connects to a running AgentBench
#     controller for full multi-turn evaluation with environment-side scoring.
#
# Usage:
#   ./scripts/experiment/run_agentbench.sh [--task-type db] [--max-tasks 50] [-v]
#
# Environment variables (via infra/.env.experiment):
#   AGENTBENCH_ROOT  — path to cloned AgentBench repo
#   AGENTBENCH_URL   — controller base URL (enables controller mode)
#   AGENT_A_URL      — Agent A /task endpoint
#
# AgentBench upstream: https://github.com/THUDM/AgentBench

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# ---------------------------------------------------------------------------
# Logging: tee all output (stdout + stderr) to logs/agentbench_<timestamp>.log
# ---------------------------------------------------------------------------
LOGS_DIR="${REPO_ROOT}/logs"
mkdir -p "${LOGS_DIR}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${LOGS_DIR}/agentbench_${TIMESTAMP}.log"

# Announce log destination to the terminal before redirecting.
echo "[run_agentbench] Logging to ${LOG_FILE}"

# Re-exec with tee only on the first invocation (avoid infinite loop).
if [ -z "${_AGENTBENCH_LOGGING:-}" ]; then
  export _AGENTBENCH_LOGGING=1
  exec > >(tee -a "${LOG_FILE}") 2>&1
fi

# Load experiment env file if present (infra/.env.experiment).
ENV_EXPERIMENT="${REPO_ROOT}/infra/.env.experiment"
if [ -f "${ENV_EXPERIMENT}" ]; then
  set -o allexport
  # shellcheck source=/dev/null
  source <(grep -v '^\s*#' "${ENV_EXPERIMENT}" | grep -v '^\s*$')
  set +o allexport
fi

# AGENTBENCH_ROOT: root of the cloned AgentBench repo.
# Used in standalone mode to read task data files.
AGENTBENCH_ROOT="${AGENTBENCH_ROOT:-"${REPO_ROOT}/../AgentBench"}"

echo "[run_agentbench] Repo root:       ${REPO_ROOT}"
echo "[run_agentbench] AGENTBENCH_ROOT: ${AGENTBENCH_ROOT}"

if [ ! -d "${AGENTBENCH_ROOT}" ]; then
  echo "[run_agentbench] AgentBench repo not found at ${AGENTBENCH_ROOT}, cloning..."
  git clone https://github.com/THUDM/AgentBench.git "${AGENTBENCH_ROOT}"
else
  echo "[run_agentbench] AgentBench repo already present, skipping clone."
fi

export AGENTBENCH_ROOT

# In controller mode, verify the controller is reachable.
if [ -n "${AGENTBENCH_URL:-}" ]; then
  echo "[run_agentbench] Controller mode: AGENTBENCH_URL=${AGENTBENCH_URL}"
  API_URL="${AGENTBENCH_URL%/}/api"
  if ! curl -sf "${API_URL}/list_workers" > /dev/null 2>&1; then
    echo "[run_agentbench] WARNING: AgentBench controller not reachable at ${API_URL}"
    echo "[run_agentbench]   Falling back to standalone mode."
    unset AGENTBENCH_URL
  else
    echo "[run_agentbench] Controller is reachable."
  fi
else
  echo "[run_agentbench] Standalone mode (no AGENTBENCH_URL set)."
fi

cd "${REPO_ROOT}"

echo "[run_agentbench] Running AgentBench against Agent A..."

python -m benchmarks.agentbench.runner "$@"

echo "[run_agentbench] Done."
