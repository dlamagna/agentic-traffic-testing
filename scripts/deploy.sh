#!/usr/bin/env bash
set -euo pipefail

#
# deploy.sh
# ---------
# Convenience script to bring up the core multi-agent + tool + LLM stack.
#
# Deployment modes (set DEPLOYMENT_MODE in infra/.env):
#   1) single (default): All containers on one Docker bridge network.
#   2) distributed: Separate Docker networks per logical node (Agent A, Agent B, LLM).
#   3) multi-vm: Services deployed to separate VMs via SSH (NODE1_HOST, NODE2_HOST, NODE3_HOST).
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/infra"
source "${ROOT_DIR}/scripts/deploy_ui.sh"

# Load .env file if it exists
ENV_FILE="${COMPOSE_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  # Export variables from .env (skip comments and empty lines)
  set -a
  source <(grep -v '^\s*#' "${ENV_FILE}" | grep -v '^\s*$')
  set +a
fi

# Default deployment mode
DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-single}"

# Select compose file based on deployment mode
get_compose_file() {
  case "${DEPLOYMENT_MODE}" in
    single)
      echo "${COMPOSE_DIR}/docker-compose.yml"
      ;;
    distributed)
      echo "${COMPOSE_DIR}/docker-compose.distributed.yml"
      ;;
    multi-vm)
      # multi-vm uses SSH, but falls back to single compose for reference
      echo "${COMPOSE_DIR}/docker-compose.yml"
      ;;
    *)
      echo "[!] Unknown DEPLOYMENT_MODE: ${DEPLOYMENT_MODE}" >&2
      echo "[!] Valid options: single, distributed, multi-vm" >&2
      exit 1
      ;;
  esac
}

COMPOSE_FILE="$(get_compose_file)"

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

echo "============================================================"
echo "Agentic Traffic Testbed - Deployment"
echo "============================================================"
echo "Deployment mode: ${DEPLOYMENT_MODE}"
echo "Compose file: ${COMPOSE_FILE}"
echo "============================================================"
echo

if [[ "${DEPLOYMENT_MODE}" == "multi-vm" ]] || { [[ -n "${NODE1_HOST}" && -n "${NODE2_HOST}" && -n "${NODE3_HOST}" ]]; }; then
  #########################################################################
  # Multi-VM mode: deploy to three different VMs via SSH.
  #
  # Expected:
  #   - The repo is cloned on each VM at REMOTE_REPO_DIR (same layout).
  #   - Passwordless SSH (or suitable auth) to NODE{1,2,3}_HOST.
  #########################################################################
  if [[ -z "${NODE1_HOST}" || -z "${NODE2_HOST}" || -z "${NODE3_HOST}" ]]; then
    echo "[!] DEPLOYMENT_MODE=multi-vm requires NODE1_HOST, NODE2_HOST, NODE3_HOST to be set."
    echo "[!] Set these in infra/.env or as environment variables."
    exit 1
  fi

  REMOTE_REPO_DIR="${REMOTE_REPO_DIR:-/home/${USER}/projects/testbed}"
  REMOTE_COMPOSE_DIR="${REMOTE_REPO_DIR}/infra"

  echo "[*] Multi-VM deployment detected."
  echo "    NODE1_HOST=${NODE1_HOST} (Agent A + Jaeger + Chat UI)"
  echo "    NODE2_HOST=${NODE2_HOST} (Agent B instances + MCP tools)"
  echo "    NODE3_HOST=${NODE3_HOST} (LLM backend - GPU node)"
  echo "    REMOTE_REPO_DIR=${REMOTE_REPO_DIR}"

  echo "[*] Deploying LLM backend on NODE3_HOST..."
  ssh "${NODE3_HOST}" "cd '${REMOTE_COMPOSE_DIR}' && docker compose up --build -d llm-backend"

  echo "[*] Deploying Agent B instances and MCP DB tool on NODE2_HOST..."
  ssh "${NODE2_HOST}" "cd '${REMOTE_COMPOSE_DIR}' && docker compose up --build -d agent-b agent-b-2 agent-b-3 agent-b-4 agent-b-5 mcp-tool-db"

  echo "[*] Deploying Agent A and Jaeger on NODE1_HOST..."
  ssh "${NODE1_HOST}" "cd '${REMOTE_COMPOSE_DIR}' && docker compose up --build -d agent-a jaeger"
  deploy_ui_multi_host "${NODE1_HOST}" "${REMOTE_COMPOSE_DIR}"

  echo "[*] Multi-VM deployment complete."
  echo "    - NODE1_HOST (Agent A + Jaeger): ${NODE1_HOST}"
  echo "    - NODE2_HOST (Agent B + tools) : ${NODE2_HOST}"
  echo "    - NODE3_HOST (LLM backend)     : ${NODE3_HOST}"
  echo "    Jaeger UI: http://${NODE1_HOST}:16686"
  echo "    Chat UI:   http://${NODE1_HOST}:3000"
  echo
  echo "[*] Final endpoint summary (via fetch_endpoints.sh):"
  bash "${ROOT_DIR}/scripts/fetch_endpoints.sh"
  wait_for_llm "http://${NODE3_HOST}:8000/health" || true
  run_health_check

