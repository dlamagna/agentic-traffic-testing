#!/usr/bin/env bash
set -euo pipefail

#
# delete_vms.sh
# --------------
# Tear down / destroy the virtual machines used for the multi-VM deployment.
#
# This is the counterpart to deploy_vms.sh and is intended to be called
# from higher-level scripts (e.g. reset_testbed.sh with a --hard_reset flag)
# when you want a full infrastructure reset.
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INFRA_DIR="${ROOT_DIR}/infra"

# Load .env if present to check deployment mode
ENV_FILE="${INFRA_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(grep -v '^\s*#' "${ENV_FILE}" | grep -v '^\s*$')
  set +a
fi

DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-multi-vm}"

echo "============================================================"
echo "Agentic Traffic Testbed - VM Deletion"
echo "============================================================"
echo "Deployment mode (from .env): ${DEPLOYMENT_MODE}"
echo "Infra directory:             ${INFRA_DIR}"
echo "============================================================"
echo

if [[ "${DEPLOYMENT_MODE}" != "multi-vm" ]]; then
  echo "[*] DEPLOYMENT_MODE is not 'multi-vm' (current: ${DEPLOYMENT_MODE})."
  echo "[*] Skipping VM deletion."
  exit 0
fi

if [[ ! -f "${INFRA_DIR}/Vagrantfile" ]]; then
  echo "[!] No Vagrantfile found at ${INFRA_DIR}/Vagrantfile."
  echo "[!] If you manage VMs with another tool, replace delete_vms.sh with the"
  echo "    appropriate delete/destroy commands for your environment."
  exit 1
fi

if ! command -v vagrant >/dev/null 2>&1; then
  echo "[!] 'vagrant' is not installed or not on PATH."
  echo "[!] Install Vagrant (and VirtualBox or your chosen provider) first."
  exit 1
fi

cd "${INFRA_DIR}"
echo "[*] Destroying VMs via Vagrant..."
vagrant destroy -f

echo
echo "[✓] VM deletion complete."

