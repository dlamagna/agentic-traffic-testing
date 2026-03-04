#!/usr/bin/env bash
#
# install_cilium_on_kind.sh
# -------------------------
# Installs Cilium + Hubble into an existing Kind cluster.
# This is optional on top of the base Kind + kube-prometheus-stack install.
#
# Usage (from repo root):
#   ./scripts/deploy/install_cilium_on_kind.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

K8S_DIR="${ROOT_DIR}/infra/k8s"
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-agentic-testbed}"
CILIUM_VALUES_KIND="${K8S_DIR}/cluster/cilium-values-kind.yaml"

echo "============================================================"
echo "Cilium + Hubble Install on Kind"
echo "============================================================"
echo "Repo root        : ${ROOT_DIR}"
echo "Kind cluster     : ${KIND_CLUSTER_NAME}"
echo "Cilium values    : ${CILIUM_VALUES_KIND}"
echo "============================================================"
echo

if ! command -v kind >/dev/null 2>&1; then
  echo "[!] 'kind' is not installed or not on PATH."
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "[!] 'kubectl' is not installed or not on PATH."
  exit 1
fi

if ! command -v helm >/dev/null 2>&1; then
  echo "[!] 'helm' is not installed or not on PATH."
  exit 1
fi

if ! kind get clusters 2>/dev/null | grep -qx "${KIND_CLUSTER_NAME}"; then
  echo "[!] No Kind cluster named '${KIND_CLUSTER_NAME}' found."
  echo "    Create one first, for example:"
  echo "      kind create cluster --name ${KIND_CLUSTER_NAME}"
  exit 1
fi

echo "[*] Using kubeconfig context for Kind cluster..."
kubectl cluster-info --context "kind-${KIND_CLUSTER_NAME}"

echo
echo "[*] Installing Cilium (CNI) + Hubble..."

helm repo add cilium https://helm.cilium.io/ >/dev/null 2>&1 || true
helm repo update >/dev/null 2>&1

helm upgrade --install cilium cilium/cilium \
  --namespace kube-system \
  --create-namespace \
  -f "${CILIUM_VALUES_KIND}"

echo "[*] Waiting for Cilium DaemonSet to roll out (best effort)..."
kubectl -n kube-system rollout status daemonset/cilium --timeout=180s || true

echo
echo "============================================================"
echo "Cilium + Hubble installation attempted on Kind cluster."
echo "Verify with: cilium status --wait (inside a Cilium CLI container) or:"
echo "  kubectl -n kube-system get pods -l k8s-app=cilium"
echo "============================================================"

