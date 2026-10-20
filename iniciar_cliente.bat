@echo off
cd /d "%~dp0"

echo ============================================================
echo   BRN Node - CLIENTE (Windows)
echo ============================================================
echo.

set BRN_IS_ORIGIN=0
set BRN_POOL_ENABLED=0
set BRN_AUTO_MINE=0
set PYTHONUNBUFFERED=1

echo client> brn_role.txt

echo [*] Rodando: python main.py --client-mode --no-pool-client
echo.

python main.py --client-mode --no-pool-client --no-tailscale

echo.
echo [*] Processo encerrado com codigo %ERRORLEVEL%
pause