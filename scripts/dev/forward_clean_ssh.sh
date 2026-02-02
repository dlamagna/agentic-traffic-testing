#!/usr/bin/env bash
set -euo pipefail

#
# forward_clean_ssh.sh
# --------------------
# Kill any local listeners on requested ports, then SSH with -L forwards.
#
# Usage:
#   ./scripts/dev/forward_clean_ssh.sh --ports "8000,8101,8102" --host saturn
#

ports_csv=""
host=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ports)
      ports_csv="${2:-}"
      shift 2
      ;;
    --host)
      host="${2:-}"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 --ports \"8000,8101\" --host saturn"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      echo "Usage: $0 --ports \"8000,8101\" --host saturn"
      exit 1
      ;;
  esac
done

if [[ -z "${ports_csv}" || -z "${host}" ]]; then
  echo "Error: both --ports and --host are required."
  echo "Usage: $0 --ports \"8000,8101\" --host saturn"
  exit 1
fi

if ! command -v lsof >/dev/null 2>&1; then
  echo "Error: lsof is required but not installed."
  echo "Install it with: sudo apt-get install -y lsof"
  exit 1
fi

kill_port_listeners() {
  local port="$1"
  local pids=""

  pids="$(lsof -t -iTCP:"${port}" -sTCP:LISTEN -nP 2>/dev/null || true)"
  if [[ -z "${pids}" ]]; then
    return 0
  fi

  echo "[*] Closing listeners on port ${port}: ${pids}"
  # Try graceful shutdown first.
  kill -TERM ${pids} 2>/dev/null || true
  sleep 0.3

  # If still listening, force kill.
  if lsof -t -iTCP:"${port}" -sTCP:LISTEN -nP >/dev/null 2>&1; then
    kill -KILL ${pids} 2>/dev/null || true
  fi
}

IFS=',' read -r -a ports <<< "${ports_csv}"

for port in "${ports[@]}"; do
  port="$(echo "${port}" | xargs)"
  if [[ -z "${port}" ]]; then
    continue
  fi
  if ! [[ "${port}" =~ ^[0-9]+$ ]]; then
    echo "Invalid port: ${port}"
    exit 1
  fi
  kill_port_listeners "${port}"
done

ssh_args=("-o" "ExitOnForwardFailure=yes")
for port in "${ports[@]}"; do
  port="$(echo "${port}" | xargs)"
  if [[ -z "${port}" ]]; then
    continue
  fi
  ssh_args+=("-L" "${port}:localhost:${port}")
done

echo "[*] Connecting to ${host} with forwards: ${ports_csv}"
exec ssh "${ssh_args[@]}" "${host}"
