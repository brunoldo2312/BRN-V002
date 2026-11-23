#!/usr/bin/env bash
# ============================================================
#  iniciar_cliente.sh — BRN Node Linux/Ubuntu (v5)
#  Equivalente ao iniciar_cliente.bat do Windows
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

echo "============================================================"
echo "  INICIANDO BRN NODE - CLIENTE (v5 - Linux)"
echo "============================================================"
echo

# ============================================================
#  [0/9] VARIAVEIS DE AMBIENTE
# ============================================================
echo "[0/9] Carregando configuracoes de rede e senhas..."

export BRN_NETWORK_SECRET="088641fd110a36bcb3a1bd3d497d8cd68287bf10e49eedfc52d1fc0329ef8d12"
export BRN_MAX_VALIDATORS=3
export BRN_VALIDATOR_QUORUM=0.51
export BRN_REQUIRE_ALL_SIGS=0
export BRN_MIN_DIFFICULTY=3
export BRN_MAX_DIFFICULTY=6
export BRN_DIFF_INTERVAL=6
export BRN_P2P_PORT=6001
export BRN_SEU_ENDERECO="100.111.253.8:6001"
export BRN_SKIP_TAILSCALE=0
export BRN_UVICORN_WORKERS=1
export ANYIO_THREAD_LIMIT=200
export BRN_WEB_HOST=0.0.0.0
export BRN_WEB_PORT=5000
export BRN_EXPLORER_PORT=8080
export BRN_CORS_ORIGIN="*"
export BRN_CHECKPOINT_INTERVAL=100
export BRN_ORIGIN_PUBKEY="03c0f4604d5264b5eda409eac8212267bd76ff46973bb14e01a16c290f437e0e6e"
export BRN_TCP_TIMEOUT=30.0

# --- Bootstrap: Windows (seu IP Tailscale) ---
export BRN_BOOTSTRAP_PEERS="100.84.11.48:6001,100.70.107.15:6001"
export BRN_POOL_URL="http://100.84.11.48:5000"

# --- Este no e CLIENTE ---
export BRN_IS_ORIGIN=0
export BRN_POOL_ENABLED=0

# --- Senhas ---
export BRN_NODE_PASSWORD="senha-forte-windows-2026"
export BRN_WALLET_SESSION_PASS="carteira-windows-2026"
export BRN_WEB_PASS="web-windows-2026"

# --- Logging ---
export BRN_LOG_LEVEL=DEBUG

# --- Auto-wire ---
export BRN_AUTO_WIRE=1

# --- Multi-sig ---
export BRN_STRICT_MULTISIG=0

# --- Fast-sync ---
export BRN_ASSUME_VALID_UNTIL=0
export BRN_BOOTSTRAP_SKIP_SIGS_UNTIL=4085

# --- CONSENSO ESTRITO A PARTIR DO BLOCO 6000 ---
export BRN_CONSENSUS_STRICT_FROM=6000
export BRN_MIN_PEERS_FOR_MINING=3
export BRN_PEER_SYNC_TOLERANCE=2

# --- Python unbuffered ---
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8

# --- PBFT off ---
export BRN_PBFT_ENABLED=0
export BRN_PBFT_ENFORCE=0

echo "OK."
echo

# ============================================================
#  [1/9] PYTHON
# ============================================================
echo "[1/9] Verificando Python..."
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERRO: python3 nao encontrado. Instale com: sudo apt install python3 python3-pip"
    exit 1
fi
PY=$(command -v python3)
$PY --version
echo "OK."
echo

# ============================================================
#  [2/9] DEPENDENCIAS
# ============================================================
echo "[2/9] Verificando dependencias (pode demorar)..."
$PY -m pip install --upgrade pip --quiet 2>/dev/null || true

# anyio 4.15 quebra GUI
$PY -c "import anyio; print(anyio.__version__)" 2>/dev/null | grep -q "4.15" && {
    echo "Corrigindo versao do anyio..."
    $PY -m pip install "anyio<4.15.0" --quiet
    $PY -m pip install "starlette<0.40.0" --quiet
}

