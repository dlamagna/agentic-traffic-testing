#!/usr/bin/env bash
set -euo pipefail

echo "[*] Installing eBPF tools (BCC and bpftrace)..."

if ! command -v apt &>/dev/null; then
  echo "This script currently supports apt-based systems (Debian/Ubuntu)."
  exit 1
fi

sudo apt update

# Kernel headers for building eBPF probes
sudo apt install -y "linux-headers-$(uname -r)" || true

# BCC (bpfcc-tools) and Python bindings
sudo apt install -y bpfcc-tools python3-bpfcc

# bpftrace for quick custom scripts
sudo apt install -y bpftrace

echo "[*] Installed packages:"
dpkg -l | grep -E "bpfcc|bpftrace" || true

echo "[*] Quick sanity checks (these may print usage and exit):"
if command -v tcplife >/dev/null 2>&1; then
  tcplife -h | head -n 1 || true
fi
if command -v tcpconnect >/dev/null 2>&1; then
  tcpconnect -h | head -n 1 || true
fi

echo "[*] eBPF tools installation complete."
