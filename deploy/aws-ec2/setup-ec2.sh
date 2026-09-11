#!/usr/bin/env bash
# Setup del hub Paper Chameleon en AWS EC2 (Ubuntu 22.04/24.04, t3.micro free tier).
# Correr DENTRO de la instancia como root (sudo).
#
#   sudo REPO_URL=https://github.com/Faridmurzone/pdf-traslator \
#        TUNNEL_TOKEN=<token-de-cloudflare>   \
#        ./setup-ec2.sh
#
# Sin TUNNEL_TOKEN la app queda en http://<ip>:8000 (solo para probar;
# para público usá el túnel: cero puertos web abiertos + HTTPS + Access).
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/pdf-chameleon}"
REPO_URL="${REPO_URL:-https://github.com/Faridmurzone/pdf-traslator}"
TUNNEL_TOKEN="${TUNNEL_TOKEN:-}"

[ "$(id -u)" -eq 0 ] || { echo "correr con sudo"; exit 1; }

# 1) Swap 2GB — la instancia tiene 1GB de RAM (PyMuPDF pica picos)
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "swap 2GB activada"
fi

# 2) Docker + compose plugin (Docker Engine nativo Linux, sin overhead)
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y ca-certificates curl git
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  echo "Docker instalado"
fi

# 3) Repo + .env + app
[ -d "$REPO_DIR/.git" ] || git clone "$REPO_URL" "$REPO_DIR"
cd "$REPO_DIR"
git pull --ff-only || true
[ -f .env ] || cp .env.example .env
docker compose up -d --build

# 4) (Opcional) Túnel gestionado desde el dashboard de Cloudflare:
#    Zero Trust → Networks → Tunnels → Create tunnel → copiar el TOKEN
if [ -n "$TUNNEL_TOKEN" ]; then
  if ! command -v cloudflared >/dev/null 2>&1; then
    curl -fsSL -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
    dpkg -i /tmp/cloudflared.deb || apt-get install -f -y
  fi
  cloudflared service install "$TUNNEL_TOKEN"
  systemctl enable cloudflared 2>/dev/null || true
  echo "túnel instalado como servicio systemd"
fi

# IP pública (IMDSv2)
MD_TOKEN=$(curl -fsSX PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || true)
PUBLIC_IP=$(curl -fsSL -H "X-aws-ec2-metadata-token: $MD_TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "<ip-publica>")

echo
echo "✅ Listo."
echo "   App:   http://$PUBLIC_IP:8000   (solo prueba — para público usá el túnel)"
echo "   .env:  $REPO_DIR/.env   (keys del hub, opcionales)"
echo "   Datos: $REPO_DIR/data   (backupeá esta carpeta)"
[ -n "$TUNNEL_TOKEN" ] && echo "   Falta el paso final en el dashboard: Public Hostname → http://localhost:8000"
