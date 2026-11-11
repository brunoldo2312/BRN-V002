#!/bin/bash

# 1. Mata qualquer processo antigo do tailscale
pkill tailscaled || true

# 2. Prepara a pasta e baixa o Tailscale
mkdir -p /tmp/ts
curl -fsSL https://pkgs.tailscale.com/stable/tailscale_1.90.6_amd64.tgz | tar xzf - --strip-components=1 -C /tmp/ts
chmod +x /tmp/ts/tailscale /tmp/ts/tailscaled

# 3. Inicia o daemon do Tailscale (modo userspace)
/tmp/ts/tailscaled --tun=userspace-networking --socks5-server=127.0.0.1:1055 --state=/tmp/ts/tailscaled.state --socket=/tmp/ts/tailscaled.sock &
sleep 5

# 4. Conecta à sua rede Tailscale
/tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock up --auth-key=${TAILSCALE_AUTHKEY}

# 5. Expõe a porta P2P (6001) do nó para os outros peers na rede Tailscale
/tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock serve --bg --tcp 6001 tcp://localhost:6001

# 6. Configura o proxy SOCKS5 para todo o tráfego do Python
# ⚠️ IMPORTANTE: Usa socks5h:// para resolver o DNS através do proxy do Tailscale
export ALL_PROXY=socks5h://127.0.0.1:1055
export HTTP_PROXY=socks5h://127.0.0.1:1055
export HTTPS_PROXY=socks5h://127.0.0.1:1055

# 7. Volta para a pasta do projeto e inicia o nó
cd /opt/render/project/src
python3 main.py --headless --no-explorer --web-port $PORT
