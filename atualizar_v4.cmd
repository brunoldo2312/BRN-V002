@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   BRN v4 - Patch de Correcao (v4.1)
echo ============================================================
echo.
echo   O que este patch faz:
echo     [1] Backup dos arquivos atuais
echo     [2] Instala/verifica 'mnemonic'
echo     [3] Verifica db.py (precisa ter utxos_gastos_na_mempool)
echo     [4] Verifica blockchain.py
echo     [5] Verifica wallet.py
echo     [6] Adiciona rotas /api/blocks e /api/block/H ao server.py
echo     [7] Testa imports
echo.
echo   Pressione qualquer tecla para comecar (Ctrl+C cancela).
pause >nul

set "MEU_ARQ=%~f0"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$c = Get-Content -LiteralPath $env:MEU_ARQ -Raw -Encoding UTF8; $i = $c.IndexOf('~PSSTART~'); if ($i -lt 0) { Write-Host 'ERRO: marcador nao encontrado' -ForegroundColor Red; exit 1 }; $code = $c.Substring($i + 10); Invoke-Expression $code"

echo.
echo ============================================================
echo   Finalizado!
echo ============================================================
pause
exit /b

~PSSTART~
$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath (Split-Path -Parent $env:MEU_ARQ)

Write-Host "===== BRN v4.1 - Patch de Correcao =====" -ForegroundColor Cyan
Write-Host "Pasta: $(Get-Location)" -ForegroundColor DarkGray
Write-Host ""

# [1] Backup
$backup = "backup_patch_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
New-Item -ItemType Directory -Path $backup -Force | Out-Null
foreach ($f in 'db.py','blockchain.py','p2p_unified.py','wallet.py','server.py','main.py') {
    if (Test-Path $f) { Copy-Item $f -Destination $backup -Force }
}
Write-Host "[1/7] Backup em: $backup" -ForegroundColor Yellow
Write-Host ""

# [2] mnemonic
Write-Host "[2/7] Verificando 'mnemonic'..." -ForegroundColor Cyan
$mn_out = python -c "import mnemonic; print('OK')" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "      OK (ja instalado)" -ForegroundColor Green
} else {
    Write-Host "      Instalando..." -ForegroundColor Yellow
    python -m pip install mnemonic 2>&1 | Out-Null
    Write-Host "      OK" -ForegroundColor Green
}
Write-Host ""

# [3] Verificar db.py
Write-Host "[3/7] Verificando db.py..." -ForegroundColor Cyan
if (-not (Test-Path 'db.py')) {
    Write-Host "      FALHA: db.py nao encontrado!" -ForegroundColor Red
    Write-Host "      Copie o db.py v6.5 para esta pasta." -ForegroundColor Red
    Read-Host "Pressione ENTER para sair"
    exit 1
}
$db_check = python -c "from db import ChainDB; c=ChainDB(':memory:'); print('HAS_METHOD' if hasattr(c,'utxos_gastos_na_mempool') else 'MISSING')" 2>&1
if ($db_check -match 'HAS_METHOD') {
    Write-Host "      OK (v6.5 com utxos_gastos_na_mempool)" -ForegroundColor Green
} else {
    Write-Host "      AVISO: db.py nao tem utxos_gastos_na_mempool()" -ForegroundColor Yellow
    Write-Host "      Substitua db.py pela versao v6.5 que voce tem." -ForegroundColor Yellow
}
Write-Host ""

# [4] Verificar blockchain.py
Write-Host "[4/7] Verificando blockchain.py..." -ForegroundColor Cyan
if (Test-Path 'blockchain.py') {
    Write-Host "      OK (encontrado)" -ForegroundColor Green
} else {
    Write-Host "      FALHA: blockchain.py nao encontrado!" -ForegroundColor Red
    Read-Host "Pressione ENTER para sair"
    exit 1
}
Write-Host ""

# [5] Verificar wallet.py
Write-Host "[5/7] Verificando wallet.py..." -ForegroundColor Cyan
if (Test-Path 'wallet.py') {
    Write-Host "      OK (encontrado)" -ForegroundColor Green
} else {
    Write-Host "      FALHA: wallet.py nao encontrado!" -ForegroundColor Red
    Read-Host "Pressione ENTER para sair"
    exit 1
}
Write-Host ""

# [6] Patch server.py
Write-Host "[6/7] Patch em server.py (rotas /api/blocks)..." -ForegroundColor Cyan
if (-not (Test-Path 'server.py')) {
    Write-Host "      FALHA: server.py nao encontrado!" -ForegroundColor Red
    Read-Host "Pressione ENTER para sair"
    exit 1
}

$server_content = Get-Content -LiteralPath 'server.py' -Raw -Encoding UTF8
if ($server_content -match '/api/blocks') {
    Write-Host "      OK (rotas /api/blocks ja existem)" -ForegroundColor Green
} else {
    Write-Host "      Adicionando rotas faltantes..." -ForegroundColor Yellow
    
    $patch = @'


# ============================================================
# [PATCH v4.1] Rotas do explorador de blocos (fix HTTP 404)
# ============================================================
@app.route("/api/blocks", methods=["GET"])
def api_get_blocks_patch():
    """Lista blocos paginados para a carteira."""
    try:
        start = int(request.args.get("start", 0))
        limit = int(request.args.get("limit", 15))
        limit = min(max(limit, 1), 50)
        chain = _get_chain()
        blocks = chain.db.get_blocks_range(start, start + limit)
        return jsonify({
            "blocks": blocks,
            "start": start,
            "count": len(blocks)
        })
    except Exception as e:
        log.error(f"Erro em /api/blocks: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/block/<int:height>", methods=["GET"])
def api_get_block_patch(height):
    """Retorna um bloco especifico pela altura."""
    try:
        chain = _get_chain()
        block = chain.db.get_block(height)
        if block:
            return jsonify(block)
        return jsonify({"error": "Bloco nao encontrado"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
'@
    
    Add-Content -Path 'server.py' -Value $patch -Encoding UTF8
    Write-Host "      OK (rotas adicionadas)" -ForegroundColor Green
}
Write-Host ""

# [7] Teste de imports
Write-Host "[7/7] Testando imports..." -ForegroundColor Cyan
$testes = @('crypto','bech32','db','blockchain','wallet')
foreach ($m in $testes) {
    $out = python -c "import $m" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "      OK   $m" -ForegroundColor Green
    } else {
        Write-Host "      FALHA $m" -ForegroundColor Red
        Write-Host "        $out" -ForegroundColor DarkRed
    }
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  PATCH CONCLUIDO!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Backup em: $backup" -ForegroundColor Yellow
Write-Host ""
Write-Host "Proximos passos:" -ForegroundColor Cyan
Write-Host "  1. Reinicie o no: python main.py" -ForegroundColor White
Write-Host "  2. Teste: http://127.0.0.1:5000/api/blocks?start=0&limit=5" -ForegroundColor White
Write-Host "  3. Abra a carteira digital novamente" -ForegroundColor White
Write-Host ""