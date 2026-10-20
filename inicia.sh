#!/usr/bin/env bash
# ============================================================
#   iniciar.sh - BRN Node (Linux / macOS)
# ------------------------------------------------------------
#   Uso:
#     ./iniciar.sh                      # modo HUB/ORIGEM (padrao)
#     ./iniciar.sh --client             # modo CLIENTE
#     ./iniciar.sh --headless           # sem GUI (só servidor HTTP)
#     ./iniciar.sh --reset-identity     # apaga identidade
#     ./iniciar.sh --no-mongo           # não testa Mongo
#     ./iniciar.sh --help               # ajuda
# ============================================================

set -u

# ------------------------------------------------------------
# Cores
# ------------------------------------------------------------
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_GREEN='\033[0;32m'
C_RED='\033[0;31m'
C_YELLOW='\033[0;33m'
C_BLUE='\033[0;34m'
C_CYAN='\033[0;36m'

info()  { echo -e "${C_BLUE}[i]${C_RESET} $*"; }
ok()    { echo -e "${C_GREEN}[ok]${C_RESET} $*"; }
warn()  { echo -e "${C_YELLOW}[!]${C_RESET} $*"; }
err()   { echo -e "${C_RED}[X]${C_RESET} $*"; }
die()   { err "$*"; exit 1; }

# ------------------------------------------------------------
# Ir para o diretório do script
# ------------------------------------------------------------
cd "$(dirname "$(readlink -f "$0")")" || die "Não consegui ir para o diretório do script"
BASE_DIR="$(pwd)"

echo ""
echo -e "${C_BOLD}============================================================${C_RESET}"
echo -e "${C_BOLD}  BRN Node - Linux/macOS${C_RESET}"
echo -e "${C_BOLD}============================================================${C_RESET}"
echo ""

# ------------------------------------------------------------
# Parse de argumentos
# ------------------------------------------------------------
MODE="origin"           # origin | client
HEADLESS=0
RESET_IDENTITY=0
SKIP_MONGO=0

while [ $# -gt 0 ]; do
    case "$1" in
        --client)         MODE="client" ;;
        --hub|--origin)   MODE="origin" ;;
        --headless)       HEADLESS=1 ;;
        --reset-identity) RESET_IDENTITY=1 ;;
        --no-mongo)       SKIP_MONGO=1 ;;
        --help|-h)
            sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            warn "Argumento desconhecido: $1"
            ;;
    esac
    shift
done

# ------------------------------------------------------------
# Configurações (AJUSTE CONFORME NECESSARIO)
# ------------------------------------------------------------
export BRN_NODE_PASSWORD="senha-no-temp-123"
export BRN_NETWORK_SECRET="9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9"

export BRN_WEB_PASS="senha-temp-123"
export BRN_WALLET_SESSION_PASS="senha-forte-da-carteira-2026"

export BRN_HTTPS=0
export BRN_AUDIT_ENABLED=1
export BRN_AUDIT_LOG="audit.log"

export BRN_P2P_AUTH=optional
export BRN_P2P_AUTH_WINDOW=30
export BRN_P2P_NONCE_CACHE=10000

export BRN_SKIP_VERIFY=1
export BRN_VERIFY_STRICT=0

export BRN_TRACKER="https://brn-tracker.onrender.com"

export BRN_P2P_PORT=6001
export BRN_WEB_PORT=5000
export BRN_EXPLORER_PORT=8080

# MongoDB Atlas
export MONGO_USER="brunolabncaolivira_db_user"
export MONGO_PASS="SUA_SENHA_DO_ATLAS_AQUI"
export MONGO_HOST="cluster0.vbxjymy.mongodb.net"
export MONGO_DB="brn_analytics"

# Modo
if [ "$MODE" = "origin" ]; then
    export BRN_IS_ORIGIN=1
    export BRN_CLIENT_MODE=0
    echo -e "${C_CYAN}Modo:${C_RESET} ${C_BOLD}HUB / ORIGEM${C_RESET}"
else
    export BRN_IS_ORIGIN=0
    export BRN_CLIENT_MODE=1
    echo -e "${C_CYAN}Modo:${C_RESET} ${C_BOLD}CLIENTE${C_RESET}"
    # Ajuste o IP da Maquina A aqui se estiver no modo cliente
    export BRN_BOOTSTRAP_PEERS="${BRN_BOOTSTRAP_PEERS:-192.168.0.19:6001}"
fi

export PYTHONUNBUFFERED=1

echo ""

# ------------------------------------------------------------
# 1) Validar arquivos essenciais
# ------------------------------------------------------------
echo -e "${C_BOLD}[i] Validando arquivos...${C_RESET}"
REQUIRED=(
    "main.py"
    "server.py"
    "blockchain.py"
    "wallet.py"
    "db.py"
    "p2p_unified.py"
    "p2p_auth.py"
    "security.py"
    "secure_store.py"
    "mongo_client.py"
    "brn_config.py"
    "brn_logger.py"
    "checkpoints.py"
    "chain_validator.py"
    "explorer.py"
)
FALTANDO=()
for f in "${REQUIRED[@]}"; do
    if [ ! -f "$f" ]; then
        echo -e "   ${C_RED}[X] Faltando:${C_RESET} $f"
        FALTANDO+=("$f")
    else
        echo -e "   ${C_GREEN}[ok]${C_RESET} $f"
    fi
