#!/usr/bin/env bash
#
# build_and_load_k8s_images.sh
# -----------------------------
# Builds the testbed container images and loads them into a local Kind cluster
# so that infra/k8s/workloads/*.yaml can use imagePullPolicy: IfNotPresent
# with *:local tags.
#
# Requires: Docker, Kind, and a running Kind cluster. Run from repo root.
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

if ! command -v docker >/dev/null 2>&1; then
  echo "[!] Docker is required to build images."
  exit 1
fi

if ! command -v kind >/dev/null 2>&1; then
  echo "[!] 'kind' is required to load images into the Kubernetes cluster."
  echo "    Install Kind from https://kind.sigs.k8s.io/ and ensure it is on PATH."
  exit 1
fi

echo "[*] Building Docker images (repo root as context)..."

docker build -t agent-a:local -f agents/Dockerfile .
docker build -t agent-b:local -f agents/Dockerfile .
docker build -t mcp-tool-db:local -f tools/mcp_tool_db/Dockerfile .

echo "[*] Loading images into Kind cluster..."

KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-agentic-testbed}"

if ! kind get clusters 2>/dev/null | grep -qx "${KIND_CLUSTER_NAME}"; then
  echo "[!] No Kind cluster named '${KIND_CLUSTER_NAME}' found."
  echo "    Create one first, for example:"
  echo "      kind create cluster --name ${KIND_CLUSTER_NAME}"
  exit 1
fi

for img in agent-a:local agent-b:local mcp-tool-db:local; do
  echo "  Loading ${img} into Kind cluster '${KIND_CLUSTER_NAME}'..."
  kind load docker-image "${img}" --name "${KIND_CLUSTER_NAME}"
done

echo "[*] Done. Jaeger uses image jaegertracing/all-in-one:1.57 (pulled by the cluster when needed). LLM backend runs externally on Saturn."
