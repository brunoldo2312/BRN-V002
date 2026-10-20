#!/usr/bin/env bash
# ============================================================
#   iniciar_cliente.sh - BRN Node CLIENTE (Maquina B) - v1
# ============================================================
#
# Diferencas em relacao ao iniciar_origem.sh:
#   1. NAO cria genesis (sincroniza do ORIGEM via P2P)
#   2. NAO roda pool server (passa --no-pool para main.py)
#   3. Conecta ao pool do ORIGEM via BRN_POOL_URL
#   4. Registra heartbeat em peers.json via BRN_SEU_ENDERECO
#   5. Bootstrap peers OBRIGATORIO (IP:porta do ORIGEM)
#   6. Gera template brn_network.env se nao existir
#
# Uso:
#   ./iniciar_cliente.sh                         # modo normal
#   ./iniciar_cliente.sh --origin-ip 100.x.y.z   # forca IP do origem
#   ./iniciar_cliente.sh --my-ip 192.168.0.20    # forca IP desta maquina
#   ./iniciar_cliente.sh --gui                   # forca carteira desktop
#   ./iniciar_cliente.sh --no-gui                # forca headless
#   ./iniciar_cliente.sh --no-pool-client        # desliga mineracao via pool
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
cli()   { printf '%s[CLIENTE]%s %s\n' "$C_CYA" "$C_RST" "$*"; }

# ============================================================
# ARGS
# ============================================================
SKIP_TAILSCALE=0; FORCE_GUI=0; FORCE_NO_GUI=0; NO_POOL_CLIENT=0
ORIGIN_IP_ARG=""; MY_IP_ARG=""
ORIGIN_P2P_PORT_ARG="6001"; ORIGIN_HTTP_PORT_ARG="5000"

for arg in "$@"; do
    case "$arg" in
        --origin-ip=*)     ORIGIN_IP_ARG="${arg#*=}" ;;
        --origin-ip)       shift_next_origin=1 ;;
        --my-ip=*)         MY_IP_ARG="${arg#*=}" ;;
        --my-ip)           shift_next_my=1 ;;
        --origin-p2p-port=*)  ORIGIN_P2P_PORT_ARG="${arg#*=}" ;;
        --origin-http-port=*) ORIGIN_HTTP_PORT_ARG="${arg#*=}" ;;
        --skip-tailscale)  SKIP_TAILSCALE=1 ;;
        --gui)             FORCE_GUI=1 ;;
        --no-gui)          FORCE_NO_GUI=1 ;;
        --no-pool-client)  NO_POOL_CLIENT=1 ;;
        -h|--help)
            sed -n '3,22p' "$0" | sed 's/^# *//'
            exit 0 ;;
    esac
done

# Suporte a "--origin-ip X" (separado por espaço) - parsing manual
prev=""
for arg in "$@"; do
    case "$prev" in
        --origin-ip)   ORIGIN_IP_ARG="$arg" ;;
        --my-ip)       MY_IP_ARG="$arg" ;;
    esac
    prev="$arg"
done

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"
cd "$SCRIPT_DIR" || { err "cd falhou"; exit 1; }

echo "============================================================"
echo "  BRN Node - CLIENTE (Maquina B) - v1"
echo "============================================================"

