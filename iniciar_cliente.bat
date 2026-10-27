@echo off
REM ============================================================
REM   comeca.bat - BRN Node setup completo (v3.0)
REM   Padrao: GUI (carteira grafica)
REM   Flags:  --headless  --skip-sync  --skip-deps
REM ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM Configura encoding UTF-8 no console
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"

REM Padrao: GUI ativada
set "FORCE_HEADLESS=0"
set "SKIP_SYNC=0"
set "SKIP_DEPS=0"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--headless"      set "FORCE_HEADLESS=1"
if /i "%~1"=="--gui"           set "FORCE_HEADLESS=0"
if /i "%~1"=="--skip-sync"     set "SKIP_SYNC=1"
if /i "%~1"=="--skip-deps"     set "SKIP_DEPS=1"
shift
goto parse_args
:args_done

echo.
echo ============================================================
echo   BRN Node - SETUP COMPLETO (v3.0)
echo ============================================================
echo   Pasta: %CD%
echo   Data:  %DATE% %TIME%
if "%FORCE_HEADLESS%"=="1" (
    echo   Modo:  HEADLESS ^(sem GUI^)
) else (
    echo   Modo:  GUI ^(carteira grafica^)
)
echo ============================================================
echo.

REM ============================================================
REM 1. DETECTAR PYTHON
REM ============================================================
echo [1/7] Detectando Python...
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    echo [ERRO] Python nao encontrado no PATH.
    echo        Instale: https://www.python.org/downloads/
    pause
    exit /b 1
)
%PY% --version
echo [ok] Python: %PY%
echo.

REM ============================================================
REM 2. INSTALAR DEPENDENCIAS
REM ============================================================
if "%SKIP_DEPS%"=="1" goto deps_skip

echo [2/7] Instalando dependencias Python...

%PY% -m pip install --quiet --upgrade pip 2>nul

REM Deps core
set "PKGS=flask flask-cors requests orjson waitress cryptography argon2-cffi mnemonic pymongo"
REM Deps GUI (so se GUI ativada)
if "%FORCE_HEADLESS%"=="0" (
    set "PKGS=!PKGS! pywebview pythonnet"
)

for %%P in (!PKGS!) do (
    echo        - %%P
    %PY% -m pip install --quiet %%P
    if errorlevel 1 (
        echo        [!] falhou, tentando --user...
        %PY% -m pip install --quiet --user %%P
        if errorlevel 1 (
            echo        [X] %%P NAO instalado.
            if /i "%%P"=="pywebview" (
                echo        GUI nao vai funcionar. Use --headless.
                set "FORCE_HEADLESS=1"
            ) else (
                echo        Abortando.
                pause
                exit /b 2
            )
        )
    )
)

echo [ok] Dependencias instaladas
echo.
:deps_skip

REM ============================================================
REM 3. CRIAR .ENV (SE NAO EXISTIR)
REM ============================================================
echo [3/7] Configurando brn_network.env...

if exist "brn_network.env" (
    echo [ok] brn_network.env ja existe - mantendo
    goto env_done
)

REM Conteudo ASCII puro - evita cp1252 do cmd >
(
echo # ============================================================
echo # brn_network.env - BRN Node Config
echo # ============================================================
echo BRN_NETWORK_SECRET=9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9
echo BRN_NODE_PASSWORD=senha-no-temp-123
echo BRN_WALLET_SESSION_PASS=senha-forte-da-carteira-2026
echo BRN_WEB_PASS=senha-temp-123
echo BRN_BOOTSTRAP_PEERS=100.111.253.8:6001,192.168.0.12:6002
echo BRN_POOL_URL=http://100.111.253.8:5000
echo BRN_IS_ORIGIN=0
echo BRN_POOL_ENABLED=1
echo BRN_AUTO_MINE=0
echo BRN_P2P_PORT=6002
echo BRN_P2P_AUTH=optional
echo BRN_UPNP=0
echo BRN_WEB_HOST=0.0.0.0
echo BRN_WEB_PORT=5000
echo BRN_CORS_ORIGIN=*
echo BRN_LOG_LEVEL=INFO
echo BRN_AUDIT_ENABLED=1
echo BRN_SKIP_VERIFY=0
) > brn_network.env

echo [ok] brn_network.env criado
:env_done

