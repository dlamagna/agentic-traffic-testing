#!/usr/bin/env bash
set -euo pipefail

#
# fetch_endpoints.sh
# -------------------
# Helper script to print all relevant service endpoints after deployment.
# It relies on the same NODE{1,2,3}_HOST environment variables that
# `deploy.sh` uses for multi-node deployments.
#
# The ports are resolved dynamically via `docker compose port` so that
# any changes to published ports in docker-compose.yml are automatically
# reflected here (no hard-coded host ports).
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/infra"

NODE1_HOST="${NODE1_HOST:-}"
NODE2_HOST="${NODE2_HOST:-}"
NODE3_HOST="${NODE3_HOST:-}"

###############################################################################
# Helpers
###############################################################################

resolve_port_local() {
  local service="$1"
  local container_port="$2"

  local line
  if line=$(cd "${COMPOSE_DIR}" && docker compose port "${service}" "${container_port}" 2>/dev/null | head -n1); then
    if [[ -n "${line}" ]]; then
      # docker compose port output example: 0.0.0.0:32768
      echo "${line}" | awk -F: 'NF>1{print $NF}'
      return 0
    fi
  fi

  # Empty string means "fallback to default"
  echo ""
}

resolve_port_remote() {
  local host="$1"
  local service="$2"
  local container_port="$3"
  local remote_compose_dir="$4"

  local line
  if line=$(ssh "${host}" "cd '${remote_compose_dir}' && docker compose port '${service}' '${container_port}' 2>/dev/null | head -n1" || true); then
    if [[ -n "${line}" ]]; then
      echo "${line}" | awk -F: 'NF>1{print $NF}'
      return 0
    fi
  fi

  echo ""
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

  llm_port="$(resolve_port_remote "${NODE3_HOST}" "llm-backend" "8000" "${REMOTE_COMPOSE_DIR}")"
  agent_a_port="$(resolve_port_remote "${NODE1_HOST}" "agent-a" "8101" "${REMOTE_COMPOSE_DIR}")"
  agent_b_port="$(resolve_port_remote "${NODE2_HOST}" "agent-b" "8102" "${REMOTE_COMPOSE_DIR}")"
  mcp_db_port="$(resolve_port_remote "${NODE2_HOST}" "mcp-tool-db" "8201" "${REMOTE_COMPOSE_DIR}")"
  jaeger_port="$(resolve_port_remote "${NODE1_HOST}" "jaeger" "16686" "${REMOTE_COMPOSE_DIR}")"
  chat_ui_port="$(resolve_port_remote "${NODE1_HOST}" "chat-ui" "3000" "${REMOTE_COMPOSE_DIR}")"

  # Fallbacks to the canonical container ports if dynamic lookup failed
  llm_port="${llm_port:-8000}"
  agent_a_port="${agent_a_port:-8101}"
  agent_b_port="${agent_b_port:-8102}"
  mcp_db_port="${mcp_db_port:-8201}"
  jaeger_port="${jaeger_port:-16686}"
  chat_ui_port="${chat_ui_port:-3000}"

  echo "[*] Service endpoints summary (multi-node):"
  echo "    - LLM backend : http://${NODE3_HOST}:${llm_port}/chat"
  echo "    - Agent A     : http://${NODE1_HOST}:${agent_a_port}/task"
  echo "    - Agent B     : http://${NODE2_HOST}:${agent_b_port}/subtask"
  echo "    - MCP DB tool : http://${NODE2_HOST}:${mcp_db_port}/query"
  echo "    - Jaeger UI   : http://${NODE1_HOST}:${jaeger_port}"
  echo "    - Chat UI     : http://${NODE1_HOST}:${chat_ui_port}"
else
  ###########################################################################
  # Single-host mode: query ports from local docker compose.
  ###########################################################################
  # For browser access from your laptop, you'd typically want the server's
  # actual DNS name instead of "localhost". You can override this by setting
  # PUBLIC_HOST before running deploy.sh/fetch_endpoints.sh.
  # host_display="${PUBLIC_HOST:-$(hostname -f 2>/dev/null || hostname || echo localhost)}"
  host_display="localhost"
  llm_port="$(resolve_port_local "llm-backend" "8000")"
  agent_a_port="$(resolve_port_local "agent-a" "8101")"
  agent_b_port="$(resolve_port_local "agent-b" "8102")"
  mcp_db_port="$(resolve_port_local "mcp-tool-db" "8201")"
  jaeger_port="$(resolve_port_local "jaeger" "16686")"
  chat_ui_port="$(resolve_port_local "chat-ui" "3000")"

  # Fallbacks to the canonical container ports if dynamic lookup failed
  llm_port="${llm_port:-8000}"
  agent_a_port="${agent_a_port:-8101}"
  agent_b_port="${agent_b_port:-8102}"
  mcp_db_port="${mcp_db_port:-8201}"
  jaeger_port="${jaeger_port:-16686}"
  chat_ui_port="${chat_ui_port:-3000}"

  echo "[*] Service endpoints summary (single-host):"
  echo "    - LLM backend : http://${host_display}:${llm_port}/chat"
  echo "    - Agent A     : http://${host_display}:${agent_a_port}/task"
  echo "    - Agent B     : http://${host_display}:${agent_b_port}/subtask"
  echo "    - MCP DB tool : http://${host_display}:${mcp_db_port}/query"
  echo "    - Jaeger UI   : http://${host_display}:${jaeger_port}"
  echo "    - Chat UI     : http://${host_display}:${chat_ui_port}"

  # Suggest a convenient SSH command to forward all relevant ports in one go.
  # You can override SSH_TARGET on your laptop if you don't use "gpu-mercuri"
  # as the SSH host alias.
  ssh_target="${SSH_TARGET:-gpu-mercuri}"
  echo
  echo "[*] Suggested SSH port-forward command (run on your laptop):"
  echo "    ssh \\"
  echo "      -L 8000:localhost:${llm_port} \\"
  echo "      -L 8101:localhost:${agent_a_port} \\"
  echo "      -L 8102:localhost:${agent_b_port} \\"
  echo "      -L 8201:localhost:${mcp_db_port} \\"
  echo "      -L 16686:localhost:${jaeger_port} \\"
  echo "      -L 3000:localhost:${chat_ui_port} \\"
  echo "      ${ssh_target}"
fi



