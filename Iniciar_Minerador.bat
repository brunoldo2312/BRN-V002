@echo off
setlocal EnableDelayedExpansion
title BRN Minerador Automatico - Cliente
color 0A

:: ==================================================================
:: CONFIGURACOES DA REDE (Dono do Origem deve alterar se necessario)
:: ==================================================================
:: IP Tailscale do Ubuntu (Origem)
set ORIGIN_IP=100.84.11.48

:: O Segredo da sua rede. Se voce mudou no Ubuntu, mude aqui tambem!
set NETWORK_SECRET=9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9

:: Senhas locais do amigo (pode deixar assim)
set NODE_PASS=senha-no-temp-123
set WALLET_PASS=senha-forte-da-carteira-2026
set WEB_PASS=senha-temp-123

:: ==================================================================

echo ========================================================
echo   BRN MINERADOR AUTOMATICO - SETUP E EXECUCAO
echo ========================================================
echo.

:: 1. Verifica se o Python esta instalado
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] Python nao encontrado!
    echo Baixe e instale o Python em: https://www.python.org/downloads/
    echo ATENCAO: Marque a caixa "Add Python to PATH" na instalacao.
    pause
    exit /b
)
echo [OK] Python instalado.

:: 2. Verifica se o Tailscale esta ativo
tailscale ip -4 >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] Tailscale nao encontrado ou nao logado!
    echo Instale o Tailscale e faca login na mesma conta do Origem.
    pause
    exit /b
)
echo [OK] Tailscale ativo.

:: 3. Testa a conexao com o Origem
echo Testando conexao com o pool do Origem (%ORIGIN_IP%)...
ping -n 2 %ORIGIN_IP% >nul
if %errorlevel% neq 0 (
    echo [AVISO] Nao foi possivel pingar o Origem (%ORIGIN_IP%).
    echo Verifique se o Ubuntu esta ligado e logado no Tailscale.
    echo.
)

:: 4. Cria o arquivo de configuracao .env automaticamente
echo Criando arquivo brn_network.env...
(
    echo BRN_IS_ORIGIN=0
    echo BRN_POOL_ENABLED=1
    echo BRN_BOOTSTRAP_PEERS=%ORIGIN_IP%:6001
    echo BRN_POOL_URL=http://%ORIGIN_IP%:5000
    echo BRN_NETWORK_SECRET=%NETWORK_SECRET%
    echo BRN_NODE_PASSWORD=%NODE_PASS%
    echo BRN_WALLET_SESSION_PASS=%WALLET_PASS%
    echo BRN_WEB_PASS=%WEB_PASS%
    echo BRN_ADMIN_TOKEN=c745d36d524ea92148d8139731ad663f929fbbe08fb7f9b2234f9109fe1d2b24
    echo BRN_WEB_HOST=127.0.0.1
    echo BRN_WEB_PORT=5000
    echo BRN_EXPLORER_HOST=127.0.0.1
    echo BRN_EXPLORER_PORT=8080
    echo BRN_AUDIT_ENABLED=1
    echo BRN_AUDIT_LOG=audit.log
    echo BRN_LOG_LEVEL=INFO
) > brn_network.env
echo [OK] brn_network.env configurado.

:: 5. Gera a carteira (se nao existir)
if not exist "current_wallet.enc" (
    echo.
    echo Gerando nova carteira segura...
    
    :: Cria um script Python temporario para gerar a carteira lendo o .env
    (
        echo import os, json
        echo from wallet import Wallet
        echo from secure_store import save_current_wallet
        echo pw = os.environ.get^("BRN_WALLET_SESSION_PASS", ""^).strip^(^)
        echo w = Wallet^(^)
        echo data = {"address": w.address, "private_key": w.priv_hex, "public_key": w.pub_hex, "pubkey": w.pub_hex, "pub_hex": w.pub_hex}
        echo save_current_wallet^(pw, data^)
        echo try:
        echo     with open^("user_wallets.json"^) as f: wallets = json.load^(f^)
        echo except: wallets = {}
        echo wallets[w.address] = {"public_key": w.pub_hex}
        echo with open^("user_wallets.json", "w"^) as f: json.dump^(wallets, f, indent=2^)
        echo print^("CARTEIRA CRIADA! Endereco:", w.address^)
        echo print^("PRIVATE KEY:", w.priv_hex^)
    ) > setup_carteira.py
    
    :: Carrega as variaveis do .env para o Python ler a senha
    for /f "tokens=1,2 delims==" %%a in (brn_network.env) do set %%a=%%b
    python setup_carteira.py
    del setup_carteira.py
    echo.
    echo ################################################################
    echo [AVISO] ANOTE A PRIVATE KEY ACIMA EM UM PAPEL!
    echo Ela nao sera mostrada novamente e e a unica forma de recuperar os BRN.
    echo ################################################################
    echo.
    pause
) else (
    echo [OK] Carteira ja existe.
)

:: 6. Inicia o minerador em loop (se cair, reinicia)
:loop
echo.
echo ========================================================
echo   INICIANDO MINERACAO... (Nao feche esta janela)
echo ========================================================
:: --headless (sem GUI) e --no-pool (nao roda servidor, so o minerador)
python main.py --headless --no-pool
echo.
echo [AVISO] O minerador parou (ou caiu). Reiniciando em 10 segundos...
timeout /t 10 >nul
goto loop