%PY% -c "from pathlib import Path; d=Path('brn_network.env').read_bytes(); print('[ok] .env sem BOM' if d[:3]!=b'\xef\xbb\xbf' else '[!] .env TEM BOM')"
echo.

REM ============================================================
REM 4. VERIFICAR ARQUIVOS ESSENCIAIS + GUI
REM ============================================================
echo [4/7] Verificando arquivos...

set "FALTANDO="
for %%F in (main.py server.py blockchain.py db.py crypto.py wallet.py sync_manager.py chain_validator.py checkpoints.py p2p_unified.py discovery_v2.py secure_store.py) do (
    if not exist "%%F" set "FALTANDO=!FALTANDO! %%F"
)
if not "!FALTANDO!"=="" (
    echo [ERRO] Arquivos faltando:!FALTANDO!
    pause
    exit /b 3
)
echo [ok] Arquivos essenciais presentes

REM Verificar arquivos da GUI
if "%FORCE_HEADLESS%"=="0" (
    set "GUI_OK=1"
    if not exist "app_wallet_v3.py" (
        echo [!] app_wallet_v3.py NAO encontrado
        set "GUI_OK=0"
    )
    if not exist "index_wallet.html" (
        echo [!] index_wallet.html NAO encontrado
        set "GUI_OK=0"
    )
    if "!GUI_OK!"=="0" (
        echo [!] GUI indisponivel - caindo para HEADLESS
        set "FORCE_HEADLESS=1"
    ) else (
        echo [ok] Arquivos da GUI presentes
    )
)
echo.

REM ============================================================
REM 5. SINCRONIZAR BLOCKCHAIN
REM ============================================================
if "%SKIP_SYNC%"=="1" goto sync_skip

echo [5/7] Verificando blockchain local...
set "DB_HEIGHT=-1"
for /f "delims=" %%H in ('%PY% -c "from db import ChainDB; d=ChainDB('brn_v2_chain.db'); print(d.height()); d.close()" 2^>nul') do set "DB_HEIGHT=%%H"

echo        Altura local: !DB_HEIGHT!
if not "!DB_HEIGHT!"=="-1" (
    echo [ok] Blockchain ja sincronizada ^(altura !DB_HEIGHT!^)
    goto sync_done
)

echo        DB vazio - sincronizando do Ubuntu...
if not exist "sync_direct.py" (
    echo [ERRO] sync_direct.py nao encontrado
    echo        Crie o arquivo OU rode com --skip-sync
    pause
    exit /b 4
)

%PY% sync_direct.py http://100.111.253.8:5000
if errorlevel 1 (
    echo [X] Sync falhou
    pause
    exit /b 5
)
:sync_done
echo.
:sync_skip

REM ============================================================
REM 6. CRIAR CARTEIRA (SE NAO EXISTIR)
REM ============================================================
echo [6/7] Verificando carteira...

if exist "current_wallet.enc" (
    echo [ok] current_wallet.enc ja existe
    goto wallet_done
)

echo        Criando carteira nova...
%PY% -c "from wallet import Wallet, WalletManager; import os; w = Wallet.create(); pw = os.environ.get('BRN_WALLET_SESSION_PASS', 'senha-forte-da-carteira-2026'); r = WalletManager.save_current(w.address, w.priv_hex, w.pub_hex, pw); print('[ok] carteira criada:', w.address if r.get('ok') else 'FALHOU: ' + r.get('msg', '?'))"

if errorlevel 1 (
    echo [!] Falha ao criar carteira - continuara sem minerar
)
:wallet_done
echo.

REM ============================================================
REM 7. RODAR main.py
REM ============================================================
echo [7/7] Iniciando BRN Node...
echo.

set "HEADLESS_FLAG=--headless"
if "%FORCE_HEADLESS%"=="0" set "HEADLESS_FLAG="

if "%FORCE_HEADLESS%"=="0" (
    echo   MODO: GUI ^(carteira grafica^)
) else (
    echo   MODO: HEADLESS ^(API^/CLI apenas^)
)
echo ============================================================
echo   %PY% main.py %HEADLESS_FLAG% --no-tailscale --skip-verify
echo ============================================================
echo.

%PY% main.py %HEADLESS_FLAG% --no-tailscale --skip-verify
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [X] main.py saiu com codigo %RC%
) else (
    echo [ok] main.py encerrado
)
pause
exit /b %RC%
