"""app_wallet_v3.py — Ponte entre a GUI pywebview e o nó BRN (v3.16)
================================================================
v3.16 (2026-11-04):
  [FIX CRÍTICO] pool_start_mining() agora envia o endereço e a pubkey
        da carteira para o servidor. Antes enviava um dict vazio {},
        fazendo o PoolClient minerar sem saber para quem pagar.
v3.15 (2026-11-03):
  [NEW] _sync_sk_to_chain() — propaga a SK da carteira ativa.
================================================================
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

__version__ = "3.16"

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    requests = None
    _HAS_REQUESTS = False


log = logging.getLogger("wallet-bridge")

BASE_DIR     = Path(__file__).resolve().parent
API_BASE     = os.environ.get("BRN_API_BASE", "http://127.0.0.1:5000").rstrip("/")
API_TIMEOUT  = float(os.environ.get("BRN_API_TIMEOUT", "200"))
GET_TIMEOUT  = float(os.environ.get("BRN_API_GET_TIMEOUT", "30"))
MAX_RETRIES  = int(os.environ.get("BRN_API_RETRIES", "2"))
RETRY_DELAY  = float(os.environ.get("BRN_API_RETRY_DELAY", "0.5"))


_WALLET_SESSION: dict = {}


def _sync_sk_to_chain(sk_hex: str, address: str = "", pubkey: str = "") -> dict:
    if not sk_hex:
        return {"ok": False, "msg": "sk vazio"}
    try:
        r = _post("/api/wallet/set_validator_sk", {
            "sk_hex": sk_hex,
            "address": address or "",
            "pubkey":  pubkey or "",
        }, timeout=10.0)
        if not r.get("ok"):
            log.warning("_sync_sk_to_chain falhou: %s", r.get("msg"))
        else:
            log.info(
                "_sync_sk_to_chain ok — pk=%s… trust_synced=%s",
                (r.get("pubkey") or "")[:16],
                r.get("trust_synced"),
            )
        return r
    except Exception as e:
        log.warning("_sync_sk_to_chain exception: %s", e)
        return {"ok": False, "msg": str(e)}


def _post(path: str, body: dict, timeout: float = None,
          retries: int = None) -> dict:
    if not _HAS_REQUESTS:
        return {"ok": False, "msg": "requests não instalado (pip install requests)"}

    url = API_BASE + path
    t = timeout or API_TIMEOUT
    r_n = MAX_RETRIES if retries is None else retries

    last_err = ""
    for attempt in range(r_n + 1):
        try:
            r = requests.post(
                url, json=body, timeout=t,
                proxies={"http": None, "https": None},
            )
            try:
                return r.json()
            except Exception:
                return {"ok": False,
                        "msg": f"HTTP {r.status_code} (resposta não-JSON)"}
        except requests.exceptions.ReadTimeout:
            last_err = f"timeout ({t}s)"
        except requests.exceptions.ConnectionError as e:
            last_err = f"connection: {e}"
        except Exception as e:
            last_err = str(e)
            break

        if attempt < r_n:
            time.sleep(RETRY_DELAY * (attempt + 1))

    if "Connection" in last_err or "connection" in last_err:
        return {"ok": False, "msg": "Servidor offline (:5000)"}
    if "timeout" in last_err.lower():
        return {"ok": False, "msg": f"Timeout ({t}s)"}
    return {"ok": False, "msg": last_err or "erro desconhecido"}


def _get(path: str, timeout: float = None,
         retries: int = None) -> dict:
    if not _HAS_REQUESTS:
        return {"error": "requests não instalado (pip install requests)"}

    url = API_BASE + path
    t = timeout or GET_TIMEOUT
    r_n = MAX_RETRIES if retries is None else retries

    last_err = ""
    for attempt in range(r_n + 1):
        try:
            r = requests.get(
                url, timeout=t,
                proxies={"http": None, "https": None},
            )
            try:
                return r.json()
            except Exception:
                return {"error": f"HTTP {r.status_code} (resposta não-JSON)"}
        except requests.exceptions.ReadTimeout:
            return {"error": f"Timeout ({t}s)"}
        except requests.exceptions.ConnectionError as e:
            last_err = str(e)
        except Exception as e:
            return {"error": str(e)}

        if attempt < r_n:
            time.sleep(RETRY_DELAY * (attempt + 1))

    if "Connection" in last_err or "connection" in last_err:
        return {"error": "Servidor offline (:5000)"}
    return {"error": last_err or "erro desconhecido"}


class WalletApi:
    def __init__(self):
        log.info("WalletApi v%s iniciada (API_BASE=%s)", __version__, API_BASE)
        if not _HAS_REQUESTS:
            log.warning("requests NÃO instalado — pip install requests")

    def has_wallet(self) -> dict:
        try:
            from secure_store import CURRENT_WALLET_ENC
            p = Path(CURRENT_WALLET_ENC)
            if not p.is_absolute():
                p = BASE_DIR / p
            return {"ok": True, "exists": p.exists()}
        except Exception:
            p = BASE_DIR / "current_wallet.enc"
            return {"ok": True, "exists": p.exists()}

    def create_and_unlock(self, password: str) -> dict:
        global _WALLET_SESSION
        if not password:
            return {"ok": False, "msg": "A senha é obrigatória."}
        if len(password) < 8:
            return {"ok": False, "msg": "A senha deve ter pelo menos 8 caracteres."}
        try:
            from wallet import Wallet
            from secure_store import save_current_wallet
            w = Wallet()
            data = {
                "address":     w.address,
                "private_key": w.priv_hex,
                "public_key":  w.pub_hex,
            }
            r = save_current_wallet(password, data)
            if not r.get("ok"):
                return {"ok": False, "msg": "Erro ao salvar: " + str(r.get("msg", ""))}
            _WALLET_SESSION.update(data)
            _sync_sk_to_chain(w.priv_hex, w.address, w.pub_hex)
            return {"ok": True, "address": w.address}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def unlock_wallet(self, password: str) -> dict:
        global _WALLET_SESSION
        try:
            from secure_store import load_current_wallet
            r = load_current_wallet(password)
            if r and r.get("ok"):
                _WALLET_SESSION["address"]     = r.get("address", "")
                _WALLET_SESSION["private_key"] = r.get("private_key", "")
                _WALLET_SESSION["public_key"]  = (
                    r.get("public_key") or r.get("pubkey") or r.get("pub_hex") or ""
                )
                if _WALLET_SESSION.get("private_key"):
                    _sync_sk_to_chain(
                        _WALLET_SESSION["private_key"],
                        _WALLET_SESSION.get("address", ""),
                        _WALLET_SESSION.get("public_key", ""),
                    )
                return {"ok": True, "address": _WALLET_SESSION["address"]}
            return {"ok": False, "msg": r.get("msg", "Senha incorreta")}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def lock_wallet(self) -> dict:
        global _WALLET_SESSION
        _WALLET_SESSION.clear()
        try:
            from wallet import WalletManager
            WalletManager.lock_session()
        except Exception:
            pass
        return {"ok": True}

    def get_active_wallet(self) -> dict:
        if _WALLET_SESSION.get("address"):
            return {"ok": True, **_WALLET_SESSION}
        return {"ok": False, "msg": "locked"}

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
                "variant":       variant,
                "trigger_event": "wallet_open",
                "rendered_flag": 1,
            })
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def node_status(self) -> dict:
        try:
            s = _get("/api/status", timeout=GET_TIMEOUT)
            if "error" in s and not s.get("height"):
                return {"ok": False, "msg": s.get("error", "erro")}
            return {
                "ok":                   True,
                "height":               s.get("height", 0),
                "mempool":              s.get("mempool", 0),
                "peers":                s.get("peers", 0),
                "utxos":                s.get("utxos", 0),
                "difficulty":           s.get("difficulty", 0),
                "reward":               s.get("reward", 0),
                "tip_hash":             s.get("tip_hash", ""),
                "l2_enabled":           s.get("l2_enabled", False),
                "pool_enabled":         s.get("pool_enabled", False),
                "fila_tamanho":         s.get("fila_tamanho", 0),
                "rede_descentralizada": s.get("rede_descentralizada", False),
                "validators_active":    s.get("validators_active", 0),
            }
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def sync_info(self) -> dict:
        try:
            s = _get("/api/sync-info", timeout=GET_TIMEOUT)
            if "error" in s and "sync" not in s:
                return {"erro": s["error"]}
            if not s.get("ok", True):
                return {"erro": s.get("erro") or s.get("error") or "erro desconhecido"}
            return s
        except Exception as e:
            return {"erro": str(e)}

    def list_blocks(self, start: int = 0, limit: int = 15) -> dict:
        try:
            s = _get(f"/api/blocks?start={int(start)}&limit={int(limit)}")
            if "error" in s:
                return {"erro": s["error"]}
            return {
                "blocks": s.get("blocks", []),
                "start":  s.get("start", 0),
                "count":  s.get("count", 0),
            }
        except Exception as e:
            return {"erro": str(e)}

    def get_block(self, height: int) -> dict:
        try:
            s = _get(f"/api/block/{int(height)}")
            return {"erro": s["error"]} if "error" in s else {"block": s}
        except Exception as e:
            return {"erro": str(e)}

    def consensus_validators(self) -> dict:
        try:
            r = _get("/api/consensus/validators", timeout=10.0)
            if "error" in r:
                return {"ok": False, "msg": r["error"], "required_sigs": 0,
                        "all_required": True, "validators": []}
            return r
        except Exception as e:
            return {"ok": False, "msg": str(e), "required_sigs": 0,
                    "all_required": True, "validators": []}

    def block_signatures(self, height: int) -> dict:
        try:
            return _get(f"/api/block/{int(height)}/signatures", timeout=10.0)
        except Exception as e:
            return {"ok": False, "msg": str(e), "signatures": []}

    def get_multi_sig_status(self) -> dict:
        try:
            validators_resp = self.consensus_validators()
            required = int(validators_resp.get("required_sigs", 0) or 0)
            all_required = bool(validators_resp.get("all_required", True))
            mining = self.mining_status()
            height = int(mining.get("height", 0) or 0)
            last_sigs = 0
            if height > 0:
                sigs_resp = self.block_signatures(height)
                last_sigs = int(sigs_resp.get("signatures_count", 0) or 0)
            return {
                "ok": True,
                "validators_active": len(validators_resp.get("validators", [])),
                "required_sigs":     required,
                "all_required":      all_required,
                "last_block_height": height,
                "last_block_sigs":   last_sigs,
                "is_multisig_active": required >= 1,
                "last_block_ready":   last_sigs >= required if required > 0 else True,
            }
        except Exception as e:
            return {"ok": False, "msg": str(e), "validators_active": 0, "required_sigs": 0}

    def portfolio(self, address: str) -> dict:
        try:
            s = _get(f"/api/portfolio/{address}", timeout=15.0)
            if "error" in s:
                return {"erro": s["error"]}
            return s.get("portfolio", {"BRN": 0, "KYC": 0})
        except Exception as e:
            return {"erro": str(e)}

    def generate_wallet(self, password: str = "") -> dict:
        try:
            r = _post("/api/nova-carteira", {})
            if not r.get("success"):
                return {"erro": r.get("error", "falha")}
            address, private_key, public_key = r["address"], r["private_key"], r["public_key"]
            pwd = ((password or "").strip()
                   or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
                   or os.environ.get("BRN_NODE_PASSWORD", "").strip())
            if pwd:
                try:
                    from secure_store import save_current_wallet
                    save_result = save_current_wallet(pwd, {
                        "address": address, "private_key": private_key, "public_key": public_key,
                    })
                    if save_result.get("ok"):
                        _WALLET_SESSION.update({
                            "address": address, "private_key": private_key, "public_key": public_key,
                        })
                except Exception as e:
                    log.warning("Falha salvando carteira em disco: %s", e)
            return {"address": address, "private_key": private_key, "public_key": public_key}
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
        global _WALLET_SESSION
        try:
            from wallet import WalletManager
            r = WalletManager.load_encrypted_wallet(name, password)
            if r and r.get("ok"):
                _WALLET_SESSION["address"]     = r.get("address", "")
                _WALLET_SESSION["private_key"] = r.get("private_key", "")
                _WALLET_SESSION["public_key"]  = (
                    r.get("public_key") or r.get("pubkey") or r.get("pub_hex") or ""
                )
                if _WALLET_SESSION["private_key"]:
                    _sync_sk_to_chain(
                        _WALLET_SESSION["private_key"],
                        _WALLET_SESSION["address"],
                        _WALLET_SESSION["public_key"],
                    )
            return r or {"ok": False, "msg": "resposta vazia"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def transfer(self, from_addr: str, to_addr: str, asset: str,
                 amount, sk: str, pk: str) -> dict:
        try:
            return _post("/api/transfer", {
                "from": from_addr, "to": to_addr, "asset_id": asset or "BRN",
                "amount": str(amount), "private_key": sk, "pubkey": pk,
            })
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def minhas_txs(self, address: str, limit: int = 50) -> dict:
        try:
            try:
                lim = max(1, min(int(limit), 500))
            except (ValueError, TypeError):
                lim = 50
            return _get(f"/api/minhas-txs/{address}?limit={lim}", timeout=60.0)
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

    def start_mining(self, address: str, pubkey: str) -> dict:
        try:
            return _post("/api/miner/start", {
                "address": address, "miner_pubkey": pubkey,
            })
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def stop_mining(self) -> dict:
        try:
            from miner_loop import get_miner
            from server import _get_chain
            chain = _get_chain()
            m = get_miner(chain)
            if m:
                ok, msg = m.stop()
                return {"ok": ok, "msg": msg, "count": m.blocks_mined}
            return {"ok": False, "msg": "Miner nao iniciado"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def mining_status(self) -> dict:
        try:
            r = _get("/api/miner/status", timeout=GET_TIMEOUT)
            if "error" not in r:
                r.setdefault("validators_active", 0)
                r.setdefault("signatures_required", 0)
                r.setdefault("signatures_collected", 0)
            return r
        except Exception as e:
            return {"running": False, "error": str(e)}

    def _resolve_pool_url(self) -> str:
        env_pool = (os.environ.get("BRN_POOL_URL", "") or "").strip()
        if env_pool:
            return env_pool.rstrip("/")
        return API_BASE

    # =================================================================
    # [FIX v3.16] Envia address e pubkey para o servidor
    # =================================================================
    def pool_start_mining(self, address: str, pubkey: str) -> dict:
        if not address or not pubkey:
            return {"ok": False, "msg": "address e pubkey obrigatorios"}
        try:
            r = _post("/api/pool/start_local", {
                "address": address,
                "pubkey": pubkey
            }, timeout=30.0)
            if r.get("ok"):
                return r
            return {"ok": False, "msg": r.get("msg", "start_local falhou")}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def pool_stop_mining(self) -> dict:
        try:
            r = _post("/api/pool/stop_local", {}, timeout=30.0)
            if r.get("ok"):
                return r
            return {"ok": False, "msg": r.get("msg", "stop_local falhou")}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def pool_status(self) -> dict:
        try:
            r = _get("/api/pool/local_status", timeout=10.0)
            if not isinstance(r, dict):
                return {"ok": False, "running": False, "msg": "resposta invalida"}

            if r.get("running") and r.get("job_share_diff") is None:
                try:
                    from miner_pool_client import get_pool_client
                    pc = get_pool_client()
                    if pc:
                        last_diff = getattr(pc, "_last_known_share_diff", None)
                        if last_diff is not None:
                            r["job_share_diff"] = last_diff
                        if r.get("job_id") is None and pc._job:
                            r["job_id"] = pc._job.get("job_id")
                            r["job_height"] = pc._job.get("height")
                            r["job_difficulty"] = pc._job.get("difficulty")
                            r["job_share_diff"] = pc._job.get("share_difficulty")
                except Exception as e:
                    log.debug("Fallback do pool_status falhou: %s", e)

            return r
        except Exception as e:
            return {"ok": False, "running": False, "msg": str(e)}

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

    def get_trust_stats(self) -> dict:
        try:
            return _get("/api/trust/stats", timeout=10.0)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_trust_nodes(self, limit: int = 100) -> dict:
        try:
            return _get(f"/api/trust/nodes?limit={int(limit)}", timeout=10.0)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def call_faucet(self, address: str, sk: str, pk: str) -> dict:
        try:
            return _post("/api/faucet", {"address": address, "private_key": sk, "pubkey": pk})
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def bridge_claim(self, txid: str, address: str) -> dict:
        try:
            return _post("/api/l2/claim", {"btc_txid": txid, "buyer_brn": address})
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def ping(self) -> dict:
        try:
            return _get("/health", timeout=5.0)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_version(self) -> dict:
        return {"version": __version__, "api_base": API_BASE, "has_requests": _HAS_REQUESTS}

    def get_debug_info(self) -> dict:
        consensus_info = {}
        try:
            consensus_info = self.get_multi_sig_status()
        except Exception:
            pass
        pool_running = False
        try:
            ps = self.pool_status()
            pool_running = bool(ps.get("running", False))
        except Exception:
            pass
        return {
            "version": __version__, "api_base": API_BASE,
            "has_requests": _HAS_REQUESTS,
            "wallet_locked": not bool(_WALLET_SESSION.get("address")),
            "wallet_address": _WALLET_SESSION.get("address", ""),
            "pool_client_running": pool_running,
            "pool_url_resolved": self._resolve_pool_url(),
            "consensus": consensus_info,
            "env": {
                "BRN_IS_ORIGIN":       os.environ.get("BRN_IS_ORIGIN", ""),
                "BRN_POOL_URL":        os.environ.get("BRN_POOL_URL", ""),
                "BRN_POOL_URL_LOCAL":  os.environ.get("BRN_POOL_URL_LOCAL", ""),
                "BRN_WALLET_SESSION_PASS_set": bool(
                    os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
                ),
            },
        }


def _self_test() -> int:
    print(f"app_wallet_v3.py v{__version__}")
    print("=" * 60)
    print(f"API_BASE:      {API_BASE}")
    print(f"requests:      {'OK' if _HAS_REQUESTS else 'AUSENTE'}")
    print()

    if not _HAS_REQUESTS:
        print("[X] requests nao instalado. Rode: pip install requests")
        return 1

    api = WalletApi()

    r = api.ping()
    if r.get("ok"):
        print(f"[1] ping OK — height={r.get('height')} v={r.get('version', '?')}")
    else:
        print(f"[1] [X] ping: {r}")
        return 1

    r = api.node_status()
    print(f"[2] node_status: ok={r.get('ok')} height={r.get('height')}")

    r = api.pool_status()
    print(f"[3] pool_status: running={r.get('running')} "
          f"hashrate_khs={r.get('hashrate_khs', 0)} "
          f"job_share_diff={r.get('job_share_diff')}")

    print("\n" + "=" * 60)
    print(" SELF-TEST CONCLUIDO")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    p = argparse.ArgumentParser(prog="app_wallet_v3.py")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--version", action="store_true")
    args = p.parse_args()

    if args.version:
        print(f"app_wallet_v3.py v{__version__}")
        sys.exit(0)

    if args.self_test:
        sys.exit(_self_test())

    print("Uso: python app_wallet_v3.py --self-test")
    sys.exit(0)
