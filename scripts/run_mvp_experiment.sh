#!/usr/bin/env bash
set -euo pipefail

#
# run_mvp_experiment.sh
# ----------------------
# Orchestrate a single-host, multi-node MVP experiment.
#
# This script is intentionally simple for the first iteration:
# - It assumes all components (agents, tools, baseline, llm_server)
#   are started manually or via separate scripts / systemd / containers.
# - It focuses on running a workload and collecting logs under logs/.
#

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${ROOT_DIR}/logs"
SCENARIO="${1:-baseline}" # baseline | agentic_simple | agentic_multi_hop
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SCENARIO_LOG_DIR="${LOG_DIR}/${TIMESTAMP}_${SCENARIO}"

mkdir -p "${SCENARIO_LOG_DIR}"

echo "[*] Running MVP experiment"
echo "    ROOT_DIR       = ${ROOT_DIR}"
echo "    SCENARIO       = ${SCENARIO}"
echo "    SCENARIO_LOG_DIR = ${SCENARIO_LOG_DIR}"

echo "[*] NOTE: This MVP script currently assumes that Node1/Node2/Node3 processes"
echo "    (agents, tools, baseline services, llm_server) are already running."
echo "    Future revisions can add container/VM lifecycle management."

# Placeholder workload drivers.
# These will be implemented as we add agents, tools, and baseline services.
case "${SCENARIO}" in
  baseline)
    echo "[*] Executing baseline workload (client -> BaselineSvc)..."
    # TODO: Replace this with a real baseline client once implemented.
    echo "Baseline scenario placeholder" | tee "${SCENARIO_LOG_DIR}/baseline_client.log"
    ;;
  agentic_simple)
    echo "[*] Executing agentic simple workload (AgentA -> Tool1 -> LLM -> response)..."
    # TODO: Replace this with a real Agent A driver once implemented.
    echo "Agentic simple scenario placeholder" | tee "${SCENARIO_LOG_DIR}/agentic_simple_client.log"
    ;;
  agentic_multi_hop)
    echo "[*] Executing agentic multi-hop workload (AgentA -> AgentB -> Tool1/Tool2 -> LLM -> response)..."
    # TODO: Replace this with a real multi-hop driver once implemented.
    echo "Agentic multi-hop scenario placeholder" | tee "${SCENARIO_LOG_DIR}/agentic_multi_hop_client.log"
    ;;
  *)
    echo "Unknown scenario: ${SCENARIO}" >&2
    echo "Usage: $0 [baseline|agentic_simple|agentic_multi_hop]" >&2
    exit 1
    ;;
esac

echo "[*] Experiment complete. Logs stored under: ${SCENARIO_LOG_DIR}"


