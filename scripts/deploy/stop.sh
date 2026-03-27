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
#   - Kills any running experiment processes (run_experiment.sh, marble, etc.)
#   - Removes experiment cron jobs (agentic-experiment-monitor, marble-experiment-monitor)
#   - Cleans up experiment state files (.experiment_state, .marble_experiment_state)
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

# ---------------------------------------------------------------
# Stop any running experiment processes
# ---------------------------------------------------------------

SCRIPT_DIR_AGENTVERSE="${ROOT_DIR}/scripts/experiment/agentverse"
SCRIPT_DIR_MARBLE="${ROOT_DIR}/scripts/experiment/marble"

EXPERIMENT_PATTERNS=(
  "run_experiment.sh"
  "run_aggregated_experiment.sh"
  "run_marble_experiment.sh"
  "run_marble_aggregated_experiment.sh"
  "run_rlm_benchmark.sh"
  "run_agentbench.sh"
  "run_oolong_benchmark.sh"
  "run_marble_benchmark.sh"
  "monitor_experiment.sh"
  "monitor_marble_experiment.sh"
  "benchmarks.marble.runner"
)

CRON_TAGS=(
  "# agentic-experiment-monitor"
  "# marble-experiment-monitor"
)

STATE_FILES=(
  "${SCRIPT_DIR_AGENTVERSE}/.experiment_state"
  "${SCRIPT_DIR_MARBLE}/.marble_experiment_state"
)

echo "[*] Checking for running experiment processes..."
FOUND_PIDS=()
for PATTERN in "${EXPERIMENT_PATTERNS[@]}"; do
  while IFS= read -r PID; do
    [[ -z "$PID" || "$PID" == "$$" ]] && continue
    FOUND_PIDS+=("$PID")
    CMD=$(ps -p "$PID" -o args= 2>/dev/null || true)
    echo "    Found: PID ${PID} — ${CMD}"
  done < <(pgrep -f "$PATTERN" 2>/dev/null || true)
done

if [[ ${#FOUND_PIDS[@]} -gt 0 ]]; then
  echo "[*] Sending SIGTERM to experiment processes..."
  for PID in "${FOUND_PIDS[@]}"; do
    kill -TERM "$PID" 2>/dev/null || true
  done
  sleep 3
  echo "[*] Sending SIGKILL to any remaining experiment processes..."
  for PID in "${FOUND_PIDS[@]}"; do
    if ps -p "$PID" > /dev/null 2>&1; then
      kill -KILL "$PID" 2>/dev/null || true
    fi
  done
  echo "[✓] Experiment processes stopped."
else
  echo "    No running experiment processes found."
fi

echo
echo "[*] Checking for experiment cron jobs..."
CURRENT_CRON=$(crontab -l 2>/dev/null || true)
CLEAN_CRON="$CURRENT_CRON"
CRON_REMOVED=false

for TAG in "${CRON_TAGS[@]}"; do
  if echo "$CURRENT_CRON" | grep -qF "$TAG"; then
    echo "    Found cron job: $TAG"
    CLEAN_CRON=$(echo "$CLEAN_CRON" | grep -vF "$TAG" || true)
    CRON_REMOVED=true
  fi
done

if [[ "$CRON_REMOVED" == "true" ]]; then
  echo "$CLEAN_CRON" | crontab -
  echo "[✓] Experiment cron jobs removed."
else
  echo "    No experiment cron jobs found."
fi

echo
echo "[*] Cleaning up experiment state files..."
STATE_FOUND=false
for STATE_FILE in "${STATE_FILES[@]}"; do
  if [[ -f "$STATE_FILE" ]]; then
    echo "    Removing: $STATE_FILE"
    rm -f "$STATE_FILE"
    STATE_FOUND=true
  fi
done
[[ "$STATE_FOUND" == "false" ]] && echo "    No state files found."

echo

# ---------------------------------------------------------------
# Stop Docker services
# ---------------------------------------------------------------

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