done
if [ ${#FALTANDO[@]} -gt 0 ]; then
    echo ""
    die "Arquivos faltando: ${FALTANDO[*]}"
fi
echo ""

# ------------------------------------------------------------
# 2) Verificar Python
# ------------------------------------------------------------
PYTHON_BIN=""
for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        PYTHON_BIN="$cmd"
        break
    fi
done
[ -z "$PYTHON_BIN" ] && die "Python não encontrado no PATH"
PYVER=$("$PYTHON_BIN" --version 2>&1)
ok "$PYVER"
echo ""

# ------------------------------------------------------------
# 3) Instalar pymongo se faltar
# ------------------------------------------------------------
if ! "$PYTHON_BIN" -c "import pymongo" >/dev/null 2>&1; then
    warn "pymongo não instalado - instalando..."
    "$PYTHON_BIN" -m pip install --quiet "pymongo[srv]"
    if [ $? -ne 0 ]; then
        warn "Falha ao instalar pymongo — Mongo será desabilitado"
        SKIP_MONGO=1
    else
        ok "pymongo instalado"
    fi
else
    ok "pymongo já instalado"
fi
echo ""

# ------------------------------------------------------------
# 4) Testar Mongo
# ------------------------------------------------------------
if [ "$SKIP_MONGO" = "0" ] && [ "$MONGO_PASS" != "SUA_SENHA_DO_ATLAS_AQUI" ]; then
    echo -e "${C_BOLD}[i] Testando conexão com MongoDB Atlas...${C_RESET}"
    "$PYTHON_BIN" -c "
from mongo_client import mongo
ok, err = mongo.ping()
print('CONEXAO OK' if ok else 'FALHOU: ' + str(err))
" 2>&1 || warn "Falha no teste do Mongo"
    echo ""
elif [ "$SKIP_MONGO" = "1" ]; then
    info "Mongo: teste pulado (--no-mongo)"
else
    warn "MONGO_PASS não configurada no script - pulando teste"
    echo "    Edite este arquivo e coloque a senha do Atlas em MONGO_PASS"
    echo ""
fi

# ------------------------------------------------------------
# 5) Resumo da configuração
# ------------------------------------------------------------
echo -e "${C_BOLD}[i] Configuração:${C_RESET}"
echo "    NODE_PASSWORD       = ${BRN_NODE_PASSWORD:0:4}..."
echo "    WALLET_SESSION_PASS = ${BRN_WALLET_SESSION_PASS:0:4}..."
echo "    NETWORK_SECRET      = ${BRN_NETWORK_SECRET:0:16}..."
echo "    BOOTSTRAP_PEERS     = ${BRN_BOOTSTRAP_PEERS:-(nenhum)}"
echo "    IS_ORIGIN           = $BRN_IS_ORIGIN"
echo "    HTTPS               = $BRN_HTTPS"
echo "    MONGO_USER          = $MONGO_USER"
echo "    MONGO_HOST          = $MONGO_HOST"
echo "    P2P_PORT            = $BRN_P2P_PORT"
echo ""

# ------------------------------------------------------------
# 6) Reset de identidade (SOMENTE se --reset-identity)
# ------------------------------------------------------------
if [ "$RESET_IDENTITY" = "1" ]; then
    info "Apagando identidade (--reset-identity)..."
    rm -f node_identity.enc node_identity.enc.bak node_identity.enc.oldformat.bak
    ok "Identidade removida"
    echo ""
fi

# ------------------------------------------------------------
# 7) Detectar formato antigo de node_identity.enc
# ------------------------------------------------------------
if [ -f "node_identity.enc" ]; then
    if ! "$PYTHON_BIN" -c "
import sys
f = open('node_identity.enc','rb'); f.seek(4); b = f.read(1); f.close()
sys.exit(0 if b == b'\x01' else 1)
" >/dev/null 2>&1; then
        warn "node_identity.enc em formato antigo - fazendo backup"
        mv -f node_identity.enc node_identity.enc.oldformat.bak
        ok "Backup salvo em node_identity.enc.oldformat.bak"
        echo ""
    else
        info "node_identity.enc em formato atual - mantendo"
        echo ""
    fi
else
    info "node_identity.enc será criado no primeiro boot"
    echo ""
fi

# ------------------------------------------------------------
# 8) Atualizar bootstrap_peers.json
# ------------------------------------------------------------
if [ -n "${BRN_BOOTSTRAP_PEERS:-}" ]; then
    echo "[$(echo "$BRN_BOOTSTRAP_PEERS" | tr ',' '\n' | sed 's/^/"/;s/$/"/' | paste -sd, -)]" > bootstrap_peers.json
    ok "bootstrap_peers.json = $(cat bootstrap_peers.json)"
    echo ""
else
    echo "[]" > bootstrap_peers.json
    info "bootstrap_peers.json vazio (modo origem)"
    echo ""
fi

# ------------------------------------------------------------
# 9) Atualizar brn_network.env
# ------------------------------------------------------------
info "Atualizando brn_network.env..."
{
    echo "BRN_NETWORK_SECRET=$BRN_NETWORK_SECRET"
    echo "BRN_TRACKER=$BRN_TRACKER"
    echo "BRN_IS_ORIGIN=$BRN_IS_ORIGIN"
    echo "BRN_HTTPS=$BRN_HTTPS"
    echo "BRN_AUDIT_ENABLED=$BRN_AUDIT_ENABLED"
    echo "BRN_AUDIT_LOG=$BRN_AUDIT_LOG"
    echo "BRN_P2P_AUTH=$BRN_P2P_AUTH"
    echo "BRN_P2P_AUTH_WINDOW=$BRN_P2P_AUTH_WINDOW"
    echo "BRN_P2P_NONCE_CACHE=$BRN_P2P_NONCE_CACHE"
    echo "BRN_SKIP_VERIFY=$BRN_SKIP_VERIFY"
    echo "BRN_VERIFY_STRICT=$BRN_VERIFY_STRICT"
    echo "MONGO_USER=$MONGO_USER"
    echo "MONGO_PASS=$MONGO_PASS"
    echo "MONGO_HOST=$MONGO_HOST"
    echo "MONGO_DB=$MONGO_DB"
} > brn_network.env
chmod 600 brn_network.env
ok "brn_network.env atualizado (chmod 600)"
echo ""

# ------------------------------------------------------------
# 10) Testar conexão com peer (só no modo cliente)
# ------------------------------------------------------------
if [ "$MODE" = "client" ] && [ -n "${BRN_BOOTSTRAP_PEERS:-}" ]; then
    echo -e "${C_BOLD}[i] Testando conexão com $BRN_BOOTSTRAP_PEERS...${C_RESET}"

    HOST="${BRN_BOOTSTRAP_PEERS%%:*}"
    PORT="${BRN_BOOTSTRAP_PEERS##*:}"

    if timeout 3 bash -c "cat < /dev/null > /dev/tcp/$HOST/$PORT" 2>/dev/null; then
        ok "Peer acessível em $HOST:$PORT"
    else
        warn "Não alcancei $BRN_BOOTSTRAP_PEERS"
        echo "    Verifique:"
        echo "      - A Máquina A está rodando?"
        echo "      - O IP $HOST está correto?"
        echo "      - Firewall liberou porta $PORT?"
        echo "    Continuando mesmo assim..."
    fi
    echo ""
fi

# ------------------------------------------------------------
# 11) Detectar e migrar current_wallet.json legado
# ------------------------------------------------------------
if [ -f "current_wallet.json" ] && [ ! -f "current_wallet.enc" ]; then
    warn "Detectado current_wallet.json em TEXTO PURO"
    info "Migrando para formato cifrado..."
    "$PYTHON_BIN" -c "
from wallet import WalletManager
import os
p = os.environ.get('BRN_WALLET_SESSION_PASS', '')
print(WalletManager.migrate_legacy_current(p))
" 2>&1 || warn "Migração falhou (não crítico)"
    echo ""
fi

# ------------------------------------------------------------
# 12) Iniciar o nó
# ------------------------------------------------------------
echo -e "${C_BOLD}============================================================${C_RESET}"
echo -e "${C_BOLD}  Iniciando BRN Node (modo ${MODE^^})${C_RESET}"
echo -e "${C_BOLD}============================================================${C_RESET}"
echo -e "  Ctrl+C para encerrar"
echo ""

# Montar args do main.py
MAIN_ARGS=()
if [ "$MODE" = "client" ]; then
    MAIN_ARGS+=("--client-mode")
fi
if [ "$HEADLESS" = "1" ]; then
    MAIN_ARGS+=("--headless")
fi

# Redireciona saída para log E mostra na tela (tee)
"$PYTHON_BIN" main.py "${MAIN_ARGS[@]}" 2>&1 | tee run.log
EXITCODE=${PIPESTATUS[0]}

echo ""
echo -e "${C_BOLD}============================================================${C_RESET}"
if [ "$EXITCODE" -ne 0 ]; then
    err "Nó saiu com código $EXITCODE"
    echo ""
    echo -e "${C_YELLOW}Dicas:${C_RESET}"
    echo "  - \"falha ao decifrar\"      → ./iniciar.sh --reset-identity"
    echo "  - \"versão não suportada\"   → já tratado automaticamente"
    echo "  - \"Aguardando genesis\"     → Máquina A não está rodando"
    echo "  - \"ModuleNotFoundError\"    → pip install -r requirements.txt"
    echo ""
    echo -e "${C_BOLD}Últimas 20 linhas do log:${C_RESET}"
    echo "------------------------------------------------------------"
    tail -n 20 run.log 2>/dev/null || echo "(sem log)"
    echo "------------------------------------------------------------"
else
    ok "Nó encerrado normalmente."
    echo ""
    tail -n 5 run.log 2>/dev/null || true
fi
echo -e "${C_BOLD}============================================================${C_RESET}"
echo ""

exit "$EXITCODE"