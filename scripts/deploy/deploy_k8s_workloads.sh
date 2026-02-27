#!/usr/bin/env bash
#
# deploy_k8s_workloads.sh
# -----------------------
# Applies the testbed workloads to the agentic-testbed namespace.
# Deploys agents, tools, and Jaeger into the agentic-testbed namespace.
# LLM backend runs externally on Saturn (saturn.cba.upc.edu:8000).
#
# Prerequisites:
#   1. kubectl configured for your k3s cluster (KUBECONFIG or default).
#   2. Images for agent-a, agent-b, and mcp-tool-db loaded into k3s (or in a registry).
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K8S_DIR="${ROOT_DIR}/infra/k8s"

# Prefer kubectl; fall back to k3s kubectl
KUBECTL="kubectl"
if ! command -v kubectl >/dev/null 2>&1 && command -v k3s >/dev/null 2>&1; then
  KUBECTL="sudo k3s kubectl"
fi

echo "[*] Ensuring namespace agentic-testbed exists..."
"${KUBECTL}" apply -f "${K8S_DIR}/base/namespace.yaml"

echo "[*] Deploying workloads (order: Jaeger, Agent B, Agent A, MCP tool)..."
"${KUBECTL}" apply -f "${K8S_DIR}/workloads/jaeger.yaml"
"${KUBECTL}" apply -f "${K8S_DIR}/workloads/agent-b.yaml"
"${KUBECTL}" apply -f "${K8S_DIR}/workloads/agent-a.yaml"
"${KUBECTL}" apply -f "${K8S_DIR}/workloads/mcp-tool-db.yaml"

echo "[*] Waiting for deployments to roll out..."
"${KUBECTL}" -n agentic-testbed rollout status deployment/jaeger --timeout=120s
"${KUBECTL}" -n agentic-testbed rollout status deployment/agent-b --timeout=120s
"${KUBECTL}" -n agentic-testbed rollout status deployment/agent-a --timeout=120s
"${KUBECTL}" -n agentic-testbed rollout status deployment/mcp-tool-db --timeout=120s

echo
echo "[*] Workloads deployed. NodePorts:"
echo "    Agent A:  http://<node-ip>:30101"
echo "    Agent B:  http://<node-ip>:30102"
echo "    MCP DB:   http://<node-ip>:30201"
echo "    Jaeger:   http://<node-ip>:31686"
