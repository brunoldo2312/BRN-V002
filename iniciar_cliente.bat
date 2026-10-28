@echo off
REM ============================================================
REM   iniciar_cliente.bat - BRN Node Auto-Setup (v4.0)
REM   Fluxo: Deps -> Tailscale -> Sync -> GUI (Carteira)
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
echo   BRN Node - AUTO SETUP E INICIALIZACAO (v4.0)
echo ============================================================
echo   Pasta: %CD%
echo   Data:  %DATE% %TIME%
echo ============================================================
echo.

REM ============================================================
REM 1. VERIFICAR PYTHON
REM ============================================================
echo [1/5] Verificando Python...
where python >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python nao encontrado no PATH.
    echo        Instale em: https://www.python.org/downloads/
    echo        Marque "Add Python to PATH".
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

echo [2/5] Verificando e instalando dependencias...
set "PKGS=flask flask-cors requests orjson waitress cryptography argon2-cffi mnemonic pymongo pywebview pythonnet"

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
    echo        Tentando iniciar o Tailscale...
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

echo        DB vazio - sincronizando do Ubuntu...
if exist "sync_direct.py" (
    python sync_direct.py http://100.111.253.8:5000
    if errorlevel 1 (
        echo [AVISO] sync_direct falhou. O main.py tentara sync automatico via HTTP.
    )
) else (
    echo [INFO] sync_direct.py nao encontrado.
    echo [INFO] O main.py v2.0 fara o sync automatico via HTTP.
)
:sync_done
echo.
:sync_skip

REM ============================================================
REM 5. INICIAR CARTEIRA GRAFICA
REM ============================================================
echo [5/5] Iniciando Carteira Grafica...
echo.

REM Cria a carteira se nao existir
if not exist "current_wallet.enc" (
    echo        Criando carteira nova...
    python -c "from wallet import Wallet, WalletManager; import os; w = Wallet.create(); pw = os.environ.get('BRN_WALLET_SESSION_PASS', 'senha-forte-da-carteira-2026'); WalletManager.save_current(w.address, w.priv_hex, w.pub_hex, pw)"
)

echo ============================================================
echo   ABRINDO GUI...
echo ============================================================
echo.

REM Roda o main.py SEM --headless para abrir a GUI
REM O --skip-verify acelera o boot inicial
python main.py --skip-verify

if errorlevel 1 (
    echo [X] O programa saiu com erro.
)
pause
