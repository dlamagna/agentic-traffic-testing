#!/usr/bin/env bash
set -euo pipefail

#
# uninstall_testbed.sh
# --------------------
# Tears down every service that deploy.sh brings up (single-host or multi-node)
# and removes any Compose-managed volumes/networks plus local log artifacts.
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/infra"
LOG_DIR="${ROOT_DIR}/logs"

if ! command -v docker >/dev/null 2>&1; then
  echo "[!] docker is not installed or not on PATH."
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "[!] docker compose is not available. Please install Docker Compose v2."
  exit 1
fi

NODE1_HOST="${NODE1_HOST:-}"
NODE2_HOST="${NODE2_HOST:-}"
NODE3_HOST="${NODE3_HOST:-}"

COMPOSE_DOWN_CMD="docker compose down --remove-orphans --volumes"

if [[ -n "${NODE1_HOST}" && -n "${NODE2_HOST}" && -n "${NODE3_HOST}" ]]; then
  #########################################################################
  # Multi-node teardown via SSH.
  #########################################################################
  REMOTE_REPO_DIR="${REMOTE_REPO_DIR:-/home/${USER}/projects/testbed}"
  REMOTE_COMPOSE_DIR="${REMOTE_REPO_DIR}/infra"

  echo "[*] Multi-node uninstall detected."
  echo "    NODE1_HOST=${NODE1_HOST}"
  echo "    NODE2_HOST=${NODE2_HOST}"
  echo "    NODE3_HOST=${NODE3_HOST}"
  echo "    REMOTE_REPO_DIR=${REMOTE_REPO_DIR}"

  echo "[*] Tearing down Agent A + Jaeger on NODE1_HOST..."
  ssh "${NODE1_HOST}" "cd '${REMOTE_COMPOSE_DIR}' && ${COMPOSE_DOWN_CMD}"

  echo "[*] Tearing down Agent B + MCP tools on NODE2_HOST..."
  ssh "${NODE2_HOST}" "cd '${REMOTE_COMPOSE_DIR}' && ${COMPOSE_DOWN_CMD}"

  echo "[*] Tearing down LLM backend on NODE3_HOST..."
  ssh "${NODE3_HOST}" "cd '${REMOTE_COMPOSE_DIR}' && ${COMPOSE_DOWN_CMD}"

  echo "[*] Multi-node uninstall complete."
else
  #########################################################################
  # Single-host teardown.
  #########################################################################
  cd "${COMPOSE_DIR}"

  echo "[*] Single-host uninstall: stopping and removing all testbed services..."
  ${COMPOSE_DOWN_CMD}
fi

if [[ -d "${LOG_DIR}" ]]; then
  echo "[*] Removing generated logs under ${LOG_DIR}..."
  find "${LOG_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
fi

echo "[*] Testbed uninstall completed."


