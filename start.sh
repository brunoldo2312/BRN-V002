#!/bin/bash

# 1. Mata qualquer processo antigo do tailscale que possa ter ficado preso
pkill tailscaled || true

# 2. Prepara a pasta e baixa o Tailscale
mkdir -p /tmp/ts
curl -fsSL https://pkgs.tailscale.com/stable/tailscale_1.90.6_amd64.tgz | tar xzf - --strip-components=1 -C /tmp/ts
chmod +x /tmp/ts/tailscale /tmp/ts/tailscaled

# 3. Liga o Tailscale em modo Userspace (sem precisar de root)
/tmp/ts/tailscaled --tun=userspace-networking --socks5-server=127.0.0.1:1055 --state=/tmp/ts/tailscaled.state --socket=/tmp/ts/tailscaled.sock &

# 4. Espera 5 segundos para o daemon iniciar
sleep 5

# 5. Conecta na sua rede Tailscale
/tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock up --auth-key=${TAILSCALE_AUTHKEY}

# 6. Redireciona todo o tráfego do Python pelo túnel do Tailscale
export ALL_PROXY=socks5://127.0.0.1:1055

# 7. Volta para a pasta do projeto e inicia o nó
cd /opt/render/project/src
python3 main.py --headless --no-explorer --web-port $PORT
