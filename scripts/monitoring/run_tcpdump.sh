#!/usr/bin/env bash
set -euo pipefail

# run_tcpdump.sh
# ---------------
# Helper wrapper to:
# - Ensure any existing tcp_metrics_collector bound to :9100 is stopped
# - Start tcpdump on the inter-agent bridge
# - Pipe tcpdump output into the TCP metrics collector (--read-stdin mode)
#
# Usage:
#   ./scripts/monitoring/run_tcpdump.sh
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT=9100
INTERFACE="br-df4088ff2909"
FILTER="tcp and net 172.23.0.0/24"

echo "[run_tcpdump] Using interface: ${INTERFACE}"
echo "[run_tcpdump] Filter: ${FILTER}"
echo "[run_tcpdump] Metrics port: ${PORT}"

# If something is already listening on :9100, try to stop it (best-effort).
EXISTING_PIDS="$(ss -ltnp | awk -v p=":${PORT}" '$4 ~ p {print $NF}' | sed -E 's/.*pid=([0-9]+),.*/\1/' || true)"
if [[ -n "${EXISTING_PIDS}" ]]; then
  echo "[run_tcpdump] Detected existing listener(s) on :${PORT} (PIDs: ${EXISTING_PIDS}). Attempting to stop them..."
  # Only kill python tcp_metrics_collector processes; ignore others.
  while read -r pid; do
    [[ -z "${pid}" ]] && continue
    if ps -p "${pid}" -o comm= | grep -q "python"; then
      echo "  -> killing python process PID=${pid}"
      kill "${pid}" || true
    else
      echo "  -> PID=${pid} is not a python process; leaving it alone."
    fi
  done <<< "${EXISTING_PIDS}"
  sleep 1
fi

cd "${ROOT_DIR}"

echo "[run_tcpdump] Starting tcpdump piped into tcp_metrics_collector.py ..."
echo "[run_tcpdump] Command:"
echo "  sudo tcpdump -i ${INTERFACE} -l -n -tt ${FILTER} | python3 scripts/monitoring/tcp_metrics_collector.py --read-stdin"

sudo tcpdump -i "${INTERFACE}" -l -n -tt ${FILTER} \
  | python3 scripts/monitoring/tcp_metrics_collector.py --read-stdin

sudo tcpdump -i br-df4088ff2909 -l -n -tt tcp and net 172.23.0.0/24   | python3 scripts/monitoring/tcp_metrics_collector.py --read-stdin