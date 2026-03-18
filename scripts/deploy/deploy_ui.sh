#!/usr/bin/env bash
set -euo pipefail

# deploy_ui.sh
# -------------
# Helper to deploy the chat UI container locally. Can be sourced by other
# scripts (e.g. deploy.sh) or run directly.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/infra"

deploy_ui_single_host() {
  cd "${COMPOSE_DIR}"
  echo "[*] Deploying Chat UI locally (http://localhost:3000)..."
  docker compose up --build -d chat-ui
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  deploy_ui_single_host
fi
