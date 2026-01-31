#!/usr/bin/env bash
set -euo pipefail

#
# apply_network_emulation.sh
# --------------------------
# Apply tc netem rules to containers for realistic network conditions.
# Only useful in distributed mode where containers are on separate networks.
#
# Reads configuration from infra/.env:
#   NETWORK_DELAY_MS      - Base delay in milliseconds (default: 10)
#   NETWORK_JITTER_MS     - Jitter/variation in delay (default: 2)
#   NETWORK_LOSS_PERCENT  - Packet loss percentage (default: 0)
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/infra"

# Load .env file if it exists
ENV_FILE="${COMPOSE_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source <(grep -v '^\s*#' "${ENV_FILE}" | grep -v '^\s*$')
  set +a
fi

# Configuration with defaults
DELAY_MS="${NETWORK_DELAY_MS:-10}"
JITTER_MS="${NETWORK_JITTER_MS:-2}"
LOSS_PERCENT="${NETWORK_LOSS_PERCENT:-0}"

echo "============================================================"
echo "Network Emulation Configuration"
echo "============================================================"
echo "Delay:  ${DELAY_MS}ms"
echo "Jitter: ${JITTER_MS}ms"
echo "Loss:   ${LOSS_PERCENT}%"
echo "============================================================"
echo

# Function to apply netem to a container
apply_netem() {
  local container="$1"
  local interface="${2:-eth0}"
  
  echo "[*] Applying netem to ${container}..."
  
  # Check if container is running
  if ! docker ps --format '{{.Names}}' | grep -q "^${container}$"; then
    echo "    [!] Container ${container} not running, skipping."
    return 0
  fi
  
  # Build netem command
  local netem_cmd="tc qdisc replace dev ${interface} root netem delay ${DELAY_MS}ms ${JITTER_MS}ms"
  if [[ "${LOSS_PERCENT}" != "0" ]]; then
    netem_cmd="${netem_cmd} loss ${LOSS_PERCENT}%"
  fi
  
  # Apply to container
  if docker exec "${container}" sh -c "${netem_cmd}" 2>/dev/null; then
    echo "    [✓] Applied: delay ${DELAY_MS}ms ±${JITTER_MS}ms, loss ${LOSS_PERCENT}%"
  else
    echo "    [!] Failed to apply netem (container may lack NET_ADMIN capability)"
  fi
}

# Function to remove netem from a container
remove_netem() {
  local container="$1"
  local interface="${2:-eth0}"
  
  echo "[*] Removing netem from ${container}..."
  
  if ! docker ps --format '{{.Names}}' | grep -q "^${container}$"; then
    echo "    [!] Container ${container} not running, skipping."
    return 0
  fi
  
  if docker exec "${container}" tc qdisc del dev "${interface}" root 2>/dev/null; then
    echo "    [✓] Removed netem rules"
  else
    echo "    [!] No netem rules to remove or removal failed"
  fi
}

# Parse command
ACTION="${1:-apply}"

case "${ACTION}" in
  apply)
    echo "[*] Applying network emulation to agent containers..."
    echo
    
    # Apply to agents (they communicate across networks)
    apply_netem "agent-a"
    apply_netem "agent-b"
    apply_netem "agent-b-2"
    apply_netem "agent-b-3"
    apply_netem "agent-b-4"
    apply_netem "agent-b-5"
    
    echo
    echo "[✓] Network emulation applied."
    echo "    Traffic between agents will now experience ${DELAY_MS}ms ±${JITTER_MS}ms delay."
    ;;
    
  remove|clear)
    echo "[*] Removing network emulation from agent containers..."
    echo
    
    remove_netem "agent-a"
    remove_netem "agent-b"
    remove_netem "agent-b-2"
    remove_netem "agent-b-3"
    remove_netem "agent-b-4"
    remove_netem "agent-b-5"
    
    echo
    echo "[✓] Network emulation removed."
    ;;
    
  status)
    echo "[*] Checking netem status on containers..."
    echo
    
    for container in agent-a agent-b agent-b-2 agent-b-3 agent-b-4 agent-b-5; do
      if docker ps --format '{{.Names}}' | grep -q "^${container}$"; then
        echo "--- ${container} ---"
        docker exec "${container}" tc qdisc show 2>/dev/null || echo "    (no tc rules)"
        echo
      fi
    done
    ;;
    
  *)
    echo "Usage: $0 [apply|remove|status]"
    echo
    echo "Commands:"
    echo "  apply   - Apply network emulation rules (default)"
    echo "  remove  - Remove network emulation rules"
    echo "  status  - Show current tc rules on containers"
    exit 1
    ;;
esac