# ============================================================
# PYTHON
# ============================================================
if command -v python3 >/dev/null 2>&1; then PY=python3; else err "Python 3 nao encontrado"; exit 1; fi
PYVER=$($PY -c "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>/dev/null)
ok "Python $PYVER ($(command -v $PY))"

# ============================================================
# TEMPLATE brn_network.env SE NAO EXISTIR
# ============================================================
if [ ! -f "brn_network.env" ]; then
    warn "brn_network.env NAO existe"
    info "Gerando TEMPLATE para MAQUINA B (CLIENTE)..."
    cat > brn_network.env <<'ENV_TEMPLATE'
# ============================================================
# BRN Network - MAQUINA B (CLIENTE)
# ============================================================
# ATENCAO: os valores SECRETOS (BRN_NETWORK_SECRET) devem ser
# COPIADOS do arquivo brn_network.env do ORIGEM. Se forem diferentes,
# o auth_tag dos blocos > 6000 falha e o sync para.
# ============================================================

# --- Segredos (COPIAR do ORIGEM) ---
BRN_NETWORK_SECRET=
# BRN_CHAIN_ID=mainnet

# --- Identidade DESTE no (gerar NOVO, nao copiar) ---
# Gere com: python3 -c "import secrets;print(secrets.token_hex(24))"
BRN_NODE_PASSWORD=
BRN_WALLET_SESSION_PASS=

# --- Networking (OBRIGATORIO) ---
# IP:porta do ORIGEM (ex: 100.84.11.48:6001 ou 192.168.0.19:6001)
BRN_BOOTSTRAP_PEERS=
# IP:porta DESTA maquina (ex: 192.168.0.20:6001) - auto-detectado se vazio
BRN_SEU_ENDERECO=
# URL HTTP do pool do ORIGEM - auto-derivado de BRN_BOOTSTRAP_PEERS se vazio
BRN_POOL_URL=

# --- Portas ---
BRN_P2P_PORT=6001
BRN_WEB_PORT=5000
BRN_EXPLORER_PORT=8080

# --- Modo (NAO MUDE para cliente) ---
BRN_IS_ORIGIN=0
BRN_MINER_AUTO=1
BRN_LOG_LEVEL=INFO

# --- Opcional ---
# BRN_BOOTSTRAP_HTTP_PORT=5000
# BRN_UPNP=0
ENV_TEMPLATE
    chmod 600 brn_network.env
    ok "brn_network.env criado como template"
    echo
    warn "EDITE o arquivo brn_network.env ANTES de rodar novamente."
    echo
    echo "  Valores obrigatorios a preencher:"
    echo "    BRN_NETWORK_SECRET  = (copie do ORIGEM)"
    echo "    BRN_NODE_PASSWORD   = (gere com: python3 -c \"import secrets;print(secrets.token_hex(24))\" )"
    echo "    BRN_WALLET_SESSION_PASS = (gere com: python3 -c \"import secrets;print(secrets.token_hex(24))\" )"
    echo "    BRN_BOOTSTRAP_PEERS = IP_DO_ORIGEM:6001"
    echo
    exit 0
fi

# ============================================================
# CARREGAR brn_network.env
# ============================================================
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

# ============================================================
# FORCAR CLIENTE (sobrescreve o que estiver no env)
# ============================================================
if [ "${BRN_IS_ORIGIN:-0}" = "1" ]; then
    warn "BRN_IS_ORIGIN=1 detectado. FORCANDO para 0 (CLIENTE)."
fi
export BRN_IS_ORIGIN=0

# ============================================================
# AUTO-DETECT IP DESTA MAQUINA
# ============================================================
detect_my_ip() {
    local ip=""
    # 1) Tailscale
    if command -v tailscale >/dev/null 2>&1; then
        ip=$(tailscale ip -4 2>/dev/null | head -1)
        if [ -n "$ip" ]; then echo "$ip"; return; fi
    fi
    # 2) hostname -I (primeiro IP nao-loopback)
    ip=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^127\.' | head -1)
    if [ -n "$ip" ]; then echo "$ip"; return; fi
    # 3) ip route get
    ip=$(ip route get 1 2>/dev/null | awk '{print $7; exit}')
    if [ -n "$ip" ]; then echo "$ip"; return; fi
    # 4) fallback
    echo "127.0.0.1"
}

# ============================================================
# PARSE DE BOOTSTRAP (extrai IP do origem para auto-config)
# ============================================================
parse_bootstrap_first_origin() {
    # Aceita formatos: ip:p2p, ip:p2p:http, ip:p2p:http/https
    local first="${1%%,*}"
    first="${first%% *}"
    [ -z "$first" ] && { echo ""; return; }
    # Remove scheme (ex: /https)
    first="${first%%/*}"
    # Extrai IP (parte antes do primeiro ':')
    echo "${first%%:*}"
}

# ============================================================
# ENV VARS ESSENCIAIS - preencher se faltando
# ============================================================
P2P_PORT="${BRN_P2P_PORT:-6001}"
WEB_PORT="${BRN_WEB_PORT:-5000}"

# --- BRN_SEU_ENDERECO ---
if [ -z "${MY_IP_ARG:-}" ] && [ -z "${BRN_SEU_ENDERECO:-}" ]; then
    DETECTED_IP="$(detect_my_ip)"
    if [ "$DETECTED_IP" != "127.0.0.1" ]; then
        BRN_SEU_ENDERECO="${DETECTED_IP}:${P2P_PORT}"
        ok "BRN_SEU_ENDERECO auto-detectado: $BRN_SEU_ENDERECO"
    else
        warn "Nao consegui auto-detectar IP publico/LAN."
        if [ -t 0 ]; then
            read -r -p "Digite o IP desta maquina (ex: 192.168.0.20): " USER_IP
            [ -n "$USER_IP" ] && BRN_SEU_ENDERECO="${USER_IP}:${P2P_PORT}"
        fi
    fi
elif [ -n "${MY_IP_ARG:-}" ]; then
    BRN_SEU_ENDERECO="${MY_IP_ARG}:${P2P_PORT}"
    ok "BRN_SEU_ENDERECO (arg): $BRN_SEU_ENDERECO"
fi
export BRN_SEU_ENDERECO

# --- BRN_BOOTSTRAP_PEERS ---
if [ -z "${ORIGIN_IP_ARG:-}" ] && [ -z "${BRN_BOOTSTRAP_PEERS:-}" ]; then
    warn "BRN_BOOTSTRAP_PEERS nao configurado no brn_network.env"
    warn "O CLIENTE precisa saber o IP do ORIGEM para sincronizar!"
    if [ -t 0 ]; then
        read -r -p "Digite o IP do ORIGEM (ex: 100.84.11.48 ou 192.168.0.19): " USER_ORIGIN
        if [ -n "$USER_ORIGIN" ]; then
            BRN_BOOTSTRAP_PEERS="${USER_ORIGIN}:${ORIGIN_P2P_PORT_ARG}"
            ok "BRN_BOOTSTRAP_PEERS (prompt): $BRN_BOOTSTRAP_PEERS"
        else
            err "Sem bootstrap peers. Abortando."
            exit 1
        fi
    else
        err "Sem BRN_BOOTSTRAP_PEERS e sem terminal interativo. Abortando."
        exit 1
    fi
elif [ -n "${ORIGIN_IP_ARG:-}" ]; then
    BRN_BOOTSTRAP_PEERS="${ORIGIN_IP_ARG}:${ORIGIN_P2P_PORT_ARG}"
    ok "BRN_BOOTSTRAP_PEERS (arg): $BRN_BOOTSTRAP_PEERS"
fi
export BRN_BOOTSTRAP_PEERS

# --- BRN_POOL_URL (auto-derivar do bootstrap) ---
if [ -z "${BRN_POOL_URL:-}" ]; then
    FIRST_ORIGIN="$(parse_bootstrap_first_origin "$BRN_BOOTSTRAP_PEERS")"
    if [ -n "$FIRST_ORIGIN" ]; then
        BRN_POOL_URL="http://${FIRST_ORIGIN}:${ORIGIN_HTTP_PORT_ARG}"
        ok "BRN_POOL_URL auto-derivado: $BRN_POOL_URL"
    else
        warn "Nao consegui auto-derivar BRN_POOL_URL"
    fi
fi
export BRN_POOL_URL

# ============================================================
# DEFAULTS DE CLIENTE
# ============================================================
export BRN_MINER_AUTO="${BRN_MINER_AUTO:-1}"
export BRN_MINER_INTERVAL="${BRN_MINER_INTERVAL:-30}"
export BRN_UPNP="${BRN_UPNP:-0}"
export BRN_P2P_AUTH="${BRN_P2P_AUTH:-optional}"
export BRN_WEB_PORT="$WEB_PORT"
export BRN_EXPLORER_PORT="${BRN_EXPLORER_PORT:-8080}"
export BRN_P2P_PORT="$P2P_PORT"
export BRN_LOG_LEVEL="${BRN_LOG_LEVEL:-INFO}"
export PYTHONUNBUFFERED=1
export BRN_BOOTSTRAP_HTTP_PORT="${BRN_BOOTSTRAP_HTTP_PORT:-$ORIGIN_HTTP_PORT_ARG}"

# Pool: cliente NAO roda pool server (--no-pool), mas ENVIA shares
# via PoolClient ao pool do ORIGEM. Nao muda BRN_POOL_ENABLED
# (senao PoolClient nao importa).
export BRN_POOL_ENABLED="${BRN_POOL_ENABLED:-1}"

# ============================================================
# VALIDACOES
# ============================================================
FALTA=""
for f in main.py server.py explorer.py blockchain.py db.py crypto.py wallet.py \
         chain_validator.py checkpoints.py reorg.py brn_config.py brn_logger.py \
         version.py security.py secure_store.py p2p_unified.py p2p_auth.py \
         p2p_secure.py p2p_cipher.py p2p_relay.py discovery_v2.py \
         rendezvous_discovery.py network_guard.py miner_loop.py update_peers.py \
         bech32.py; do
    [ -f "$f" ] || FALTA="$FALTA $f"
done
# Pool client e opcional (warning, nao fatal)
for f in miner_pool_client.py mining_pool.py; do
    if [ ! -f "$f" ]; then
        warn "Arquivo $f ausente — pool ficara desabilitado nesta maquina"
    fi
done

if [ -n "$FALTA" ]; then
    err "Arquivos essenciais faltando:$FALTA"
    exit 1
fi
ok "Arquivos essenciais OK"

# Escrever brn_role.txt como CLIENTE (sobrescreve o que tiver)
printf 'client\n' > brn_role.txt
cli "Papel: CLIENTE (brn_role.txt='client', BRN_IS_ORIGIN=0)"

# --- Secret validation (WARN, nao exit) ---
BRN_NETWORK_SECRET="${BRN_NETWORK_SECRET:-}"
if [ -z "$BRN_NETWORK_SECRET" ] || \
   [ "$BRN_NETWORK_SECRET" = "9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9" ]; then
    warn "============================================================"
    warn " BRN_NETWORK_SECRET NAO CONFIGURADO ou usando DEFAULT PUBLICO"
    warn "============================================================"
    warn " O ORIGEM usa outro secret. Sync VAI PARAR em #6000"
    warn " (AUTH_TAG_START_HEIGHT) com 'auth_tag invalido'."
    warn ""
    warn " ACOES:"
    warn "   1. Copie BRN_NETWORK_SECRET do ORIGEM/brn_network.env"
    warn "   2. Cole no brn_network.env desta maquina"
    warn "   3. Rode novamente"
    warn "============================================================"
    if [ -t 0 ]; then
        read -r -p "Continuar mesmo assim? [s/N] " R
        [ "$R" != "s" ] && [ "$R" != "S" ] && { err "Abortado."; exit 1; }
    fi
fi

NODE_PASS="${BRN_NODE_PASSWORD:-}"
if [ -z "$NODE_PASS" ] || [ "$NODE_PASS" = "senha-no-temp-123" ] || \
   [ "$NODE_PASS" = "senha-no-local-2026" ]; then
    warn "BRN_NODE_PASSWORD fraco. Recomendo gerar novo:"
    warn "  python3 -c \"import secrets;print(secrets.token_hex(24))\""
fi

WALLET_PASS="${BRN_WALLET_SESSION_PASS:-}"
if [ -z "$WALLET_PASS" ] || [ "$WALLET_PASS" = "senha-temp-123" ] || \
   [ "$WALLET_PASS" = "senha-carteira-2026" ]; then
    warn "BRN_WALLET_SESSION_PASS fraco. Recomendo gerar novo."
fi

# ============================================================
# VERIFICAR IMPORT DO POOLCLIENT
# ============================================================
if [ "$NO_POOL_CLIENT" -eq 0 ]; then
    if $PY -c "from miner_pool_client import PoolClient" 2>/tmp/brn_pc_import.err; then
        ok "miner_pool_client.PoolClient importavel"
        POOL_CLIENT_IMPORT_OK=1
    else
        warn "miner_pool_client.PoolClient FALHOU no import:"
        sed 's/^/    /' /tmp/brn_pc_import.err | head -10
        warn "Mineração via pool ficara DESATIVADA."
        warn "O cliente vai usar mineracao local solo (se BRN_MINER_AUTO=1)."
        POOL_CLIENT_IMPORT_OK=0
    fi
    rm -f /tmp/brn_pc_import.err
else
    POOL_CLIENT_IMPORT_OK=0
fi

# ============================================================
# TAILSCALE (opcional)
# ============================================================
TAILSCALE_FLAG="--no-tailscale"
if [ "$SKIP_TAILSCALE" -eq 1 ]; then
    info "Tailscale: pulado (--skip-tailscale)"
elif [ ! -f "brn_tailscale_setup.py" ]; then
    info "brn_tailscale_setup.py nao encontrado"
elif [ -z "${TS_AUTH_KEY:-}" ]; then
    info "TS_AUTH_KEY vazia. Pulando Tailscale."
else
    info "Configurando Tailscale..."
    if $PY brn_tailscale_setup.py --auto 2>/dev/null; then
        ok "Tailscale OK"
    else
        warn "Tailscale nao esta pronto."
    fi
fi

# ============================================================
# CHECK: ORIGEM REACHABLE (TCP)
# ============================================================
check_origin_reachable() {
    local peers="$1"
    local first="${peers%%,*}"
    first="${first%% *}"
    first="${first%%/*}"
    local host="${first%%:*}"
    local rest="${first#*:}"
    local port="${rest%%:*}"
    [ -z "$host" ] && return 1
    [ -z "$port" ] && port=6001
    info "Verificando conexao TCP com ORIGEM ${host}:${port}..."
    if $PY - "$host" "$port" <<'PYEOF' >/dev/null 2>&1
import socket, sys, time
host, port = sys.argv[1], int(sys.argv[2])
deadline = time.time() + 5
while time.time() < deadline:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect((host, port))
        s.close()
        sys.exit(0)
    except Exception:
        time.sleep(0.5)
sys.exit(1)
PYEOF
    then
        ok "ORIGEM alcancavel em ${host}:${port}"
        return 0
    else
        warn "ORIGEM ${host}:${port} NAO RESPONDEU em 5s"
        warn "Verifique: firewall, BRN_P2P_PORT do origem, VPN Tailscale"
        return 1
    fi
}
check_origin_reachable "$BRN_BOOTSTRAP_PEERS" || true

# ============================================================
# GUI DETECTION
# ============================================================
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

# ============================================================
# FLAGS FINAIS
# ============================================================
# Cliente SEMPRE usa --no-pool (nao roda pool server)
POOL_SERVER_FLAG="--no-pool"

POOL_CLIENT_FLAG=""
if [ "$NO_POOL_CLIENT" -eq 1 ] || [ "$POOL_CLIENT_IMPORT_OK" -eq 0 ]; then
    POOL_CLIENT_FLAG="--no-pool-client"
    pool "PoolClient DESATIVADO (fallback: mineracao local solo)"
else
    pool "PoolClient ATIVO -> $BRN_POOL_URL"
fi

# ============================================================
# CONFIG SUMMARY
# ============================================================
NODE_VERSION="$($PY main.py --version 2>/dev/null || echo '?')"
if [ -n "${BRN_NETWORK_SECRET:-}" ]; then
    SEC_FP="$(printf '%s' "$BRN_NETWORK_SECRET" | $PY -c 'import hashlib,sys;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])' 2>/dev/null)"
else
    SEC_FP="(vazio)"
fi

echo
echo "============================================================"
echo "  CONFIGURACAO CLIENTE (MAQUINA B)"
echo "============================================================"
echo "  Modo           : CLIENTE (BRN_IS_ORIGIN=0)"
echo "  Versao         : $NODE_VERSION"
echo "  Auth FP        : $SEC_FP..."
echo "  Bootstrap      : ${BRN_BOOTSTRAP_PEERS}"
echo "  Meu endereco   : ${BRN_SEU_ENDERECO:-(nao definido)}"
echo "  Pool URL       : ${BRN_POOL_URL:-(nao definido)}"
echo "  Pool server    : OFF (--no-pool)"
echo "  Pool client    : $([ -z "$POOL_CLIENT_FLAG" ] && echo ON || echo OFF)"
echo "  Miner local    : ${BRN_MINER_AUTO:-0}"
echo "  P2P porta      : $P2P_PORT"
echo "  HTTP           : http://0.0.0.0:$WEB_PORT"
echo "  Explorer       : http://0.0.0.0:$BRN_EXPLORER_PORT"
echo "  Python         : $PYVER"
echo "  pywebview      : $([ -z "$HEADLESS_FLAG" ] && echo GUI || echo HEADLESS)"
echo "============================================================"
echo
cli "Deixe este terminal ABERTO enquanto o no roda."
echo

# ============================================================
# START
# ============================================================
info "Iniciando CLIENTE BRN..."
info "CMD: $PY main.py $POOL_SERVER_FLAG $POOL_CLIENT_FLAG $TAILSCALE_FLAG $HEADLESS_FLAG"
echo

cleanup() {
    echo
    warn "Encerrando no cliente..."
    exit 130
}
trap cleanup INT TERM

$PY main.py $POOL_SERVER_FLAG $POOL_CLIENT_FLAG $TAILSCALE_FLAG $HEADLESS_FLAG
EXITCODE=$?

echo
if [ "$EXITCODE" -ne 0 ]; then
    err "O no cliente saiu com codigo $EXITCODE"
    echo "  Diagnostico:"
    echo "    ls -la brn_v2_chain.db* node_identity.enc"
    echo "    cat audit.log | tail -30"
    echo
    echo "  Reset de emergencia (se genesis corrompeu):"
    echo "    rm brn_v2_chain.db* && ./iniciar_cliente.sh"
else
    ok "No cliente encerrado normalmente."
fi

if [ -t 0 ]; then
    read -r -p "Pressione ENTER para fechar..." _
fi
exit "$EXITCODE"
