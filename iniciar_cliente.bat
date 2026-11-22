@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

title BRN Wallet Node v13.9.28

set "BOOT_START=%TIME%"

echo ============================================================
echo   INICIANDO BRN NODE - CLIENTE (v3 - GUI + LOGS)
echo ============================================================
echo.

REM ═══════════════════════════════════════════════════════════
REM  [0/9] VARIÁVEIS DE AMBIENTE
REM ═══════════════════════════════════════════════════════════
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

REM ─── [v3] LOG VERBOSO ──────────────────────────────────────
REM  DEBUG = mostra [auto_wire], [collector], [SIG], [p2p]
REM  Se quiser menos ruído, troque para INFO
set "BRN_LOG_LEVEL=DEBUG"
REM ───────────────────────────────────────────────────────────

REM ─── [v3] AUTO-WIRE FORÇADO ────────────────────────────────
set "BRN_AUTO_WIRE=1"
REM ───────────────────────────────────────────────────────────

REM ─── [v3] FAST-SYNC NÃO FORÇADO ────────────────────────────
REM  Remove BRN_ASSUME_VALID_UNTIL e BRN_BOOTSTRAP_SKIP_SIGS_UNTIL
REM  para o main.py decidir baseado na altura atual (já em 4084)
REM  Se você quer manter, descomente as duas linhas abaixo:
REM set "BRN_ASSUME_VALID_UNTIL=4001"
REM set "BRN_BOOTSTRAP_SKIP_SIGS_UNTIL=4084"
REM ───────────────────────────────────────────────────────────

REM ─── [v3] MULTI-SIG ────────────────────────────────────────
REM  Mantém bypass de bootstrap (rede pequena, sem SK em todos os nós)
set "BRN_STRICT_MULTISIG=0"
REM ───────────────────────────────────────────────────────────

REM ─── PBFT ──────────────────────────────────────────────────
REM  main.py v13.9.28 NÃO chama chain.set_pbft()
REM  então o PBFT fica desligado mesmo com BRN_PBFT_ENABLED=1.
REM  Se você adicionar suporte no main.py, pode remover esta linha.
set "BRN_PBFT_ENABLED=0"
set "BRN_PBFT_ENFORCE=0"
set "BRN_PBFT_TIMEOUT=15"
set "BRN_PBFT_MAX_STATES=100"
REM ───────────────────────────────────────────────────────────

echo OK.
echo.

REM ═══════════════════════════════════════════════════════════
REM  [1/9] PYTHON
REM ═══════════════════════════════════════════════════════════
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

REM ═══════════════════════════════════════════════════════════
REM  [2/9] DEPENDÊNCIAS
REM ═══════════════════════════════════════════════════════════
echo [2/9] Verificando dependencias (isso pode demorar)...
python -m pip install --upgrade pip --quiet

REM Fix anyio 4.15 (quebra GUI em alguns ambientes)
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
        echo [ok] %%P ja instalado.
    )
)

python -c "import uvicorn" >nul 2>&1
if errorlevel 1 (
    echo Instalando uvicorn[standard]...
    python -m pip install --quiet "uvicorn[standard]"
)
echo OK.
echo.

REM ═══════════════════════════════════════════════════════════
REM  [3/9] TAILSCALE
REM ═══════════════════════════════════════════════════════════
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
        echo [aviso] Tailscale nao encontrado - ignorando
    )
) else (
    "%TS_PATH%" status | findstr /C:"100."
)
:tailscale_ok
echo OK.
echo.

REM ═══════════════════════════════════════════════════════════
REM  [4/9] PRE-FLIGHT: ARQUIVOS E AMBIENTE
REM ═══════════════════════════════════════════════════════════
echo [4/9] PRE-FLIGHT: arquivos e ambiente...
echo.

