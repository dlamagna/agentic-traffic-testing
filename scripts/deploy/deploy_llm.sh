#!/usr/bin/env bash
#
# deploy_llm.sh
# -------------
# Convenience script to deploy the LLM backend on the Saturn server
# using Docker Compose.
#
# This should be run on saturn.cba.upc.edu (or whichever host owns the GPU).
# It starts only the llm-backend service from infra/docker-compose.yml.
#
# Usage (from repo root on Saturn):
#   ./scripts/deploy/deploy_llm.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/infra"

ENV_FILE="${COMPOSE_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "[!] ${ENV_FILE} not found."
  echo "    Copy infra/.env.example to infra/.env and set HF_TOKEN and related LLM settings."
  echo "    Example:"
  echo "      cd infra"
  echo "      cp .env.example .env"
  echo "      # edit .env and set HF_TOKEN=<your-hf-token>"
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[!] docker is not installed or not on PATH."
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "[!] docker compose (v2) is required."
  exit 1
fi

echo "============================================================"
echo "Agentic Traffic Testbed - LLM Backend Deploy (Saturn)"
echo "============================================================"
echo "Repo root : ${ROOT_DIR}"
echo "Compose   : ${COMPOSE_DIR}/docker-compose.yml"
echo "============================================================"

cd "${COMPOSE_DIR}"

echo "[*] Starting llm-backend (build + up -d)..."
docker compose up --build -d llm-backend

echo
echo "[*] Waiting for LLM backend healthcheck to pass..."
docker compose ps llm-backend

echo
echo "LLM backend should now be reachable at:"
echo "  http://saturn.cba.upc.edu:8000/health"
echo "  http://saturn.cba.upc.edu:8000/metrics"
echo
echo "You can test it locally on Saturn with:"
echo "  curl http://localhost:8000/health"
echo "  curl http://localhost:8000/metrics | head"
echo "============================================================"

