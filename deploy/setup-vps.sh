#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu 22.04/24.04 VPS (Hetzner, DigitalOcean, AWS Lightsail...).
# Run as root:  bash setup-vps.sh you@example.com
# It installs Docker, turns on the firewall (SSH, HTTP, HTTPS only), adds swap, enables
# automatic security updates, and creates a 'plateau' user that can run Docker.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Run this as root (sudo -i)."; exit 1; fi

apt-get update
apt-get -y upgrade
apt-get install -y ca-certificates curl git ufw unattended-upgrades fail2ban

# Docker Engine + Compose plugin, from Docker's official repository.
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

# Firewall: only SSH, HTTP and HTTPS.
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

# 2 GB of swap, so a burst of imports can't run the machine out of memory.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

dpkg-reconfigure -f noninteractive unattended-upgrades

id plateau >/dev/null 2>&1 || adduser --disabled-password --gecos "" plateau
usermod -aG docker plateau
if [[ -f /root/.ssh/authorized_keys ]]; then
  mkdir -p /home/plateau/.ssh && cp /root/.ssh/authorized_keys /home/plateau/.ssh/ && chown -R plateau:plateau /home/plateau/.ssh
fi

echo
echo "Done. Next, as the plateau user:"
echo "  su - plateau"
echo "  git clone https://github.com/<you>/chess_analytics.git && cd chess_analytics/deploy"
echo "  cp .env.example .env && nano .env"
echo "  docker compose up -d --build"
