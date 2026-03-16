#!/usr/bin/env bash

set -euo pipefail

# Resolve repository root (two levels up from this script).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Default location for the OOLONG checkout can be overridden via OOLONG_ROOT.
OOLONG_ROOT="${OOLONG_ROOT:-"${REPO_ROOT}/../OOLONG"}"

echo "[run_oolong_benchmark] Using repo root: ${REPO_ROOT}"
echo "[run_oolong_benchmark] Using OOLONG_ROOT: ${OOLONG_ROOT}"

if [ ! -d "${OOLONG_ROOT}" ]; then
  echo "[run_oolong_benchmark] OOLONG repo not found at ${OOLONG_ROOT}, cloning..."
  git clone https://github.com/yale-nlp/OOLONG.git "${OOLONG_ROOT}"
else
  echo "[run_oolong_benchmark] OOLONG repo already present, skipping clone."
fi

export OOLONG_ROOT

cd "${REPO_ROOT}"

echo "[run_oolong_benchmark] Running OOLONG trec_coarse benchmark against Agent A..."

python -m benchmarks.oolong.runner "$@"

echo "[run_oolong_benchmark] Done."

