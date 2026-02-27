#!/usr/bin/env bash
#
# install_k3s_cilium.sh
# ----------------------
# Runs Phase 1 and Phase 2 of the k3s + Cilium + Hubble migration
# (docs/k3s_cilium_migration.md). Requires sudo for k3s install and kubeconfig.
#
# Usage: run from repo root, e.g.:
#   ./scripts/deploy/install_k3s_cilium.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

K8S_DIR="${ROOT_DIR}/infra/k8s"
CILIUM_VERSION="${CILIUM_VERSION:-1.16.0}"

echo "============================================================"
echo "k3s + Cilium + Hubble + Observability Stack Install"
echo "============================================================"

# --- Phase 1: k3s + Cilium ---
echo
echo "[Phase 1] Installing k3s with Cilium..."

if command -v k3s >/dev/null 2>&1; then
  echo "  k3s is already installed: $(k3s --version 2>/dev/null || true)"
  if [[ -t 0 ]]; then
    read -r -p "  Reinstall k3s? [y/N] " ans
    if [[ "${ans}" != "y" && "${ans}" != "Y" ]]; then
      echo "  Skipping k3s install."
    else
      echo "  Uninstalling existing k3s..."
      if [[ -f /usr/local/bin/k3s-uninstall.sh ]]; then
        sudo /usr/local/bin/k3s-uninstall.sh || true
      fi
      INSTALL_K3S=1
    fi
  else
    echo "  Skipping k3s install (already present, non-interactive)."
  fi
else
  INSTALL_K3S=1
fi

if [[ "${INSTALL_K3S:-0}" == "1" ]]; then
  echo "  Installing k3s (Flannel disabled, Traefik disabled for Cilium)..."
  curl -sfL https://get.k3s.io | \
    INSTALL_K3S_EXEC='--flannel-backend=none --disable-network-policy --disable=traefik' \
    sh -s -
fi

echo "  Exporting kubeconfig..."
mkdir -p "${HOME}/.kube"
sudo cp /etc/rancher/k3s/k3s.yaml "${HOME}/.kube/config"
sudo chown "$(id -u):$(id -g)" "${HOME}/.kube/config"
export KUBECONFIG="${HOME}/.kube/config"

# Ensure kubectl uses k3s
if ! command -v kubectl >/dev/null 2>&1; then
  if [[ -x /usr/local/bin/k3s ]]; then
    sudo ln -sf /usr/local/bin/k3s /usr/local/bin/kubectl 2>/dev/null || true
  fi
fi
# Prefer kubectl if in PATH and can talk to cluster; else use k3s kubectl
if command -v kubectl >/dev/null 2>&1 && kubectl get nodes >/dev/null 2>&1; then
  KUBECTL="kubectl"
elif command -v k3s >/dev/null 2>&1; then
  KUBECTL="sudo k3s kubectl"
else
  echo "[!] No kubectl or k3s kubectl available."
  exit 1
fi

echo "  Waiting for node to appear..."
"${KUBECTL}" get nodes || true
echo "  (Node may be NotReady until Cilium is installed.)"

# Install Helm if not present
if ! command -v helm >/dev/null 2>&1; then
  echo "  Installing Helm..."
  curl -sSfL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

echo "  Adding Cilium Helm repo and installing Cilium..."
helm repo add cilium https://helm.cilium.io/
helm repo update

helm upgrade --install cilium cilium/cilium \
  --version "${CILIUM_VERSION}" \
  --namespace kube-system \
  -f "${K8S_DIR}/cluster/cilium-values.yaml"

echo "  Waiting for Cilium to be ready (this may take 1–2 minutes)..."
"${KUBECTL}" -n kube-system rollout status daemonset/cilium --timeout=120s || true

echo
echo "[Phase 1] Done. Verify with: cilium status --wait (install cilium-cli if needed)"
"${KUBECTL}" get nodes

# --- Phase 2: Observability stack ---
echo
echo "[Phase 2] Deploying observability stack (kube-prometheus-stack + Hubble ServiceMonitor)..."

"${KUBECTL}" apply -f "${K8S_DIR}/base/namespace.yaml"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm upgrade --install kube-prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  -f "${K8S_DIR}/monitoring/kube-prometheus-values.yaml"

echo "  Waiting for Prometheus stack to be ready..."
"${KUBECTL}" -n monitoring rollout status deployment/kube-prometheus-prometheus-operator --timeout=120s || true
"${KUBECTL}" -n monitoring rollout status statefulset/prometheus-kube-prometheus-prometheus --timeout=60s || true

echo "  Applying Hubble ServiceMonitor..."
"${KUBECTL}" apply -f "${K8S_DIR}/monitoring/hubble-servicemonitor.yaml"

echo
echo "[Phase 2] Done."
echo
echo "============================================================"
echo "Next steps (Phase 3 – workloads)"
echo "============================================================"
echo "1. Build and load container images into k3s (agents + tools only):"
echo "   ./scripts/deploy/build_and_load_k8s_images.sh"
echo ""
echo "2. Deploy workloads (agents, tools, Jaeger):"
echo "   ./scripts/deploy/deploy_k8s_workloads.sh"
echo ""
echo "Grafana: http://<node-ip>:3001 (admin/admin)"
echo "============================================================"
