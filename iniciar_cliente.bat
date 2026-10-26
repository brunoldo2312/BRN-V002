@echo off
REM ============================================================
REM   iniciar_cliente.bat - BRN Node CLIENTE (Windows) - v12.0
REM ============================================================
REM   v12.0:
REM     [FIX] Removeu a leitura do .env no .bat. Agora o main.py
REM           le direto do arquivo, evitando conflitos de variaveis.
REM     [FIX] Todos os parenteses dentro de blocos IF foram
REM           escapados, evitando o erro "foi inesperado neste momento".
REM     [NEW] Cria o brn_network.env completo se ele nao existir.
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
echo   BRN Node - CLIENTE (Windows) - v12.0
echo ============================================================
echo.

set "PY="
where python >nul 2>&1
if not errorlevel 1 set "PY=python"
if "%PY%"=="" ( where py >nul 2>&1 & set "PY=py -3" )
if "%PY%"=="" ( 
    echo [ERRO] Python nao encontrado. 
    pause 
    exit /b 1 
)
set "PYTHONUNBUFFERED=1"

REM ============================================================
REM VERIFICA E INSTALA DEPENDENCIAS PYTHON
REM ============================================================
echo [*] Verificando dependencias do Python...

%PY% -c "import waitress" >nul 2>&1
if errorlevel 1 ( 
    echo [!] Waitress ausente. Instalando... 
    %PY% -m pip install waitress --quiet 
) else ( echo [ok] Waitress OK )

%PY% -c "import requests" >nul 2>&1
if errorlevel 1 ( 
    echo [!] Requests ausente. Instalando... 
    %PY% -m pip install requests --quiet 
) else ( echo [ok] Requests OK )

%PY% -c "import orjson" >nul 2>&1
if errorlevel 1 ( 
    echo [!] Orjson ausente. Instalando... 
    %PY% -m pip install orjson --quiet 
) else ( echo [ok] Orjson OK )

%PY% -c "import flask, flask_cors" >nul 2>&1
if errorlevel 1 ( 
    echo [!] Flask ausente. Instalando... 
    %PY% -m pip install flask flask-cors --quiet 
) else ( echo [ok] Flask OK )

%PY% -c "import webview" >nul 2>&1
if errorlevel 1 ( 
    echo [!] PyWebView ausente. Instalando... 
    %PY% -m pip install pywebview --quiet 
) else ( echo [ok] PyWebView OK )
echo.

REM ============================================================
REM CRIA O .ENV DE EMERGENCIA (SE NAO EXISTIR)
REM ============================================================
if not exist "brn_network.env" (
    echo [AVISO] brn_network.env nao existe - criando com o modelo CORRETO...
    (
        echo # ============================================================
        echo # brn_network.env - Config do no BRN - Windows Cliente
        echo # ============================================================
        echo BRN_CORS_ORIGIN=*
        echo.
        echo # 1 - SEGREDOS COMPARTILHADOS
        echo BRN_NETWORK_SECRET=9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9
        echo BRN_NODE_PASSWORD=senha-no-temp-123
        echo BRN_WALLET_SESSION_PASS=senha-forte-da-carteira-2026
        echo BRN_WEB_PASS=senha-temp-123
        echo.
        echo # 2 - ADMIN TOKEN
        echo BRN_ADMIN_TOKEN=c745d36d524ea92148d8139731ad663f929fbbe08fb7f9b2234f9109fe1d2b24
        echo.
        echo # 3 - DESCOBERTA DE PEERS
        echo BRN_BOOTSTRAP_PEERS=100.111.253.8:6001
        echo BRN_FALLBACK_PEERS=100.111.253.8:6001
        echo BRN_TRACKER=
        echo BRN_RENDEZVOUS_URL=
        echo.
        echo # 4 - MODO DE OPERACAO
        echo BRN_IS_ORIGIN=0
        echo BRN_POOL_ENABLED=1
        echo BRN_POOL_URL=http://100.111.253.8:5000
        echo.
        echo # 5 - CONSENSO
        echo BRN_SYNC_BATCH=50
        echo BRN_SYNC_BATCH_DELAY=0.06
        echo BRN_SYNC_INTERVAL=5
        echo.
        echo # 6 - P2P
        echo BRN_P2P_PORT=6002
        echo BRN_P2P_AUTH=optional
        echo BRN_P2P_AUTH_WINDOW=30
        echo BRN_P2P_NONCE_CACHE=10000
        echo BRN_RATE_LIMIT=200
        echo BRN_UPNP=0
        echo BRN_TCP_TIMEOUT=10.0
        echo.
        echo # 7 - MINERACAO
        echo BRN_MINER_INTERVAL=30
        echo BRN_ALLOW_SOLO_MINING=0
        echo BRN_MIN_PEER_STABLE=1
        echo.
        echo # 8 - HTTP / API
        echo BRN_WEB_HOST=0.0.0.0
        echo BRN_WEB_PORT=5000
        echo BRN_EXPLORER_PORT=8080
        echo BRN_EXPLORER_HOST=0.0.0.0
        echo BRN_HTTP_THREADS=16
        echo BRN_TRUSTED_PROXIES=
        echo BRN_BEHIND_PROXY=0
        echo BRN_HTTPS=0
        echo.
        echo # 9 - AUDIT
        echo BRN_AUDIT_ENABLED=1
        echo BRN_AUDIT_LOG=audit.log
        echo BRN_AUDIT_MAX_BYTES=52428800
        echo.
        echo # 10 - VERIFY
        echo BRN_SKIP_VERIFY=0
        echo BRN_VERIFY_STRICT=0
        echo BRN_LOG_LEVEL=INFO
        echo.
        echo # 14 - PYTHON
        echo PYTHONUNBUFFERED=1
    ) > brn_network.env
    echo [ok] brn_network.env criado com o modelo correto.
)

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

if not "%FALTA%"=="" ( 
    echo [ERRO] Arquivos faltando:%FALTA% 
    pause 
    exit /b 1 
)
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
