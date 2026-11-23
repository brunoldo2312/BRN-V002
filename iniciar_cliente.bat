@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title BRN Wallet Node v5 (FIX Tee)

echo ============================================================
echo   INICIANDO BRN NODE - CLIENTE (v5)
echo ============================================================
echo.

REM ============================================================
REM  [0/9] VARIAVEIS DE AMBIENTE
REM ============================================================
echo [0/9] Carregando configuracoes de rede e senhas...

set "BRN_NETWORK_SECRET=088641fd110a36bcb3a1bd3d497d8cd68287bf10e49eedfc52d1fc0329ef8d12"
set "BRN_MAX_VALIDATORS=3"
set "BRN_VALIDATOR_QUORUM=0.51"
set "BRN_REQUIRE_ALL_SIGS=0"
set "BRN_MIN_DIFFICULTY=3"
set "BRN_MAX_DIFFICULTY=6"
set "BRN_DIFF_INTERVAL=6"
set "BRN_P2P_PORT=6001"
set "BRN_SEU_ENDERECO=100.84.11.48:6001"
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

REM --- Bootstrap: Ubuntu (o 100.64.151.67 esta morto) ---
set "BRN_BOOTSTRAP_PEERS=100.111.253.8:6001"
set "BRN_POOL_URL=http://100.111.253.8:5000"

REM --- Este no e CLIENTE (nao Origin) ---
set "BRN_IS_ORIGIN=0"
set "BRN_POOL_ENABLED=0"

REM --- Senhas (para decifrar current_wallet.enc) ---
set "BRN_NODE_PASSWORD=senha-forte-windows-2026"
set "BRN_WALLET_SESSION_PASS=carteira-windows-2026"
set "BRN_WEB_PASS=web-windows-2026"

REM --- Logging ---
set "BRN_LOG_LEVEL=DEBUG"

REM --- Auto-wire ---
set "BRN_AUTO_WIRE=1"

REM --- Multi-sig ---
set "BRN_STRICT_MULTISIG=0"

REM --- Fast-sync ---
set "BRN_ASSUME_VALID_UNTIL=0"
set "BRN_BOOTSTRAP_SKIP_SIGS_UNTIL=4085"

REM --- [v5] CONSENSO ESTRITO A PARTIR DO BLOCO 6000 ---
set "BRN_CONSENSUS_STRICT_FROM=6000"
set "BRN_MIN_PEERS_FOR_MINING=3"
set "BRN_PEER_SYNC_TOLERANCE=2"

REM --- Python unbuffered ---
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

REM --- PBFT off ---
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
echo [4/9] PRE-FLIGHT: arquivos, carteira, SK...
echo.

echo   --- Arquivos criticos ---
for %%F in (blockchain.py main.py server.py db.py wallet.py crypto.py auto_wire.py secure_store.py) do (
    if exist "%%F" (
        echo   [ok] %%F
    ) else (
        echo   [X]  %%F AUSENTE - critico
    )
)
if exist "brn_miner.dll" (echo   [ok] brn_miner.dll) else (echo   [WARN] brn_miner.dll ausente)
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
    echo   --- current_wallet.enc ja existe ---
)

REM --- Cria .validator_sk se nao existir ---
if not exist ".validator_sk" (
    echo.
    echo   --- Gerando .validator_sk ---
    python -c "from wallet import Wallet; w = Wallet(); open('.validator_sk','w').write(w.priv_hex); print('  Endereco:', w.address); print('  Pubkey  :', w.pub_hex[:16] + '...')"
    if errorlevel 1 (
        echo   [X] Falha ao gerar .validator_sk
        pause
        exit /b 1
    )
) else (
    echo   --- .validator_sk ja existe ---
    python -c "from pathlib import Path; c = Path('.validator_sk').read_text().strip().split(chr(10))[0]; print('  .validator_sk OK' if len(c) == 64 else '  [X] .validator_sk INVALIDO len=' + str(len(c)))"
)
echo.

