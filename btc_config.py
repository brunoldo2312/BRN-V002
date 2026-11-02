# ============================================================
# btc_config.py — Configuração da ponte BTC ↔ BRN
# ============================================================
from __future__ import annotations

import os


# ============================================================
# OFFRAMP (BRN → BTC)  — operacional
# ============================================================
# ⚠️ ATENÇÃO SEGURANÇA: A WIF nunca deve ser hardcoded.
# Use a variável de ambiente BRN_BTC_WIF.
BTC_WIF = os.environ.get("BRN_BTC_WIF", "")

# Endereço BTC de recebimento (PÚBLICO — usado no ONRAMP)
# ⚠️ TROQUE PELO SEU ENDEREÇO BTC REAL (Ex: bc1q... ou 1...)
BTC_RECEIVE_ADDRESS = os.environ.get(
    "BRN_BTC_RECEIVE_ADDRESS",
    "1BBKio11f87a5f3jD2QHCa6mALw667Liag",
)

# Endereço BRN do cofre (PÚBLICO) — ATUALIZADO AQUI
BRN_TREASURY_ADDRESS = os.environ.get(
    "BRN_TREASURY_ADDRESS",
    "brn1x9v8xq6v7dgrszl3ep6zupk28y7jfr8a0cwmgu",
)

# Limites OFFRAMP
MIN_OFFRAMP_BRN_SATS = int(os.environ.get("BRN_OFFRAMP_MIN_BRN_SATS", "10000000"))    # 0.1 BRN
MAX_OFFRAMP_BRN_SATS = int(os.environ.get("BRN_OFFRAMP_MAX_BRN_SATS", "10000000000")) # 100 BRN
MIN_OFFRAMP_BTC_SATS = int(os.environ.get("BRN_OFFRAMP_MIN_BTC_SATS", "5000"))        # 5000 sats
OFFRAMP_TTL_S        = int(os.environ.get("BRN_OFFRAMP_TTL_S", "1800"))


# ============================================================
# ONRAMP (BTC → BRN)
# ============================================================
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
