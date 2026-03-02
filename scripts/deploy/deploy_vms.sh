#!/usr/bin/env bash
set -euo pipefail

#
# deploy_vms.sh
# --------------
# Create and (optionally) provision the virtual machines that host the
# multi-VM deployment of the agentic traffic testbed.
#
# This script is intended as the *infrastructure layer* below deploy.sh:
# - deploy_vms.sh: creates VMs (e.g. via Vagrant)
# - deploy.sh:     deploys Docker services onto those VMs
#
# By default this script looks for an infra/Vagrantfile and uses `vagrant up`.
# If you use a different VM orchestrator, you can replace the body of this
# script with your own commands.
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
echo "Agentic Traffic Testbed - VM Deployment"
echo "============================================================"
echo "Deployment mode (from .env): ${DEPLOYMENT_MODE}"
echo "Infra directory:             ${INFRA_DIR}"
echo "============================================================"
echo

if [[ "${DEPLOYMENT_MODE}" != "multi-vm" ]]; then
  echo "[*] DEPLOYMENT_MODE is not 'multi-vm' (current: ${DEPLOYMENT_MODE})."
  echo "[*] Skipping VM creation. Set DEPLOYMENT_MODE=multi-vm in infra/.env to enable."
  exit 0
fi

if [[ ! -f "${INFRA_DIR}/Vagrantfile" ]]; then
  echo "[!] No Vagrantfile found at ${INFRA_DIR}/Vagrantfile."
  echo "[!] Please create a Vagrantfile in the infra/ directory or"
  echo "    replace scripts/deploy/deploy_vms.sh with commands for your VM stack."
  exit 1
fi

if ! command -v vagrant >/dev/null 2>&1; then
  echo "[!] 'vagrant' is not installed or not on PATH."
  echo "[!] Install Vagrant (and VirtualBox or your chosen provider) first."
  exit 1
fi

cd "${INFRA_DIR}"
echo "[*] Bringing up VMs via Vagrant..."
vagrant up

echo
echo "[✓] VM deployment complete."
echo "    You can now set NODE1_HOST / NODE2_HOST / NODE3_HOST / NODE4_HOST in infra/.env"
echo "    to match the VM IPs you chose for NODE{1,2,3,4}_HOST."

