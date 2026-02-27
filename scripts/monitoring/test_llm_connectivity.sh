#!/usr/bin/env bash
#
# test_llm_connectivity.sh
# ------------------------
# Simple script to verify that the k3s observability node can reach
# the external LLM backend (running on Saturn or another host).
#
# Usage (from repo root on the k3s node):
#   ./scripts/monitoring/test_llm_connectivity.sh
#   ./scripts/monitoring/test_llm_connectivity.sh --llm-url http://saturn.cba.upc.edu:8000
#
set -euo pipefail

LLM_URL_DEFAULT="http://saturn.cba.upc.edu:8000"
LLM_URL="${LLM_URL:-${LLM_URL_DEFAULT}}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --llm-url)
      LLM_URL="$2"
      shift 2
      ;;
    *)
      echo "[!] Unknown argument: $1"
      echo "    Usage: $0 [--llm-url http://host:8000]"
      exit 1
      ;;
  esac
done

if ! command -v curl >/dev/null 2>&1; then
  echo "[!] curl is required for this script."
  exit 1
fi

echo "============================================================"
echo "Testing connectivity to external LLM backend"
echo "============================================================"
echo "LLM base URL: ${LLM_URL}"
echo "  Health URL: ${LLM_URL%/}/health"
echo " Metrics URL: ${LLM_URL%/}/metrics"
echo "============================================================"

BASE="${LLM_URL%/}"

echo "[*] Checking /health..."
if curl -fsS "${BASE}/health" >/dev/null 2>&1; then
  echo "  ✓ /health reachable"
else
  echo "  ✗ /health NOT reachable"
  echo "    Try running with: curl -v ${BASE}/health"
  exit 1
fi

echo "[*] Checking /metrics (optional)..."
if METRICS_OUTPUT="$(curl -fsS "${BASE}/metrics" 2>/dev/null | head -n 50)"; then
  echo "  ✓ /metrics reachable"
  if echo "${METRICS_OUTPUT}" | grep -q "llm_request_latency_seconds"; then
    echo "  ✓ Found llm_request_latency_seconds in metrics output"
  else
    echo "  ⚠ /metrics reachable, but llm_request_latency_seconds not found in first 50 lines"
  fi
else
  echo "  ⚠ /metrics not reachable or returned an error (this is non-fatal)"
fi

echo
echo "LLM connectivity test finished."
echo "============================================================"

