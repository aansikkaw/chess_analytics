#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu 22.04/24.04 VPS (Hetzner, DigitalOcean, AWS Lightsail...).
# Run as root:  bash setup-vps.sh you@example.com
# It installs Docker, turns on the firewall (SSH, HTTP, HTTPS only), adds swap, enables
# automatic security updates, switches SSH to keys only (if you have a key installed), turns on fail2ban,
# and creates a 'plateau' user that can run Docker.
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
  chmod 700 /home/plateau/.ssh && chmod 600 /home/plateau/.ssh/authorized_keys
fi

# SSH: keys only. Password logins are the main way bots get into servers. Only switched off when an SSH key
# is already installed, so this can never lock you out.
if [[ -s /root/.ssh/authorized_keys ]]; then
  cat > /etc/ssh/sshd_config.d/10-plateau-hardening.conf <<'SSHD'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
MaxAuthTries 3
X11Forwarding no
SSHD
  sshd -t && (systemctl reload ssh 2>/dev/null || systemctl reload sshd)
  echo "SSH now accepts keys only (password logins are off)."
else
  echo "WARNING: no SSH key in /root/.ssh/authorized_keys, so password logins are still on."
  echo "         Add your key (ssh-copy-id root@THIS_SERVER) and run this script again to switch them off."
fi

# fail2ban: block addresses that keep failing SSH logins.
systemctl enable --now fail2ban

echo
echo "Done. Next, as the plateau user:"
echo "  su - plateau"
echo "  git clone https://github.com/<you>/chess_analytics.git && cd chess_analytics/deploy"
echo "  cp .env.example .env && chmod 600 .env && nano .env    # .env holds your secrets: readable by you only"
echo "  docker compose up -d --build"
