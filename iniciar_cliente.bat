@echo off
REM ============================================================
REM   iniciar_cliente.bat - BRN Node CLIENTE (Windows) - v13.0
REM ============================================================
REM   v13.0 (2026-10-26):
REM     [FIX] where py robusto (antes PY ficava "py -3" fantasma)
REM     [FIX] --no-gui agora realmente forca headless (era ignorado)
REM     [FIX] --no-tailscale agora propaga para o main.py
REM     [FIX] Verifica brn_tailscale_setup.py antes de rodar
REM     [FIX] Template .env atualizado (bootstrap 100.70.107.15)
REM     [NEW] --bootstrap <IP:PORTA> para trocar sem editar .env
REM     [NEW] Header mostra FLAGS antes de rodar (debug)
REM     [NEW] try/fail no pip install (nao silencia erro)
REM ============================================================

setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM ===== DEFAULTS =====
set "SKIP_TAILSCALE=0"
set "FORCE_GUI=0"
set "FORCE_NO_GUI=0"
set "NO_POOL_CLIENT=1"
set "BOOTSTRAP_OVERRIDE="

REM ===== PARSE ARGS =====
:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--skip-tailscale"  set "SKIP_TAILSCALE=1"
if /i "%~1"=="--gui"             set "FORCE_GUI=1"
if /i "%~1"=="--no-gui"          set "FORCE_NO_GUI=1"
if /i "%~1"=="--no-pool-client"  set "NO_POOL_CLIENT=1"
if /i "%~1"=="--pool-client"     set "NO_POOL_CLIENT=0"
if /i "%~1"=="--bootstrap" (
    if not "%~2"=="" set "BOOTSTRAP_OVERRIDE=%~2"
    shift
)
shift
goto parse_args
:args_done

REM ===== BANNER =====
echo ============================================================
echo   BRN Node - CLIENTE (Windows) - v13.0
echo ============================================================
echo.

REM ===== 1. FIND PYTHON (ROBUSTO) =====
set "PY="
where python >nul 2>&1
if not errorlevel 1 goto py_python
where py >nul 2>&1
if not errorlevel 1 goto py_py_launcher
goto py_not_found

:py_python
set "PY=python"
goto py_ok

:py_py_launcher
set "PY=py -3"
goto py_ok

:py_not_found
echo [ERRO] Python nao encontrado.
echo        Instale em: https://www.python.org/downloads/
echo        Marque "Add Python to PATH" na instalacao.
pause
exit /b 1

:py_ok
%PY% --version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] "%PY%" nao funciona. Verifique a instalacao do Python.
    pause
    exit /b 1
)
echo [ok] Python: %PY%
set "PYTHONUNBUFFERED=1"

REM ===== 2. DEPENDENCIAS =====
echo.
echo [*] Verificando dependencias Python...
call :dep waitress
call :dep requests
call :dep orjson
call :dep flask
call :dep flask_cors
echo.

REM ===== 3. .ENV (CRIA SE NAO EXISTIR) =====
if not exist "brn_network.env" call :create_env_template

REM ===== 4. TAILSCALE =====
if "%SKIP_TAILSCALE%"=="1" goto skip_tailscale
where tailscale >nul 2>&1
if errorlevel 1 (
    echo [!] Tailscale nao instalado. Pulando descoberta global.
    echo     Instale em: https://tailscale.com/download/windows
    goto skip_tailscale
)
if not exist "brn_tailscale_setup.py" (
    echo [!] brn_tailscale_setup.py nao encontrado. Pulando.
    goto skip_tailscale
)
echo [*] Executando descoberta global via Tailscale...
%PY% brn_tailscale_setup.py --auto
if errorlevel 1 echo [!] brn_tailscale_setup.py falhou (continuando)
echo.
:skip_tailscale

REM ===== 5. VERIFICA ARQUIVOS ESSENCIAIS =====
set "FALTA="
for %%F in (
    main.py server.py blockchain.py db.py crypto.py wallet.py
    chain_validator.py checkpoints.py security.py secure_store.py
    p2p_unified.py discovery_v2.py miner_loop.py sync_manager.py
) do (
    if not exist "%%F" set "FALTA=!FALTA! %%F"
)
if not "%FALTA%"=="" (
    echo [ERRO] Arquivos faltando:!FALTA!
    pause
    exit /b 1
)
echo [ok] Arquivos essenciais OK

REM ===== 6. ROLE =====
> "brn_role.txt" echo client
echo [ok] Papel: CLIENTE

