#!/usr/bin/env bash
#
# reset_k8_cluster.sh
# --------------------
# Logically resets the *testbed* workloads and observability stack running
# on an existing Kubernetes cluster (e.g., Kind), without uninstalling or
# recreating the cluster itself (no sudo required).
#
# This assumes:
#   - A Kubernetes cluster (such as a local Kind cluster) is already running
#   - kubectl is configured to talk to that cluster
#   - Helm is available for the monitoring stack
#
# Usage (from repo root on the cluster host):
#   ./scripts/reset_k8_cluster.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

K8S_DIR="${ROOT_DIR}/infra/k8s"
DEPLOY_WORKLOADS_SCRIPT="${ROOT_DIR}/scripts/deploy/deploy_k8s_workloads.sh"
UNINSTALL_TESTBED="${ROOT_DIR}/scripts/deploy/uninstall_testbed.sh"
LOG_DIR="${ROOT_DIR}/logs"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "[!] 'kubectl' is not available on PATH. Ensure your admin has"
  echo "    configured kubeconfig for your user, then re-run this script."
  exit 1
fi

KUBECTL="kubectl"

echo "============================================================"
echo "Agentic Traffic Testbed - Logical k8s Reset (no sudo)"
echo "============================================================"
echo "Repo root : ${ROOT_DIR}"
echo "Workloads : ${DEPLOY_WORKLOADS_SCRIPT}"
echo "============================================================"
echo
echo "This will:"
echo "  - Stop and remove any Docker-based testbed services (if uninstall_testbed.sh is present)"
echo "  - Clear testbed logs and GPU caches (via uninstall_testbed.sh, if available)"
echo "  - Delete the agentic-testbed and monitoring namespaces (workloads + Prometheus/Grafana)"
echo "  - Recreate the monitoring stack via Helm (kube-prometheus-stack)"
echo "  - Redeploy agentic testbed workloads into a clean namespace"
echo

read -r -p "Are you sure you want to RESET the Kubernetes namespaces and monitoring stack on this host? [y/N] " ans
if [[ "${ans}" != "y" && "${ans}" != "Y" ]]; then
  echo "Aborting reset."
  exit 0
fi

echo
echo "[1/3] Cleaning Docker-based testbed, logs, and GPU cache (if present)..."

if [[ -x "${UNINSTALL_TESTBED}" ]]; then
  echo "[*] Running uninstall_testbed.sh to clear Docker services, logs, and GPU cache..."
  if ! "${UNINSTALL_TESTBED}"; then
    echo "[!] uninstall_testbed.sh exited with a non-zero status; continuing with k8s reset."
  fi
else
  echo "[*] uninstall_testbed.sh not found; skipping Docker testbed cleanup."
  if [[ -d "${LOG_DIR}" ]]; then
    echo "    Removing logs under ${LOG_DIR}..."
    rm -rf "${LOG_DIR}"/* 2>/dev/null || true
  fi
fi

echo
echo "[2/3] Resetting Kubernetes namespaces and monitoring stack (no sudo)..."

echo "[*] Deleting agentic-testbed namespace (if present)..."
"${KUBECTL}" delete namespace agentic-testbed --ignore-not-found=true || true

echo "[*] Deleting monitoring namespace (if present)..."
"${KUBECTL}" delete namespace monitoring --ignore-not-found=true || true

echo "[*] Recreating monitoring stack via Helm..."
if command -v helm >/dev/null 2>&1; then
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo update >/dev/null 2>&1 || true

  helm upgrade --install kube-prometheus prometheus-community/kube-prometheus-stack \
    --namespace monitoring --create-namespace \
    -f "${K8S_DIR}/monitoring/kube-prometheus-values.yaml"

  # Hubble-specific ServiceMonitor is only relevant when Cilium/Hubble
  # are installed; skip it by default in the Kind-based setup.
else
  echo "[!] 'helm' is not installed or not on PATH; skipping monitoring stack reinstall."
  echo "    Install Helm or ask your admin if you need Prometheus/Grafana re-created."
fi

echo
echo "[3/3] Re-deploying agentic testbed workloads into a clean namespace..."

if [[ ! -x "${DEPLOY_WORKLOADS_SCRIPT}" ]]; then
  echo "[!] Workloads deploy script not found or not executable at: ${DEPLOY_WORKLOADS_SCRIPT}"
  echo "    Expected scripts/deploy/deploy_k8s_workloads.sh under the repo root."
  exit 1
fi

"${DEPLOY_WORKLOADS_SCRIPT}"

echo
echo "============================================================"
echo "[✓] Cluster reset complete."
echo "============================================================"

