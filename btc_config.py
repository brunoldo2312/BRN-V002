"""btc_config.py — Configuração do subsistema BTC (L2)
================================================================
Este arquivo é lido por:
  - l2_manager.py          (rate, fee, min/max BTC)
  - btc_watcher.py         (endereço de recebimento, confirmações)
  - contracts/factory.py   (min/max, rate, fee, endereço)
  - server.py              (BTC_RECEIVE_ADDRESS, BTC_NETWORK)

⚠️ EDITE ANTES DE USAR EM PRODUÇÃO:
    BTC_RECEIVE_ADDRESS  = seu endereço BTC real
    BTC_NETWORK          = "mainnet" ou "testnet"
    BRN_PER_BTC          = cotação fixa (ajuste conforme quiser)
    FEE_PERCENT          = taxa percentual cobrada

Não requer dependências externas.
================================================================
"""
from __future__ import annotations

import os


# ============================================================
# ENDEREÇO DE RECEBIMENTO BTC
# ------------------------------------------------------------
# ⚠️ SUBSTITUA pelo seu endereço real
#   mainnet : começa com bc1q... ou 1... ou 3...
#   testnet : começa com tb1q... ou m... ou 2...
# ============================================================
BTC_RECEIVE_ADDRESS = os.environ.get(
    "BRN_BTC_RECEIVE_ADDRESS",
    "bc1qSEU_ENDERECO_AQUI_TROQUE_ISSO"
)

# ============================================================
# REDE BTC
# ------------------------------------------------------------
# Valores aceitos: "mainnet", "testnet"
# ============================================================
BTC_NETWORK = os.environ.get("BRN_BTC_NETWORK", "mainnet")

# ============================================================
# COTAÇÃO
# ------------------------------------------------------------
# 1 BTC = X BRN (fixo por enquanto)
# Ajuste conforme sua política de preço
# ============================================================
BRN_PER_BTC = float(os.environ.get("BRN_BRN_PER_BTC", "10000"))

# Taxa percentual cobrada pelo serviço (%)
FEE_PERCENT = float(os.environ.get("BRN_FEE_PERCENT", "2.0"))

# ============================================================
# LIMITES DE VALOR
# ------------------------------------------------------------
# Valores em satoshis (1 BTC = 100_000_000 sats)
#   MIN: 10_000 sats  = 0.0001 BTC
#   MAX: 100_000_000  = 1 BTC
# ============================================================
MIN_BTC_SATS = int(os.environ.get("BRN_MIN_BTC_SATS", "10000"))
MAX_BTC_SATS = int(os.environ.get("BRN_MAX_BTC_SATS", "100000000"))

# ============================================================
# CONFIRMAÇÕES BTC
# ------------------------------------------------------------
# Número de confirmações mínimas antes de creditar BRN.
# 3 é um bom equilíbrio entre segurança e velocidade.
# ============================================================
BTC_MIN_CONFIRMATIONS = int(os.environ.get("BRN_BTC_MIN_CONFIRMATIONS", "3"))

# ============================================================
# RPCS PÚBLICOS (fallback em ordem)
# ============================================================
BTC_RPCS = [
    "https://blockstream.info/api",
    "https://mempool.space/api",
    "https://api.blockcypher.com/v1/btc/main",
]

# ============================================================
# POLL INTERVAL
# ------------------------------------------------------------
# Intervalo (segundos) entre checagens do btc_watcher
# ============================================================
BTC_POLL_INTERVAL = int(os.environ.get("BRN_BTC_POLL_INTERVAL", "30"))

# ============================================================
# IDENTIFICADOR DO VALIDADOR PADRÃO
# ============================================================
VALIDATOR_ID = os.environ.get("BRN_VALIDATOR_ID", "main_validator")

# ============================================================
# HELPER DE SANIDADE
# ============================================================
def validate() -> tuple:
    """
    Roda sanidade básica. Retorna (ok, msg).
    Chame no boot para avisar se algo está mal configurado.
    """
    warns = []

    if not BTC_RECEIVE_ADDRESS:
        return False, "BTC_RECEIVE_ADDRESS vazio"

    if BTC_RECEIVE_ADDRESS.startswith("bc1qSEU_"):
        warns.append("BTC_RECEIVE_ADDRESS ainda e o placeholder")

    if BTC_NETWORK not in ("mainnet", "testnet"):
        return False, f"BTC_NETWORK invalido: {BTC_NETWORK}"

    if BRN_PER_BTC <= 0:
        return False, f"BRN_PER_BTC invalido: {BRN_PER_BTC}"

    if FEE_PERCENT < 0 or FEE_PERCENT > 50:
        return False, f"FEE_PERCENT fora do range: {FEE_PERCENT}"

    if MIN_BTC_SATS <= 0 or MAX_BTC_SATS <= 0:
        return False, "MIN/MAX BTC_SATS devem ser > 0"

    if MIN_BTC_SATS >= MAX_BTC_SATS:
        return False, (
            f"MIN_BTC_SATS ({MIN_BTC_SATS}) >= "
            f"MAX_BTC_SATS ({MAX_BTC_SATS})"
        )

    if BTC_MIN_CONFIRMATIONS < 1 or BTC_MIN_CONFIRMATIONS > 12:
        warns.append(f"BTC_MIN_CONFIRMATIONS={BTC_MIN_CONFIRMATIONS} "
                     f"(recomendado 3-6)")

    if warns:
        return True, "avisos: " + " | ".join(warns)
    return True, "config OK"


def info() -> dict:
    """Retorna dict com toda a config para debug."""
    ok, msg = validate()
    return {
        "ok":                   ok,
        "msg":                  msg,
        "btc_receive_address":  BTC_RECEIVE_ADDRESS,
        "btc_network":          BTC_NETWORK,
        "brn_per_btc":          BRN_PER_BTC,
        "fee_percent":          FEE_PERCENT,
        "min_btc_sats":         MIN_BTC_SATS,
        "max_btc_sats":         MAX_BTC_SATS,
        "min_confirmations":    BTC_MIN_CONFIRMATIONS,
        "poll_interval_s":      BTC_POLL_INTERVAL,
        "rpcs":                 BTC_RPCS,
        "validator_id":         VALIDATOR_ID,
        "min_btc_brl":          f"{MIN_BTC_SATS/1e8:.8f} BTC",
        "max_btc_brl":          f"{MAX_BTC_SATS/1e8:.8f} BTC",
    }


# ============================================================
# AUTOTESTE
# ============================================================
if __name__ == "__main__":
    import json
    print("btc_config.py — status:")
    print(json.dumps(info(), indent=2, ensure_ascii=False))

    ok, msg = validate()
    if not ok:
        print(f"\n[X] {msg}")
        raise SystemExit(1)
    print(f"\n[ok] {msg}")