#!/bin/bash

# ============================================================
#   iniciar_ubuntu.sh - BRN Node Auto-Setup (v4.0 - Linux)
#   Fluxo: Deps -> Tailscale -> Sync -> Inicializacao
# ============================================================

# Cores para o terminal
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}============================================================${NC}"
echo -e "${CYAN}  BRN Node - AUTO SETUP E INICIALIZACAO (Linux)${NC}"
echo -e "${CYAN}============================================================${NC}"
echo -e "Pasta: $(pwd)"
echo -e "Data:  $(date)"
echo ""

# 1. VERIFICAR PYTHON
echo -e "${CYAN}[1/5] Verificando Python...${NC}"
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}[ERRO] Python 3 nao encontrado.${NC}"
    echo "       Instale com: sudo apt update && sudo apt install python3 python3-pip python3-venv"
    exit 1
fi
python3 --version
echo -e "${GREEN}[ok] Python encontrado.${NC}\n"

# 2. INSTALAR DEPENDENCIAS
echo -e "${CYAN}[2/5] Verificando e instalando dependencias...${NC}"
# Garante que o pip esta atualizado
python3 -m pip install --upgrade pip --quiet 2>/dev/null

# Lista de pacotes (uvloop e otimo para Linux)
PKGS="fastapi uvicorn uvloop flask flask-cors waitress requests orjson cryptography argon2-cffi mnemonic pymongo"

for PKG in $PKGS; do
    # Verifica se o pacote ja esta instalado
    if ! python3 -c "import $PKG" &> /dev/null; then
        echo -e "${YELLOW}       Instalando $PKG...${NC}"
        python3 -m pip install --quiet "$PKG"
        if [ $? -ne 0 ]; then
            echo -e "${YELLOW}       [AVISO] Falha ao instalar $PKG. Tentando com --user...${NC}"
            python3 -m pip install --quiet --user "$PKG"
        fi
    fi
done

# Tenta instalar pywebview apenas se for usar GUI
if [ -n "$DISPLAY" ]; then
    if ! python3 -c "import webview" &> /dev/null; then
        echo -e "${YELLOW}       Instalando pywebview (Ambiente grafico detectado)...${NC}"
        python3 -m pip install --quiet pywebview
    fi
else
    echo -e "       (Modo Headless detectado - pulando pywebview)"
fi

echo -e "${GREEN}[ok] Dependencias verificadas.${NC}\n"

# 3. VERIFICAR TAILSCALE
echo -e "${CYAN}[3/5] Verificando Tailscale...${NC}"
if command -v tailscale &> /dev/null; then
    if tailscale status &> /dev/null; then
        echo -e "${GREEN}[ok] Tailscale esta rodando.${NC}"
    else
        echo -e "${YELLOW}       Tailscale nao esta conectado. Tentando iniciar...${NC}"
        sudo tailscale up
    fi
else
    echo -e "${YELLOW}       [AVISO] Tailscale nao instalado.${NC}"
    echo "       O no tentara se conectar via rede local (LAN)."
fi
echo ""

# 4. SINCRONIZAR BLOCKCHAIN
echo -e "${CYAN}[4/5] Verificando blockchain...${NC}"
DB_HEIGHT=$(python3 -c "from db import ChainDB; d=ChainDB('brn_v2_chain.db'); print(d.height()); d.close()" 2>/dev/null)

if [ -z "$DB_HEIGHT" ]; then
    DB_HEIGHT="-1"
fi

echo -e "       Altura local: $DB_HEIGHT"

if [ "$DB_HEIGHT" == "-1" ]; then
    echo -e "${YELLOW}       DB vazio - tentando sincronizar...${NC}"
    if [ -f "sync_direct.py" ]; then
        python3 sync_direct.py http://100.111.253.8:5000
        if [ $? -ne 0 ]; then
            echo -e "${YELLOW}[AVISO] sync_direct falhou. O main.py tentara sync automatico.${NC}"
        fi
    else
        echo -e "       sync_direct.py nao encontrado. O main.py tentara via HTTP."
    fi
else
    echo -e "${GREEN}[ok] Blockchain ja sincronizada.${NC}"
fi
echo ""

# 5. INICIAR O NO
echo -e "${CYAN}[5/5] Iniciando BRN Node...${NC}"
echo -e "${CYAN}============================================================${NC}"

# Se estiver num servidor (sem tela), forca headless
FLAGS="--skip-verify"
if [ -z "$DISPLAY" ]; then
    FLAGS="$FLAGS --headless"
    echo -e "   MODO: HEADLESS (Servidor)"
else
    echo -e "   MODO: GUI (Desktop)"
fi

echo -e "   Comando: python3 main.py $FLAGS"
echo -e "${CYAN}============================================================${NC}\n"

# Roda o main.py passando as flags
python3 main.py $FLAGS

if [ $? -ne 0 ]; then
    echo -e "\n${RED}[X] O programa saiu com erro.${NC}"
fi
