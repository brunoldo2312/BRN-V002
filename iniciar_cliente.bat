@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title BRN Wallet Node v13.9.28

REM ============================================================
REM  [0/9] VARIAVEIS DE AMBIENTE
REM ============================================================
echo ============================================================
echo   INICIANDO BRN NODE - CLIENTE (v3 - GUI + LOGS)
echo ============================================================
echo.
echo [0/9] Carregando configuracoes de rede e senhas...
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
set "BRN_EXPLORER_PORT=8080"
set "BRN_CORS_ORIGIN=*"
set "BRN_CHECKPOINT_INTERVAL=100"
set "BRN_ORIGIN_PUBKEY=03c0f4604d5264b5eda409eac8212267bd76ff46973bb14e01a16c290f437e0e6e"
set "BRN_TCP_TIMEOUT=30.0"

REM --- LOG VERBOSO (DEBUG mostra [wiring], [auto_wire], [collector]) ---
set "BRN_LOG_LEVEL=DEBUG"

REM --- AUTO-WIRE FORCADO ---
set "BRN_AUTO_WIRE=1"

REM --- MULTI-SIG ---
set "BRN_STRICT_MULTISIG=0"

REM --- FAST-SYNC (bootstrap) ---
REM  Blocos 1..4084 nao tem assinatura. Manter 4084 permite
REM  re-sincronizar caso o DB seja apagado. Para desligar,
REM  troque para 0.
set "BRN_ASSUME_VALID_UNTIL=0"
set "BRN_BOOTSTRAP_SKIP_SIGS_UNTIL=4084"
set "BRN_CHECKPOINT_AUTO_HEIGHT=4001"

REM --- PYTHON UNBUFFERED (logs em tempo real) ---
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

REM --- PBFT (main.py v13.9.28 nao integra set_pbft) ---
set "BRN_PBFT_ENABLED=0"
set "BRN_PBFT_ENFORCE=0"
echo OK.
echo.

REM ============================================================
REM  [1/9] PYTHON
REM ============================================================
echo [1/9] Verificando Python...
where python >nul 2>&1
if errorlevel 1 (
    echo ERRO: Python nao encontrado no PATH.
    pause
    exit /b 1
)
python --version
echo OK.
echo.

REM ============================================================
REM  [2/9] DEPENDENCIAS
REM ============================================================
echo [2/9] Verificando dependencias (pode demorar)...
python -m pip install --upgrade pip --quiet

REM Fix anyio 4.15
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
        echo [ok] %%P
    )
)

python -c "import uvicorn" >nul 2>&1
if errorlevel 1 (
    echo Instalando uvicorn[standard]...
    python -m pip install --quiet "uvicorn[standard]"
)
echo OK.
echo.

REM ============================================================
REM  [3/9] TAILSCALE
REM ============================================================
echo [3/9] Verificando Tailscale...
if "%BRN_SKIP_TAILSCALE%"=="1" (
    echo [skip] BRN_SKIP_TAILSCALE=1
    goto :tailscale_ok
)
set "TS_PATH=C:\Program Files\Tailscale\tailscale.exe"
if not exist "%TS_PATH%" set "TS_PATH=tailscale"
"%TS_PATH%" status >nul 2>&1
if errorlevel 1 (
    if exist "C:\Program Files\Tailscale\tailscale.exe" (
        start "" "C:\Program Files\Tailscale\tailscale.exe"
        timeout /t 3 >nul
        "%TS_PATH%" up
    ) else (
        echo [WARN] Tailscale nao encontrado - ignorando
    )
)
:tailscale_ok
echo OK.
echo.