for PKG in cryptography argon2-cffi orjson requests fastapi flask flask-cors waitress mnemonic pymongo bitcoinlib zeroconf; do
    if $PY -c "import ${PKG//-/_}" >/dev/null 2>&1; then
        echo "[ok] $PKG"
    else
        echo "Instalando $PKG..."
        $PY -m pip install --quiet "$PKG" || echo "[WARN] falhou: $PKG"
    fi
done

$PY -c "import uvicorn" >/dev/null 2>&1 || {
    echo "Instalando uvicorn[standard]..."
    $PY -m pip install --quiet "uvicorn[standard]"
}
echo "OK."
echo

# ============================================================
#  [3/9] TAILSCALE
# ============================================================
echo "[3/9] Verificando Tailscale..."
if [ "${BRN_SKIP_TAILSCALE:-0}" = "1" ]; then
    echo "[skip] BRN_SKIP_TAILSCALE=1"
elif command -v tailscale >/dev/null 2>&1; then
    tailscale status >/dev/null 2>&1 || {
        echo "[WARN] Tailscale nao conectado - tentando up..."
        sudo tailscale up 2>/dev/null || echo "[WARN] falhou"
    }
    tailscale ip -4 2>/dev/null | head -1
else
    echo "[WARN] Tailscale nao encontrado - ignorando"
fi
echo "OK."
echo

# ============================================================
#  [4/9] PRE-FLIGHT
# ============================================================
echo "[4/9] PRE-FLIGHT: arquivos, carteira, SK..."
echo

echo "  --- Arquivos criticos ---"
for F in blockchain.py main.py server.py db.py wallet.py crypto.py auto_wire.py secure_store.py; do
    if [ -f "$F" ]; then
        echo "  [ok] $F"
    else
        echo "  [X]  $F AUSENTE - critico"
    fi
done
[ -f "brn_miner.so" ] && echo "  [ok] brn_miner.so" || echo "  [WARN] brn_miner.so ausente (PoW lento)"
echo

echo "  --- Versao do blockchain.py ---"
$PY -c "import blockchain; print('  blockchain.py v' + getattr(blockchain, '__version__', '?'))" 2>/dev/null || {
    echo "  [X] Nao consegui importar blockchain.py"
    exit 1
}
echo

echo "  --- Altura do DB ---"
$PY -c "
from db import ChainDB
d = ChainDB('brn_v2_chain.db')
h = d.height()
print('  Altura:', h)
print('  Tip:', d.tip_hash()[:32] + '...') if h >= 0 else print('  (vazio)')
d.close()
" 2>/dev/null
echo

# --- Cria carteira ---
if [ ! -f "current_wallet.enc" ]; then
    echo "  --- Criando carteira nova ---"
    $PY -c "
import os
pw = os.environ.get('BRN_WALLET_SESSION_PASS', 'carteira-windows-2026')
from wallet import Wallet
from secure_store import save_current_wallet
w = Wallet()
r = save_current_wallet(pw, {'address': w.address, 'private_key': w.priv_hex, 'public_key': w.pub_hex})
print('  Carteira criada:', w.address) if r.get('ok') else print('  FALHA:', r.get('msg'))
"
else
    echo "  --- current_wallet.enc ja existe ---"
fi

# --- Cria .validator_sk ---
if [ ! -f ".validator_sk" ]; then
    echo
    echo "  --- Gerando .validator_sk ---"
    $PY -c "
from wallet import Wallet
w = Wallet()
open('.validator_sk', 'w').write(w.priv_hex)
print('  Endereco:', w.address)
print('  Pubkey  :', w.pub_hex[:16] + '...')
"
    chmod 600 .validator_sk
else
    echo "  --- .validator_sk ja existe ---"
    $PY -c "
from pathlib import Path
c = Path('.validator_sk').read_text().strip().split('\n')[0]
print('  .validator_sk OK' if len(c) == 64 else '  [X] .validator_sk INVALIDO len=' + str(len(c)))
"
fi
echo

# --- Verifica auto_wire ---
echo "  --- Verificando deteccao de SK pelo auto_wire ---"
$PY -c "
import os
os.environ['BRN_AUTO_WIRE'] = '1'
from auto_wire import _detect_sk
w, src = _detect_sk()
print('  auto_wire detecta:', src or 'NENHUMA SK') if w else print('  [X] NENHUMA SK ENCONTRADA')
"
echo