REM ===== 7. GUI vs HEADLESS (CORRIGIDO) =====
set "HEADLESS_FLAG=--headless"

if "%FORCE_NO_GUI%"=="1" goto gui_decided
if "%FORCE_GUI%"=="1" (
    set "HEADLESS_FLAG="
    goto gui_decided
)

REM Auto-detect: se pywebview existe, usa GUI
%PY% -c "import webview" >nul 2>&1
if not errorlevel 1 set "HEADLESS_FLAG="
:gui_decided

REM ===== 8. FLAGS =====
set "POOL_CLIENT_FLAG="
if "%NO_POOL_CLIENT%"=="0" set "POOL_CLIENT_FLAG=--pool-client"

set "TAILSCALE_FLAG="
if "%SKIP_TAILSCALE%"=="1" set "TAILSCALE_FLAG=--no-tailscale"

REM ===== 9. BOOTSTRAP OVERRIDE (--bootstrap) =====
if not "%BOOTSTRAP_OVERRIDE%"=="" (
    echo [*] Aplicando bootstrap override: %BOOTSTRAP_OVERRIDE%
    set "BRN_BOOTSTRAP_PEERS=%BOOTSTRAP_OVERRIDE%"
)

REM ===== 10. START =====
echo.
echo ============================================================
echo   INICIANDO CLIENTE...
echo ============================================================
echo   HEADLESS_FLAG  : %HEADLESS_FLAG%
echo   POOL_CLIENT    : %POOL_CLIENT_FLAG%
echo   TAILSCALE_FLAG : %TAILSCALE_FLAG%
echo   BOOTSTRAP      : %BOOTSTRAP_OVERRIDE%
echo ============================================================
echo.

%PY% main.py %HEADLESS_FLAG% %POOL_CLIENT_FLAG% %TAILSCALE_FLAG%
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [X] main.py saiu com codigo %RC%
) else (
    echo [ok] main.py encerrado normalmente
)
pause
exit /b %RC%

REM ============================================================
REM SUBROTINAS
REM ============================================================
:dep
%PY% -c "import %~1" >nul 2>&1
if not errorlevel 1 (
    echo [ok] %~1 OK
    exit /b 0
)
echo [!] %~1 ausente. Instalando...
%PY% -m pip install %~1 --quiet
if errorlevel 1 (
    echo [X] Falha ao instalar %~1
    exit /b 1
)
echo [ok] %~1 instalado
exit /b 0

:create_env_template
echo [AVISO] brn_network.env nao existe - criando template...
(
    echo # ============================================================
    echo # brn_network.env - Config do no BRN - Windows Cliente
    echo # EDITE OS VALORES ABAIXO ANTES DE RODAR!
    echo # ============================================================
    echo.
    echo # 1 - SEGREDOS (devem ser IGUAIS ao ORIGEM)
    echo BRN_NETWORK_SECRET=9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9
    echo BRN_NODE_PASSWORD=senha-no-temp-123
    echo BRN_WALLET_SESSION_PASS=senha-forte-da-carteira-2026
    echo BRN_WEB_PASS=senha-temp-123
    echo.
    echo # 2 - DESCOBERTA DE PEERS (IP:PORTA do ORIGEM)
    echo # Para Desktop-61ltr37 (chain 5026): 100.70.107.15:6002
    echo # Para Ubuntu (chain 13862):         100.111.253.8:6001
    echo BRN_BOOTSTRAP_PEERS=100.70.107.15:6002
    echo BRN_POOL_URL=http://100.70.107.15:5000
    echo.
    echo # 3 - MODO
    echo BRN_IS_ORIGIN=0
    echo BRN_POOL_ENABLED=1
    echo BRN_AUTO_MINE=0
    echo.
    echo # 4 - P2P
    echo BRN_P2P_PORT=6002
    echo BRN_P2P_AUTH=optional
    echo BRN_UPNP=0
    echo.
    echo # 5 - HTTP
    echo BRN_WEB_HOST=0.0.0.0
    echo BRN_WEB_PORT=5000
    echo BRN_CORS_ORIGIN=*
    echo.
    echo # 6 - LOG
    echo BRN_LOG_LEVEL=INFO
    echo BRN_AUDIT_ENABLED=1
    echo BRN_SKIP_VERIFY=0
    echo.
    echo PYTHONUNBUFFERED=1
) > brn_network.env
echo [ok] brn_network.env criado
echo [AVISO] EDITE o template antes de rodar ^(especialmente BRN_BOOTSTRAP_PEERS^)!
exit /b 0
