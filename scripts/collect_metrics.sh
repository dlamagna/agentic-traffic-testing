#!/usr/bin/env bash
set -euo pipefail

#
# collect_metrics.sh
# -------------------
# Start eBPF-based TCP observability tools and save their output to logs/.
#
# For the MVP, this script is intended to run on EACH NODE separately.
# You can run it in the background before starting an experiment:
#   ./scripts/collect_metrics.sh &
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${ROOT_DIR}/logs"
NODE_NAME="${NODE_NAME:-node_unspecified}"  # set NODE_NAME env per node (e.g. node1_agentA)
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
NODE_LOG_DIR="${LOG_DIR}/${TIMESTAMP}_${NODE_NAME}"

mkdir -p "${NODE_LOG_DIR}"

echo "[*] Starting eBPF TCP metric collectors for node '${NODE_NAME}'"
echo "    Logs will be written to: ${NODE_LOG_DIR}"

start_tool() {
  local cmd="$1"
  local outfile="$2"

  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "[!] Command '${cmd}' not found; skipping."
    return
  fi

  echo "[*] Launching '${cmd}' -> ${outfile}"
  sudo "${cmd}" > "${outfile}" 2>&1 &
}

start_tool "tcpconnect" "${NODE_LOG_DIR}/tcpconnect.log"
start_tool "tcplife"    "${NODE_LOG_DIR}/tcplife.log"
start_tool "tcprtt"     "${NODE_LOG_DIR}/tcprtt.log"
start_tool "tcpretrans" "${NODE_LOG_DIR}/tcpretrans.log"

echo "[*] eBPF collectors started in background for node '${NODE_NAME}'."
echo "    Use 'ps aux | grep tcp' to see running tools and 'kill' to stop them when done."



