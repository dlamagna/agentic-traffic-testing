#!/usr/bin/env bash
#
# build_and_load_k8s_images.sh
# -----------------------------
# Builds the testbed container images and loads them into k3s's containerd
# so that infra/k8s/workloads/*.yaml can use imagePullPolicy: IfNotPresent.
#
# Requires: Docker, k3s running. Run from repo root.
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

if ! command -v docker >/dev/null 2>&1; then
  echo "[!] Docker is required to build images."
  exit 1
fi

echo "[*] Building Docker images (repo root as context)..."

docker build -t agent-a:local -f agents/Dockerfile .
docker build -t agent-b:local -f agents/Dockerfile .
docker build -t mcp-tool-db:local -f tools/mcp_tool_db/Dockerfile .

echo "[*] Loading images into k3s containerd..."

for img in agent-a:local agent-b:local mcp-tool-db:local; do
  tmp=$(mktemp -u).tar
  docker save "${img}" -o "${tmp}"
  sudo k3s ctr images import "${tmp}"
  rm -f "${tmp}"
  echo "  Loaded ${img}"
done

echo "[*] Done. Jaeger uses image jaegertracing/all-in-one:1.57 (pulled by k3s when needed). LLM backend runs externally on Saturn."
