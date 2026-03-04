#!/usr/bin/env bash
#
# reset_k8_cluster_full.sh
# ------------------------
# Fully resets the local Kind-based observability cluster on this host:
#   - Stops and removes any Docker-based testbed services
#   - Deletes the Kind cluster (via kind delete cluster)
#   - Re-creates the Kind cluster + kube-prometheus-stack
#   - Builds and loads agent/tool images into Kind
#   - Deploys agentic testbed workloads into a fresh namespace
#
# Usage (from repo root on the Kind host):
#   ./scripts/reset_k8_cluster_full.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

UNINSTALL_TESTBED="${ROOT_DIR}/scripts/deploy/uninstall_testbed.sh"
DEPLOY_CLUSTER="${ROOT_DIR}/scripts/deploy/deploy_cluster.sh"
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-agentic-testbed}"

echo "============================================================"
echo "Agentic Traffic Testbed - FULL Kind Reset"
echo "============================================================"
echo "Repo root      : ${ROOT_DIR}"
echo "Uninstall test : ${UNINSTALL_TESTBED}"
echo "Kind cluster   : ${KIND_CLUSTER_NAME}"
echo "Deploy cluster : ${DEPLOY_CLUSTER}"
echo "============================================================"
echo
echo "This will:"
echo "  - Stop and remove Docker-based testbed services (via uninstall_testbed.sh, if present)"
echo "  - Delete the local Kind cluster entirely (kind delete cluster)"
echo "  - Recreate Kind + monitoring + workloads via deploy_cluster.sh"
echo

read -r -p "Are you sure you want to FULLY RESET the Kind cluster on this host? [y/N] " ans
if [[ "${ans}" != "y" && "${ans}" != "Y" ]]; then
  echo "Aborting full reset."
  exit 0
fi

echo
echo "[1/3] Uninstalling Docker-based testbed (if present)..."

if [[ -x "${UNINSTALL_TESTBED}" ]]; then
  echo "[*] Running uninstall_testbed.sh..."
  if ! "${UNINSTALL_TESTBED}"; then
    echo "[!] uninstall_testbed.sh exited with a non-zero status; continuing with k3s uninstall."
  fi
else
  echo "[*] uninstall_testbed.sh not found; skipping Docker testbed cleanup."
fi

echo
echo "[2/3] Deleting existing Kind cluster (if present)..."

if command -v kind >/dev/null 2>&1 && kind get clusters 2>/dev/null | grep -qx "${KIND_CLUSTER_NAME}"; then
  echo "[*] Deleting Kind cluster '${KIND_CLUSTER_NAME}'..."
  kind delete cluster --name "${KIND_CLUSTER_NAME}"
else
  echo "[*] No Kind cluster named '${KIND_CLUSTER_NAME}' detected; skipping delete."
fi

echo
echo "[3/3] Re-deploying full Kind observability stack via deploy_cluster.sh..."

if [[ ! -x "${DEPLOY_CLUSTER}" ]]; then
  echo "[!] deploy_cluster.sh not found or not executable at: ${DEPLOY_CLUSTER}"
  echo "    Expected scripts/deploy/deploy_cluster.sh under the repo root."
  exit 1
fi

"${DEPLOY_CLUSTER}"

echo
echo "============================================================"
echo "[✓] FULL Kind reset complete."
echo "============================================================"

