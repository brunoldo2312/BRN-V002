@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

title BRN Wallet Node

echo ============================================================
echo   INICIANDO BRN NODE - CLIENTE
echo ============================================================
echo.

echo [0/6] Carregando configuracoes de rede e senhas...
set "BRN_NETWORK_SECRET=088641fd110a36bcb3a1bd3d497d8cd68287bf10e49eedfc52d1fc0329ef8d12"
set "BRN_NODE_PASSWORD=senha-forte-windows-2026"
set "BRN_WALLET_SESSION_PASS=carteira-windows-2026"
set "BRN_WEB_PASS=web-windows-2026"
set "BRN_IS_ORIGIN=0"
set "BRN_POOL_ENABLED=0"
set "BRN_MAX_VALIDATORS=3"
set "BRN_VALIDATOR_QUORUM=0.51"
set "BRN_REQUIRE_ALL_SIGS=0"
set "BRN_MIN_DIFFICULTY=3"
set "BRN_MAX_DIFFICULTY=6"
set "BRN_DIFF_INTERVAL=6"
set "BRN_BOOTSTRAP_PEERS=100.70.107.15:6001,100.111.253.8:6001"
set "BRN_POOL_URL=http://100.111.253.8:5000"
set "BRN_SEU_ENDERECO=100.84.11.48:6001"
set "BRN_P2P_PORT=6001"
set "BRN_SKIP_TAILSCALE=0"
set "BRN_UVICORN_WORKERS=1"
set "ANYIO_THREAD_LIMIT=200"
set "BRN_WEB_HOST=0.0.0.0"
set "BRN_WEB_PORT=5000"
set "BRN_CORS_ORIGIN=*"
set "BRN_CHECKPOINT_INTERVAL=100"
set "BRN_ORIGIN_PUBKEY=03c0f4604d5264b5eda409eac8212267bd76ff46973bb14e01a16c290f437e0e6e"
set "BRN_LOG_LEVEL=INFO"
set "BRN_SKIP_VERIFY=1"
set "BRN_TCP_TIMEOUT=30.0"
set "BRN_ASSUME_VALID_UNTIL=4001"
set "BRN_CHECKPOINT_AUTO_HEIGHT=4001"

REM ─── [NOVO] PBFT ───────────────────────────────────────────
set "BRN_PBFT_ENABLED=1"
set "BRN_PBFT_ENFORCE=0"
set "BRN_PBFT_TIMEOUT=15"
set "BRN_PBFT_MAX_STATES=100"
REM ───────────────────────────────────────────────────────────
echo OK.
echo.

echo [1/6] Verificando Python...
where python >nul 2>&1
if errorlevel 1 (
    echo ERRO: Python nao encontrado no PATH.
    pause
    exit /b 1
)
python --version
echo OK.
echo.

echo [2/6] Verificando dependencias (isso pode demorar)...
python -m pip install --upgrade pip --quiet

REM Corrige o bug do anyio 4.15 que quebra a GUI
python -c "import anyio; print(anyio.__version__)" 2>nul | findstr "4.15" >nul
if not errorlevel 1 (
    echo Corrigindo versao do anyio...
    python -m pip install "anyio<4.15.0" --quiet
    python -m pip install "starlette<0.40.0" --quiet
)

set "PKGS=cryptography argon2-cffi orjson requests fastapi flask flask-cors waitress mnemonic pymongo pywebview pythonnet bitcoinlib zeroconf"

for %%P in (!PKGS!) do (
    python -c "import %%P" >nul 2>&1
    if errorlevel 1 (
        echo Instalando %%P...
        python -m pip install --quiet %%P
    ) else (
        echo [ok] %%P ja instalado.
    )
)

python -c "import uvicorn" >nul 2>&1
if errorlevel 1 (
    echo Instalando uvicorn[standard]...
    python -m pip install --quiet "uvicorn[standard]"
)
echo OK.
echo.

echo [3/6] Verificando Tailscale...
set "TS_PATH=C:\Program Files\Tailscale\tailscale.exe"
if not exist "%TS_PATH%" set "TS_PATH=tailscale"
"%TS_PATH%" status >nul 2>&1
if errorlevel 1 (
    start "" "C:\Program Files\Tailscale\tailscale.exe"
    timeout /t 3 >nul
    "%TS_PATH%" up
)
echo OK.
echo.

echo [4/6] Verificando blockchain e carteira...
python -c "from db import ChainDB; d=ChainDB('brn_v2_chain.db'); print('Altura local:', d.height()); d.close()"

if not exist "current_wallet.enc" (
    echo Criando carteira nova...
    python -c "import os; from wallet import Wallet, WalletManager; w = Wallet.create(); pw = os.environ.get('BRN_WALLET_SESSION_PASS', '12345678'); WalletManager.save_current(w.address, w.priv_hex, w.pub_hex, pw); print('Carteira criada:', w.address)"
)
echo OK.
echo.

echo [5/6] Verificando suporte a PBFT...
set "HAS_PBFT=0"
if exist "pbft_consensus.py" (
    python -c "import pbft_consensus" >nul 2>&1
    if not errorlevel 1 (
        findstr /C:"_setup_pbft" main.py >nul 2>&1
        if not errorlevel 1 (
            set "HAS_PBFT=1"
            echo [ok] pbft_consensus.py presente e main.py integrado
        ) else (
            echo [AVISO] main.py NAO tem _setup_pbft^(^) - PBFT sera desativado
        )
    ) else (
        echo [AVISO] pbft_consensus.py nao importavel - PBFT desativado
    )
) else (
    echo [AVISO] pbft_consensus.py ausente - PBFT desativado
)

if "!HAS_PBFT!"=="0" (
    set "BRN_PBFT_ENABLED=0"
    echo [INFO] BRN_PBFT_ENABLED forcado para 0 ^(multi-sig antigo continua^)
) else (
    echo [ok] PBFT ligado ^(ENABLED=1 ENFORCE=0^)
)
echo OK.
echo.

echo [6/6] Iniciando o no...
echo ============================================================
echo   MODO: GUI (Carteira Grafica)
echo   STATUS: AGUARDANDO CLIQUE NO BOTAO "MINERAR POOL"
if "!HAS_PBFT!"=="1" (
    echo   PBFT: ATIVO ^(quorum 2f+1^)
) else (
    echo   PBFT: desligado
)
echo ============================================================
echo.

REM [FIX] So passa --skip-verify se o main.py aceitar esse argumento
set "MAIN_ARGS="
python main.py --help 2>&1 | findstr /C:"--skip-verify" >nul
if not errorlevel 1 set "MAIN_ARGS=--skip-verify"

echo [bat] python main.py !MAIN_ARGS!
echo.
python main.py !MAIN_ARGS!

echo.
echo ============================================================
echo O programa foi encerrado.
echo ============================================================
pause