echo   --- Arquivos criticos ---
for %%F in (blockchain.py main.py server.py db.py wallet.py crypto.py auto_wire.py) do (
    if exist "%%F" (
        echo   [ok] %%F
    ) else (
        echo   [X]  %%F AUSENTE ^(critico!^)
    )
)
if exist "trust_network.json" (
    echo   [ok] trust_network.json
) else (
    echo   [!]  trust_network.json AUSENTE ^(trust_net=stub^)
)
if exist "auto_wire.py" (
    echo   [ok] auto_wire.py presente
) else (
    echo   [X]  auto_wire.py AUSENTE ^(sem auto-wire!^)
)
if exist ".validator_sk" (
    echo   [ok] .validator_sk presente
) else (
    echo   [!]  .validator_sk AUSENTE ^(SK virá do current_wallet.enc^)
)
if exist "current_wallet.enc" (
    echo   [ok] current_wallet.enc presente
) else (
    echo   [!]  current_wallet.enc AUSENTE ^(será criado abaixo^)
)
if exist "wallets\current_wallet.enc" (
    echo   [ok] wallets\current_wallet.enc (backup)
)
if exist "brn_miner.dll" (
    echo   [ok] brn_miner.dll ^(PoW C acelerado^)
) else (
    echo   [!]  brn_miner.dll AUSENTE ^(PoW Python lento^)
)
if exist "pbft.py" (
    echo   [!]  pbft.py presente, mas main.py v13.9.28 NAO integra
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
python -c "from db import ChainDB; d=ChainDB('brn_v2_chain.db'); print('  Altura:', d.height()); print('  Tip:', d.tip_hash()[:32] + '...' if d.height() >= 0 else '  (vazio)'); d.close()" 2>nul
echo.

REM ─── Criar carteira se não existir ────────────────────────
if not exist "current_wallet.enc" (
    echo   --- Criando carteira nova (current_wallet.enc) ---
    python -c "import os; pw = os.environ.get('BRN_WALLET_SESSION_PASS', 'carteira-windows-2026'); from wallet import Wallet; from secure_store import save_current_wallet; w = Wallet(); r = save_current_wallet(pw, {'address': w.address, 'private_key': w.priv_hex, 'public_key': w.pub_hex}); print('  Carteira criada:', w.address) if r.get('ok') else print('  FALHA:', r.get('msg'))"
) else (
    echo   --- current_wallet.enc ja existe - nao vou mexer ---
)
echo.

REM ─── Testar peers de bootstrap ────────────────────────────
echo   --- Conectividade dos peers ---
for %%P in (100.70.107.15:6001 100.111.253.8:6001) do (
    for /f "tokens=1,2 delims=:" %%A in ("%%P") do (
        powershell -NoProfile -Command "if (Test-NetConnection %%A -Port %%B -InformationLevel Quiet -WarningAction SilentlyContinue) { Write-Host '  [ok] %%A:%%B alcancavel' } else { Write-Host '  [X]  %%A:%%B inalcancavel' }" 2>nul
    )
)
echo.

REM ─── Testar origin (:5000 do 100.111.253.8) ───────────────
echo   --- Origin / pool URL ---
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://100.111.253.8:5000/health' -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop; Write-Host '  [ok] origin online - height='$r.Content.Substring(0,[Math]::Min(60,$r.Content.Length)) } catch { Write-Host '  [!]  origin OFFLINE - o pool client vai aguardar' }" 2>nul
echo.
echo OK (pre-flight).
echo.

REM ═══════════════════════════════════════════════════════════
REM  [5/9] PORTA 5000
REM ═══════════════════════════════════════════════════════════
echo [5/9] Verificando porta 5000...
powershell -NoProfile -Command "$l = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue; if ($l) { Write-Host '  [X] porta 5000 JA EM USO - PID:'$l[0].OwningProcess; exit 1 } else { Write-Host '  [ok] porta 5000 livre'; exit 0 }"
if errorlevel 1 (
    echo.
    echo ERRO: porta 5000 ocupada. Feche o processo anterior ^(outra janela do main.py^) e tente novamente.
    pause
    exit /b 1
)
echo OK.
echo.

REM ═══════════════════════════════════════════════════════════
REM  [6/9] DIRETÓRIO DE LOGS
REM ═══════════════════════════════════════════════════════════
echo [6/9] Preparando diretorio de logs...
if not exist "logs" mkdir "logs"
set "LOG_FILE=logs\brn_node_%DATE:~-4%%DATE:~3,2%%DATE:~0,2%_%TIME:~0,2%%TIME:~3,2%.log"
set "LOG_FILE=%LOG_FILE: =0%"
echo Arquivo de log: !LOG_FILE!
echo OK.
echo.

REM ═══════════════════════════════════════════════════════════
REM  [7/9] GUI OPENER (background, aguarda :5000 responder)
REM ═══════════════════════════════════════════════════════════
echo [7/9] Preparando GUI opener (aguarda :5000 nao-bloqueante)...
start "BRN GUI Opener" /B powershell -NoProfile -WindowStyle Hidden -Command ^
  "$deadline = (Get-Date).AddSeconds(90);" ^
  "while ((Get-Date) -lt $deadline) {" ^
  "  try {" ^
  "    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:5000/health' -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop;" ^
  "    if ($r.StatusCode -eq 200) { break }" ^
  "  } catch {};" ^
  "  Start-Sleep -Seconds 2" ^
  "};" ^
  "Write-Host '[GUI] :5000 respondeu, abrindo carteira...';" ^
  "try {" ^
  "  python -c \"import webview; webview.create_window('BRN Wallet v3.23', 'http://127.0.0.1:5000/wallet', width=1200, height=900); webview.start()\";" ^
  "} catch {" ^
  "  Write-Host '[GUI] pywebview falhou, abrindo browser';" ^
  "  Start-Process 'http://127.0.0.1:5000/wallet'" ^
  "}"
echo [ok] GUI opener disparado em background
echo.

REM ═══════════════════════════════════════════════════════════
REM  [8/9] RESUMO DO QUE PROCURAR NO LOG
REM ═══════════════════════════════════════════════════════════
echo [8/9] O que procurar no log abaixo:
echo.
echo   ============================================================
echo   PADROES ESPERADOS (se tudo funcionar)
echo   ============================================================
echo   [wiring:post-blockchain]  trust=TrustNetwork sk=None  collector=None
echo   [wiring:post-trust]       trust=TrustNetwork sk=None  collector=None
echo   [wiring:post-p2p]         trust=TrustNetwork sk=None  p2p=P2PManager collector=OK
echo   [p2p] set_p2p OK - collector=OK
echo   [GUI] carteira desbloqueada via env:BRN_WALLET_SESSION_PASS: brn1...
echo   [wiring:post-gui-unlock]  trust=TrustNetwork sk=OK p2p=P2PManager collector=OK
echo   [watchdog] SK detectada pk=...
echo   [wiring:post-start]       trust=TrustNetwork sk=OK p2p=P2PManager collector=OK
echo   ============================================================
echo.
echo   PADROES DE PROBLEMA (se aparecer, algo esta errado)
   ============================================================
echo   [watchdog] _sign_collector=None          ^<-- blockchain.py NAO e v9.6.0
echo   [SIG] EMERGENCIA: bloco #... aceito SEM multi-sig  ^<-- SK nao instalada
echo   [SYNC] Origin offline                    ^<-- origin (100.111.253.8) down
echo   [ISOLATION] Altura travada em #...       ^<-- chain parada
echo   ============================================================
echo.
echo OK.
echo.

REM ═══════════════════════════════════════════════════════════
REM  [9/9] INICIAR NÓ COM DUAL LOGGING (console + arquivo)
REM ═══════════════════════════════════════════════════════════
echo [9/9] Iniciando no (Ctrl+C para parar)...
echo ============================================================
echo   MODO: GUI ^(abrira automaticamente^)
echo   LOG : !LOG_FILE!
echo   API : http://127.0.0.1:5000
echo   WALLET: http://127.0.0.1:5000/wallet
echo   EXPLORER: http://127.0.0.1:8080
echo ============================================================
echo.

REM Passa --skip-verify se suportado
set "MAIN_ARGS="
python main.py --help 2>&1 | findstr /C:"--skip-verify" >nul
if not errorlevel 1 set "MAIN_ARGS=--skip-verify"

echo [bat] python main.py !MAIN_ARGS!
echo.

REM ─── ESTA LINHA BLOQUEIA ATÉ O NÓ MORRER ──────────────────
powershell -NoProfile -Command "$ErrorActionPreference='Continue'; python main.py !MAIN_ARGS! 2>&1 | Tee-Object -FilePath '!LOG_FILE!'"

echo.
echo ============================================================
echo NO ENCERRADO em %TIME% (iniciado em %BOOT_START%)
echo ============================================================
echo.
echo Log salvo em: !LOG_FILE!
echo.
echo Para me enviar o log, cole o conteudo do arquivo:
echo   type !LOG_FILE!
echo.
echo Ou para abrir a pasta:
echo   explorer logs
echo.

pause
