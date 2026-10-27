@echo off
REM ============================================================
REM   instalar_e_rodar.bat - BRN Node setup completo (v2.0)
REM   Detecta, instala, sincroniza e roda - em sequencia segura
REM ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "SKIP_SYNC=0"
set "SKIP_DEPS=0"
set "FORCE_HEADLESS=1"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--skip-sync"     set "SKIP_SYNC=1"
if /i "%~1"=="--skip-deps"     set "SKIP_DEPS=1"
if /i "%~1"=="--gui"           set "FORCE_HEADLESS=0"
shift
goto parse_args
:args_done

echo.
echo ============================================================
echo   BRN Node - SETUP COMPLETO (v2.0)
echo ============================================================
echo   Pasta: %CD%
echo   Data:  %DATE% %TIME%
echo ============================================================
echo.

REM ============================================================
REM 1. DETECTAR PYTHON
REM ============================================================
echo [1/6] Detectando Python...
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    echo [ERRO] Python nao encontrado no PATH.
    echo        Instale: https://www.python.org/downloads/
    echo        Marque "Add Python to PATH" na instalacao.
    pause
    exit /b 1
)
%PY% --version
set "PYTHONUNBUFFERED=1"
echo [ok] Python: %PY%
echo.

REM ============================================================
REM 2. INSTALAR DEPENDENCIAS
REM ============================================================
if "%SKIP_DEPS%"=="1" goto deps_skip

echo [2/6] Instalando dependencias Python...
echo        (isso pode levar 2-3 min na primeira vez)
echo.

%PY% -m pip install --quiet --upgrade pip 2>nul
if errorlevel 1 echo [!] pip upgrade falhou (continuando)

set "PKGS=flask flask-cors requests orjson waitress cryptography argon2-cffi mnemonic pymongo"
for %%P in (%PKGS%) do (
    echo        - %%P
    %PY% -m pip install --quiet %%P
    if errorlevel 1 (
        echo        [X] falhou ao instalar %%P
        echo        Tentando com --user...
        %PY% -m pip install --quiet --user %%P
        if errorlevel 1 (
            echo        [X] %%P NAO instalado. Abortando.
            pause
            exit /b 2
        )
    )
)

echo [ok] Dependencias instaladas
echo.
:deps_skip

REM ============================================================
REM 3. CRIAR .ENV (SE NAO EXISTIR)
REM ============================================================
echo [3/6] Configurando brn_network.env...

if exist "brn_network.env" (
    echo [ok] brn_network.env ja existe - mantendo
    goto env_done
)

(
echo # ============================================================
echo # brn_network.env - BRN Node Config
echo # ============================================================
echo.
echo # SEGREDOS - DEVEM SER IGUAIS AO UBUNTU
echo BRN_NETWORK_SECRET=9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9
echo BRN_NODE_PASSWORD=senha-no-temp-123
echo BRN_WALLET_SESSION_PASS=senha-forte-da-carteira-2026
echo BRN_WEB_PASS=senha-temp-123
echo.
echo # PEERS: Ubuntu primeiro ^(mais atualizado^), 61ltr37 segundo
echo BRN_BOOTSTRAP_PEERS=100.111.253.8:6001,192.168.0.12:6002
echo BRN_POOL_URL=http://100.111.253.8:5000
echo.
echo # MODO
echo BRN_IS_ORIGIN=0
echo BRN_POOL_ENABLED=1
echo BRN_AUTO_MINE=0
echo.
echo # P2P
echo BRN_P2P_PORT=6002
echo BRN_P2P_AUTH=optional
echo BRN_UPNP=0
echo.
echo # HTTP
echo BRN_WEB_HOST=0.0.0.0
echo BRN_WEB_PORT=5000
echo BRN_CORS_ORIGIN=*
echo.
echo # LOG
echo BRN_LOG_LEVEL=INFO
echo BRN_AUDIT_ENABLED=1
echo BRN_SKIP_VERIFY=0
) > brn_network.env

echo [ok] brn_network.env criado
:env_done

REM Verificar BOM (PowerShell cmd) - avisa se o arquivo esta estranho
%PY% -c "from pathlib import Path; d=Path('brn_network.env').read_bytes(); print('[ok] .env sem BOM' if d[:3]!=b'\xef\xbb\xbf' else '[!] .env TEM BOM - considere reescrever')"
echo.

REM ============================================================
REM 4. VERIFICAR ARQUIVOS ESSENCIAIS
REM ============================================================
echo [4/6] Verificando arquivos do BRN...
set "FALTANDO="
for %%F in (main.py server.py blockchain.py db.py crypto.py wallet.py sync_manager.py chain_validator.py checkpoints.py p2p_unified.py discovery_v2.py secure_store.py) do (
    if not exist "%%F" set "FALTANDO=!FALTANDO! %%F"
)
if not "!FALTANDO!"=="" (
    echo [ERRO] Arquivos faltando:!FALTANDO!
    echo        Copie a pasta BRN-V002-main completa do outro PC.
    pause
    exit /b 3
)
echo [ok] Todos os arquivos essenciais presentes
echo.

REM ============================================================
REM 5. SINCRONIZAR BLOCKCHAIN (SE NECESSARIO)
REM ============================================================
if "%SKIP_SYNC%"=="1" goto sync_skip

echo [5/6] Verificando blockchain local...

REM Verifica altura do DB atual
set "DB_HEIGHT=-1"
for /f "delims=" %%H in ('%PY% -c "from db import ChainDB; d=ChainDB('brn_v2_chain.db'); print(d.height()); d.close()" 2^>nul') do set "DB_HEIGHT=%%H"

echo        Altura local: !DB_HEIGHT!

if not "!DB_HEIGHT!"=="-1" (
    echo [ok] Blockchain ja sincronizada ^(altura !DB_HEIGHT!^) - pulando sync
    goto sync_done
)

echo        DB vazio - precisa sincronizar do Ubuntu
echo.

if not exist "sync_direct.py" (
    echo [ERRO] sync_direct.py nao encontrado.
    echo        Crie manualmente com o codigo fornecido, OU
    echo        copie do PC anterior ^(laban^).
    echo.
    echo        Depois rode:
    echo          python sync_direct.py http://100.111.253.8:5000
    echo.
    echo        Ou execute este .bat com --skip-sync para tentar
    echo        sync via P2P ^(mais lento, menos confiavel^).
    pause
    exit /b 4
)

echo        Rodando sync_direct.py do Ubuntu ^(100.111.253.8^)...
echo.
%PY% sync_direct.py http://100.111.253.8:5000
if errorlevel 1 (
    echo.
    echo [X] Sync falhou. Verifique:
    echo     - Ubuntu acessivel? ^(Test-NetConnection 100.111.253.8 -Port 5000^)
    echo     - Tailscale UP? ^(tailscale status^)
    pause
    exit /b 5
)

echo.
echo [ok] Sync concluido
:sync_done
echo.

:sync_skip

REM ============================================================
REM 6. RODAR main.py
REM ============================================================
echo [6/6] Iniciando BRN Node...
echo.

set "HEADLESS_FLAG=--headless"
if "%FORCE_HEADLESS%"=="0" set "HEADLESS_FLAG="

echo ============================================================
echo   INICIANDO: %PY% main.py %HEADLESS_FLAG% --no-tailscale --skip-verify
echo ============================================================
echo.

%PY% main.py %HEADLESS_FLAG% --no-tailscale --skip-verify
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [X] main.py saiu com codigo %RC%
) else (
    echo [ok] main.py encerrado normalmente
)
pause
exit /b %RC%
