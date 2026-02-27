#!/usr/bin/env bash
#
# deploy_cluster.sh
# ------------------
# End-to-end deployment for the k3s + Cilium + Hubble observability node.
# This script is intended to run on the k3s server (147.83.130.68 or similar)
# and assumes the LLM backend is running remotely on Saturn
# (saturn.cba.upc.edu:8000).
#
# It performs:
#   1) Optional connectivity check to the external LLM backend
#   2) k3s + Cilium + kube-prometheus-stack + Hubble install
#   3) Build + load agent/tool images into k3s
#   4) Deploy agents, tools, and Jaeger workloads
#
# Usage (from repo root on the k3s node):
#   ./scripts/deploy/deploy_cluster.sh
#   LLM_URL=http://saturn.cba.upc.edu:8000 ./scripts/deploy/deploy_cluster.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

LLM_URL="${LLM_URL:-http://saturn.cba.upc.edu:8000}"

echo "============================================================"
echo "Agentic Traffic Testbed - k3s Observability Cluster Deploy"
echo "============================================================"
echo "Repo root : ${ROOT_DIR}"
echo "LLM URL   : ${LLM_URL}"
echo "============================================================"

if ! command -v curl >/dev/null 2>&1; then
  echo "[!] curl is required for connectivity checks. Please install curl."
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "[!] python3 is required for some helper scripts (health checks)."
fi

echo
echo "[1/4] Testing connectivity to external LLM backend on Saturn..."
if ./scripts/monitoring/test_llm_connectivity.sh --llm-url "${LLM_URL}"; then
  echo "[*] LLM connectivity check passed."
else
  echo "[!] LLM connectivity check failed. You can still continue, but agents"
  echo "    will not be able to call the LLM until this is resolved."
fi

echo
echo "[2/4] Installing k3s + Cilium + kube-prometheus-stack + Hubble..."
./scripts/deploy/install_k3s_cilium.sh

echo
echo "[3/4] Building and loading agent/tool images into k3s..."
./scripts/deploy/build_and_load_k8s_images.sh

echo
echo "[4/4] Deploying agents, tools, and Jaeger workloads into k3s..."
./scripts/deploy/deploy_k8s_workloads.sh

echo
echo "============================================================"
echo "Deployment complete."
echo "============================================================"
echo "NodePorts on the k3s server:"
echo "  Agent A : http://<node-ip>:30101"
echo "  Agent B : http://<node-ip>:30102"
echo "  MCP DB  : http://<node-ip>:30201"
echo "  Jaeger  : http://<node-ip>:31686"
echo "  Grafana : http://<node-ip>:3001 (admin/admin)"
echo
echo "You can now run the health check from any host that can reach the k3s node:"
echo
echo "  python3 scripts/monitoring/health_check.py \\"
echo "    --mode k8s \\"
echo "    --llm-url ${LLM_URL}/chat \\"
echo "    --agent-a-url http://<node-ip>:30101/task \\"
echo "    --agent-b-url http://<node-ip>:30102/subtask \\"
echo "    --ui-url http://<node-ip>:3001 \\"
echo "    --skip-monitoring"
echo "============================================================"

