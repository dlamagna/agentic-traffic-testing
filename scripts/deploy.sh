#!/usr/bin/env bash
set -euo pipefail

#
# deploy.sh
# ---------
# Convenience script to bring up the core multi-agent + tool + LLM stack.
#
# It supports two modes:
#   1) Single-host (default): all services run as containers on the local host.
#   2) Multi-node via SSH: each logical node runs on a different VM, if you
#      set NODE1_HOST, NODE2_HOST, NODE3_HOST (and optionally REMOTE_REPO_DIR).
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/infra"
source "${ROOT_DIR}/scripts/deploy_ui.sh"

run_health_check() {
  local health_script="${ROOT_DIR}/scripts/health_check.py"
  if [[ ! -f "${health_script}" ]]; then
    echo "[!] Health check script not found at ${health_script}"
    return 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[!] python3 is not installed; skipping health check."
    return 1
  fi
  echo
  echo "[*] Running health check..."
  python3 "${health_script}" --docker-compose-dir "${COMPOSE_DIR}" || true
}

wait_for_llm() {
  local url="$1"
  local timeout_seconds="${2:-600}"
  local interval_seconds="${3:-5}"
  local start_ts
  start_ts="$(date +%s)"

  echo "[*] Waiting for LLM backend to be healthy at ${url}..."
  echo "    (this can take a few minutes while the model loads)"
  while true; do
    if python3 - <<PY >/dev/null 2>&1; then
import urllib.request
urllib.request.urlopen("${url}", timeout=2).read()
PY
      echo "[*] LLM backend is healthy."
      return 0
    fi

    local now_ts
    now_ts="$(date +%s)"
    local elapsed
    elapsed=$(( now_ts - start_ts ))
    echo "[*] Still waiting for LLM backend... (${elapsed}s elapsed)"
    if (( now_ts - start_ts >= timeout_seconds )); then
      echo "[!] Timed out waiting for LLM backend at ${url}."
      return 1
    fi
    sleep "${interval_seconds}"
  done
}

if ! command -v docker >/dev/null 2>&1; then
  echo "[!] docker is not installed or not on PATH."
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "[!] docker compose is not available. Please install Docker Compose v2."
  exit 1
fi

NODE1_HOST="${NODE1_HOST:-}"
NODE2_HOST="${NODE2_HOST:-}"
NODE3_HOST="${NODE3_HOST:-}"

if [[ -n "${NODE1_HOST}" && -n "${NODE2_HOST}" && -n "${NODE3_HOST}" ]]; then
  #########################################################################
  # Multi-node mode: deploy to three different VMs via SSH.
  #
  # Expected:
  #   - The repo is cloned on each VM at REMOTE_REPO_DIR (same layout).
  #   - Passwordless SSH (or suitable auth) to NODE{1,2,3}_HOST.
  #########################################################################
  REMOTE_REPO_DIR="${REMOTE_REPO_DIR:-/home/${USER}/projects/testbed}"
  REMOTE_COMPOSE_DIR="${REMOTE_REPO_DIR}/infra"

  echo "[*] Multi-node deployment detected."
  echo "    NODE1_HOST=${NODE1_HOST} (Agent A + Jaeger)"
  echo "    NODE2_HOST=${NODE2_HOST} (Agent B + tools)"
  echo "    NODE3_HOST=${NODE3_HOST} (LLM backend)"
  echo "    REMOTE_REPO_DIR=${REMOTE_REPO_DIR}"

  echo "[*] Deploying Agent A and Jaeger on NODE1_HOST..."
  ssh "${NODE1_HOST}" "cd '${REMOTE_COMPOSE_DIR}' && docker compose up --build -d agent-a jaeger"
  deploy_ui_multi_host "${NODE1_HOST}" "${REMOTE_COMPOSE_DIR}"

  echo "[*] Deploying Agent B and MCP DB tool on NODE2_HOST..."
  ssh "${NODE2_HOST}" "cd '${REMOTE_COMPOSE_DIR}' && docker compose up --build -d agent-b mcp-tool-db"

  echo "[*] Deploying LLM backend on NODE3_HOST..."
  ssh "${NODE3_HOST}" "cd '${REMOTE_COMPOSE_DIR}' && docker compose up --build -d llm-backend"

  echo "[*] Multi-node deployment complete."
  echo "    - NODE1_HOST (Agent A + Jaeger): ${NODE1_HOST}"
  echo "    - NODE2_HOST (Agent B + tools) : ${NODE2_HOST}"
  echo "    - NODE3_HOST (LLM backend)     : ${NODE3_HOST}"
  echo "    Jaeger UI is exposed on NODE1_HOST at http://<NODE1_HOST>:16686"
  echo "    Chat UI is exposed on NODE1_HOST at http://<NODE1_HOST>:3000"
  echo
  echo "[*] Final endpoint summary (via fetch_endpoints.sh):"
  bash "${ROOT_DIR}/scripts/fetch_endpoints.sh"
  wait_for_llm "http://${NODE3_HOST}:8000/health" || true
  run_health_check
else
  #########################################################################
  # Single-host mode: all containers on the local machine.
  #########################################################################
  cd "${COMPOSE_DIR}"

  echo "[*] Single-host deployment: building and starting llm-backend, agent-a, agent-b, mcp-tool-db, and jaeger..."
  docker compose up --build -d llm-backend agent-a agent-b mcp-tool-db jaeger
  deploy_ui_single_host

  echo "[*] Current container status:"
  docker compose ps

  # echo "[*] Services should now be reachable on the host:"
  # echo "    - LLM backend : http://localhost:8000/chat"
  # echo "    - Agent A     : http://localhost:8101/task"
  # echo "    - Agent B     : http://localhost:8102/subtask"
  # echo "    - MCP DB tool : http://localhost:8201/query"
  # echo "    - Jaeger UI   : http://localhost:16686"
  # echo "    - Chat UI     : http://localhost:3000"
  # echo
  echo "[*] Final endpoint summary (via fetch_endpoints.sh):"
  bash "${ROOT_DIR}/scripts/fetch_endpoints.sh"
  wait_for_llm "http://localhost:8000/health" || true
  run_health_check
fi


