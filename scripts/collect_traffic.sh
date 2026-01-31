#!/usr/bin/env bash
set -euo pipefail

#
# collect_traffic.sh
# ------------------
# Collect network traffic metrics for the agentic traffic testbed.
#
# DESCRIPTION:
#   This script captures network traffic between agents in distributed mode.
#   It uses tcpdump to capture packets on Docker bridge interfaces and can
#   optionally collect Docker network stats.
#
#   For distributed mode, traffic flows through the inter_agent_network
#   (172.23.0.0/24), so we capture on that bridge interface.
#
# PREREQUISITES:
#   - tcpdump installed: sudo apt install tcpdump
#   - Testbed running in distributed mode
#   - Run with sudo (for tcpdump)
#
# USAGE:
#   sudo ./scripts/collect_traffic.sh [OPTIONS]
#
# OPTIONS:
#   -l, --label NAME      Label for this capture (default: experiment)
#   -d, --duration SECS   Capture duration in seconds (default: run until Ctrl+C)
#   -o, --output DIR      Output directory (default: ./logs/traffic)
#   -s, --stats           Also collect Docker network stats periodically
#   -f, --full-packets    Capture full packets (default: headers only)
#   -h, --help            Show this help
#
# EXAMPLES:
#   # Capture traffic for 60 seconds
#   sudo ./scripts/collect_traffic.sh --duration 60 --label my_experiment
#
#   # Capture with Docker stats
#   sudo ./scripts/collect_traffic.sh --stats --label baseline_test
#
#   # Run until Ctrl+C
#   sudo ./scripts/collect_traffic.sh --label interactive_test
#
# OUTPUT FILES:
#   logs/traffic/
#   ├── packets_<label>_<timestamp>.pcap    # Packet capture
#   ├── stats_<label>_<timestamp>.jsonl     # Docker stats (if --stats)
#   └── summary_<label>_<timestamp>.txt     # Capture summary
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGS_DIR="${ROOT_DIR}/logs/traffic"

# Defaults
LABEL="experiment"
DURATION=""
COLLECT_STATS=false
FULL_PACKETS=false
SNAP_LEN=96  # Headers only by default

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    -l|--label)
      LABEL="$2"
      shift 2
      ;;
    -d|--duration)
      DURATION="$2"
      shift 2
      ;;
    -o|--output)
      LOGS_DIR="$2"
      shift 2
      ;;
    -s|--stats)
      COLLECT_STATS=true
      shift
      ;;
    -f|--full-packets)
      FULL_PACKETS=true
      SNAP_LEN=0  # 0 = capture full packets
      shift
      ;;
    -h|--help)
      head -50 "$0" | grep -E "^#" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      echo "[!] Unknown option: $1"
      exit 1
      ;;
  esac
done

# Create output directory
mkdir -p "${LOGS_DIR}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
PCAP_FILE="${LOGS_DIR}/packets_${LABEL}_${TIMESTAMP}.pcap"
STATS_FILE="${LOGS_DIR}/stats_${LABEL}_${TIMESTAMP}.jsonl"
SUMMARY_FILE="${LOGS_DIR}/summary_${LABEL}_${TIMESTAMP}.txt"

# Find the inter-agent network bridge interface
find_bridge_interface() {
  # Look for the inter_agent_network bridge
  local network_id
  network_id=$(docker network ls --filter "name=inter_agent" --format "{{.ID}}" 2>/dev/null | head -1)
  
  if [[ -z "${network_id}" ]]; then
    echo ""
    return
  fi
  
  # The bridge interface is typically br-<network_id_prefix>
  local bridge="br-${network_id:0:12}"
  
  if ip link show "${bridge}" &>/dev/null; then
    echo "${bridge}"
  else
    echo ""
  fi
}

# Collect Docker stats in background
collect_docker_stats() {
  local output_file="$1"
  local interval="${2:-2}"
  
  while true; do
    # Get stats for agent containers
    docker stats --no-stream --format '{"timestamp":"{{json .}}","time":"'$(date -Iseconds)'"}' \
      agent-a agent-b agent-b-2 agent-b-3 agent-b-4 agent-b-5 llm-backend 2>/dev/null \
      >> "${output_file}" || true
    sleep "${interval}"
  done
}

# Cleanup function
TCPDUMP_PID=""
STATS_PID=""

cleanup() {
  echo
  echo "[*] Stopping capture..."
  
  if [[ -n "${TCPDUMP_PID}" ]]; then
    kill "${TCPDUMP_PID}" 2>/dev/null || true
    wait "${TCPDUMP_PID}" 2>/dev/null || true
  fi
  
  if [[ -n "${STATS_PID}" ]]; then
    kill "${STATS_PID}" 2>/dev/null || true
    wait "${STATS_PID}" 2>/dev/null || true
  fi
  
  # Generate summary
  echo "[*] Generating summary..."
  generate_summary
  
  echo
  echo "[✓] Capture complete!"
  echo "    Packets: ${PCAP_FILE}"
  [[ "${COLLECT_STATS}" == "true" ]] && echo "    Stats:   ${STATS_FILE}"
  echo "    Summary: ${SUMMARY_FILE}"
}

