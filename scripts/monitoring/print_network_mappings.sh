#!/usr/bin/env bash
set -euo pipefail

#
# print_network_mappings.sh
# -------------------------
# Helper script to print human-readable mappings between:
# - Docker bridge interfaces (br-*) and Docker network names
# - systemd cgroup IDs (docker-*.scope) and container / compose service names
# - Inter-agent network IPs and logical service/container names
#
# Usage:
#   ./scripts/monitoring/print_network_mappings.sh
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if ! command -v docker >/dev/null 2>&1; then
  echo "[!] docker is not installed or not on PATH."
  exit 1
fi

echo "============================================================"
echo "Docker bridge interfaces → Docker networks"
echo "============================================================"

# Map br-<network_id_prefix> → docker network name
docker network ls --format '{{.ID}} {{.Name}}' | while read -r net_id net_name; do
  br="br-${net_id:0:12}"
  if ip link show "$br" >/dev/null 2>&1; then
    printf "  %-18s -> %s\n" "$br" "$net_name"
  fi
done

echo
echo "============================================================"
echo "Containers → systemd cgroup scopes → compose services"
echo "============================================================"

# Map docker-<container_id>.scope → container name / compose service
docker ps --format '{{.ID}} {{.Names}} {{.Label "com.docker.compose.service"}}' | while read -r cid cname csvc; do
  scope="docker-${cid}.scope"
  # If no compose service label, fall back to container name
  if [[ -z "${csvc}" || "${csvc}" == "<no value>" ]]; then
    csvc="${cname}"
  fi
  printf "  scope=%-70s  container=%-20s  service=%s\n" "/system.slice/${scope}" "${cname}" "${csvc}"
done

echo
echo "============================================================"
echo "Inter-agent network IPs → containers / services"
echo "============================================================"

INTER_NET_NAME="infra_inter_agent_network"

# Print which network we're inspecting
if ! docker network inspect "${INTER_NET_NAME}" >/dev/null 2>&1; then
  echo "[!] Docker network '${INTER_NET_NAME}' not found. Skipping IP mapping."
  exit 0
fi

printf "Network: %s\n\n" "${INTER_NET_NAME}"

docker ps --format '{{.ID}} {{.Names}} {{.Label "com.docker.compose.service"}}' | while read -r cid cname csvc; do
  ip_addr="$(docker inspect -f '{{with index .NetworkSettings.Networks "'"${INTER_NET_NAME}"'"}}{{.IPAddress}}{{end}}' "${cid}")" || ip_addr=""
  if [[ -n "${ip_addr}" ]]; then
    if [[ -z "${csvc}" || "${csvc}" == "<no value>" ]]; then
      csvc="${cname}"
    fi
    printf "  %-15s -> container=%-20s service=%s\n" "${ip_addr}" "${cname}" "${csvc}"
  fi
done

echo
echo "Done."

