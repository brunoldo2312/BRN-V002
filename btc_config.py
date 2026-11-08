# ============================================================
# btc_config.py — Configuração da ponte BTC ↔ BRN
# v3 (2026-11-05): treasury atualizado para o novo genesis
# ============================================================
from __future__ import annotations

import os


# ============================================================
# Endereço BRN do cofre — DEVE SER O MESMO DO GENESIS
# ============================================================
# Novo genesis: brn1g2xdpr8r4cmq725pjmza43pzce9ha5mnjzjaz8
DEFAULT_BRN_TREASURY = "brn1g2xdpr8r4cmq725pjmza43pzce9ha5mnjzjaz8"


# ============================================================
# OFFRAMP (BRN → BTC)
# ============================================================
# ⚠️ A WIF NUNCA deve ser hardcoded. Use BRN_BTC_WIF no .env.
BTC_WIF = os.environ.get("BRN_BTC_WIF", "")


# ============================================================
# ONRAMP (BTC → BRN)
# ============================================================
# Endereço BTC de recebimento (PÚBLICO)
# ⚠️ Use TESTNET durante desenvolvimento. Só mude para mainnet quando
#    tiver certeza e controle total da WIF.
BTC_RECEIVE_ADDRESS = os.environ.get(
    "BRN_BTC_RECEIVE_ADDRESS",
    "tb1qSEU_ENDERECO_TESTNET_AQUI",   # ⚠️ trocar pelo seu endereço testnet
)

BRN_TREASURY_ADDRESS = os.environ.get(
    "BRN_TREASURY_ADDRESS",
    DEFAULT_BRN_TREASURY,
)

# Limites OFFRAMP
MIN_OFFRAMP_BRN_SATS = int(os.environ.get("BRN_OFFRAMP_MIN_BRN_SATS", "10000000"))     # 0.1 BRN
MAX_OFFRAMP_BRN_SATS = int(os.environ.get("BRN_OFFRAMP_MAX_BRN_SATS", "10000000000"))  # 100 BRN
MIN_OFFRAMP_BTC_SATS = int(os.environ.get("BRN_OFFRAMP_MIN_BTC_SATS", "5000"))
OFFRAMP_TTL_S        = int(os.environ.get("BRN_OFFRAMP_TTL_S", "1800"))


# ============================================================
# ONRAMP (BTC → BRN)
# ============================================================
BTC_NETWORK          = os.environ.get("BRN_BTC_NETWORK", "testnet")  # ⚠️ mude para "mainnet" só em produção
BRN_PER_BTC          = int(os.environ.get("BRN_PER_BTC", "10000"))
FEE_PERCENT          = float(os.environ.get("BRN_L2_FEE_PERCENT", "2.0"))
BTC_MIN_CONFIRMATIONS = int(os.environ.get("BRN_BTC_MIN_CONFIRMATIONS", "3"))
MIN_BTC_SATS         = int(os.environ.get("BRN_L2_MIN_BTC_SATS", "5000"))
MAX_BTC_SATS         = int(os.environ.get("BRN_L2_MAX_BTC_SATS", "100000000"))
BTC_RPCS = [
    x.strip()
    for x in os.environ.get(
        "BRN_BTC_RPCS",
        "https://blockstream.info/testnet/api,https://mempool.space/testnet/api",
    ).split(",")
    if x.strip()
]