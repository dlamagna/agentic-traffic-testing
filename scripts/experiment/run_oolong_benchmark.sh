#!/usr/bin/env bash
# Run the OOLONG-synth benchmark against Agent A.
#
# Data is loaded automatically from the HuggingFace Hub (oolongbench/oolong-synth).
# Scoring uses the official OOLONG eval_helpers from the cloned repo at OOLONG_ROOT.
#
# Usage:
#   ./scripts/experiment/run_oolong_benchmark.sh [--scenario agentic_simple] \
#       [--dataset trec_coarse] [--max-tasks 50] [--context-size 20000]
#
# OOLONG upstream: https://github.com/abertsch72/oolong

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Load experiment env file if present (infra/.env.experiment).
# This is the right place for OOLONG_ROOT, AGENT_A_URL, etc.
# Variables already set in the environment take precedence.
ENV_EXPERIMENT="${REPO_ROOT}/infra/.env.experiment"
if [ -f "${ENV_EXPERIMENT}" ]; then
  set -o allexport
  # shellcheck source=/dev/null
  source <(grep -v '^\s*#' "${ENV_EXPERIMENT}" | grep -v '^\s*$')
  set +o allexport
fi

# OOLONG_ROOT: root of the cloned OOLONG repo.  Only used for scoring
# (eval_helpers.py); data is fetched from HuggingFace.
# Override via environment variable, .env.experiment, or default to sibling checkout.
OOLONG_ROOT="${OOLONG_ROOT:-"${REPO_ROOT}/../oolong"}"

echo "[run_oolong_benchmark] Repo root:   ${REPO_ROOT}"
echo "[run_oolong_benchmark] OOLONG_ROOT: ${OOLONG_ROOT}"

if [ ! -d "${OOLONG_ROOT}" ]; then
  echo "[run_oolong_benchmark] OOLONG repo not found at ${OOLONG_ROOT}, cloning..."
  git clone https://github.com/abertsch72/oolong.git "${OOLONG_ROOT}"
else
  echo "[run_oolong_benchmark] OOLONG repo already present, skipping clone."
fi

export OOLONG_ROOT

cd "${REPO_ROOT}"

echo "[run_oolong_benchmark] Running OOLONG-synth benchmark against Agent A..."

python -m benchmarks.oolong.runner "$@"

echo "[run_oolong_benchmark] Done."
