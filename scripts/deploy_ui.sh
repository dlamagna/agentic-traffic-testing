#!/usr/bin/env bash
set -euo pipefail

# deploy_ui.sh
# -------------
# Helper to deploy the chat UI container either locally (single-host) or to the
# NODE1 host in multi-node mode. Can be sourced by other scripts (e.g. deploy.sh)
# or run directly.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/infra"

deploy_ui_single_host() {
  cd "${COMPOSE_DIR}"
  echo "[*] Deploying Chat UI locally (http://localhost:3000)..."
  docker compose up --build -d chat-ui
}

deploy_ui_multi_host() {
  local node1_host="$1"
  local remote_compose_dir="$2"
  echo "[*] Deploying Chat UI on NODE1_HOST (${node1_host})..."
  ssh "${node1_host}" "cd '${remote_compose_dir}' && docker compose up --build -d chat-ui"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  NODE1_HOST="${NODE1_HOST:-}"
  NODE2_HOST="${NODE2_HOST:-}"
  NODE3_HOST="${NODE3_HOST:-}"

  if [[ -n "${NODE1_HOST}" && -n "${NODE2_HOST}" && -n "${NODE3_HOST}" ]]; then
    REMOTE_REPO_DIR="${REMOTE_REPO_DIR:-/home/${USER}/projects/testbed}"
    REMOTE_COMPOSE_DIR="${REMOTE_REPO_DIR}/infra"
    deploy_ui_multi_host "${NODE1_HOST}" "${REMOTE_COMPOSE_DIR}"
    echo "[*] Chat UI available at http://${NODE1_HOST}:3000"
  else
    deploy_ui_single_host
  fi
fi

