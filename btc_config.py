# ============================================================
# OFFRAMP (BRN → BTC)
# ------------------------------------------------------------
# WIF do cofre (⚠️ SEGREDO — guarde offline)
# ============================================================
BTC_WIF = os.environ.get("BRN_BTC_WIF", "")

# Endereço BRN que recebe os fundos dos saques (cofre BRN)
BRN_TREASURY_ADDRESS = os.environ.get(
    "BRN_TREASURY_ADDRESS",
    "brn1qxyzk7y0v2j4g0a8d9n5t2k7h4s6w8c9p2e",
)

# Limites de saque
MIN_OFFRAMP_BRN_SATS = int(os.environ.get("BRN_OFFRAMP_MIN_BRN_SATS", "10000000"))   # 0.1 BRN
MAX_OFFRAMP_BRN_SATS = int(os.environ.get("BRN_OFFRAMP_MAX_BRN_SATS", "10000000000")) # 100 BRN
MIN_OFFRAMP_BTC_SATS = int(os.environ.get("BRN_OFFRAMP_MIN_BTC_SATS", "5000"))       # 5000 sats

# TTL do escrow (antes de enviar BTC)
OFFRAMP_TTL_S = int(os.environ.get("BRN_OFFRAMP_TTL_S", "1800"))
