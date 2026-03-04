#!/usr/bin/env bash
#
# deploy_cluster.sh
# ------------------
# End-to-end deployment for a local Kind-based observability cluster.
# This script is intended to run on the host where the Kind cluster
# will be created and assumes the LLM backend is running remotely on
# Saturn (SATURN_LLM_HOST / SATURN_LLM_PORT).
#
# It performs:
#   1) Optional connectivity check to the external LLM backend
#   2) Kind + kube-prometheus-stack install
#   3) Cilium + Hubble install on Kind (optional but enabled by default)
#   4) Build + load agent/tool images into the cluster
#   5) Deploy agents, tools, and Jaeger workloads
#
# Usage (from repo root on the k3s node):
#   ./scripts/deploy/deploy_cluster.sh
#   LLM_URL=http://saturn.cba.upc.edu:8000 ./scripts/deploy/deploy_cluster.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

COMPOSE_ENV="${ROOT_DIR}/infra/.env"
if [[ -f "${COMPOSE_ENV}" ]]; then
  # Export variables from infra/.env (skip comments and empty lines)
  set -a
  # shellcheck disable=SC1090
  source <(grep -v '^\s*#' "${COMPOSE_ENV}" | grep -v '^\s*$')
  set +a
fi

LLM_URL="${LLM_URL:-http://${SATURN_LLM_HOST:-saturn.cba.upc.edu}:${SATURN_LLM_PORT:-8000}}"

echo "============================================================"
echo "Agentic Traffic Testbed - Kind Observability Cluster Deploy"
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
echo "[1/5] Testing connectivity to external LLM backend on Saturn..."
if ./scripts/monitoring/test_llm_connectivity.sh --llm-url "${LLM_URL}"; then
  echo "[*] LLM connectivity check passed."
else
  echo "[!] LLM connectivity check failed. You can still continue, but agents"
  echo "    will not be able to call the LLM until this is resolved."
fi

echo
echo "[2/5] Ensuring Kind cluster + kube-prometheus-stack are installed..."

if command -v kubectl >/dev/null 2>&1 && kubectl get nodes >/dev/null 2>&1; then
  echo "[*] Detected existing Kubernetes cluster via kubectl; assuming Kind + monitoring are present."
  echo "    If the cluster is missing kube-prometheus-stack, you can run"
  echo "    ./scripts/deploy/install_kind_cluster.sh to (re)install it."
else
  echo "[*] No working kubectl context detected; running install_kind_cluster.sh..."
  ./scripts/deploy/install_kind_cluster.sh
fi

echo
echo "[3/5] Installing Cilium + Hubble on Kind cluster..."
./scripts/deploy/install_cilium_on_kind.sh || echo "[!] Cilium install reported an error; continuing with deploy."

echo
echo "[4/5] (Optional) Build and publish images to Docker Hub via:"
echo "      DOCKERHUB_USER=dlamagna ./scripts/deploy/publish_k8s_images_to_dockerhub.sh"
echo "      (Skipping automatic publish; assuming images already exist in registry.)"

echo
echo "[5/5] Deploying agents, tools, and Jaeger workloads into Kubernetes..."
./scripts/deploy/deploy_k8s_workloads.sh

echo
echo "============================================================"
echo "Deployment complete."
echo "============================================================"
echo "Services are exposed as NodePorts inside the Kind cluster."
echo "Use 'kubectl port-forward' to access them from your host, e.g.:"
echo
echo "  # Grafana"
echo "  kubectl -n monitoring port-forward svc/kube-prometheus-grafana 3000:80"
echo
echo "  # Jaeger UI"
echo "  kubectl -n agentic-testbed port-forward svc/jaeger 16686:16686"
echo
echo "  # Agent A / Agent B / MCP DB"
echo "  kubectl -n agentic-testbed port-forward svc/agent-a 30101:30101"
echo "  kubectl -n agentic-testbed port-forward svc/agent-b 30102:30102"
echo "  kubectl -n agentic-testbed port-forward svc/mcp-tool-db 30201:30201"
echo
echo "You can now run the health check pointing at the forwarded ports, for example:"
echo
echo "  python3 scripts/monitoring/health_check.py \\"
echo "    --mode k8s \\"
echo "    --llm-url ${LLM_URL}/chat \\"
echo "    --agent-a-url http://localhost:30101/task \\"
echo "    --agent-b-url http://localhost:30102/subtask \\"
echo "    --ui-url http://localhost:3000 \\"
echo "    --skip-monitoring"
echo "============================================================"

