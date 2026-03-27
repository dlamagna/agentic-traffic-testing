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
  # Important: chat-ui depends_on agents (which depend_on llm-backend) in the
  # main compose file. We want UI-only, so we explicitly disable dependencies.
  docker compose up --build --no-deps -d chat-ui
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  deploy_ui_single_host
fi