elif [[ "${DEPLOYMENT_MODE}" == "distributed" ]]; then
  #########################################################################
  # Distributed mode: separate Docker networks on local machine.
  #########################################################################
  cd "${COMPOSE_DIR}"

  echo "[*] Distributed deployment: separate Docker networks per logical node."
  echo "    Networks:"
  echo "      - agent_a_network (172.20.0.0/24): Agent A"
  echo "      - agent_b_network (172.21.0.0/24): Agent B instances + MCP tools"
  echo "      - llm_network (172.22.0.0/24): LLM backend"
  echo "      - inter_agent_network (172.23.0.0/24): Cross-service communication"
  echo

  echo "[*] Building and starting services with distributed network topology..."
  docker compose -f docker-compose.distributed.yml up --build -d \
    llm-backend \
    agent-a \
    agent-b agent-b-2 agent-b-3 agent-b-4 agent-b-5 \
    mcp-tool-db \
    chat-ui \
    jaeger

  # Optional: Deploy monitoring stack
  if [[ "${ENABLE_MONITORING:-0}" == "1" ]]; then
    echo
    echo "[*] Deploying monitoring stack (Prometheus + Grafana + cAdvisor)..."
    docker compose -f docker-compose.monitoring.distributed.yml up -d
  fi

  echo "[*] Current container status:"
  docker compose -f docker-compose.distributed.yml ps

  echo
  echo "[*] Network topology:"
  echo "    Agent A (172.23.0.10) <--inter_agent_network--> Agent B instances (172.23.0.20-24)"
  echo "    All agents <--inter_agent_network--> LLM backend (172.23.0.30)"
  echo
  echo "[*] Final endpoint summary (via fetch_endpoints.sh):"
  bash "${ROOT_DIR}/scripts/fetch_endpoints.sh"

  if [[ "${ENABLE_MONITORING:-0}" == "1" ]]; then
    echo
    echo "[*] Monitoring endpoints:"
    echo "    Grafana:    http://localhost:3001 (admin/admin)"
    echo "    Prometheus: http://localhost:9090"
    echo "    cAdvisor:   http://localhost:8080"
  fi

  wait_for_llm "http://localhost:8000/health" || true
  run_health_check

  # Optional: Apply network emulation if enabled
  if [[ "${ENABLE_NETWORK_EMULATION:-0}" == "1" ]]; then
    echo
    echo "[*] Network emulation is enabled. Applying tc netem rules..."
    bash "${ROOT_DIR}/scripts/apply_network_emulation.sh" || echo "[!] Network emulation script not found or failed."
  fi

else
  #########################################################################
  # Single mode: all containers on one Docker bridge network (default).
  #########################################################################
  cd "${COMPOSE_DIR}"

  echo "[*] Single-network deployment: all containers on one bridge network."
  echo "[*] Building and starting services..."
  docker compose up --build -d llm-backend agent-a agent-b agent-b-2 agent-b-3 agent-b-4 agent-b-5 mcp-tool-db chat-ui jaeger
  deploy_ui_single_host

  # Optional: Deploy monitoring stack
  if [[ "${ENABLE_MONITORING:-0}" == "1" ]]; then
    echo
    echo "[*] Deploying monitoring stack (Prometheus + Grafana + cAdvisor)..."
    docker compose -f docker-compose.monitoring.yml up -d
  fi

  echo "[*] Current container status:"
  docker compose ps

  echo "[*] Final endpoint summary (via fetch_endpoints.sh):"
  bash "${ROOT_DIR}/scripts/fetch_endpoints.sh"

  if [[ "${ENABLE_MONITORING:-0}" == "1" ]]; then
    echo
    echo "[*] Monitoring endpoints:"
    echo "    Grafana:    http://localhost:3001 (admin/admin)"
    echo "    Prometheus: http://localhost:9090"
    echo "    cAdvisor:   http://localhost:8080"
  fi

  wait_for_llm "http://localhost:8000/health" || true
  run_health_check
fi


