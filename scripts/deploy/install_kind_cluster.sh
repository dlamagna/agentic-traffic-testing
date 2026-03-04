#!/usr/bin/env bash
#
# install_kind_cluster.sh
# -----------------------
# Creates (or reuses) a local Kind cluster for the agentic traffic testbed
# and installs the observability stack (kube-prometheus-stack).
#
# This replaces the previous k3s+Cilium-based install path for local dev.
#
# Usage (from repo root):
#   ./scripts/deploy/install_kind_cluster.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

K8S_DIR="${ROOT_DIR}/infra/k8s"
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-agentic-testbed}"
KIND_CONFIG="${K8S_DIR}/kind-config.yaml"

echo "============================================================"
echo "Kind + kube-prometheus-stack Install"
echo "============================================================"
echo "Repo root        : ${ROOT_DIR}"
echo "Kind cluster     : ${KIND_CLUSTER_NAME}"
echo "Kind config file : ${KIND_CONFIG}"
echo "============================================================"
echo

if ! command -v kind >/dev/null 2>&1; then
  echo "[!] 'kind' is not installed or not on PATH."
  echo "    Install Kind from https://kind.sigs.k8s.io/ and try again."
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "[!] 'kubectl' is not installed or not on PATH."
  exit 1
fi

if ! command -v helm >/dev/null 2>&1; then
  echo "[!] 'helm' is not installed or not on PATH."
  echo "    Install Helm from https://helm.sh/docs/intro/install/ and try again."
  exit 1
fi

echo "[*] Ensuring Kind cluster '${KIND_CLUSTER_NAME}' exists..."

if kind get clusters 2>/dev/null | grep -qx "${KIND_CLUSTER_NAME}"; then
  echo "    Kind cluster '${KIND_CLUSTER_NAME}' already exists; reusing."
else
  if [[ -f "${KIND_CONFIG}" ]]; then
    echo "    Creating Kind cluster '${KIND_CLUSTER_NAME}' with config ${KIND_CONFIG}..."
    kind create cluster --name "${KIND_CLUSTER_NAME}" --config "${KIND_CONFIG}"
  else
    echo "    Creating Kind cluster '${KIND_CLUSTER_NAME}' with default config..."
    kind create cluster --name "${KIND_CLUSTER_NAME}"
  fi
fi

echo
echo "[*] Using kubeconfig context for Kind cluster..."
kubectl cluster-info --context "kind-${KIND_CLUSTER_NAME}"

echo
echo "[*] Installing observability stack (kube-prometheus-stack)..."

kubectl apply -f "${K8S_DIR}/base/namespace.yaml"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null 2>&1

helm upgrade --install kube-prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  -f "${K8S_DIR}/monitoring/kube-prometheus-values.yaml"

echo "[*] Waiting briefly for monitoring pods to start (non-blocking)..."
kubectl -n monitoring get pods

echo
echo "[*] (Optional) To install Cilium + Hubble on this Kind cluster, run:"
echo "    ./scripts/deploy/install_cilium_on_kind.sh"

echo
echo "============================================================"
echo "Kind cluster and observability stack ready."
echo "============================================================"

