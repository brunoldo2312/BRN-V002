@echo off
REM ============================================================
REM   iniciar_cliente.bat - BRN Node Auto-Setup (v4.3 - Final)
REM   Fluxo: Env -> Deps -> Tailscale -> Sync -> GUI (Carteira)
REM ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM Configura encoding UTF-8 no console
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"

set "SKIP_DEPS=0"
set "SKIP_SYNC=0"
set "FORCE_HEADLESS=0"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--skip-deps" set "SKIP_DEPS=1"
if /i "%~1"=="--skip-sync" set "SKIP_SYNC=1"
if /i "%~1"=="--headless"  set "FORCE_HEADLESS=1"
shift
goto parse_args
:args_done

echo.
echo ============================================================
echo   BRN Node - AUTO SETUP E INICIALIZACAO (v4.3)
echo ============================================================
echo   Pasta: %CD%
echo   Data:  %DATE% %TIME%
if "%FORCE_HEADLESS%"=="1" (
    echo   Modo:  HEADLESS ^(API^)
) else (
    echo   Modo:  GUI ^(Carteira Grafica^)
)
echo ============================================================
echo.

REM ============================================================
REM 0. VERIFICAR ARQUIVO DE CONFIGURACAO (.env)
REM ============================================================
if not exist "brn_network.env" (
    echo [AVISO] O arquivo brn_network.env NAO ENCONTRADO!
    if exist "brn_network.env.example" (
        copy "brn_network.env.example" "brn_network.env" >nul
        echo         [IMPORTANTE] Copia criada. Edite o brn_network.env e preencha as senhas!
    ) else (
        echo [ERRO] brn_network.env.example tambem nao existe. Crie o .env manualmente.
    )
    pause
)
echo.

REM ============================================================
REM 1. VERIFICAR PYTHON
REM ============================================================
echo [1/5] Verificando Python...
where python >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python nao encontrado no PATH.
    echo        Instale em: https://www.python.org/downloads/ e marque "Add to PATH".
    pause
    exit /b 1
)
python --version
echo [ok] Python encontrado.
echo.

REM ============================================================
REM 2. INSTALAR DEPENDENCIAS
REM ============================================================
if "%SKIP_DEPS%"=="1" goto deps_skip

echo [2/5] Verificando dependencias (isso pode demorar)...
REM Adicionado zeroconf, bitcoinlib, pythonnet
set "PKGS=flask flask-cors fastapi uvicorn requests orjson waitress cryptography argon2-cffi mnemonic pymongo pywebview pythonnet bitcoinlib zeroconf"

for %%P in (!PKGS!) do (
    python -c "import %%P" >nul 2>&1
    if errorlevel 1 (
        echo        Instalando %%P...
        python -m pip install --quiet %%P
        if errorlevel 1 (
            echo        [AVISO] Falha ao instalar %%P. Tentando com --user...
            python -m pip install --quiet --user %%P
        )
    ) else (
        echo        [ok] %%P ja instalado.
    )
)
echo [ok] Dependencias verificadas.
echo.
:deps_skip

REM ============================================================
REM 3. VERIFICAR TAILSCALE
REM ============================================================
echo [3/5] Verificando Tailscale...
set "TS_PATH=C:\Program Files\Tailscale\tailscale.exe"
if not exist "%TS_PATH%" set "TS_PATH=tailscale"

"%TS_PATH%" status >nul 2>&1
if errorlevel 1 (
    echo        Tailscale nao esta rodando ou desconectado.
    start "" "C:\Program Files\Tailscale\tailscale.exe"
    timeout /t 3 >nul
    "%TS_PATH%" up
) else (
    echo [ok] Tailscale esta rodando.
)
echo.

REM ============================================================
REM 4. SINCRONIZAR BLOCKCHAIN
REM ============================================================
if "%SKIP_SYNC%"=="1" goto sync_skip

echo [4/5] Verificando blockchain...
set "DB_HEIGHT=-1"
for /f "delims=" %%H in ('python -c "from db import ChainDB; d=ChainDB('brn_v2_chain.db'); print(d.height()); d.close()" 2^>nul') do set "DB_HEIGHT=%%H"

echo        Altura local: !DB_HEIGHT!
if not "!DB_HEIGHT!"=="-1" (
    echo [ok] Blockchain ja sincronizada.
    goto sync_done
)

echo        DB vazio - tentando sincronizar via HTTP...
if exist "sync_direct.py" (
    python sync_direct.py http://100.111.253.8:5000
    if errorlevel 1 (
        echo [AVISO] sync_direct falhou. O main.py tentara sync automatico.
    )
) else (
    echo [INFO] sync_direct.py nao encontrado. Sync automatico no main.py.
)
:sync_done
echo.
:sync_skip

REM ============================================================
REM 5. INICIAR CARTEIRA GRAFICA
REM ============================================================
echo [5/5] Iniciando BRN Node...
echo.

REM Cria a carteira se nao existir (Lendo o .env para pegar a senha correta!)
if not exist "current_wallet.enc" (
    echo        Criando carteira nova...
    python -c "import os; from pathlib import Path; [os.environ.setdefault(k.strip(), v.strip()) for line in Path('brn_network.env').read_text(encoding='utf-8').splitlines() if '=' in line and not line.strip().startswith('#') for k,v in [line.split('=',1)]]; from wallet import Wallet, WalletManager; w = Wallet.create(); pw = os.environ.get('BRN_WALLET_SESSION_PASS', '12345678'); WalletManager.save_current(w.address, w.priv_hex, w.pub_hex, pw); print('Carteira criada:', w.address)"
)

set "HEADLESS_FLAG=--headless"
if "%FORCE_HEADLESS%"=="0" set "HEADLESS_FLAG="

echo ============================================================
echo   INICIANDO O NO...
echo   python main.py %HEADLESS_FLAG% --skip-verify
echo ============================================================
echo.

python main.py %HEADLESS_FLAG% --skip-verify

if errorlevel 1 (
    echo [X] O programa saiu com erro.
)
pause