# --- Forca rede descentralizada ---
echo "  --- Verificando fila de mineradores ---"
$PY -c "
from db import ChainDB
d = ChainDB('brn_v2_chain.db')
m = d.get_meta('rede_descentralizada')
d.close()
print('  rede_descentralizada =', m or 'nao setado')
"
$PY -c "
from db import ChainDB
d = ChainDB('brn_v2_chain.db')
d.set_meta('rede_descentralizada', '1')
d.close()
print('  [ok] rede_descentralizada=1')
"
echo

# --- Conectividade ---
echo "  --- Conectividade dos peers ---"
for PEER in 100.84.11.48:6001 100.70.107.15:6001; do
    IP="${PEER%:*}"
    PORT="${PEER#*:}"
    if timeout 2 bash -c "cat < /dev/null > /dev/tcp/$IP/$PORT" 2>/dev/null; then
        echo "  [ok] $PEER"
    else
        echo "  [X]  $PEER timeout"
    fi
done
echo

echo "OK (pre-flight)."
echo

# ============================================================
#  [5/9] PORTA 5000
# ============================================================
echo "[5/9] Verificando porta 5000..."
if ss -ltn 2>/dev/null | grep -q ":5000 "; then
    echo "  [X] porta 5000 JA EM USO"
    ss -ltnp 2>/dev/null | grep ":5000 " || true
    echo
    echo "ERRO: porta 5000 ocupada. Feche o main.py anterior."
    exit 1
else
    echo "  [ok] porta 5000 livre"
fi
echo "OK."
echo

# ============================================================
#  [6/9] LOG + TIMESTAMP
# ============================================================
echo "[6/9] Preparando diretorio de logs..."
mkdir -p logs
TS=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$PWD/logs/brn_node_${TS}.log"
echo "Arquivo de log: $LOG_FILE"
echo "OK."
echo

# ============================================================
#  [7/9] GUI OPENER
# ============================================================
echo "[7/9] GUI opener disparado (aguarda :5000)..."
(
    for i in $(seq 1 60); do
        if curl -s -o /dev/null -w "%{http_code}" --max-time 2 http://127.0.0.1:5000/health 2>/dev/null | grep -q "200"; then
            echo "[GUI] wallet aberta"
            if command -v xdg-open >/dev/null 2>&1; then
                xdg-open "http://127.0.0.1:5000/wallet" >/dev/null 2>&1
            elif command -v lynx >/dev/null 2>&1; then
                lynx "http://127.0.0.1:5000/wallet"
            fi
            exit 0
        fi
        sleep 2
    done
    echo "[GUI] timeout - abra manualmente http://127.0.0.1:5000/wallet"
) &
echo "[ok] GUI opener iniciado em background"
echo

# ============================================================
#  [8/9] LOG TAIL EM BACKGROUND
# ============================================================
echo "[8/9] Log em tempo real (background)..."
( tail -F "$LOG_FILE" 2>/dev/null | sed --unbuffered 's/^/[LOG] /' ) &
TAIL_PID=$!
echo "[ok] tail PID=$TAIL_PID"
echo

# ============================================================
#  [9/9] INICIA NO COM REDIRECIONAMENTO (sem pipe)
# ============================================================
echo "[9/9] Iniciando no (Ctrl+C para parar)..."
echo "============================================================"
echo "  API    : http://127.0.0.1:5000"
echo "  WALLET : http://127.0.0.1:5000/wallet"
echo "  LOG    : $LOG_FILE"
echo "============================================================"
echo

MAIN_ARGS=""
if $PY main.py --help 2>&1 | grep -q -- "--skip-verify"; then
    MAIN_ARGS="--skip-verify"
fi

echo "[sh] python3 main.py $MAIN_ARGS"
echo

cleanup() {
    echo
    echo "============================================================"
    echo "NO ENCERRADO em $(date +%H:%M:%S)"
    echo "============================================================"
    echo
    echo "Log salvo em: $LOG_FILE"
    echo
    kill "$TAIL_PID" 2>/dev/null || true
    exit 0
}
trap cleanup INT TERM

# Redireciona stdout+stderr direto pro arquivo (SEM pipe)
$PY main.py $MAIN_ARGS > "$LOG_FILE" 2>&1

cleanup
