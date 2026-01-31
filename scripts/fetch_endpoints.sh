#!/usr/bin/env bash
set -euo pipefail

#
# fetch_endpoints.sh
# -------------------
# Helper script to print all relevant service endpoints after deployment.
# It relies on the same NODE{1,2,3}_HOST environment variables that
# `deploy.sh` uses for multi-node deployments.
#
# The ports are resolved dynamically via `docker compose ps/config` so that
# any changes to published ports in docker-compose.yml are automatically
# reflected here (no hard-coded host ports).
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/infra"

NODE1_HOST="${NODE1_HOST:-}"
NODE2_HOST="${NODE2_HOST:-}"
NODE3_HOST="${NODE3_HOST:-}"
VERBOSE=0

for arg in "$@"; do
  if [[ "${arg}" == "-vv" ]]; then
    VERBOSE=1
  fi
done

###############################################################################
# Helpers
###############################################################################

endpoint_path_for_service() {
  local service="$1"

  case "${service}" in
    llm-backend)
      echo "/chat"
      ;;
    agent-a)
      echo "/task"
      ;;
    agent-b*)
      echo "/subtask"
      ;;
    mcp-tool-db)
      echo "/query"
      ;;
    jaeger)
      echo ""  # Jaeger UI is at root
      ;;
    grafana)
      echo ""  # Grafana UI is at root
      ;;
    prometheus)
      echo ""  # Prometheus UI is at root
      ;;
    cadvisor)
      echo ""  # cAdvisor UI is at root
      ;;
    chat-ui)
      echo ""  # Chat UI is at root
      ;;
    *)
      echo ""
      ;;
  esac
}

debug() {
  if [[ "${VERBOSE}" -eq 1 ]]; then
    printf '[debug] %s\n' "$*" >&2
  fi
}

extract_endpoints_from_ps_json() {
  local host_display="$1"
  debug "Parsing docker compose ps JSON for host=${host_display}"

  local py_script
  py_script="$(cat <<'PY'
import json
import os
import sys

host = os.environ.get("HOST_DISPLAY", "localhost")

def path_for(service):
    if service == "llm-backend":
        return "/chat"
    if service == "agent-a":
        return "/task"
    if service.startswith("agent-b"):
        return "/subtask"
    if service == "mcp-tool-db":
        return "/query"
    # Monitoring and UI services (root path)
    if service in ("jaeger", "grafana", "prometheus", "cadvisor", "chat-ui"):
        return ""
    return ""

def iter_items():
    raw = sys.stdin.read().strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            payload = json.loads(raw)
        except Exception:
            return []
        return payload if isinstance(payload, list) else []
    items = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except Exception:
            continue
    return items

for item in iter_items():
    service = item.get("Service") or item.get("Name") or ""
    if not service:
        continue
    for publisher in item.get("Publishers") or []:
        published = publisher.get("PublishedPort")
        if published in (None, "", 0):
            continue
        try:
            port = int(published)
        except (TypeError, ValueError):
            continue
        path = path_for(service)
        print(f"{service}|{host}|{port}|{path}")
PY
)"

  HOST_DISPLAY="${host_display}" python3 -c "${py_script}"
}

extract_endpoints_from_config_json() {
  local host_display="$1"
  debug "Parsing docker compose config JSON for host=${host_display}"

  local py_script
  py_script="$(cat <<'PY'
import json
import os
import sys

host = os.environ.get("HOST_DISPLAY", "localhost")

def path_for(service):
    if service == "llm-backend":
        return "/chat"
    if service == "agent-a":
        return "/task"
    if service.startswith("agent-b"):
        return "/subtask"
    if service == "mcp-tool-db":
        return "/query"
    # Monitoring and UI services (root path)
    if service in ("jaeger", "grafana", "prometheus", "cadvisor", "chat-ui"):
        return ""
    return ""

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)

services = payload.get("services") if isinstance(payload, dict) else None
if not isinstance(services, dict):
    sys.exit(0)

for service, config in services.items():
    ports = config.get("ports") or []
    if not isinstance(ports, list):
        continue
    for port in ports:
        if isinstance(port, str):
            parts = port.split(":")
            published = parts[0] if len(parts) > 1 else parts[0]
            target = parts[-1]
            port_value = published or target
        elif isinstance(port, dict):
            published = port.get("published")
            target = port.get("target")
            port_value = published or target
        else:
            continue
        try:
            port_number = int(port_value)
        except (TypeError, ValueError):
            continue
        path = path_for(service)
        print(f"{service}|{host}|{port_number}|{path}")
PY
)"

  HOST_DISPLAY="${host_display}" python3 -c "${py_script}"
}

collect_endpoints_local() {
  local host_display="$1"
  local payload
  debug "Running: docker compose ps --format json (dir=${COMPOSE_DIR})"
  payload=$(cd "${COMPOSE_DIR}" && docker compose ps --format json 2>/dev/null || true)
  debug "ps payload bytes: ${#payload}"
  if [[ -n "${payload}" && "${payload}" != "[]" ]]; then
    debug "Using ps JSON payload"
    printf "%s" "${payload}" | extract_endpoints_from_ps_json "${host_display}"
    return 0
  fi

  debug "Running: docker compose config --format json (dir=${COMPOSE_DIR})"
  payload=$(cd "${COMPOSE_DIR}" && docker compose config --format json 2>/dev/null || true)
  debug "config payload bytes: ${#payload}"
  if [[ -n "${payload}" ]]; then
    debug "Using config JSON payload"
    printf "%s" "${payload}" | extract_endpoints_from_config_json "${host_display}"
  fi
}

