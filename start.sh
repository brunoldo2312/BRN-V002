#!/bin/bash

# 1. Mata processos antigos do tailscale
pkill tailscaled || true

# 2. Prepara a pasta e baixa o Tailscale
mkdir -p /tmp/ts
curl -fsSL https://pkgs.tailscale.com/stable/tailscale_1.90.6_amd64.tgz | tar xzf - --strip-components=1 -C /tmp/ts
chmod +x /tmp/ts/tailscale /tmp/ts/tailscaled

# 3. Inicia o daemon
/tmp/ts/tailscaled --tun=userspace-networking --socks5-server=127.0.0.1:1055 --state=/tmp/ts/tailscaled.state --socket=/tmp/ts/tailscaled.sock &
sleep 5

# 4. Conecta na rede
/tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock up --auth-key=${TAILSCALE_AUTHKEY}

# 5. Limpa regras antigas e expõe a porta P2P (6001)
/tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock serve --tcp=6001 off || true
/tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock serve --bg --tcp 6001 tcp://localhost:6001

# 6. Proxy para o Python
export ALL_PROXY=socks5h://127.0.0.1:1055
export HTTP_PROXY=socks5h://127.0.0.1:1055
export HTTPS_PROXY=socks5h://127.0.0.1:1055

# 7. Inicia o nó
cd /opt/render/project/src
python3 main.py --headless --no-explorer --web-port $PORT