REM ============================================================
REM  [4/9] PRE-FLIGHT
REM ============================================================
echo [4/9] PRE-FLIGHT: arquivos e ambiente...
echo.
echo   --- Arquivos criticos ---
for %%F in (blockchain.py main.py server.py db.py wallet.py crypto.py auto_wire.py) do (
    if exist "%%F" (
        echo   [ok] %%F
    ) else (
        echo   [X]  %%F AUSENTE - critico
    )
)
if exist "trust_network.json" (
    echo   [ok] trust_network.json
) else (
    echo   [WARN] trust_network.json AUSENTE - trust_net sera stub
)
if exist ".validator_sk" (
    echo   [ok] .validator_sk
) else (
    echo   [WARN] .validator_sk AUSENTE - SK vira do current_wallet.enc
)
if exist "current_wallet.enc" (
    echo   [ok] current_wallet.enc
) else (
    echo   [WARN] current_wallet.enc AUSENTE - sera criado abaixo
)
if exist "brn_miner.dll" (
    echo   [ok] brn_miner.dll - PoW C acelerado
) else (
    echo   [WARN] brn_miner.dll AUSENTE - PoW Python lento
)
echo.

echo   --- Versao do blockchain.py ---
python -c "import blockchain; print('  blockchain.py v' + getattr(blockchain, '__version__', '?'))" 2>nul
if errorlevel 1 (
    echo   [X] Nao consegui importar blockchain.py
    pause
    exit /b 1
)
echo.

echo   --- Altura do DB ---
python -c "from db import ChainDB; d=ChainDB('brn_v2_chain.db'); h=d.height(); print('  Altura:', h); print('  Tip:', d.tip_hash()[:32] + '...') if h >= 0 else print('  (vazio)'); d.close()" 2>nul
echo.

REM --- Cria carteira se nao existir ---
if not exist "current_wallet.enc" (
    echo   --- Criando carteira nova ---
    python -c "import os; pw = os.environ.get('BRN_WALLET_SESSION_PASS', 'carteira-windows-2026'); from wallet import Wallet; from secure_store import save_current_wallet; w = Wallet(); r = save_current_wallet(pw, {'address': w.address, 'private_key': w.priv_hex, 'public_key': w.pub_hex}); print('  Carteira criada:', w.address) if r.get('ok') else print('  FALHA:', r.get('msg'))"
) else (
    echo   --- current_wallet.enc ja existe - nao vou mexer ---
)
echo.

REM --- Conectividade dos peers ---
echo   --- Conectividade dos peers ---
for %%P in (100.70.107.15:6001 100.111.253.8:6001) do (
    for /f "tokens=1,2 delims=:" %%A in ("%%P") do (
        powershell -NoProfile -Command "$s = New-Object System.Net.Sockets.TcpClient; try { $r = $s.BeginConnect('%%A', %%B, $null, $null); if ($r.AsyncWaitHandle.WaitOne(1500)) { Write-Host '  [ok] %%A:%%B alcancavel' } else { Write-Host '  [X]  %%A:%%B inalcancavel' } } catch { Write-Host '  [X]  %%A:%%B erro' } finally { $s.Close() }" 2>nul
    )
)
echo.

REM --- Origin / pool URL ---
echo   --- Origin / pool URL ---
powershell -NoProfile -Command "try { Invoke-WebRequest 'http://100.111.253.8:5000/health' -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop | Out-Null; Write-Host '  [ok] origin online' } catch { Write-Host '  [WARN] origin OFFLINE - pool client vai aguardar' }" 2>nul
echo.
echo OK.
echo.

REM ============================================================
REM  [5/9] PORTA 5000
REM ============================================================
echo [5/9] Verificando porta 5000...
powershell -NoProfile -Command "$l = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue; if ($l) { Write-Host '  [X] porta 5000 JA EM USO - PID:'$l[0].OwningProcess; exit 1 } else { Write-Host '  [ok] porta 5000 livre'; exit 0 }"
if errorlevel 1 (
    echo.
    echo ERRO: porta 5000 ocupada. Feche o main.py anterior e tente de novo.
    pause
    exit /b 1
)
echo OK.
echo.

REM ============================================================
REM  [6/9] DIRETORIO DE LOGS + TIMESTAMP
REM ============================================================
echo [6/9] Preparando diretorio de logs...
if not exist "logs" mkdir "logs"

