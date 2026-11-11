#!/bin/bash

# 1. Mata processos antigos
pkill tailscaled || true

# 2. Prepara e baixa o Tailscale
mkdir -p /tmp/ts
curl -fsSL https://pkgs.tailscale.com/stable/tailscale_1.90.6_amd64.tgz | tar xzf - --strip-components=1 -C /tmp/ts
chmod +x /tmp/ts/tailscale /tmp/ts/tailscaled

# 3. Inicia o daemon do Tailscale
/tmp/ts/tailscaled --tun=userspace-networking --socks5-server=127.0.0.1:1055 --state=/tmp/ts/tailscaled.state --socket=/tmp/ts/tailscaled.sock &
sleep 5

# 4. Conecta à rede Tailscale (sua chave deve começar com tskey-auth-...)
/tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock up --auth-key=${TAILSCALE_AUTHKEY}

# 5. Expõe a porta P2P (6001) para os outros nós da sua rede Tailscale
/tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock serve --bg --tcp 6001 tcp://localhost:6001

# 6. Configura o proxy SOCKS para as conexões de saída do Python
export ALL_PROXY=socks5://127.0.0.1:1055

# 7. Inicia o nó BRN
cd /opt/render/project/src
python3 main.py --headless --no-explorer --web-port $PORT
