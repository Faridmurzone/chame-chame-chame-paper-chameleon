#!/usr/bin/env bash
# Setup del hub casero (Paper Chameleon) en la Mac Mini.
#
# Prerequisitos:
#   - Repo clonado y brew instalado
#   - Túnel de Cloudflare creado:
#       cloudflared tunnel login
#       cloudflared tunnel create paper-chameleon
#   - TUNNEL_ID en el entorno (sale del create / ~/.cloudflared/*.json)
#
# Uso:
#   TUNNEL_ID=<id> ./setup.sh /ruta/al/repo papers.tudominio.com
set -euo pipefail

REPO_DIR="${1:?uso: TUNNEL_ID=<id> $0 /ruta/al/repo papers.tudominio.com}"
DOMAIN="${2:?uso: TUNNEL_ID=<id> $0 /ruta/al/repo papers.tudominio.com}"
TUNNEL_ID="${TUNNEL_ID:?exportá TUNNEL_ID (cloudflared tunnel create paper-chameleon)}"

LABEL_APP="ar.paperchameleon.app"
LABEL_TUNNEL="ar.paperchameleon.tunnel"

command -v brew >/dev/null || { echo "Instalá Homebrew primero: https://brew.sh"; exit 1; }
command -v cloudflared >/dev/null || brew install cloudflared
CLOUDFLARED="$(command -v cloudflared)"

python3 - <<'PY' || { echo "Necesitás Python 3.10+ (brew install python@3.13)"; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

[ -f "$TUNNEL_ID.json" ] && TUNNEL_ID="${TUNNEL_ID%.json}"
CRED_FILE="$HOME/.cloudflared/$TUNNEL_ID.json"
[ -f "$CRED_FILE" ] || { echo "No está $CRED_FILE — ¿corriste 'cloudflared tunnel create paper-chameleon'?"; exit 1; }

cd "$REPO_DIR"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q -e ".[web,providers]"

mkdir -p "$HOME/.cloudflared" "$HOME/.pdf-chameleon"
PLISTS_DIR="$(cd "$(dirname "$0")" && pwd)"

cat > "$HOME/.cloudflared/config.yml" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CRED_FILE
ingress:
  - hostname: $DOMAIN
    service: http://localhost:8000
  - service: http_status:404
EOF

sed -e "s|__REPO__|$REPO_DIR|g" -e "s|__HOME__|$HOME|g" "$PLISTS_DIR/app.plist.tmpl" > "$HOME/Library/LaunchAgents/$LABEL_APP.plist"
sed -e "s|__CLOUDFLARED__|$CLOUDFLARED|g" -e "s|__HOME__|$HOME|g" "$PLISTS_DIR/tunnel.plist.tmpl" > "$HOME/Library/LaunchAgents/$LABEL_TUNNEL.plist"

launchctl unload "$HOME/Library/LaunchAgents/$LABEL_APP.plist" 2>/dev/null || true
launchctl load "$HOME/Library/LaunchAgents/$LABEL_APP.plist"
launchctl unload "$HOME/Library/LaunchAgents/$LABEL_TUNNEL.plist" 2>/dev/null || true
launchctl load "$HOME/Library/LaunchAgents/$LABEL_TUNNEL.plist"

# DNS del dominio → túnel (si el dominio ya está en Cloudflare)
cloudflared tunnel route dns -f "$TUNNEL_ID" "$DOMAIN" || echo "⚠️  Ruteá el DNS a mano: cloudflared tunnel route dns $TUNNEL_ID $DOMAIN"

echo
echo "✅ Hub instalado. Verificá:"
echo "   curl -s http://127.0.0.1:8000/api/meta"
echo "   curl -s https://$DOMAIN/api/meta"
echo
echo "Logs: ~/.pdf-chameleon/*.log"
