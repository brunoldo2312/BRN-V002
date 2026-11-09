# ============================================================
# btc_config.py — Configuração da ponte BTC ↔ BRN
# v3.1 (2026-11-09):
#   - Lê o brn_network.env automaticamente se rodado via terminal.
#   - Define as RPCs (APIs da rede Bitcoin) baseadas na rede escolhida.
#   - Nenhum endereço BTC/BRN hardcoded no arquivo.
#   - WIF NUNCA é hardcoded. Vem de btc_wif.enc (cifrado) ou env.
# ============================================================
from __future__ import annotations
import json
import os
from pathlib import Path

BASE_DIR       = Path(__file__).resolve().parent
_CONFIG_FILE   = BASE_DIR / "btc_config_local.json"
_WIF_FILE      = BASE_DIR / "btc_wif.enc"
_ENV_FILE      = BASE_DIR / "brn_network.env"

# ============================================================
# 0. Carrega o .env automaticamente (caso rode o script direto)
# ============================================================
if _ENV_FILE.exists():
    try:
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception as e:
        print(f"[btc_config] aviso: falha ao ler .env ({e})")

# ============================================================
# 1. Carregar btc_config_local.json (se existir)
# ============================================================
def _load_local() -> dict:
    if not _CONFIG_FILE.exists():
        return {}
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[btc_config] aviso: {_CONFIG_FILE.name} invalido ({e})")
        return {}

_LOCAL = _load_local()

def _get(name: str, local_key: str, default: str = "") -> str:
    """
    Prioridade: env var > btc_config_local.json > default.
    """
    v = (os.environ.get(name, "") or "").strip()
    if v:
        return v
    v = str(_LOCAL.get(local_key, "") or "").strip()
    if v:
        return v
    return default

# ============================================================
# 2. Endereços (sem hardcode — vêm de env ou JSON)
# ============================================================
BTC_RECEIVE_ADDRESS = _get(
    "BRN_BTC_RECEIVE_ADDRESS",
    "btc_receive_address",
    "",                       # sem default — precisa ser configurado
)

BRN_TREASURY_ADDRESS = _get(
    "BRN_TREASURY_ADDRESS",
    "brn_treasury_address",
    "",                       # sem default — precisa ser configurado
)

BTC_NETWORK = _get(
    "BRN_BTC_NETWORK",
    "btc_network",
    "testnet",                # default seguro = testnet
).lower()

if BTC_NETWORK not in ("mainnet", "testnet"):
    BTC_NETWORK = "testnet"

# ============================================================
# 3. WIF (SECRETA — nunca hardcoded)
# ============================================================
def _load_wif() -> str:
    # 1) env var (compat)
    v = (os.environ.get("BRN_BTC_WIF", "") or "").strip()
    if v:
        return v

    # 2) btc_wif.enc cifrado
    if _WIF_FILE.exists():
        senha = (
            os.environ.get("BRN_NODE_PASSWORD", "").strip()
            or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
        )
        if not senha:
            print("[btc_config] btc_wif.enc existe, mas BRN_NODE_PASSWORD vazia")
            return ""
        try:
            from secure_store import _load_blob
            data = _load_blob(str(_WIF_FILE), senha, aad=b"btc_wif")
            return str((data or {}).get("wif", "")).strip()
        except Exception as e:
            print(f"[btc_config] falha ao decifrar btc_wif.enc: {e}")
            return ""

    return ""

BTC_WIF = _load_wif()

# ============================================================
# 4. OFFRAMP (BRN → BTC)
# ============================================================
MIN_OFFRAMP_BRN_SATS = int(_get("BRN_OFFRAMP_MIN_BRN_SATS", "_", "10000000"))
MAX_OFFRAMP_BRN_SATS = int(_get("BRN_OFFRAMP_MAX_BRN_SATS", "_", "10000000000"))
MIN_OFFRAMP_BTC_SATS = int(_get("BRN_OFFRAMP_MIN_BTC_SATS", "_", "5000"))
OFFRAMP_TTL_S        = int(_get("BRN_OFFRAMP_TTL_S",        "_", "1800"))

