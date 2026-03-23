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
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/infra"
source "${ROOT_DIR}/scripts/deploy/deploy_ui.sh"

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
    *)
      echo "[!] Unknown DEPLOYMENT_MODE: ${DEPLOYMENT_MODE}" >&2
      echo "[!] Valid options: single, distributed" >&2
      exit 1
      ;;
  esac
}

COMPOSE_FILE="$(get_compose_file)"

run_health_check() {
  local health_script="${ROOT_DIR}/scripts/monitoring/health_check.py"
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

# Retry wrapper for docker compose up --build.
# Usage: docker_compose_up_with_retry <max_attempts> <compose args...>
docker_compose_up_with_retry() {
  local max_attempts="$1"; shift
  local attempt=1
  while true; do
    if docker compose "$@"; then
      return 0
    fi
    if (( attempt >= max_attempts )); then
      echo "[!] docker compose failed after ${max_attempts} attempt(s). Giving up."
      return 1
    fi
    echo "[!] docker compose failed (attempt ${attempt}/${max_attempts}). Retrying in 30s..."
    sleep 30
    attempt=$(( attempt + 1 ))
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

echo "============================================================"
echo "Agentic Traffic Testbed - Deployment"
echo "============================================================"
echo "Deployment mode: ${DEPLOYMENT_MODE}"
echo "Compose file: ${COMPOSE_FILE}"
echo "============================================================"
echo

if [[ "${DEPLOYMENT_MODE}" == "distributed" ]]; then
  #########################################################################
  # Distributed mode: separate Docker networks on local machine.
  #########################################################################
  cd "${COMPOSE_DIR}"

  echo "[*] Distributed deployment: separate Docker networks per logical node."
  echo "    Networks:"
  echo "      - agent_a_network (172.20.0.0/24): Agent A"
  echo "      - agent_b_network (172.21.0.0/24): Agent B instances"
  echo "      - llm_network (172.22.0.0/24): LLM backend"
  echo "      - inter_agent_network (172.23.0.0/24): Cross-service communication"
  echo "      - tools_network (172.24.0.0/24): MCP tools/servers"
  echo

  echo "[*] Building and starting services with distributed network topology..."
  docker_compose_up_with_retry 3 -f docker-compose.distributed.yml up --build -d \
    llm-backend \
    agent-a \
    agent-b agent-b-2 agent-b-3 agent-b-4 agent-b-5 \
    mcp-tool-db \
    chat-ui \
    jaeger

  # Optional: Deploy monitoring stack
  if [[ "${ENABLE_MONITORING:-0}" == "1" ]]; then
    echo
    echo "[*] Deploying monitoring stack (Prometheus + Grafana)..."
    docker_compose_up_with_retry 3 -f docker-compose.monitoring.distributed.yml up --build -d prometheus grafana docker-mapping-exporter
    echo "[*] Ensuring host-mode cAdvisor is running on :8080..."
    if docker ps --format '{{.Names}}' | grep -q '^cadvisor-host$'; then
      echo "    cadvisor-host already running; skipping."
    else
      echo "    Starting cadvisor-host (no sudo required)..."
      docker run -d \
        --name cadvisor-host \
        --net=host \
        --privileged \
        -v /:/rootfs:ro \
        -v /var/run:/var/run:ro \
        -v /sys:/sys:ro \
        -v /var/lib/docker/:/var/lib/docker:ro \
        -v /dev/disk/:/dev/disk:ro \
        gcr.io/cadvisor/cadvisor:v0.47.2 \
          --docker_only=true \
          --store_container_labels=true || \
        echo "    [!] Failed to start cadvisor-host automatically. See docs/monitoring.md to start it manually."
    fi
    echo "[*] Ensuring TCP metrics collector (tcpdump + tcp_metrics_collector.py) is running..."
    if pgrep -f "tcp_metrics_collector.py" >/dev/null 2>&1; then
      echo "    tcp_metrics_collector.py already running; skipping."
    else
      mkdir -p "${ROOT_DIR}/logs"
      echo "    Starting tcpdump + tcp_metrics_collector via scripts/monitoring/run_tcpdump.sh..."
      (cd "${ROOT_DIR}" && bash scripts/monitoring/run_tcpdump.sh >> logs/tcp_metrics_collector.log 2>&1 &) || \
        echo "    [!] Failed to start tcp_metrics_collector.py automatically. See docs/monitoring.md to start it manually."
    fi
    echo "[*] Ensuring TCP metrics collector is running on host (for service-level network metrics)..."
    if pgrep -f "tcp_metrics_collector.py" >/dev/null 2>&1; then
      echo "    tcp_metrics_collector.py already running; skipping."
    else
      mkdir -p "${ROOT_DIR}/logs"
      echo "    Starting tcp_metrics_collector.py (you may be prompted for sudo for tcpdump)..."
      (cd "${ROOT_DIR}" && python3 scripts/monitoring/tcp_metrics_collector.py --sudo-tcpdump >> logs/tcp_metrics_collector.log 2>&1 &) || \
        echo "    [!] Failed to start tcp_metrics_collector.py automatically. See docs/monitoring.md to start it manually."
    fi
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
    bash "${ROOT_DIR}/scripts/traffic/apply_network_emulation.sh" || echo "[!] Network emulation script not found or failed."
  fi

else
  #########################################################################
  # Single mode: all containers on one Docker bridge network (default).
  #########################################################################
  cd "${COMPOSE_DIR}"

  echo "[*] Single-network deployment: all containers on one bridge network."
  echo "[*] Building and starting services..."
  docker_compose_up_with_retry 3 up --build -d llm-backend agent-a agent-b agent-b-2 agent-b-3 agent-b-4 agent-b-5 mcp-tool-db chat-ui jaeger
  deploy_ui_single_host

  # Optional: Deploy monitoring stack
  if [[ "${ENABLE_MONITORING:-0}" == "1" ]]; then
    echo
    echo "[*] Deploying monitoring stack (Prometheus + Grafana)..."
    docker_compose_up_with_retry 3 -f docker-compose.monitoring.yml up --build -d prometheus grafana docker-mapping-exporter
    echo "[*] Ensuring host-mode cAdvisor is running on :8080..."
    if docker ps --format '{{.Names}}' | grep -q '^cadvisor-host$'; then
      echo "    cadvisor-host already running; skipping."
    else
      echo "    Starting cadvisor-host (no sudo required)..."
      docker run -d \
        --name cadvisor-host \
        --net=host \
        --privileged \
        -v /:/rootfs:ro \
        -v /var/run:/var/run:ro \
        -v /sys:/sys:ro \
        -v /var/lib/docker/:/var/lib/docker:ro \
        -v /dev/disk/:/dev/disk:ro \
        gcr.io/cadvisor/cadvisor:v0.47.2 \
          --docker_only=true \
          --store_container_labels=true || \
        echo "    [!] Failed to start cadvisor-host automatically. See docs/monitoring.md to start it manually."
    fi
    echo "[*] Ensuring TCP metrics collector (tcpdump + tcp_metrics_collector.py) is running..."
    if pgrep -f "tcp_metrics_collector.py" >/dev/null 2>&1; then
      echo "    tcp_metrics_collector.py already running; skipping."
    else
      mkdir -p "${ROOT_DIR}/logs"
      echo "    Starting tcpdump + tcp_metrics_collector via scripts/monitoring/run_tcpdump.sh..."
      (cd "${ROOT_DIR}" && bash scripts/monitoring/run_tcpdump.sh >> logs/tcp_metrics_collector.log 2>&1 &) || \
        echo "    [!] Failed to start tcp_metrics_collector.py automatically. See docs/monitoring.md to start it manually."
    fi
    echo "[*] Ensuring TCP metrics collector is running on host (for service-level network metrics)..."
    if pgrep -f "tcp_metrics_collector.py" >/dev/null 2>&1; then
      echo "    tcp_metrics_collector.py already running; skipping."
    else
      mkdir -p "${ROOT_DIR}/logs"
      echo "    Starting tcp_metrics_collector.py (you may be prompted for sudo for tcpdump)..."
      (cd "${ROOT_DIR}" && python3 scripts/monitoring/tcp_metrics_collector.py --sudo-tcpdump >> logs/tcp_metrics_collector.log 2>&1 &) || \
        echo "    [!] Failed to start tcp_metrics_collector.py automatically. See docs/monitoring.md to start it manually."
    fi
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
    echo
    echo "[*] To start the TCP metrics collector for service-level network metrics, run (in a separate terminal):"
    echo "    cd ${ROOT_DIR}"
    echo "    sudo tcpdump -i br-df4088ff2909 -l -n -tt tcp and net 172.23.0.0/24 \\"
    echo "      | python3 scripts/monitoring/tcp_metrics_collector.py --read-stdin"
  fi

  wait_for_llm "http://localhost:8000/health" || true
  run_health_check
fi
