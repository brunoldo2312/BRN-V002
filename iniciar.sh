#!/usr/bin/env bash
# ============================================================
#   iniciar_origem.sh - BRN Node ORIGEM (Maquina A) - v3
# ============================================================
set -o pipefail

if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
    C_BLU=$'\033[34m'; C_MAG=$'\033[35m'; C_CYA=$'\033[36m'
    C_RST=$'\033[0m'; C_BLD=$'\033[1m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_MAG=""; C_CYA=""
    C_RST=""; C_BLD=""
fi

info()  { printf '%s[*]%s %s\n' "$C_BLU" "$C_RST" "$*"; }
ok()    { printf '%s[ok]%s %s\n' "$C_GRN" "$C_RST" "$*"; }
warn()  { printf '%s[!]%s %s\n' "$C_YEL" "$C_RST" "$*"; }
err()   { printf '%s[X]%s %s\n' "$C_RED" "$C_RST" "$*" >&2; }
pool()  { printf '%s[POOL]%s %s\n' "$C_MAG" "$C_RST" "$*"; }

SKIP_TAILSCALE=0; FORCE_GUI=0; FORCE_NO_GUI=0; NO_POOL_CLIENT=0
for arg in "$@"; do
    case "$arg" in
        --skip-tailscale) SKIP_TAILSCALE=1 ;;
        --gui) FORCE_GUI=1 ;;
        --no-gui) FORCE_NO_GUI=1 ;;
        --no-pool-client) NO_POOL_CLIENT=1 ;;
        -h|--help)
            echo "Uso: $0 [--skip-tailscale] [--gui] [--no-gui] [--no-pool-client]"
            exit 0 ;;
    esac
done

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"; SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"; cd "$SCRIPT_DIR" || { err "cd falhou"; exit 1; }

echo "============================================================"
echo "  BRN Node - ORIGEM (Maquina A / Ubuntu) - v3"
echo "============================================================"

