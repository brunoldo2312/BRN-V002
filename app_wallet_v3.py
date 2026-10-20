"""app_wallet_v3.py — Ponte entre a GUI pywebview e o nó BRN (v3.7)"""
from __future__ import annotations
import os
import time
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("wallet-bridge")
BASE_DIR = Path(__file__).resolve().parent
API_BASE = os.environ.get("BRN_API_BASE", "http://127.0.0.1:5000")
API_TIMEOUT = float(os.environ.get("BRN_API_TIMEOUT", "200"))

_POOL_CLIENT = None
_POOL_LOCK = threading.RLock()
_WALLET_SESSION = {}


def _post(path: str, body: dict, timeout: float = None) -> dict:
    import requests
    url = API_BASE.rstrip("/") + path
    try:
        r = requests.post(
            url, json=body, timeout=timeout or API_TIMEOUT,
            proxies={"http": None, "https": None},
        )
        try:
            return r.json()
        except Exception:
            return {"ok": False, "msg": f"HTTP {r.status_code}"}
    except requests.exceptions.ReadTimeout:
        return {"ok": False, "msg": "Timeout"}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "msg": "Servidor offline (:5000)"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def _get(path: str, timeout: float = 30.0) -> dict:
    import requests
    url = API_BASE.rstrip("/") + path
    try:
        r = requests.get(
            url, timeout=timeout,
            proxies={"http": None, "https": None},
        )
        try:
            return r.json()
        except Exception:
            return {"error": f"HTTP {r.status_code}"}
    except requests.exceptions.ReadTimeout:
        return {"error": "Timeout (servidor ocupado)"}
    except requests.exceptions.ConnectionError:
        return {"error": "Servidor offline (:5000)"}
    except Exception as e:
        return {"error": str(e)}


