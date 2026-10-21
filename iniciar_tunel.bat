@echo off
title Cloudflare Tunnel - BRN
cd /d "%~dp0"
echo Criando link publico para o painel...
cloudflared.exe tunnel --url http://localhost:8080
pause