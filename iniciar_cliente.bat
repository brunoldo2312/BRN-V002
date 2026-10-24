set "BRN_BOOTSTRAP_PEERS=100.84.11.48:6001"  // Se o Origem for a Mayra
set "BRN_POOL_URL=http://100.84.11.48:5000"
@echo off
REM ============================================================
REM   iniciar_cliente.bat - BRN Node CLIENTE (Windows) - v6
REM ============================================================
REM   v6:
REM     [FIX] Corrigido para atuar como CLIENTE (não força Origem).
REM     [FIX] Mantém o BRN_BOOTSTRAP_PEERS para conectar no Origem.
REM     [FIX] Abre a GUI corretamente usando Waitress.
REM ============================================================

setlocal EnableDelayedExpansion

cd /d "%~dp0"
if errorlevel 1 (
    echo [ERRO] Nao consegui cd para %~dp0
    pause
    exit /b 1
)

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
if /i "%~1"=="-h"                goto show_help
if /i "%~1"=="--help"            goto show_help
shift
goto parse_args

:show_help
echo Uso: iniciar_cliente.bat [--skip-tailscale] [--gui] [--no-gui] [--pool-client]
echo   --gui     Forca abrir a carteira
echo   --no-gui  Roda apenas o servidor em background
exit /b 0

:args_done

echo ============================================================
echo   BRN Node - CLIENTE (Windows) - v6
echo ============================================================
echo.

set "PY="
where python >nul 2>&1
if not errorlevel 1 set "PY=python"

if "%PY%"=="" (
    where py >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)

if "%PY%"=="" (
    echo [ERRO] Python 3 nao encontrado
    pause
    exit /b 1
)

