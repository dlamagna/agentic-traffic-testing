#!/usr/bin/env bash
#
# publish_k8s_images_to_dockerhub.sh
# ----------------------------------
# Builds the testbed Kubernetes images and pushes them to Docker Hub so that
# the Kind cluster (or any Kubernetes cluster) can pull them directly from
# the registry instead of relying on *:local tags.
#
# Usage (from repo root):
#   DOCKERHUB_USER=dlamagna ./scripts/deploy/publish_k8s_images_to_dockerhub.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

DOCKERHUB_USER="${DOCKERHUB_USER:-dlamagna}"
TAG="${TAG:-latest}"

if ! command -v docker >/dev/null 2>&1; then
  echo "[!] Docker is required to build and push images."
  exit 1
fi

echo "============================================================"
echo "Publishing Kubernetes images to Docker Hub"
echo "============================================================"
echo "Repo root      : ${ROOT_DIR}"
echo "Docker Hub user: ${DOCKERHUB_USER}"
echo "Tag            : ${TAG}"
echo "============================================================"
echo

echo "[*] Building Docker images (repo root as context)..."

docker build -t agent-a:local -f agents/Dockerfile .
docker build -t agent-b:local -f agents/Dockerfile .
docker build -t mcp-tool-db:local -f tools/mcp_tool_db/Dockerfile .

echo
echo "[*] Tagging images for Docker Hub..."

AGENT_A_IMAGE="docker.io/${DOCKERHUB_USER}/agent-a:${TAG}"
AGENT_B_IMAGE="docker.io/${DOCKERHUB_USER}/agent-b:${TAG}"
MCP_DB_IMAGE="docker.io/${DOCKERHUB_USER}/mcp-tool-db:${TAG}"

docker tag agent-a:local "${AGENT_A_IMAGE}"
docker tag agent-b:local "${AGENT_B_IMAGE}"
docker tag mcp-tool-db:local "${MCP_DB_IMAGE}"

echo
echo "[*] Pushing images to Docker Hub (you may be prompted to 'docker login')..."

docker push "${AGENT_A_IMAGE}"
docker push "${AGENT_B_IMAGE}"
docker push "${MCP_DB_IMAGE}"

echo
echo "============================================================"
echo "[✓] Images published to Docker Hub:"
echo "    ${AGENT_A_IMAGE}"
echo "    ${AGENT_B_IMAGE}"
echo "    ${MCP_DB_IMAGE}"
echo
echo "Update infra/k8s/workloads/*.yaml to use these image names if not already."
echo "============================================================"

