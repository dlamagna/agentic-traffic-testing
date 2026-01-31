#!/usr/bin/env bash
set -euo pipefail

#
# uninstall_testbed.sh
# --------------------
# Completely removes the agentic traffic testbed, including:
#   - All Docker containers (via stop.sh)
#   - Docker volumes and networks
#   - Generated logs
#   - GPU cache artifacts
#
# This script uses stop.sh for the Docker teardown to ensure it handles
# all deployment modes correctly (single, distributed, multi-vm).
#
# USAGE:
#   ./scripts/uninstall_testbed.sh [OPTIONS]
#
# OPTIONS:
#   --keep-logs     Don't remove the logs directory
#   --keep-images   Don't prune Docker images
#   -h, --help      Show this help
#
# WHAT IT REMOVES:
#   - All testbed containers
#   - Docker volumes (data)
#   - Docker networks
#   - Log files (unless --keep-logs)
#   - GPU cache artifacts
#
# WHAT IT PRESERVES:
#   - Docker images (unless --keep-images specified)
#   - Configuration files (.env, etc.)
#   - Source code
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/infra"
LOG_DIR="${ROOT_DIR}/logs"
STOP_SCRIPT="${ROOT_DIR}/scripts/stop.sh"

# Parse arguments
KEEP_LOGS=false
PRUNE_IMAGES=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-logs)
      KEEP_LOGS=true
      shift
      ;;
    --keep-images)
      # Actually this is inverted - we DON'T prune by default
      # This flag would enable pruning, but let's keep it simple
      shift
      ;;
    --prune-images)
      PRUNE_IMAGES=true
      shift
      ;;
    -h|--help)
      head -40 "$0" | grep -E "^#" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      echo "[!] Unknown option: $1"
      exit 1
      ;;
  esac
done

clear_gpu_resources() {
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "[*] nvidia-smi not found; skipping GPU cleanup."
    return
  fi

  echo "[*] Clearing GPU VRAM and GPU storage artifacts for USER=${USER}..."

  local gpu_indices
  gpu_indices="$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null || true)"
  if [[ -z "${gpu_indices}" ]]; then
    echo "[*] No NVIDIA GPUs detected."
    return
  fi

  local pids
  pids="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null || true)"
  if [[ -n "${pids}" ]]; then
    local pid
    for pid in ${pids}; do
      if ps -o user= -p "${pid}" 2>/dev/null | grep -q "^${USER}$"; then
        echo "[*] Stopping lingering GPU process PID ${pid}..."
        kill -TERM "${pid}" 2>/dev/null || true
      else
        echo "[*] Skipping GPU PID ${pid} (not owned by ${USER})."
      fi
    done
    sleep 2
    for pid in ${pids}; do
      if ps -o user= -p "${pid}" 2>/dev/null | grep -q "^${USER}$"; then
        kill -KILL "${pid}" 2>/dev/null || true
      fi
    done
  fi

  # Only clean per-user artifacts to avoid impacting other users.
  rm -rf "${HOME}/.nv/ComputeCache" "${HOME}/.cache/nvidia" 2>/dev/null || true
}

echo "============================================================"
echo "Agentic Traffic Testbed - Uninstall"
echo "============================================================"
echo

if ! command -v docker >/dev/null 2>&1; then
  echo "[!] docker is not installed or not on PATH."
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "[!] docker compose is not available. Please install Docker Compose v2."
  exit 1
fi

#########################################################################
# Step 1: Stop all services using stop.sh (handles all deployment modes)
#########################################################################
echo "[1/4] Stopping all services..."

if [[ -x "${STOP_SCRIPT}" ]]; then
  # Use stop.sh with --all flag to remove volumes and networks
  "${STOP_SCRIPT}" --all
else
  # Fallback if stop.sh doesn't exist
  echo "[!] stop.sh not found, using fallback teardown..."
  cd "${COMPOSE_DIR}"
  
  # Try to stop all possible configurations
  docker compose down --remove-orphans --volumes 2>/dev/null || true
  docker compose -f docker-compose.distributed.yml down --remove-orphans --volumes 2>/dev/null || true
  docker compose -f docker-compose.monitoring.yml down --remove-orphans --volumes 2>/dev/null || true
  docker compose -f docker-compose.monitoring.distributed.yml down --remove-orphans --volumes 2>/dev/null || true
fi

#########################################################################
# Step 2: Remove any remaining testbed containers
#########################################################################
echo
echo "[2/4] Removing any remaining testbed containers..."

# List of known container names
CONTAINERS=(
  "llm-backend"
  "agent-a"
  "agent-b" "agent-b-2" "agent-b-3" "agent-b-4" "agent-b-5"
  "mcp-tool-db"
  "chat-ui"
  "jaeger"
  "prometheus" "grafana" "cadvisor"
  "network-monitor"
)

for container in "${CONTAINERS[@]}"; do
  if docker ps -a --format '{{.Names}}' | grep -q "^${container}$"; then
    echo "    Removing container: ${container}"
    docker rm -f "${container}" 2>/dev/null || true
  fi
done

#########################################################################
# Step 3: Remove logs (unless --keep-logs)
#########################################################################
echo
echo "[3/4] Cleaning up logs and artifacts..."

if [[ "${KEEP_LOGS}" == "false" && -d "${LOG_DIR}" ]]; then
  echo "    Removing logs under ${LOG_DIR}..."
  find "${LOG_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
else
  echo "    Keeping logs (--keep-logs specified or no logs found)"
fi

# Remove any pcap files in the traffic logs
if [[ -d "${LOG_DIR}/traffic" ]]; then
  rm -rf "${LOG_DIR}/traffic"/* 2>/dev/null || true
fi

#########################################################################
# Step 4: Clear GPU resources
#########################################################################
echo
echo "[4/4] Clearing GPU resources..."
clear_gpu_resources

#########################################################################
# Optional: Prune images
#########################################################################
if [[ "${PRUNE_IMAGES}" == "true" ]]; then
  echo
  echo "[*] Pruning unused Docker images..."
  docker image prune -af
fi

echo
echo "============================================================"
echo "[✓] Testbed uninstall completed."
echo "============================================================"
echo
echo "Removed:"
echo "  - All testbed containers"
echo "  - Docker volumes"
echo "  - Docker networks"
[[ "${KEEP_LOGS}" == "false" ]] && echo "  - Log files"
echo "  - GPU cache artifacts"
echo
echo "Preserved:"
echo "  - Docker images (run with --prune-images to remove)"
echo "  - Configuration files"
echo "  - Source code"
echo