set "PYVER=?"
for /f "tokens=*" %%V in ('%PY% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PYVER=%%V"
echo [ok] Python %PYVER% ^(%PY%^)
echo.

if not exist "brn_network.env" (
    echo [AVISO] brn_network.env nao existe - criando com defaults de CLIENTE...
    (
        echo BRN_NETWORK_SECRET=9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9
        echo BRN_NODE_PASSWORD=senha-no-temp-123
        echo BRN_WEB_PASS=senha-temp-123
        echo BRN_WALLET_SESSION_PASS=senha-forte-da-carteira-2026
        echo BRN_ADMIN_TOKEN=
        echo BRN_BOOTSTRAP_PEERS=100.111.253.8:6001
        echo BRN_IS_ORIGIN=0
        echo BRN_ALL_MINERS=0
        echo BRN_WEB_HOST=0.0.0.0
        echo BRN_WEB_PORT=5000
        echo BRN_EXPLORER_PORT=8080
        echo BRN_EXPLORER_HOST=0.0.0.0
        echo BRN_HTTP_THREADS=16
        echo BRN_P2P_PORT=6002
        echo BRN_P2P_AUTH=optional
        echo BRN_RATE_LIMIT=200
        echo BRN_UPNP=0
        echo BRN_SKIP_VERIFY=0
        echo BRN_VERIFY_STRICT=0
        echo BRN_MINER_AUTO=1
        echo BRN_MINER_INTERVAL=30
        echo BRN_LOG_LEVEL=INFO
        echo BRN_POOL_ENABLED=1
        echo BRN_POOL_URL=http://100.111.253.8:5000
    ) > brn_network.env
    echo [ok] brn_network.env criado ^(BRN_IS_ORIGIN=0^)
)

echo [*] Carregando brn_network.env...
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("brn_network.env") do (
    set "K=%%A"
    set "V=%%B"
    if not "!K!"=="" (
        if "!V:~0,1!"=="""" set "V=!V:~1!"
        if "!V:~-1!"=="""" set "V=!V:~0,-1!"
        if "!V:~0,1!"=="'" set "V=!V:~1!"
        if "!V:~-1!"=="'" set "V=!V:~0,-1!"
        for /f "tokens=* delims= " %%X in ("!K!") do set "K=%%X"
        if not "!K!"=="" set "!K!=!V!"
    )
)
echo [ok] brn_network.env carregado
echo.

if "%BRN_NODE_PASSWORD%"==""       set "BRN_NODE_PASSWORD=senha-no-temp-123"
if "%BRN_WEB_PASS%"==""            set "BRN_WEB_PASS=senha-temp-123"
if "%BRN_WALLET_SESSION_PASS%"=="" set "BRN_WALLET_SESSION_PASS=senha-forte-da-carteira-2026"
if "%BRN_WEB_PORT%"==""            set "BRN_WEB_PORT=5000"
if "%BRN_EXPLORER_PORT%"==""       set "BRN_EXPLORER_PORT=8080"
if "%BRN_P2P_PORT%"==""            set "BRN_P2P_PORT=6002"
if "%BRN_LOG_LEVEL%"==""           set "BRN_LOG_LEVEL=INFO"
if "%BRN_MINER_AUTO%"==""          set "BRN_MINER_AUTO=1"
if "%BRN_MINER_INTERVAL%"==""      set "BRN_MINER_INTERVAL=30"
set "PYTHONUNBUFFERED=1"

REM [FIX] Força modo CLIENTE, independentemente do .env
set "BRN_IS_ORIGIN=0"

set "FALTA="
for %%F in (
    main.py server.py explorer.py
    blockchain.py db.py crypto.py wallet.py bech32.py
    chain_validator.py checkpoints.py
    brn_config.py brn_logger.py version.py
    security.py secure_store.py
    p2p_unified.py p2p_auth.py p2p_secure.py p2p_cipher.py p2p_relay.py
    discovery_v2.py rendezvous_discovery.py rendezvous_mongo.py
    miner_loop.py mining_pool.py miner_pool_client.py
    sync_manager.py
    l2_manager.py btc_watcher.py btc_config.py
    contracts\__init__.py contracts\vm.py contracts\escrow_btc_brn.py contracts\factory.py
) do (
    if not exist "%%F" set "FALTA=!FALTA! %%F"
)

if not "%FALTA%"=="" (
    echo [ERRO] Arquivos faltando:%FALTA%
    pause
    exit /b 1
)
echo [ok] Arquivos essenciais OK
echo.

set "MY_TS_IP="

if "%SKIP_TAILSCALE%"=="1" (
    echo [*] Tailscale: pulado ^(--skip-tailscale^)
    goto tailscale_done
)

where tailscale >nul 2>&1
if errorlevel 1 (
    echo [AVISO] Tailscale NAO instalado.
    pause
    exit /b 1
)
echo [ok] Tailscale instalado

for /f "tokens=*" %%I in ('tailscale ip -4 2^>nul') do (
    if "!MY_TS_IP!"=="" set "MY_TS_IP=%%I"
)
echo [ok] Meu IP Tailscale: %MY_TS_IP%
echo [*] Este no vai se conectar ao Origem em %BRN_BOOTSTRAP_PEERS%.

:tailscale_done
echo.

%PY% -c "import waitress" >nul 2>&1
if errorlevel 1 (
    echo [AVISO] Waitress NAO instalado - HTTP usara Flask dev server
) else (
    for /f "tokens=*" %%V in ('%PY% -c "import waitress;print(waitress.__version__)" 2^>nul') do set "WV=%%V"
    echo [ok] Waitress v!WV!
)
echo.

> "brn_role.txt" echo client
echo [ok] Papel: CLIENTE
echo.

set "HEADLESS_FLAG=--headless"

if "%FORCE_NO_GUI%"=="1" (
    echo [*] GUI desabilitada por --no-gui
    goto gui_done
)

if "%FORCE_GUI%"=="1" (
    set "HEADLESS_FLAG="
    echo [ok] GUI forcada por --gui
    goto gui_done
)

%PY% -c "import webview" >nul 2>&1
if errorlevel 1 (
    echo [AVISO] pywebview ausente - modo HEADLESS
    goto gui_done
)

echo [ok] pywebview OK - carteira desktop vai abrir
set "HEADLESS_FLAG="

:gui_done
set "GUI_STATUS=HEADLESS"
if "%HEADLESS_FLAG%"=="" set "GUI_STATUS=GUI"
echo.

set "ADMIN_STATUS=NAO (endpoints admin ABERTOS)"
if not "%BRN_ADMIN_TOKEN%"=="" (
    set "ADMIN_STATUS=SIM"
)
echo.

set "POOL_CLIENT_FLAG="
set "POOL_CLIENT_STATUS=DISABLED (evita duplicidade com a GUI)"
if "%NO_POOL_CLIENT%"=="0" (
    set "POOL_CLIENT_FLAG=--pool-client"
    set "POOL_CLIENT_STATUS=ENABLED"
)

set "NODE_VERSION=?"
for /f "tokens=*" %%V in ('%PY% main.py --version 2^>nul') do set "NODE_VERSION=%%V"
echo [ok] main.py versao: %NODE_VERSION%
echo.

echo ============================================================
echo   CONFIGURACAO ATUAL (CLIENTE)
echo ============================================================
echo   Modo       : CLIENTE ^(conectando ao Origem^)
echo   Versao     : %NODE_VERSION%
echo   IS_ORIGIN  : %BRN_IS_ORIGIN%
echo   Bootstrap  : %BRN_BOOTSTRAP_PEERS%
echo   Pool URL   : %BRN_POOL_URL%
echo   Dificuldade: Automatica ^(Calculada pelo programa^)
echo   Miner auto : %BRN_MINER_AUTO%
echo   P2P porta  : %BRN_P2P_PORT%
echo   HTTP       : http://0.0.0.0:%BRN_WEB_PORT%
echo   pywebview  : %GUI_STATUS%
echo   PoolClient : %POOL_CLIENT_STATUS%
echo   Tailscale  : %MY_TS_IP%
echo ============================================================
echo.
echo   ATENCAO: deixe esta janela ABERTA enquanto o CLIENTE roda.
echo.

echo [*] Iniciando CLIENTE BRN... ^(Ctrl+C para encerrar^)
echo.

%PY% main.py %HEADLESS_FLAG% %POOL_CLIENT_FLAG%
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo [ERRO] O no saiu com codigo %EXITCODE%
    echo.
    echo   Se o erro foi 'Falha ao decifrar node_identity.enc':
    echo     del node_identity.enc
    echo     iniciar_cliente.bat
    echo.
) else (
    echo [ok] No encerrado normalmente.
)

echo.
pause
exit /b %EXITCODE%
