#!/bin/bash

echo "[start.sh] iniciando..."

if [ -z "${TAILSCALE_AUTHKEY}" ]; then
  echo "[start.sh] ⚠️  TAILSCALE_AUTHKEY vazio"
  TS_ENABLED=0
else
  TS_ENABLED=1
fi

if [ -z "${PORT}" ]; then PORT=10000; fi
cd /opt/render/project/src

if [ "$TS_ENABLED" = "1" ]; then
  pkill tailscaled 2>/dev/null || true
  mkdir -p /tmp/ts
  curl -fsSL https://pkgs.tailscale.com/stable/tailscale_1.90.6_amd64.tgz \
    | tar xzf - --strip-components=1 -C /tmp/ts
  chmod +x /tmp/ts/tailscale /tmp/ts/tailscaled

  # ATENÇÃO: Adicionado o --socks5-server de volta!
  /tmp/ts/tailscaled \
    --tun=userspace-networking \
    --socks5-server=127.0.0.1:1055 \
    --state=/tmp/ts/tailscaled.state \
    --socket=/tmp/ts/tailscaled.sock \
    >/tmp/ts/tailscaled.log 2>&1 &

  for i in $(seq 1 15); do
    if [ -S /tmp/ts/tailscaled.sock ]; then break; fi
    sleep 1
  done

  for i in 1 2 3; do
    if /tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock up --auth-key="${TAILSCALE_AUTHKEY}" --hostname=brn-render 2>&1; then
      TS_OK=1; break
    fi
    sleep 3
  done

  if [ "$TS_OK" = "1" ]; then
    /tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock serve reset 2>/dev/null || true
    /tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock serve --bg --tcp 6001 tcp://localhost:6001 2>/dev/null || true
  else
    TS_ENABLED=0
  fi
fi

# Configura o Python para usar o SOCKS5 ao acessar IPs do Tailscale (100.x)
# O NO_PROXY impede que o localhost (127.0.0.1) passe pelo proxy.
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
export ALL_PROXY="socks5://127.0.0.1:1055"
export NO_PROXY="localhost,127.0.0.1,0.0.0.0,10.0.0.0/8,192.168.0.0/16"
export PYTHONUNBUFFERED=1

echo "[start.sh] iniciando main.py..."
exec python3 main.py --headless --no-explorer --web-port "$PORT"
