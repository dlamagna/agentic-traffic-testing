#!/usr/bin/env bash
set -euo pipefail

#
# stop.sh
# -------
# Stop the agentic traffic testbed services.
#
# DESCRIPTION:
#   Gracefully stops all running containers for the testbed. This is NOT an
#   uninstallation - it preserves Docker images, volumes, and configuration
#   so that the next deploy is fast.
#
#   The script automatically detects the deployment mode from infra/.env and
#   uses the correct docker-compose file.
#
# WHAT IT DOES (by default):
#   - Stops all running containers
#   - Removes the stopped containers
#   - Keeps Docker images (cached for faster redeploy)
#   - Keeps Docker volumes (data persists)
#   - Keeps Docker networks
#
# WHAT IT PRESERVES:
#   - Built Docker images (redeploy will be fast)
#   - Log files in logs/ directory
#   - Configuration in infra/.env
#
# USAGE:
#   ./scripts/deploy/stop.sh [OPTIONS]
#
# OPTIONS:
#   -v, --volumes   Also prune unused Docker volumes
#   -n, --networks  Also remove custom networks (distributed mode only)
#   --all           Remove both volumes and networks
#   -h, --help      Show help message
#
# EXAMPLES:
#   # Stop containers (default - keeps images/volumes)
#   ./scripts/deploy/stop.sh
#
#   # Stop and remove volumes (clears persisted data)
#   ./scripts/deploy/stop.sh --volumes
#
#   # Stop and remove networks (distributed mode cleanup)
#   ./scripts/deploy/stop.sh --networks
#
#   # Full cleanup (volumes + networks)
#   ./scripts/deploy/stop.sh --all
#
# DEPLOYMENT MODES:
#   The script reads DEPLOYMENT_MODE from infra/.env:
#
#   single      - Runs: docker compose down
#   distributed - Runs: docker compose -f docker-compose.distributed.yml down
#
# FOR COMPLETE UNINSTALL:
#   To fully remove everything including cached images:
#     ./scripts/deploy/stop.sh --all
#     docker image prune -a
#     docker system prune -a
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/infra"

# Load .env file if it exists
ENV_FILE="${COMPOSE_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source <(grep -v '^\s*#' "${ENV_FILE}" | grep -v '^\s*$')
  set +a
fi

# Default deployment mode
DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-single}"

# Parse arguments
REMOVE_VOLUMES=false
REMOVE_NETWORKS=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    -v|--volumes)
      REMOVE_VOLUMES=true
      shift
      ;;
    -n|--networks)
      REMOVE_NETWORKS=true
      shift
      ;;
    --all)
      REMOVE_VOLUMES=true
      REMOVE_NETWORKS=true
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [OPTIONS]"
      echo
      echo "Stop the agentic traffic testbed services."
      echo
      echo "Options:"
      echo "  -v, --volumes   Also remove volumes"
      echo "  -n, --networks  Also remove networks"
      echo "  --all           Remove volumes and networks"
      echo "  -h, --help      Show this help"
      exit 0
      ;;
    *)
      echo "[!] Unknown option: $1"
      exit 1
      ;;
  esac
done

echo "============================================================"
echo "Agentic Traffic Testbed - Stop"
echo "============================================================"
echo "Deployment mode: ${DEPLOYMENT_MODE}"
echo "============================================================"
echo

cd "${COMPOSE_DIR}"

case "${DEPLOYMENT_MODE}" in
  single)
    echo "[*] Stopping single-network deployment..."
    docker compose -f docker-compose.yml -f docker-compose.monitoring.yml down
    ;;
    
  distributed)
    echo "[*] Stopping distributed deployment..."
    docker compose -f docker-compose.distributed.yml -f docker-compose.monitoring.distributed.yml down
    
    if [[ "${REMOVE_NETWORKS}" == "true" ]]; then
      echo "[*] Removing distributed networks..."
      docker network rm infra_agent_a_network 2>/dev/null || true
      docker network rm infra_agent_b_network 2>/dev/null || true
      docker network rm infra_llm_network 2>/dev/null || true
      docker network rm infra_inter_agent_network 2>/dev/null || true
      docker network rm infra_tools_network 2>/dev/null || true
    fi
    ;;
    
  *)
    echo "[!] Unknown DEPLOYMENT_MODE: ${DEPLOYMENT_MODE}"
    echo "[!] Attempting to stop both single and distributed..."
    docker compose down 2>/dev/null || true
    docker compose -f docker-compose.distributed.yml down 2>/dev/null || true
    ;;
esac

if [[ "${REMOVE_VOLUMES}" == "true" ]]; then
  echo "[*] Removing Docker volumes..."
  docker volume prune -f
fi

echo
echo "[✓] Testbed stopped."
