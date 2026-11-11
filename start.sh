#!/bin/bash
# set -e REMOVIDO — cada comando tem seu próprio || true ou checagem

echo "[start.sh] iniciando..."

# ─────────────────────────────────────────────────────────────
# 0. Valida env vars
# ─────────────────────────────────────────────────────────────
if [ -z "${TAILSCALE_AUTHKEY}" ]; then
  echo "[start.sh] ⚠️  TAILSCALE_AUTHKEY vazio — P2P via tailnet DESATIVADO"
  echo "[start.sh]    o nó vai rodar apenas em HTTP local ($PORT)"
  TS_ENABLED=0
else
  TS_ENABLED=1
fi

if [ -z "${PORT}" ]; then
  echo "[start.sh] ⚠️  PORT vazio — usando 10000"
  PORT=10000
fi

cd /opt/render/project/src

# ─────────────────────────────────────────────────────────────
# 1-5. Tailscale (só se TAILSCALE_AUTHKEY estiver setado)
# ─────────────────────────────────────────────────────────────
if [ "$TS_ENABLED" = "1" ]; then
  echo "[start.sh] configurando Tailscale..."

  pkill tailscaled 2>/dev/null || true

  mkdir -p /tmp/ts
  if ! curl -fsSL https://pkgs.tailscale.com/stable/tailscale_1.90.6_amd64.tgz \
       | tar xzf - --strip-components=1 -C /tmp/ts; then
    echo "[start.sh] ⚠️  falha ao baixar Tailscale — seguindo sem ele"
    TS_ENABLED=0
  fi

  if [ "$TS_ENABLED" = "1" ]; then
    chmod +x /tmp/ts/tailscale /tmp/ts/tailscaled

    # Daemon SEM socks5-server (Correção 1)
    /tmp/ts/tailscaled \
      --tun=userspace-networking \
      --state=/tmp/ts/tailscaled.state \
      --socket=/tmp/ts/tailscaled.sock \
      >/tmp/ts/tailscaled.log 2>&1 &

    # Espera o socket ficar pronto (até 15s)
    echo "[start.sh] aguardando socket do tailscaled..."
    for i in $(seq 1 15); do
      if [ -S /tmp/ts/tailscaled.sock ]; then
        echo "[start.sh] ✅ socket pronto em ${i}s"
        break
      fi
      sleep 1
    done

    if [ ! -S /tmp/ts/tailscaled.sock ]; then
      echo "[start.sh] ⚠️  socket não criado — seguindo sem Tailscale"
      TS_ENABLED=0
    fi
  fi

  # Autentica com retry (3 tentativas)
  if [ "$TS_ENABLED" = "1" ]; then
    TS_OK=0
    for i in 1 2 3; do
      echo "[start.sh] tailscale up (tentativa $i/3)..."
      if /tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock \
           up --auth-key="${TAILSCALE_AUTHKEY}" \
              --hostname=brn-render 2>&1; then
        TS_OK=1
        echo "[start.sh] ✅ tailscale up OK"
        break
      fi
      sleep 3
    done

    if [ "$TS_OK" = "0" ]; then
      echo "[start.sh] ⚠️  tailscale up falhou 3x — seguindo sem Tailscale"
      TS_ENABLED=0
    fi
  fi

  # Serve (só expõe P2P 6001)
  if [ "$TS_ENABLED" = "1" ]; then
    /tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock \
      serve reset 2>/dev/null || true

    /tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock \
      serve --bg --tcp 6001 tcp://localhost:6001 2>/dev/null || true

    echo "[start.sh] ✅ serve: 6001 → localhost:6001"
    /tmp/ts/tailscale --socket=/tmp/ts/tailscaled.sock ip -4 2>/dev/null \
      | head -1 | xargs -I{} echo "[start.sh]    IP Tailscale: {}"
  fi
fi

# ─────────────────────────────────────────────────────────────
# 6. Limpa proxy herdado do Render (Correção 1b)
# ─────────────────────────────────────────────────────────────
unset ALL_PROXY HTTP_PROXY HTTPS_PROXY all_proxy http_proxy https_proxy
unset NO_PROXY no_proxy
export NO_PROXY="*"
export no_proxy="*"
export PYTHONUNBUFFERED=1

# ─────────────────────────────────────────────────────────────
# 7. Roda o nó
# ─────────────────────────────────────────────────────────────
echo "[start.sh] iniciando main.py (TS_ENABLED=$TS_ENABLED PORT=$PORT)..."
exec python3 main.py --headless --no-explorer --web-port "$PORT"