collect_endpoints_remote() {
  local host="$1"
  local remote_compose_dir="$2"
  local payload

  debug "Running on ${host}: docker compose ps --format json (dir=${remote_compose_dir})"
  payload=$(ssh "${host}" "cd '${remote_compose_dir}' && docker compose ps --format json 2>/dev/null" || true)
  debug "ps payload bytes (${host}): ${#payload}"
  if [[ -n "${payload}" && "${payload}" != "[]" ]]; then
    debug "Using ps JSON payload from ${host}"
    printf "%s" "${payload}" | extract_endpoints_from_ps_json "${host}"
    return 0
  fi

  debug "Running on ${host}: docker compose config --format json (dir=${remote_compose_dir})"
  payload=$(ssh "${host}" "cd '${remote_compose_dir}' && docker compose config --format json 2>/dev/null" || true)
  debug "config payload bytes (${host}): ${#payload}"
  if [[ -n "${payload}" ]]; then
    debug "Using config JSON payload from ${host}"
    printf "%s" "${payload}" | extract_endpoints_from_config_json "${host}"
  fi
}

print_endpoint_summary() {
  local label="$1"
  local endpoints="$2"

  echo "[*] Service endpoints summary (${label}):"
  if [[ -z "${endpoints}" ]]; then
    echo "    (no running services detected)"
    return 0
  fi

  printf "%s\n" "${endpoints}" \
    | awk -F'|' '!seen[$1 "|" $2 "|" $3 "|" $4]++' \
    | sort -t'|' -k2,2 -k1,1 \
    | while IFS='|' read -r service host port path; do
      printf "    - %-12s: http://%s:%s%s\n" "${service}" "${host}" "${port}" "${path}"
    done
}

collect_ports_csv() {
  local endpoints="$1"

  printf "%s\n" "${endpoints}" \
    | awk -F'|' '{print $3}' \
    | sort -n \
    | uniq \
    | paste -sd, -
}

###############################################################################
# Single-host vs multi-node logic (mirrors deploy.sh)
###############################################################################

if [[ -n "${NODE1_HOST}" && -n "${NODE2_HOST}" && -n "${NODE3_HOST}" ]]; then
  ###########################################################################
  # Multi-node mode: query ports on each remote host via SSH.
  ###########################################################################
  REMOTE_REPO_DIR="${REMOTE_REPO_DIR:-/home/${USER}/projects/testbed}"
  REMOTE_COMPOSE_DIR="${REMOTE_REPO_DIR}/infra"

  endpoints_all=""
  endpoints_node1="$(collect_endpoints_remote "${NODE1_HOST}" "${REMOTE_COMPOSE_DIR}")"
  endpoints_node2="$(collect_endpoints_remote "${NODE2_HOST}" "${REMOTE_COMPOSE_DIR}")"
  endpoints_node3="$(collect_endpoints_remote "${NODE3_HOST}" "${REMOTE_COMPOSE_DIR}")"
  if [[ -n "${endpoints_node1}" ]]; then
    endpoints_all+="${endpoints_node1}"$'\n'
  fi
  if [[ -n "${endpoints_node2}" ]]; then
    endpoints_all+="${endpoints_node2}"$'\n'
  fi
  if [[ -n "${endpoints_node3}" ]]; then
    endpoints_all+="${endpoints_node3}"$'\n'
  fi
  endpoints_all="$(printf "%s" "${endpoints_all}" | sed '/^$/d')"
  print_endpoint_summary "multi-node" "${endpoints_all}"
else
  ###########################################################################
  # Single-host mode: query ports from local docker compose.
  ###########################################################################
  # For browser access from your laptop, you'd typically want the server's
  # actual DNS name instead of "localhost". You can override this by setting
  # PUBLIC_HOST before running deploy.sh/fetch_endpoints.sh.
  # host_display="${PUBLIC_HOST:-$(hostname -f 2>/dev/null || hostname || echo localhost)}"
  host_display="${PUBLIC_HOST:-localhost}"
  endpoints_local="$(collect_endpoints_local "${host_display}")"
  endpoints_local="$(printf "%s" "${endpoints_local}" | sed '/^$/d')"
  print_endpoint_summary "single-host" "${endpoints_local}"

  # Suggest a convenient SSH command to forward all relevant ports in one go.
  # You can override SSH_TARGET on your laptop if you don't use "saturn"
  # as the SSH host alias.
  ssh_target="${SSH_TARGET:-saturn}"
  ports_csv="$(collect_ports_csv "${endpoints_local}")"
  if [[ -n "${ports_csv}" ]]; then
    echo
    echo "[*] Suggested SSH port-forward command (run on your laptop):"
    echo "    ./forward_clean_ssh.sh --ports \"${ports_csv}\" --host ${ssh_target}"
  fi
fi
