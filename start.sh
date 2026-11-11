#!/bin/bash
set -e

echo "[start.sh] iniciando..."

# 1. Mata processos antigos
pkill tailscaled 2>/dev/null || true

# 2. Baixa Tailscale
mkdir -p /tmp/ts
curl -fsSL https://pkgs.tailscale.com/stable/tailscale_1.90.6_amd64.tgz \
  | tar xzf - --strip-components=1 -C /tmp/ts
chmod +x /tmp/ts/tailscale /tmp/ts/tailscaled

# 3. Daemon userspace
/tmp/ts/tailscaled \
  --tun=userspace-networking \
  --socks5-server=127.0.0.1:1055 \
  --state=/tmp/ts/tailscaled.state \
  --socket=/tmp/ts/tailscaled.sock &
sleep 5

# 4. Autentica
/tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock \
  up --auth-key=${TAILSCALE_AUTHKEY} --hostname=brn-render

# 4b. [CRÍTICO] Limpa serve configs antigos (o stale 5000)
/tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock serve reset 2>/dev/null || true

# 5. Expõe apenas P2P na 6001
/tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock \
  serve --bg --tcp 6001 tcp://localhost:6001 2>/dev/null || true

# 6. Proxy SOCKS5h (h = DNS via proxy)
export ALL_PROXY=socks5h://127.0.0.1:1055
export HTTP_PROXY=socks5h://127.0.0.1:1055
export HTTPS_PROXY=socks5h://127.0.0.1:1055

# 7. Roda o nó
cd /opt/render/project/src
exec python3 main.py --headless --no-explorer --web-port $PORT
