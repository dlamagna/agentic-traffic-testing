#!/usr/bin/env bash
#
# deploy_k8s_workloads.sh
# -----------------------
# Applies the testbed workloads to the agentic-testbed namespace.
# Deploys agents, tools, and Jaeger into the agentic-testbed namespace.
# LLM backend runs externally on Saturn (saturn.cba.upc.edu:8000).
#
# Prerequisites:
#   1. kubectl configured for your Kubernetes cluster (e.g., Kind) (KUBECONFIG or default).
#   2. Images for agent-a, agent-b, and mcp-tool-db available to the cluster
#      (either loaded into Kind via kind load docker-image, or pulled from a registry).
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K8S_DIR="${ROOT_DIR}/infra/k8s"

# Require kubectl configured for the target cluster (Kind or other Kubernetes).
KUBECTL="kubectl"
if ! command -v kubectl >/dev/null 2>&1; then
  echo "[!] 'kubectl' is not available on PATH."
  echo "    Install kubectl and configure KUBECONFIG for your cluster, then re-run this script."
  exit 1
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
