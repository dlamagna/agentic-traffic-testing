#!/usr/bin/env bash
set -euo pipefail

#
# reset_testbed.sh
# ----------------
# Convenience wrapper that ensures a clean slate by running the uninstall
# script first and then redeploying the full stack.
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[*] Resetting testbed: uninstalling existing resources..."
"${SCRIPT_DIR}/deploy/uninstall_testbed.sh"

echo "[*] Redeploying testbed from a clean state..."
"${SCRIPT_DIR}/deploy/deploy.sh"

echo "[*] Reset complete."