class WalletApi:
    def __init__(self):
        log.info("WalletApi v3.7 iniciada (API_BASE=%s)", API_BASE)

    # ========================================================
    # GERENCIAMENTO DE SESSÃO DA CARTEIRA
    # ========================================================
    def has_wallet(self) -> dict:
        try:
            p = BASE_DIR / "current_wallet.enc"
            return {"ok": True, "exists": p.exists()}
        except Exception as e:
            return {"ok": False, "exists": False, "msg": str(e)}

    def create_and_unlock(self, password: str) -> dict:
        global _WALLET_SESSION
        if not password or len(password) < 4:
            return {"ok": False, "msg": "A senha deve ter pelo menos 4 caracteres."}
        try:
            from wallet import Wallet
            from secure_store import save_current_wallet
            w = Wallet()
            data = {
                "address": w.address,
                "private_key": w.priv_hex,
                "public_key": w.pub_hex,
            }
            r = save_current_wallet(password, data)
            if not r.get("ok"):
                return {"ok": False, "msg": "Erro ao salvar: " + str(r.get("msg", ""))}
            _WALLET_SESSION.update(data)
            log.info(f"Nova carteira criada: {w.address[:16]}...")
            return {"ok": True, "address": w.address}
        except Exception as e:
            log.exception("Erro ao criar carteira")
            return {"ok": False, "msg": str(e)}

    def unlock_wallet(self, password: str) -> dict:
        global _WALLET_SESSION
        try:
            from secure_store import load_current_wallet
            r = load_current_wallet(password)
            if r and r.get("ok"):
                _WALLET_SESSION["address"] = r.get("address", "")
                _WALLET_SESSION["private_key"] = r.get("private_key", "")
                _WALLET_SESSION["public_key"] = (
                    r.get("public_key") or r.get("pubkey") or r.get("pub_hex") or ""
                )
                return {"ok": True, "address": _WALLET_SESSION["address"]}
            return {"ok": False, "msg": r.get("msg", "Senha incorreta")}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def lock_wallet(self) -> dict:
        global _WALLET_SESSION
        _WALLET_SESSION.clear()
        return {"ok": True}

    def get_active_wallet(self) -> dict:
        if _WALLET_SESSION.get("address"):
            return {"ok": True, **_WALLET_SESSION}
        return {"ok": False, "msg": "locked"}

    # ========================================================
    # EXPERIMENTO A/B
    # ========================================================
    def get_experiment_variant(self) -> dict:
        try:
            from ab_test import get_variant, get_node_id_hash
            if not _WALLET_SESSION.get("address"):
                return {"variant": "A", "error": "locked"}
            node_id = _WALLET_SESSION["address"]
            node_hash = get_node_id_hash(node_id)
            from security import set_node_id_hash
            set_node_id_hash(node_hash)
            return {"variant": get_variant(node_hash), "node_hash": node_hash}
        except Exception as e:
            return {"variant": "A", "error": str(e)}

    def log_exposure(self, variant: str) -> dict:
        try:
            from security import audit_log
            audit_log("exposure_log", {
                "experiment_id": "activation_v1",
                "variant": variant,
                "trigger_event": "wallet_open",
                "rendered_flag": 1,
            })
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ========================================================
    # NÓ / CHAIN
    # ========================================================
    def node_status(self) -> dict:
        try:
            s = _get("/api/status", timeout=30.0)
            if "error" in s and not s.get("height"):
                return {"ok": False, "msg": s.get("error", "erro")}
            return {
                "ok": True,
                "height": s.get("height", 0),
                "mempool": s.get("mempool", 0),
                "peers": s.get("peers", 0),
                "utxos": s.get("utxos", 0),
                "difficulty": s.get("difficulty", 0),
                "reward": s.get("reward", 0),
                "tip_hash": s.get("tip_hash", ""),
                "l2_enabled": s.get("l2_enabled", False),
                "pool_enabled": s.get("pool_enabled", False),
                "fila_tamanho": s.get("fila_tamanho", 0),
                "rede_descentralizada": s.get("rede_descentralizada", False),
            }
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def sync_info(self) -> dict:
        try:
            s = _get("/api/sync-info", timeout=30.0)
            return s if "erro" not in s else {"erro": s["erro"]}
        except Exception as e:
            return {"erro": str(e)}

    def list_blocks(self, start: int = 0, limit: int = 15) -> dict:
        try:
            s = _get(f"/api/blocks?start={int(start)}&limit={int(limit)}")
            return (
                {"erro": s["error"]}
                if "error" in s
                else {"blocks": s.get("blocks", []),
                      "start": s.get("start", 0),
                      "count": s.get("count", 0)}
            )
        except Exception as e:
            return {"erro": str(e)}

    def get_block(self, height: int) -> dict:
        try:
            s = _get(f"/api/block/{int(height)}")
            return {"erro": s["error"]} if "error" in s else {"block": s}
        except Exception as e:
            return {"erro": str(e)}

    # ========================================================
    # CARTEIRA BÁSICA
    # ========================================================
    def portfolio(self, address: str) -> dict:
        try:
            s = _get(f"/api/portfolio/{address}", timeout=15.0)
            return (
                {"erro": s["error"]}
                if "error" in s
                else s.get("portfolio", {"BRN": 0, "KYC": 0})
            )
        except Exception as e:
            return {"erro": str(e)}

    def generate_wallet(self) -> dict:
        try:
            r = _post("/api/nova-carteira", {})
            return (
                {"address": r["address"],
                 "private_key": r["private_key"],
                 "public_key": r["public_key"]}
                if r.get("success")
                else {"erro": r.get("error", "falha")}
            )
        except Exception as e:
            return {"erro": str(e)}

    def save_wallet(self, name: str, password: str,
                    address: str, sk: str, pk: str) -> dict:
        try:
            from wallet import WalletManager
            return WalletManager.save_encrypted_wallet(name, password, address, sk, pk)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def list_wallets(self) -> list:
        try:
            from wallet import WalletManager
            return WalletManager.list_wallets()
        except Exception:
            return []

    def load_wallet(self, name: str, password: str) -> dict:
        try:
            from wallet import WalletManager
            return WalletManager.load_encrypted_wallet(name, password)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ========================================================
    # TRANSAÇÕES
    # ========================================================
    def transfer(self, from_addr: str, to_addr: str, asset: str,
                 amount, sk: str, pk: str) -> dict:
        try:
            return _post("/api/transfer", {
                "from": from_addr, "to": to_addr,
                "asset_id": asset or "BRN",
                "amount": str(amount),
                "private_key": sk, "pubkey": pk,
            })
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def minhas_txs(self, address: str) -> dict:
        """[FIX v3.7] Pede apenas 50 transações para aliviar o banco."""
        try:
            return _get(f"/api/minhas-txs/{address}?limit=50", timeout=30.0)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def tx_status(self, txid: str) -> dict:
        try:
            return _get(f"/api/tx-status/{txid}")
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def verificar_recebimento(self, address: str, txid: str) -> dict:
        try:
            return _get(f"/api/verificar-recebimento/{address}/{txid}")
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ========================================================
    # MINERAÇÃO
    # ========================================================
    def start_mining(self, address: str, pubkey: str) -> dict:
        try:
            return _post("/api/miner/start", {
                "address": address, "miner_pubkey": pubkey,
            })
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def stop_mining(self) -> dict:
        try:
            return _post("/api/miner/stop", {})
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def mining_status(self) -> dict:
        try:
            return _get("/api/miner/status", timeout=30.0)
        except Exception as e:
            return {"running": False, "error": str(e)}

    def pool_start_mining(self, address: str, pubkey: str) -> dict:
        global _POOL_CLIENT
        with _POOL_LOCK:
            if not address or not pubkey:
                return {"ok": False, "msg": "address e pubkey obrigatorios"}
            try:
                from miner_pool_client import PoolClient
            except ImportError as e:
                return {"ok": False, "msg": f"miner_pool_client indisponivel: {e}"}

            if _POOL_CLIENT is not None and _POOL_CLIENT.is_running():
                if _POOL_CLIENT.address == address:
                    return {"ok": True, "msg": "ja minerando",
                            "miner_id": _POOL_CLIENT.miner_id}
                try:
                    _POOL_CLIENT.stop_mining()
                except Exception:
                    pass

            try:
                pool_url = os.environ.get("BRN_POOL_URL", API_BASE)
                _POOL_CLIENT = PoolClient(
                    address=address, pubkey=pubkey, pool_url=pool_url,
                )
                ok, msg = _POOL_CLIENT.start_mining()
                return (
                    {"ok": True, "msg": "Pool miner iniciado",
                     "miner_id": _POOL_CLIENT.miner_id}
                    if ok else {"ok": False, "msg": msg}
                )
            except Exception as e:
                return {"ok": False, "msg": str(e)}

    def pool_stop_mining(self) -> dict:
        global _POOL_CLIENT
        with _POOL_LOCK:
            if _POOL_CLIENT is None:
                return {"ok": False, "msg": "nao esta a minerar"}
            try:
                ok, msg = _POOL_CLIENT.stop_mining()
                return {"ok": ok, "msg": msg}
            except Exception as e:
                return {"ok": False, "msg": str(e)}

    def pool_status(self) -> dict:
        global _POOL_CLIENT
        with _POOL_LOCK:
            if _POOL_CLIENT is None:
                return {"ok": True, "running": False,
                        "hashrate": 0.0, "shares_accepted": 0}
            try:
                return _POOL_CLIENT.status()
            except Exception as e:
                return {"ok": False, "msg": str(e)}

    def pool_stats(self) -> dict:
        try:
            return _get("/api/pool/stats", timeout=10.0)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def pool_miners(self) -> dict:
        try:
            return _get("/api/pool/miners", timeout=10.0)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ========================================================
    # FAUCET / BRIDGE
    # ========================================================
    def call_faucet(self, address: str, sk: str, pk: str) -> dict:
        try:
            return _post("/api/faucet", {
                "address": address, "private_key": sk, "pubkey": pk,
            })
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def bridge_claim(self, txid: str, address: str) -> dict:
        try:
            return _post("/api/l2/claim", {
                "btc_txid": txid, "buyer_brn": address,
            })
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ========================================================
    # UTIL
    # ========================================================
    def ping(self) -> dict:
        try:
            return _get("/health", timeout=5.0)
        except Exception as e:
            return {"ok": False, "msg": str(e)}