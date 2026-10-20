@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM Carrega .env
if exist "brn_network.env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("brn_network.env") do (
        set "K=%%A"
        set "V=%%B"
        if not "!K!"=="" set "!K!=!V!"
    )
)

REM Forca cliente (nao origin)
set "BRN_IS_ORIGIN=0"
> brn_role.txt echo client

echo ============================================================
echo   BRN Node - CLIENTE
echo ============================================================
echo.

python --version
echo.

REM Mostra bootstrap e modo
echo Bootstrap: %BRN_BOOTSTRAP_PEERS%
echo Modo     : CLIENTE
echo.

echo [*] Iniciando node...
echo.

python main.py --client-mode --headless --no-pool-client

echo.
echo [*] Node encerrado. Pressione qualquer tecla...
pause >nul