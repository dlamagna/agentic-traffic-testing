#!/usr/bin/env bash
set -euo pipefail

#
# install_docker.sh
# ------------------
# Install Docker Engine and Docker Compose (v2) on Debian/Ubuntu.
# Safe to re-run; uses official Docker repositories.
#

if ! command -v apt >/dev/null 2>&1; then
  echo "[!] This script currently supports apt-based systems (Debian/Ubuntu)."
  exit 1
fi

echo "[*] Updating package index..."
sudo apt update

echo "[*] Installing prerequisites..."
sudo apt install -y \
  ca-certificates \
  curl \
  gnupg \
  lsb-release

echo "[*] Setting up Docker APT repository..."
sudo install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
fi
sudo chmod a+r /etc/apt/keyrings/docker.gpg

CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-$(lsb_release -cs)}")"
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${CODENAME} stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

echo "[*] Installing Docker Engine and plugins..."
sudo apt update
sudo apt install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin

echo "[*] Adding current user to 'docker' group (you may need to log out/in)..."
sudo usermod -aG docker "${USER}"

echo "[*] Docker installation complete."
echo "    - Verify:  docker --version"
echo "    - Verify:  docker compose version"
echo "    - You may need to log out and log back in for group changes to apply."