REM --- Verifica deteccao pelo auto_wire ---
echo   --- Verificando deteccao de SK pelo auto_wire ---
python -c "import os; os.environ['BRN_AUTO_WIRE']='1'; from auto_wire import _detect_sk; w, src = _detect_sk(); print('  auto_wire detecta:', src or 'NENHUMA SK') if w else print('  [X] NENHUMA SK ENCONTRADA')"
if errorlevel 1 (
    echo   [X] Falha ao importar auto_wire
)
echo.

REM --- Forca rede descentralizada ---
echo   --- Verificando fila de mineradores ---
python -c "from db import ChainDB; d=ChainDB('brn_v2_chain.db'); m=d.get_meta('rede_descentralizada'); d.close(); print('  rede_descentralizada =', m or 'nao setado')"
python -c "from db import ChainDB; d=ChainDB('brn_v2_chain.db'); d.set_meta('rede_descentralizada','1'); d.close(); print('  [ok] rede_descentralizada=1')"
echo.

REM --- Conectividade dos peers ---
echo   --- Conectividade dos peers ---
for %%P in (100.111.253.8:6001) do (
    for /f "tokens=1,2 delims=:" %%A in ("%%P") do (
        powershell -NoProfile -Command "$s = New-Object System.Net.Sockets.TcpClient; try { $r = $s.BeginConnect('%%A', %%B, $null, $null); if ($r.AsyncWaitHandle.WaitOne(1500)) { Write-Host '  [ok] %%A:%%B' } else { Write-Host '  [X]  %%A:%%B timeout' } } catch { Write-Host '  [X]  %%A:%%B erro' } finally { $s.Close() }" 2>nul
    )
)
echo.
echo OK (pre-flight).
echo.

REM ============================================================
REM  [5/9] PORTA 5000
REM ============================================================
echo [5/9] Verificando porta 5000...
powershell -NoProfile -Command "$l = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue; if ($l) { Write-Host '  [X] porta 5000 JA EM USO - PID:'$l[0].OwningProcess; exit 1 } else { Write-Host '  [ok] porta 5000 livre'; exit 0 }"
if errorlevel 1 (
    echo.
    echo ERRO: porta 5000 ocupada. Feche o main.py anterior.
    pause
    exit /b 1
)
echo OK.
echo.

REM ============================================================
REM  [6/9] LOG + TIMESTAMP
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
REM  [7/9] GUI OPENER (aguarda :5000 responder)
REM ============================================================
echo [7/9] GUI opener disparado (aguarda :5000)...
start "BRN-GUI-Opener" /MIN powershell -NoProfile -WindowStyle Hidden -Command "$deadline=(Get-Date).AddSeconds(120); while((Get-Date) -lt $deadline){try{Invoke-WebRequest 'http://127.0.0.1:5000/health' -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop | Out-Null; Start-Process 'http://127.0.0.1:5000/wallet'; Write-Host '[GUI] wallet aberta'; exit}catch{Start-Sleep -Seconds 2}}; Write-Host '[GUI] timeout - abra manualmente http://127.0.0.1:5000/wallet'"
echo [ok] GUI opener iniciado
echo.

REM ============================================================
REM  [8/9] LOG TAIL (segundo terminal)
REM ============================================================
echo [8/9] Abrindo terminal de log em tempo real...
start "BRN-Log-Tail" cmd /k "echo Log: %LOG_FILE% & echo Aguardando arquivo... & :wait & if not exist \"%LOG_FILE%\" (timeout /t 1 >nul & goto wait) & powershell -NoProfile -Command \"Get-Content '%LOG_FILE%' -Wait -Tail 50\""
echo [ok] terminal de log aberto (outra janela)
echo.

REM ============================================================
REM  [9/9] INICIA NO COM REDIRECIONAMENTO CMD (sem PowerShell Tee)
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
REM  [FIX CRITICO] Redireciona via cmd /c, NAO via PowerShell
REM  PowerShell Tee trata stderr como erro fatal e mata o main.py
REM ============================================================
cmd /c "python main.py %MAIN_ARGS% > \"%LOG_FILE%\" 2>&1"

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
pause
