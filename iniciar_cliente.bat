@echo off
REM ============================================================
REM   iniciar_cliente.bat - BRN Node CLIENTE (Windows) - v11
REM ============================================================
REM   v11:
REM     [NEW] Instala automaticamente dependencias Python (waitress,
REM           requests, flask, flask_cors, orjson, pywebview).
REM     [NEW] Instala Tailscale automaticamente via Winget se faltar.
REM ============================================================

setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "SKIP_TAILSCALE=0"
set "FORCE_GUI=0"
set "FORCE_NO_GUI=0"
set "NO_POOL_CLIENT=1"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--skip-tailscale"  set "SKIP_TAILSCALE=1"
if /i "%~1"=="--gui"             set "FORCE_GUI=1"
if /i "%~1"=="--no-gui"          set "FORCE_NO_GUI=1"
if /i "%~1"=="--no-pool-client"  set "NO_POOL_CLIENT=1"
if /i "%~1"=="--pool-client"     set "NO_POOL_CLIENT=0"
shift
goto parse_args
:args_done

echo ============================================================
echo   BRN Node - CLIENTE (Windows) - v11
echo ============================================================
echo.

set "PY="
where python >nul 2>&1
if not errorlevel 1 set "PY=python"
if "%PY%"=="" ( where py >nul 2>&1 & set "PY=py -3" )
if "%PY%"=="" ( echo [ERRO] Python nao encontrado. & pause & exit /b 1 )
set "PYTHONUNBUFFERED=1"

REM ============================================================
REM [NOVO v11] VERIFICA E INSTALA DEPENDENCIAS PYTHON
REM ============================================================
echo [*] Verificando dependencias do Python...

REM 1. Waitress (Servidor HTTP)
%PY% -c "import waitress" >nul 2>&1
if errorlevel 1 (
    echo [!] Waitress ausente. Instalando...
    %PY% -m pip install waitress --quiet
) else (
    echo [ok] Waitress OK
)

REM 2. Requests (API da Carteira)
%PY% -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo [!] Requests ausente. Instalando...
    %PY% -m pip install requests --quiet
) else (
    echo [ok] Requests OK
)

REM 3. Orjson (Banco de dados)
%PY% -c "import orjson" >nul 2>&1
if errorlevel 1 (
    echo [!] Orjson ausente. Instalando...
    %PY% -m pip install orjson --quiet
) else (
    echo [ok] Orjson OK
)

REM 4. Flask (Servidor do nó)
%PY% -c "import flask, flask_cors" >nul 2>&1
if errorlevel 1 (
    echo [!] Flask ausente. Instalando...
    %PY% -m pip install flask flask-cors --quiet
) else (
    echo [ok] Flask OK
)

REM 5. PyWebView (Carteira Gráfica)
%PY% -c "import webview" >nul 2>&1
if errorlevel 1 (
    echo [!] PyWebView ausente. Instalando...
    %PY% -m pip install pywebview --quiet
) else (
    echo [ok] PyWebView OK
)
echo.

if not exist "brn_network.env" (
    echo [AVISO] brn_network.env nao existe - criando...
    (
        echo BRN_NETWORK_SECRET=9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9
        echo BRN_IS_ORIGIN=0
        echo BRN_BOOTSTRAP_PEERS=
        echo BRN_POOL_URL=http://100.111.253.8:5000
        echo BRN_P2P_PORT=6002
        echo BRN_WEB_PORT=5000
        echo TS_AUTH_KEY=
    ) > brn_network.env
)

for /f "usebackq eol=# tokens=1,* delims==" %%A in ("brn_network.env") do (
    set "K=%%A"
    set "V=%%B"
    if not "!K!"=="" (
        if "!V:~0,1!"=="""" set "V=!V:~1!"
        if "!V:~-1!"=="""" set "V=!V:~0,-1!"
        for /f "tokens=* delims= " %%X in ("!K!") do set "K=%%X"
        if not "!K!"=="" set "!K!=!V!"
    )
)

set "BRN_IS_ORIGIN=0"

REM ============================================================
REM INSTALACAO AUTOMATICA DO TAILSCALE
REM ============================================================
if "%SKIP_TAILSCALE%"=="0" (
    where tailscale >nul 2>&1
    if errorlevel 1 (
        echo [!] Tailscale NAO instalado.
        echo [*] Tentando instalar automaticamente via Winget...
        winget install -e --id Tailscale.Tailscale --silent --accept-source-agreements --accept-package-agreements
        
        set "PATH=%PATH%;C:\Program Files\Tailscale"
        
        where tailscale >nul 2>&1
        if errorlevel 1 (
            echo [X] Falha ao instalar automaticamente.
            echo     Instale manualmente em: https://tailscale.com/download/windows
            pause
            exit /b 1
        )
        echo [ok] Tailscale instalado com sucesso!
    )
    
    echo [*] Executando descoberta global via Tailscale...
    %PY% brn_tailscale_setup.py --auto
    
    if errorlevel 1 (
        echo [AVISO] Descoberta automatica falhou ou Tailscale offline.
    ) else (
        set "BRN_BOOTSTRAP_PEERS="
    )
    echo.
)

set "FALTA="
for %%F in (
    main.py server.py explorer.py blockchain.py db.py crypto.py wallet.py bech32.py
    chain_validator.py checkpoints.py brn_config.py brn_logger.py version.py
    security.py secure_store.py p2p_unified.py p2p_auth.py p2p_secure.py p2p_cipher.py p2p_relay.py
    discovery_v2.py rendezvous_discovery.py rendezvous_mongo.py miner_loop.py mining_pool.py miner_pool_client.py
    sync_manager.py l2_manager.py btc_watcher.py btc_config.py
    contracts\__init__.py contracts\vm.py contracts\escrow_btc_brn.py contracts\factory.py
) do ( if not exist "%%F" set "FALTA=!FALTA! %%F" )

if not "%FALTA%"=="" ( echo [ERRO] Arquivos faltando:%FALTA% & pause & exit /b 1 )
echo [ok] Arquivos essenciais OK

> "brn_role.txt" echo client
echo [ok] Papel: CLIENTE

set "HEADLESS_FLAG=--headless"
if "%FORCE_GUI%"=="1" ( set "HEADLESS_FLAG=" )
%PY% -c "import webview" >nul 2>&1
if not errorlevel 1 set "HEADLESS_FLAG="

set "POOL_CLIENT_FLAG="
if "%NO_POOL_CLIENT%"=="0" set "POOL_CLIENT_FLAG=--pool-client"

echo ============================================================
echo   INICIANDO CLIENTE...
echo ============================================================
%PY% main.py %HEADLESS_FLAG% %POOL_CLIENT_FLAG%
pause
exit /b %ERRORLEVEL%
