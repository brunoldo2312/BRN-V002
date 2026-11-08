#!/usr/bin/env bash
set -o pipefail

C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
C_BLU=$'\033[34m'; C_RST=$'\033[0m'

info()  { printf '%s[*]%s %s\n' "$C_BLU" "$C_RST" "$*"; }
ok()    { printf '%s[ok]%s %s\n' "$C_GRN" "$C_RST" "$*"; }
warn()  { printf '%s[!]%s %s\n' "$C_YEL" "$C_RST" "$*"; }
err()   { printf '%s[X]%s %s\n' "$C_RED" "$C_RST" "$*" >&2; }

SKIP_TAILSCALE=0; FORCE_GUI=0; FORCE_NO_GUI=0; NO_POOL_CLIENT=0
for arg in "$@"; do
    case "$arg" in
        --skip-tailscale) SKIP_TAILSCALE=1 ;;
        --gui) FORCE_GUI=1 ;;
        --no-gui) FORCE_NO_GUI=1 ;;
        --no-pool-client) NO_POOL_CLIENT=1 ;;
        -h|--help) echo "Uso: $0 [--skip-tailscale] [--gui] [--no-gui] [--no-pool-client]"; exit 0 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { err "cd falhou"; exit 1; }

echo "============================================================"
echo "  BRN Node - ORIGEM (Ubuntu)"
echo "============================================================"

if command -v python3 >/dev/null 2>&1; then PY=python3; else err "Python 3 nao encontrado"; exit 1; fi
ok "Python $($PY --version 2>&1)"

if [ ! -f "brn_network.env" ]; then err "brn_network.env NAO existe"; exit 1; fi
info "Carregando brn_network.env..."
set -a
while IFS='=' read -r k v || [ -n "$k" ]; do
    k="$(printf '%s' "$k" | tr -d '\r\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    v="$(printf '%s' "$v" | tr -d '\r\n')"
    [ -z "$k" ] && continue
    case "$k" in \#*) continue ;; esac
    case "$k" in *[!A-Za-z0-9_]*) continue ;; esac
    case "$v" in \"*\") v="${v#\"}"; v="${v%\"}" ;; \'*\') v="${v#\'}"; v="${v%\'}" ;; esac
    export "$k=$v"
done < <(sed 's/\r$//' brn_network.env)
set +a
ok "brn_network.env carregado"

: "${BRN_NODE_PASSWORD:=senha-no-local-2026}"
: "${BRN_WEB_PASS:=senha-carteira-2026}"
: "${BRN_WALLET_SESSION_PASS:=$BRN_WEB_PASS}"
export BRN_NODE_PASSWORD BRN_WEB_PASS BRN_WALLET_SESSION_PASS

export BRN_WEB_PORT="${BRN_WEB_PORT:-5000}"
export BRN_EXPLORER_PORT="${BRN_EXPLORER_PORT:-8080}"
export BRN_P2P_PORT="${BRN_P2P_PORT:-6001}"
export BRN_LOG_LEVEL="${BRN_LOG_LEVEL:-INFO}"
export BRN_POOL_URL_LOCAL="${BRN_POOL_URL_LOCAL:-http://127.0.0.1:5000}"
export PYTHONUNBUFFERED=1
export BRN_IS_ORIGIN=1
export BRN_WEB_HOST="0.0.0.0"
export BRN_EXPLORER_HOST="0.0.0.0"

# --- CORREÇÃO CRÍTICA: Desabilita o Rate Limit para rede local ---
export BRN_DISABLE_RATE_LIMIT=1

FALTA=""
for f in main.py server.py explorer.py blockchain.py db.py crypto.py wallet.py chain_validator.py checkpoints.py reorg.py brn_config.py brn_logger.py version.py security.py secure_store.py p2p_unified.py p2p_auth.py p2p_secure.py p2p_cipher.py p2p_relay.py discovery_v2.py rendezvous_discovery.py network_guard.py miner_loop.py update_peers.py bech32.py mining_pool.py miner_pool_client.py; do
    [ -f "$f" ] || FALTA="$FALTA $f"
done
if [ -n "$FALTA" ]; then err "Arquivos faltando:$FALTA"; exit 1; fi
ok "Arquivos essenciais OK"

printf 'origin\n' > brn_role.txt
ok "Papel: ORIGEM"

if [ -n "${BRN_BOOTSTRAP_PEERS:-}" ]; then ok "BRN_BOOTSTRAP_PEERS = $BRN_BOOTSTRAP_PEERS"; else info "BRN_BOOTSTRAP_PEERS vazio"; fi

if [ -n "${BRN_NETWORK_SECRET:-}" ]; then
    SEC_FP="$(printf '%s' "$BRN_NETWORK_SECRET" | $PY -c 'import hashlib,sys;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])' 2>/dev/null)"
else
    SEC_FP="(vazio)"
fi

HEADLESS_FLAG=""
if [ "$FORCE_NO_GUI" -eq 1 ]; then
    HEADLESS_FLAG="--headless"; info "GUI desabilitada por --no-gui"
elif [ "$FORCE_GUI" -eq 1 ]; then
    ok "GUI forcada por --gui"
else
    if ! $PY -c "import webview" >/dev/null 2>&1; then
        HEADLESS_FLAG="--headless"; warn "pywebview ausente - modo HEADLESS"
    elif ! $PY -c "import gi; gi.require_version('Gtk','3.0')" >/dev/null 2>&1; then
        HEADLESS_FLAG="--headless"; warn "faltam libs GTK - modo HEADLESS"
    elif [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
        HEADLESS_FLAG="--headless"; warn "Sem display grafico - modo HEADLESS"
    else
        ok "pywebview OK - carteira desktop vai abrir"
    fi
fi

NODE_VERSION="$($PY main.py --version 2>/dev/null || echo '?')"
ok "main.py versao: $NODE_VERSION"

POOL_CLIENT_FLAG=""
if [ "$NO_POOL_CLIENT" -eq 1 ]; then POOL_CLIENT_FLAG="--no-pool-client"; fi

echo "============================================================"
echo "  CONFIGURACAO ATUAL (ORIGEM)"
echo "============================================================"
echo "  Modo       : ORIGEM"
echo "  Versao     : $NODE_VERSION"
echo "  Auth FP    : $SEC_FP..."
echo "  Bootstrap  : ${BRN_BOOTSTRAP_PEERS:-(vazio)}"
echo "  PBFT Enforce: ${BRN_PBFT_ENFORCE:-0}"
echo "  PBFT Start : ${BRN_PBFT_START_HEIGHT:-0}"
echo "  P2P porta  : $BRN_P2P_PORT"
echo "  HTTP       : http://0.0.0.0:$BRN_WEB_PORT"
echo "  Explorer   : http://0.0.0.0:$BRN_EXPLORER_PORT"
echo "  Rate Limit : DESLIGADO"
echo "============================================================"
echo

info "Iniciando ORIGEM BRN... (Ctrl+C para encerrar)"
cleanup() { echo; warn "Encerrando no..."; exit 130; }
trap cleanup INT TERM

# [FIX CRÍTICO] Nao passa --no-tailscale (main.py nao aceita mais)
$PY main.py --origem $POOL_CLIENT_FLAG $HEADLESS_FLAG

EXITCODE=$?
echo
if [ "$EXITCODE" -ne 0 ]; then
    err "O no saiu com codigo $EXITCODE"
else
    ok "No encerrado normalmente."
fi
if [ -t 0 ]; then read -r -p "Pressione ENTER para fechar..." _; fi
exit "$EXITCODE"