set "H=%TIME:~0,2%"
if "%H:~0,1%"==" " set "H=0%H:~1,1%"
set "M=%TIME:~3,2%"
set "S=%TIME:~6,2%"
set "TS=%DATE:~-4%%DATE:~3,2%%DATE:~0,2%_%H%%M%%S%"
set "LOG_FILE=%CD%\logs\brn_node_%TS%.log"
echo Arquivo de log: %LOG_FILE%
echo OK.
echo.

REM ============================================================
REM  [7/9] GUI OPENER (background)
REM ============================================================
echo [7/9] GUI opener disparado em background (aguarda :5000)...
start "BRN-GUI-Opener" /MIN powershell -NoProfile -WindowStyle Hidden -Command "$deadline=(Get-Date).AddSeconds(120); while((Get-Date) -lt $deadline){try{Invoke-WebRequest 'http://127.0.0.1:5000/health' -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop | Out-Null; Start-Process 'http://127.0.0.1:5000/wallet'; Write-Host '[GUI] wallet aberta'; exit}catch{Start-Sleep -Seconds 2}}; Write-Host '[GUI] timeout - abra manualmente http://127.0.0.1:5000/wallet'"
echo [ok] GUI opener iniciado
echo.

REM ============================================================
REM  [8/9] O QUE PROCURAR NO LOG
REM ============================================================
echo [8/9] Padroes esperados no log:
echo.
echo   ============================================================
echo   SE MULTI-SIG + AUTO-WIRE ESTAO OK:
echo   ============================================================
echo   [wiring:post-blockchain] trust=TrustNetwork sk=None  collector=None
echo   [wiring:post-trust]      trust=TrustNetwork sk=None  collector=None
echo   [p2p] set_p2p OK - collector=OK
echo   [wiring:post-p2p]        trust=TrustNetwork sk=None  p2p=P2PManager collector=OK
echo   [GUI] carteira desbloqueada via env:BRN_WALLET_SESSION_PASS: brn1...
echo   [wiring:post-gui-unlock] trust=TrustNetwork sk=OK   p2p=P2PManager collector=OK
echo   [watchdog] SK detectada pk=...
echo   [wiring:post-start]      trust=TrustNetwork sk=OK   p2p=P2PManager collector=OK
echo   ============================================================
echo.
echo   ============================================================
echo   SE ALGO ESTA ERRADO:
echo   ============================================================
echo   [wiring:post-p2p] ... collector=None    - blockchain.py NAO e v9.6.0
echo   [watchdog] _sign_collector=None         - mesmo problema acima
echo   [SIG] EMERGENCIA: bloco #... SEM multi-sig - SK nao instalada
echo   [SYNC] Origin offline                   - origin 100.111.253.8 down
echo   [ISOLATION] Altura travada              - chain parada
echo   ============================================================
echo.

REM ============================================================
REM  [9/9] INICIA NO COM TEE (CONSOLE + ARQUIVO)
REM ============================================================
echo [9/9] Iniciando no (Ctrl+C para parar)...
echo ============================================================
echo   API    : http://127.0.0.1:5000
echo   WALLET : http://127.0.0.1:5000/wallet
echo   LOG    : %LOG_FILE%
echo ============================================================
echo.

set "MAIN_ARGS="
python main.py --help 2>&1 | findstr /C:"--skip-verify" >nul
if not errorlevel 1 set "MAIN_ARGS=--skip-verify"

echo [bat] python main.py %MAIN_ARGS%
echo.

REM ============================================================
REM  ESTA LINHA BLOQUEIA ATE O NO MORRER
REM  Usa %LOG_FILE% (expansao normal do cmd) NAO !LOG_FILE!
REM ============================================================
powershell -NoProfile -Command "python main.py %MAIN_ARGS% 2>&1 | Tee-Object -FilePath '%LOG_FILE%'"

echo.
echo ============================================================
echo NO ENCERRADO em %TIME%
echo ============================================================
echo.
echo Log salvo em: %LOG_FILE%
echo.
echo Para ver o log completo:
echo   type "%LOG_FILE%"
echo.
echo Para abrir a pasta:
echo   explorer logs
echo.

pause