if command -v python3 >/dev/null 2>&1; then PY=python3; else err "Python 3 nao encontrado"; exit 1; fi
PYVER=$($PY -c "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>/dev/null)
ok "Python $PYVER ($(command -v $PY))"

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

export BRN_MINER_AUTO="${BRN_MINER_AUTO:-0}"
export BRN_MINER_INTERVAL="${BRN_MINER_INTERVAL:-30}"
export BRN_ALLOW_SOLO_MINING="${BRN_ALLOW_SOLO_MINING:-1}"
export BRN_MIN_PEER_STABLE="${BRN_MIN_PEER_STABLE:-1}"
export BRN_UPNP="${BRN_UPNP:-0}"
export BRN_P2P_AUTH="${BRN_P2P_AUTH:-optional}"
export BRN_WEB_PORT="${BRN_WEB_PORT:-5000}"
export BRN_EXPLORER_PORT="${BRN_EXPLORER_PORT:-8080}"
export BRN_P2P_PORT="${BRN_P2P_PORT:-6001}"
export BRN_LOG_LEVEL="${BRN_LOG_LEVEL:-INFO}"
export BRN_POOL_URL_LOCAL="${BRN_POOL_URL_LOCAL:-http://127.0.0.1:5000}"
export PYTHONUNBUFFERED=1
export BRN_IS_ORIGIN=1

# --- CORREÇÃO CRÍTICA: Permitir ligações do Tailscale ---
export BRN_WEB_HOST="0.0.0.0"
export BRN_EXPLORER_HOST="0.0.0.0"

FALTA=""
for f in main.py server.py explorer.py blockchain.py db.py crypto.py wallet.py chain_validator.py checkpoints.py reorg.py brn_config.py brn_logger.py version.py security.py secure_store.py p2p_unified.py p2p_auth.py p2p_secure.py p2p_cipher.py p2p_relay.py discovery_v2.py rendezvous_discovery.py network_guard.py miner_loop.py update_peers.py bech32.py mining_pool.py miner_pool_client.py; do
    [ -f "$f" ] || FALTA="$FALTA $f"
done
if [ -n "$FALTA" ]; then err "Arquivos faltando:$FALTA"; exit 1; fi
ok "Arquivos essenciais OK"

printf 'origin\n' > brn_role.txt
ok "Papel: ORIGEM"

TAILSCALE_FLAG="--no-tailscale"
if [ "$SKIP_TAILSCALE" -eq 1 ]; then info "Tailscale: pulado"
elif [ ! -f "brn_tailscale_setup.py" ]; then info "brn_tailscale_setup.py nao encontrado"
elif [ -z "${TS_AUTH_KEY:-}" ]; then info "TS_AUTH_KEY vazia. Pulando."
else
    info "Configurando Tailscale..."
    if $PY brn_tailscale_setup.py --auto 2>/dev/null; then ok "Tailscale OK"
    else warn "Tailscale nao esta pronto."; fi
    TAILSCALE_FLAG="--no-tailscale"
fi

if [ -n "${BRN_BOOTSTRAP_PEERS:-}" ]; then ok "BRN_BOOTSTRAP_PEERS = $BRN_BOOTSTRAP_PEERS"; else info "BRN_BOOTSTRAP_PEERS vazio"; fi
if [ -n "${BRN_BOOTSTRAP_FILE:-}" ] && [ ! -f "$BRN_BOOTSTRAP_FILE" ]; then echo '[]' > "$BRN_BOOTSTRAP_FILE"; ok "$BRN_BOOTSTRAP_FILE criado (vazio)"; fi

if [ -n "${BRN_NETWORK_SECRET:-}" ]; then SEC_FP="$(printf '%s' "$BRN_NETWORK_SECRET" | $PY -c 'import hashlib,sys;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])' 2>/dev/null)"; else SEC_FP="(vazio)"; fi

HEADLESS_FLAG=""
if [ "$FORCE_NO_GUI" -eq 1 ]; then HEADLESS_FLAG="--headless"; info "GUI desabilitada por --no-gui"
elif [ "$FORCE_GUI" -eq 1 ]; then ok "GUI forcada por --gui"
else
    if ! $PY -c "import webview" >/dev/null 2>&1; then HEADLESS_FLAG="--headless"; warn "pywebview ausente - modo HEADLESS"
    elif ! $PY -c "import gi; gi.require_version('Gtk','3.0')" >/dev/null 2>&1; then HEADLESS_FLAG="--headless"; warn "faltam libs GTK - modo HEADLESS"
    elif [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then HEADLESS_FLAG="--headless"; warn "Sem display grafico - modo HEADLESS"
    else ok "pywebview OK - carteira desktop vai abrir"; fi
fi

NODE_VERSION="$($PY main.py --version 2>/dev/null || echo '?')"
ok "main.py versao: $NODE_VERSION"

POOL_CLIENT_FLAG=""
if [ "$NO_POOL_CLIENT" -eq 1 ]; then POOL_CLIENT_FLAG="--no-pool-client"; pool "PoolClient local: DESATIVADO"
elif [ "${BRN_POOL_ENABLED:-0}" = "1" ]; then pool "PoolClient local: ATIVO"
else pool "PoolClient local: INATIVO"; fi

echo "============================================================"
echo "  CONFIGURACAO ATUAL (ORIGEM - MAQUINA A)"
echo "============================================================"
echo "  Modo       : ORIGEM"
echo "  Versao     : $NODE_VERSION"
echo "  Auth FP    : $SEC_FP..."
echo "  Tracker    : ${BRN_TRACKER:-(nao definido)}"
echo "  Bootstrap  : ${BRN_BOOTSTRAP_PEERS:-(vazio)}"
echo "  Pool Server: ${BRN_POOL_ENABLED:-0}"
echo "  Pool Client: $([ "$NO_POOL_CLIENT" -eq 1 ] && echo 'OFF' || echo "${BRN_POOL_ENABLED:-0}")"
echo "  Pool URL   : ${BRN_POOL_URL_LOCAL:-http://127.0.0.1:5000}"
echo "  Miner local: ${BRN_MINER_AUTO:-0}"
echo "  Dificuldade: min=${BRN_MIN_DIFFICULTY:-5} max=${BRN_MAX_DIFFICULTY:-8}"
echo "  P2P porta  : $BRN_P2P_PORT"
echo "  HTTP       : http://0.0.0.0:$BRN_WEB_PORT"
echo "  Explorer   : http://0.0.0.0:$BRN_EXPLORER_PORT"
echo "  Python     : $PYVER"
echo "  pywebview  : $([ -z "$HEADLESS_FLAG" ] && echo GUI || echo HEADLESS)"
echo "============================================================"
echo "  ATENCAO: deixe este terminal ABERTO enquanto o no roda."
echo

info "Iniciando ORIGEM BRN... (Ctrl+C para encerrar)"
cleanup() { echo; warn "Encerrando no..."; exit 130; }
trap cleanup INT TERM
$PY main.py $POOL_CLIENT_FLAG $TAILSCALE_FLAG $HEADLESS_FLAG
EXITCODE=$?
echo
if [ "$EXITCODE" -ne 0 ]; then
    err "O no saiu com codigo $EXITCODE"
    echo "  rm node_identity.enc && ./iniciar_origem.sh"
    echo "  rm brn_v2_chain.db-wal brn_v2_chain.db-shm"
else ok "No encerrado normalmente."; fi
if [ -t 0 ]; then read -r -p "Pressione ENTER para fechar..." _; fi
exit "$EXITCODE"
