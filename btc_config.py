# ============================================================
# btc_config.py — Configuração da ponte BTC ↔ BRN
# ============================================================
from __future__ import annotations

import os


# ============================================================
# OFFRAMP (BRN → BTC)  — operacional
# ------------------------------------------------------------
# WIF do cofre (⚠️ SEGREDO — guarde offline, nunca no Git)
# ============================================================
BTC_WIF = os.environ.get("BRN_BTC_WIF", "")

BRN_TREASURY_ADDRESS = os.environ.get(
    "BRN_TREASURY_ADDRESS",
    "brn1qxyzk7y0v2j4g0a8d9n5t2k7h4s6w8c9p2e",
)

MIN_OFFRAMP_BRN_SATS = int(os.environ.get("BRN_OFFRAMP_MIN_BRN_SATS", "10000000"))    # 0.1 BRN
MAX_OFFRAMP_BRN_SATS = int(os.environ.get("BRN_OFFRAMP_MAX_BRN_SATS", "10000000000")) # 100 BRN
MIN_OFFRAMP_BTC_SATS = int(os.environ.get("BRN_OFFRAMP_MIN_BTC_SATS", "5000"))        # 5000 sats
OFFRAMP_TTL_S        = int(os.environ.get("BRN_OFFRAMP_TTL_S", "1800"))


# ============================================================
# ONRAMP (BTC → BRN)  — NÃO OPERACIONAL (ver README §6)
# ------------------------------------------------------------
# server.py ainda registra /api/l2/quote, /api/l2/create, etc.
# e essas rotas leem as chaves abaixo. Sem Blockchain.add_l2_transaction()
# a conversão real NÃO funciona — as rotas vão retornar erro 500.
# Os placeholders evitam AttributeError na importação do módulo.
# ============================================================
BTC_RECEIVE_ADDRESS  = os.environ.get("BRN_BTC_RECEIVE_ADDRESS", "")
BTC_NETWORK          = os.environ.get("BRN_BTC_NETWORK", "mainnet")
BRN_PER_BTC          = int(os.environ.get("BRN_PER_BTC", "10000"))
FEE_PERCENT          = float(os.environ.get("BRN_L2_FEE_PERCENT", "2.0"))
BTC_MIN_CONFIRMATIONS = int(os.environ.get("BRN_BTC_MIN_CONFIRMATIONS", "3"))
MIN_BTC_SATS         = int(os.environ.get("BRN_L2_MIN_BTC_SATS", "5000"))
MAX_BTC_SATS         = int(os.environ.get("BRN_L2_MAX_BTC_SATS", "100000000"))
BTC_RPCS = [
    x.strip()
    for x in os.environ.get(
        "BRN_BTC_RPCS",
        "https://blockstream.info/api,https://mempool.space/api",
    ).split(",")
    if x.strip()
]