# ============================================================
# 5. ONRAMP (BTC → BRN)
# ============================================================
BRN_PER_BTC           = int(_get("BRN_PER_BTC",              "_", "10000"))
FEE_PERCENT           = float(_get("BRN_L2_FEE_PERCENT",     "_", "2.0"))
BTC_MIN_CONFIRMATIONS = int(_get("BRN_BTC_MIN_CONFIRMATIONS", "_", "3"))
MIN_BTC_SATS          = int(_get("BRN_L2_MIN_BTC_SATS",      "_", "5000"))
MAX_BTC_SATS          = int(_get("BRN_L2_MAX_BTC_SATS",      "_", "100000000"))

# Poll do watcher (segundos)
BTC_POLL_INTERVAL     = int(_get("BRN_BTC_POLL_INTERVAL",    "_", "30"))

# ============================================================
# 6. RPCs (Endpoints públicos — dependem da rede)
# ============================================================
if BTC_NETWORK == "mainnet":
    _default_rpcs = (
        "https://blockstream.info/api,"
        "https://mempool.space/api"
    )
else:
    _default_rpcs = (
        "https://blockstream.info/testnet/api,"
        "https://mempool.space/testnet/api"
    )

BTC_RPCS = [
    x.strip()
    for x in _get("BRN_BTC_RPCS", "btc_rpcs", _default_rpcs).split(",")
    if x.strip()
]

# ============================================================
# 7. Validação de config no import (falha cedo se algo crítico falta)
# ============================================================
def validate() -> dict:
    """
    Retorna dict {ok: bool, errors: [...], warnings: [...]}.
    O watcher/sender chamam isso no boot.
    """
    errors = []
    warnings = []

    if not BTC_RECEIVE_ADDRESS:
        errors.append(
            "BTC_RECEIVE_ADDRESS não configurado "
            "(defina BRN_BTC_RECEIVE_ADDRESS ou btc_config_local.json)"
        )
    elif BTC_NETWORK == "testnet" and BTC_RECEIVE_ADDRESS.startswith("bc1"):
        warnings.append(
            "BTC_NETWORK=testnet mas BTC_RECEIVE_ADDRESS parece mainnet (bc1...)"
        )
    elif BTC_NETWORK == "mainnet" and BTC_RECEIVE_ADDRESS.startswith("tb1"):
        warnings.append(
            "BTC_NETWORK=mainnet mas BTC_RECEIVE_ADDRESS parece testnet (tb1...)"
        )

    if not BRN_TREASURY_ADDRESS:
        errors.append(
            "BRN_TREASURY_ADDRESS não configurado "
            "(defina BRN_TREASURY_ADDRESS ou btc_config_local.json)"
        )
    elif not BRN_TREASURY_ADDRESS.startswith("brn1"):
        errors.append(
            f"BRN_TREASURY_ADDRESS inválido (deve começar com brn1): "
            f"{BRN_TREASURY_ADDRESS[:20]}"
        )

    if not BTC_WIF:
        warnings.append("BTC_WIF não configurado — offramp (BRN→BTC) bloqueado")

    return {"ok": not errors, "errors": errors, "warnings": warnings}

# ============================================================
# 8. Execução direta (diagnóstico)
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print(" btc_config.py — status (v3.1)")
    print("=" * 60)
    print(f"BTC_RECEIVE_ADDRESS : {BTC_RECEIVE_ADDRESS or '(VAZIO)'}")
    print(f"BRN_TREASURY_ADDRESS: {BRN_TREASURY_ADDRESS or '(VAZIO)'}")
    print(f"BTC_NETWORK         : {BTC_NETWORK}")
    print(f"BTC_WIF             : {'(configurada)' if BTC_WIF else '(vazia)'}")
    print(f"BTC_RPCS            : {BTC_RPCS}")
    print(f"BTC_POLL_INTERVAL   : {BTC_POLL_INTERVAL}s")
    print()

    v = validate()
    print(f"OK          : {v['ok']}")
    for e in v["errors"]:
        print(f"  [ERRO] {e}")
    for w in v["warnings"]:
        print(f"  [WARN] {w}")
