"""app_wallet_v3.py — Ponte entre a GUI pywebview e o nó BRN (v3.8.1)
================================================================
v3.8.1 (2026-10-22):
  [FIX CRÍTICO] sync_info() ainda ignorava {"error": ...} retornado
                por _get() em falha de rede. Retornava cru o dict com
                "error" (EN), que não casava com o check `"erro" in r`
                do self-test. Agora traduz "error" -> "erro" primeiro.
  [FIX] has_wallet() respeita secure_store.CURRENT_WALLET_ENC (antes
        hardcoded em BASE_DIR/current_wallet.enc, ignorando
        BRN_CURRENT_WALLET).
  [FIX] create_and_unlock() exige senha com 8+ chars (alinhado com
        secure_store.MIN_PASSWORD_LEN=8). Antes aceitava 4 e falhava
        silenciosamente no save.
  [FIX] pool_stop_mining() limpa _POOL_CLIENT após sucesso (antes
        ficava com referência viva mas is_running()=False, deixando
        o estado ambíguo para o próximo start).
v3.8 (2026-10-22):
  [FIX CRÍTICO] sync_info() validava `"erro"` mas /api/sync-info
                retorna `"error"` em falha. Corrigido (parcialmente —
                faltava o caminho de falha de rede).
  [FIX CRÍTICO] generate_wallet() agora persiste em current_wallet.enc.
  [FIX] requests import no topo com fallback.
  [FIX] _post/_get com retry (2 tentativas, backoff 0.5s).
  [FIX] lock_wallet() chama WalletManager.lock_session().
  [FIX] load_wallet() atualiza _WALLET_SESSION.
  [FIX] pool_start_mining() limpa _POOL_CLIENT em falha.
  [FIX] minhas_txs(limit=...) parametrizável (até 500).
  [NEW] _resolve_pool_url(), get_version(), get_debug_info(),
        --self-test, __version__.
v3.7:
  - minhas_txs com timeout 60s (era 30s)
v3.6:
  - bridge_claim e faucet com timeout 200s (era 30s)
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

__version__ = "3.8.1"

# Import de requests no topo com fallback claro
try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    requests = None  # type: ignore
    _HAS_REQUESTS = False


log = logging.getLogger("wallet-bridge")

BASE_DIR     = Path(__file__).resolve().parent
API_BASE     = os.environ.get("BRN_API_BASE", "http://127.0.0.1:5000").rstrip("/")
API_TIMEOUT  = float(os.environ.get("BRN_API_TIMEOUT", "200"))
GET_TIMEOUT  = float(os.environ.get("BRN_API_GET_TIMEOUT", "30"))
MAX_RETRIES  = int(os.environ.get("BRN_API_RETRIES", "2"))
RETRY_DELAY  = float(os.environ.get("BRN_API_RETRY_DELAY", "0.5"))


_POOL_CLIENT: object = None
_POOL_LOCK = threading.RLock()
_WALLET_SESSION: dict = {}


# ============================================================
# HTTP HELPERS (com retry)
# ============================================================
def _post(path: str, body: dict, timeout: float = None,
          retries: int = None) -> dict:
    """
    POST JSON com retry em ConnectionError transitório.
    Retorna dict (nunca levanta).
    """
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
            break  # erro não-transitório

        if attempt < r_n:
            log.debug("POST %s: retry %d/%d após %s", path, attempt + 1, r_n, last_err)
            time.sleep(RETRY_DELAY * (attempt + 1))

    # Mensagem amigável
    if "Connection" in last_err or "connection" in last_err:
        return {"ok": False, "msg": "Servidor offline (:5000)"}
    if "timeout" in last_err.lower():
        return {"ok": False, "msg": f"Timeout ({t}s)"}
    return {"ok": False, "msg": last_err or "erro desconhecido"}


def _get(path: str, timeout: float = None,
         retries: int = None) -> dict:
    """
    GET JSON com retry em ConnectionError transitório.
    Retorna dict (nunca levanta).
    Em falha de rede: {"error": "..."}
    """
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
            return {"error": f"Timeout ({t}s)"}  # timeout não-retry
        except requests.exceptions.ConnectionError as e:
            last_err = str(e)
        except Exception as e:
            return {"error": str(e)}

        if attempt < r_n:
            log.debug("GET %s: retry %d/%d após %s", path, attempt + 1, r_n, last_err)
            time.sleep(RETRY_DELAY * (attempt + 1))

    if "Connection" in last_err or "connection" in last_err:
        return {"error": "Servidor offline (:5000)"}
    return {"error": last_err or "erro desconhecido"}


# ============================================================
# WalletApi
# ============================================================
class WalletApi:
    """API exposta ao pywebview via js_api."""

    def __init__(self):
        log.info("WalletApi v%s iniciada (API_BASE=%s)", __version__, API_BASE)
        if not _HAS_REQUESTS:
            log.warning("requests NÃO instalado — pip install requests")

    # ========================================================
    # SESSÃO DA CARTEIRA
    # ========================================================
    def has_wallet(self) -> dict:
        """
        True se current_wallet.enc existe no disco.
        [FIX v3.8.1] Respeita secure_store.CURRENT_WALLET_ENC.
        """
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
        """
        Cria carteira nova + persiste em current_wallet.enc.
        [FIX v3.8.1] Exige 8+ chars (alinhado com secure_store).
        """
        global _WALLET_SESSION
        if not password:
            return {"ok": False, "msg": "A senha é obrigatória."}
        if len(password) < 8:
            return {"ok": False,
                    "msg": "A senha deve ter pelo menos 8 caracteres."}
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
                return {"ok": False,
                        "msg": "Erro ao salvar: " + str(r.get("msg", ""))}

            _WALLET_SESSION.update(data)
            log.info("Nova carteira criada: %s...", w.address[:16])
            return {"ok": True, "address": w.address}
        except Exception as e:
            log.exception("Erro ao criar carteira")
            return {"ok": False, "msg": str(e)}

    def unlock_wallet(self, password: str) -> dict:
        """Abre current_wallet.enc com a senha."""
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
                return {"ok": True, "address": _WALLET_SESSION["address"]}
            return {"ok": False, "msg": r.get("msg", "Senha incorreta")}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def lock_wallet(self) -> dict:
        """Limpa sessão + WalletManager.lock_session()."""
        global _WALLET_SESSION
        _WALLET_SESSION.clear()
        try:
            from wallet import WalletManager
            WalletManager.lock_session()
        except Exception:
            pass
        return {"ok": True}

    def get_active_wallet(self) -> dict:
        """Retorna dict da sessão atual."""
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
                "variant":       variant,
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
            }
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def sync_info(self) -> dict:
        """
        [FIX v3.8.1] Trata os três casos do endpoint:
          1) falha de rede → _get retorna {"error": "..."}   → traduz
          2) falha interna → {"ok": False, "erro": "..."}    → repassa
          3) sucesso       → {"ok": True, "sync": {...}}      → repassa
        """
        try:
            s = _get("/api/sync-info", timeout=GET_TIMEOUT)
            # Caso 1: _get falhou (rede/timeout) → {"error": ...}
            if "error" in s and "sync" not in s:
                return {"erro": s["error"]}
            # Caso 2: servidor respondeu com erro interno
            if not s.get("ok", True):
                return {"erro": s.get("erro") or s.get("error")
                        or "erro desconhecido"}
            # Caso 3: sucesso
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

    # ========================================================
    # CARTEIRA BÁSICA
    # ========================================================
    def portfolio(self, address: str) -> dict:
        try:
            s = _get(f"/api/portfolio/{address}", timeout=15.0)
            if "error" in s:
                return {"erro": s["error"]}
            return s.get("portfolio", {"BRN": 0, "KYC": 0})
        except Exception as e:
            return {"erro": str(e)}

    def generate_wallet(self, password: str = "") -> dict:
        """
        Gera carteira via /api/nova-carteira e PERSISTE em
        current_wallet.enc (se senha disponível).
        """
        try:
            r = _post("/api/nova-carteira", {})
            if not r.get("success"):
                return {"erro": r.get("error", "falha")}

            address     = r["address"]
            private_key = r["private_key"]
            public_key  = r["public_key"]

            pwd = (
                (password or "").strip()
                or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
                or os.environ.get("BRN_NODE_PASSWORD", "").strip()
            )
            if pwd:
                try:
                    from secure_store import save_current_wallet
                    save_result = save_current_wallet(pwd, {
                        "address":     address,
                        "private_key": private_key,
                        "public_key":  public_key,
                    })
                    if save_result.get("ok"):
                        log.info("Carteira nova persistida: %s...", address[:16])
                        _WALLET_SESSION.update({
                            "address":     address,
                            "private_key": private_key,
                            "public_key":  public_key,
                        })
                    else:
                        log.warning("Falha persistindo carteira: %s",
                                    save_result.get("msg"))
                except Exception as e:
                    log.warning("Falha salvando carteira em disco: %s", e)

            return {
                "address":     address,
                "private_key": private_key,
                "public_key":  public_key,
            }
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
        """
        Carrega carteira salva e atualiza _WALLET_SESSION.
        """
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
            return r or {"ok": False, "msg": "resposta vazia"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ========================================================
    # TRANSAÇÕES
    # ========================================================
    def transfer(self, from_addr: str, to_addr: str, asset: str,
                 amount, sk: str, pk: str) -> dict:
        try:
            return _post("/api/transfer", {
                "from":         from_addr,
                "to":           to_addr,
                "asset_id":     asset or "BRN",
                "amount":       str(amount),
                "private_key":  sk,
                "pubkey":       pk,
            })
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def minhas_txs(self, address: str, limit: int = 50) -> dict:
        """
        Parametrizável (server aceita até 500).
        """
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

    # ========================================================
    # MINERAÇÃO (Miner local)
    # ========================================================
    def start_mining(self, address: str, pubkey: str) -> dict:
        try:
            return _post("/api/miner/start", {
                "address":      address,
                "miner_pubkey": pubkey,
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
            return _get("/api/miner/status", timeout=GET_TIMEOUT)
        except Exception as e:
            return {"running": False, "error": str(e)}

    # ========================================================
    # POOL
    # ========================================================
    def _resolve_pool_url(self) -> str:
        """
        Prefere BRN_POOL_URL_LOCAL (origin), depois BRN_POOL_URL
        (cliente), por fim API_BASE.
        """
        local = os.environ.get("BRN_POOL_URL_LOCAL", "").strip()
        remote = os.environ.get("BRN_POOL_URL", "").strip()
        if local:
            return local.rstrip("/")
        if remote:
            return remote.rstrip("/")
        return API_BASE

    def pool_start_mining(self, address: str, pubkey: str) -> dict:
        global _POOL_CLIENT
        with _POOL_LOCK:
            if not address or not pubkey:
                return {"ok": False, "msg": "address e pubkey obrigatorios"}
            try:
                from miner_pool_client import PoolClient
            except ImportError as e:
                return {"ok": False,
                        "msg": f"miner_pool_client indisponivel: {e}"}

            # Já tem um rodando com mesmo address?
            if _POOL_CLIENT is not None and _POOL_CLIENT.is_running():
                if _POOL_CLIENT.address == address:
                    return {"ok": True, "msg": "ja minerando",
                            "miner_id": _POOL_CLIENT.miner_id}
                try:
                    _POOL_CLIENT.stop_mining()
                except Exception:
                    pass
                _POOL_CLIENT = None

            pool_url = self._resolve_pool_url()
            try:
                _POOL_CLIENT = PoolClient(
                    address=address, pubkey=pubkey, pool_url=pool_url,
                )
                ok, msg = _POOL_CLIENT.start_mining()
                if ok:
                    return {"ok": True,
                            "msg": "Pool miner iniciado",
                            "miner_id": _POOL_CLIENT.miner_id,
                            "pool_url": pool_url}
                # Falha em start → limpa referência
                _POOL_CLIENT = None
                return {"ok": False, "msg": msg}
            except Exception as e:
                _POOL_CLIENT = None
                return {"ok": False, "msg": str(e)}

    def pool_stop_mining(self) -> dict:
        """
        [FIX v3.8.1] Limpa _POOL_CLIENT após sucesso para o próximo
        start começar do zero (antes ficava referência viva mas
        is_running()=False).
        """
        global _POOL_CLIENT
        with _POOL_LOCK:
            if _POOL_CLIENT is None:
                return {"ok": False, "msg": "nao esta a minerar"}
            try:
                ok, msg = _POOL_CLIENT.stop_mining()
            except Exception as e:
                _POOL_CLIENT = None
                return {"ok": False, "msg": str(e)}
            if ok:
                _POOL_CLIENT = None
            return {"ok": ok, "msg": msg}

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
                "address":     address,
                "private_key": sk,
                "pubkey":      pk,
            })
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def bridge_claim(self, txid: str, address: str) -> dict:
        try:
            return _post("/api/l2/claim", {
                "btc_txid":  txid,
                "buyer_brn": address,
            })
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ========================================================
    # DIAGNÓSTICO
    # ========================================================
    def ping(self) -> dict:
        try:
            return _get("/health", timeout=5.0)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_version(self) -> dict:
        """Versão da ponte + API_BASE."""
        return {
            "version":  __version__,
            "api_base": API_BASE,
            "has_requests": _HAS_REQUESTS,
        }

    def get_debug_info(self) -> dict:
        """Estado interno para troubleshooting."""
        return {
            "version":  __version__,
            "api_base": API_BASE,
            "has_requests": _HAS_REQUESTS,
            "wallet_locked": not bool(_WALLET_SESSION.get("address")),
            "wallet_address": _WALLET_SESSION.get("address", ""),
            "pool_client_running": bool(
                _POOL_CLIENT is not None and _POOL_CLIENT.is_running()
            ),
            "pool_url_resolved": self._resolve_pool_url(),
            "env": {
                "BRN_IS_ORIGIN":       os.environ.get("BRN_IS_ORIGIN", ""),
                "BRN_POOL_URL":        os.environ.get("BRN_POOL_URL", ""),
                "BRN_POOL_URL_LOCAL":  os.environ.get("BRN_POOL_URL_LOCAL", ""),
                "BRN_WALLET_SESSION_PASS_set": bool(
                    os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
                ),
            },
        }


# ============================================================
# CLI SELF-TEST
# ============================================================
def _self_test() -> int:
    """Valida a ponte sem GUI."""
    print(f"app_wallet_v3.py v{__version__}")
    print("=" * 60)
    print(f"API_BASE:      {API_BASE}")
    print(f"requests:      {'OK' if _HAS_REQUESTS else 'AUSENTE'}")
    print(f"MAX_RETRIES:   {MAX_RETRIES}")
    print()

    if not _HAS_REQUESTS:
        print("[X] requests nao instalado. Rode: pip install requests")
        return 1

    api = WalletApi()

    # 1. Ping /health
    print("[1] ping /health...")
    r = api.ping()
    if r.get("ok"):
        print(f"    OK — height={r.get('height')} v={r.get('version', '?')}")
    else:
        print(f"    [X] FALHOU: {r}")
        return 1

    # 2. node_status
    print("\n[2] node_status...")
    r = api.node_status()
    if r.get("ok"):
        print(f"    height={r.get('height')} peers={r.get('peers')} "
              f"diff={r.get('difficulty')} des={r.get('rede_descentralizada')}")
    else:
        print(f"    [X] FALHOU: {r}")

    # 3. sync_info
    print("\n[3] sync_info...")
    r = api.sync_info()
    if "erro" in r:
        print(f"    [X] FALHOU: {r['erro']}")
    else:
        sync = r.get("sync", {})
        print(f"    height={sync.get('height')} target={sync.get('target')} "
              f"percent={sync.get('percent')}%")

    # 4. version
    print("\n[4] get_version...")
    print(f"    {api.get_version()}")

    # 5. debug_info
    print("\n[5] get_debug_info...")
    info = api.get_debug_info()
    print(f"    wallet_locked={info['wallet_locked']} "
          f"pool_running={info['pool_client_running']}")
    print(f"    pool_url={info['pool_url_resolved']}")

    # 6. has_wallet
    print("\n[6] has_wallet...")
    r = api.has_wallet()
    print(f"    exists={r.get('exists')}")

    print()
    print("=" * 60)
    print(" SELF-TEST CONCLUIDO")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    p = argparse.ArgumentParser(prog="app_wallet_v3.py")
    p.add_argument("--self-test", action="store_true",
                   help="Valida a ponte sem GUI")
    p.add_argument("--version", action="store_true",
                   help="Mostra versão e sai")
    args = p.parse_args()

    if args.version:
        print(f"app_wallet_v3.py v{__version__}")
        sys.exit(0)

    if args.self_test:
        sys.exit(_self_test())

    print("Uso: python app_wallet_v3.py --self-test")
    print("     (a GUI usa via pywebview: from app_wallet_v3 import WalletApi)")
    sys.exit(0)