generate_summary() {
  {
    echo "============================================================"
    echo "Traffic Capture Summary"
    echo "============================================================"
    echo "Label:     ${LABEL}"
    echo "Timestamp: ${TIMESTAMP}"
    echo "Duration:  ${DURATION:-"manual (Ctrl+C)"}"
    echo
    echo "--- Capture Details ---"
    
    if [[ -f "${PCAP_FILE}" ]]; then
      echo "Packet file: ${PCAP_FILE}"
      echo "File size:   $(du -h "${PCAP_FILE}" | cut -f1)"
      
      # Basic packet stats if capinfos is available
      if command -v capinfos &>/dev/null; then
        echo
        capinfos -c -d -e "${PCAP_FILE}" 2>/dev/null || true
      fi
      
      # Show packet count and protocols
      if command -v tcpdump &>/dev/null; then
        echo
        echo "--- Protocol Summary ---"
        tcpdump -r "${PCAP_FILE}" -q 2>/dev/null | head -20 || true
        echo "..."
        echo
        echo "Total packets: $(tcpdump -r "${PCAP_FILE}" 2>/dev/null | wc -l)"
      fi
    fi
    
    echo
    echo "--- Network Topology (Distributed Mode) ---"
    echo "Agent A:     172.23.0.10"
    echo "Agent B:     172.23.0.20-24"
    echo "LLM Backend: 172.23.0.30"
    echo "MCP Tool DB: 172.23.0.40"
    echo
    echo "--- Useful Analysis Commands ---"
    echo "# View packet summary"
    echo "tcpdump -r ${PCAP_FILE} -q | head -50"
    echo
    echo "# Filter Agent A to LLM traffic"
    echo "tcpdump -r ${PCAP_FILE} 'host 172.23.0.10 and host 172.23.0.30'"
    echo
    echo "# Filter Agent A to Agent B traffic"
    echo "tcpdump -r ${PCAP_FILE} 'host 172.23.0.10 and net 172.23.0.20/29'"
    echo
    echo "# Show HTTP requests (if full packets captured)"
    echo "tcpdump -r ${PCAP_FILE} -A 'tcp port 8101 or tcp port 8102' | grep -E 'POST|GET|HTTP'"
    echo
    echo "# Open in Wireshark"
    echo "wireshark ${PCAP_FILE}"
    
  } > "${SUMMARY_FILE}"
  
  cat "${SUMMARY_FILE}"
}

# Main
echo "============================================================"
echo "Agentic Traffic Testbed - Traffic Collection"
echo "============================================================"
echo "Label:    ${LABEL}"
echo "Output:   ${LOGS_DIR}"
echo "Duration: ${DURATION:-"run until Ctrl+C"}"
echo "Stats:    ${COLLECT_STATS}"
echo "============================================================"
echo

# Check if running as root (needed for tcpdump)
if [[ $EUID -ne 0 ]]; then
  echo "[!] This script requires root privileges for tcpdump."
  echo "[!] Please run with: sudo $0 $*"
  exit 1
fi

# Check if tcpdump is installed
if ! command -v tcpdump &>/dev/null; then
  echo "[!] tcpdump is not installed."
  echo "[!] Install with: sudo apt install tcpdump"
  exit 1
fi

# Find the bridge interface
BRIDGE_IFACE=$(find_bridge_interface)

if [[ -z "${BRIDGE_IFACE}" ]]; then
  echo "[!] Could not find inter_agent_network bridge interface."
  echo "[!] Make sure the testbed is running in distributed mode:"
  echo "    DEPLOYMENT_MODE=distributed ./scripts/deploy.sh"
  echo
  echo "[*] Falling back to 'any' interface (will capture all traffic)..."
  BRIDGE_IFACE="any"
fi

echo "[*] Capturing on interface: ${BRIDGE_IFACE}"
echo "[*] Packet capture: ${PCAP_FILE}"

# Set up cleanup trap
trap cleanup EXIT INT TERM

# Start Docker stats collection if requested
if [[ "${COLLECT_STATS}" == "true" ]]; then
  echo "[*] Starting Docker stats collection: ${STATS_FILE}"
  collect_docker_stats "${STATS_FILE}" &
  STATS_PID=$!
fi

# Build tcpdump filter for agent traffic
# Capture traffic on the inter-agent network (172.23.0.0/24)
FILTER="net 172.23.0.0/24"

# Start tcpdump
echo "[*] Starting packet capture..."
echo "[*] Filter: ${FILTER}"
echo
echo "Press Ctrl+C to stop capture..."
echo

if [[ -n "${DURATION}" ]]; then
  # Run for specified duration
  timeout "${DURATION}" tcpdump -i "${BRIDGE_IFACE}" -w "${PCAP_FILE}" -s "${SNAP_LEN}" "${FILTER}" &
  TCPDUMP_PID=$!
  
  # Wait with progress
  for ((i=1; i<=DURATION; i++)); do
    echo -ne "\r[*] Capturing... ${i}/${DURATION}s"
    sleep 1
  done
  echo
else
  # Run until Ctrl+C
  tcpdump -i "${BRIDGE_IFACE}" -w "${PCAP_FILE}" -s "${SNAP_LEN}" "${FILTER}" &
  TCPDUMP_PID=$!
  
  # Wait for tcpdump
  wait "${TCPDUMP_PID}" || true
fi
