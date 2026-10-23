"""
server.py — Backend HTTP do nó BRN (v9.9.14)
================================================================
v9.9.14 (2026-10-23):
  [NEW] Proxy do Pool: se o nó for Cliente (sem PoolManager local),
        as rotas /api/pool/* redirecionam automaticamente para o
        BRN_POOL_URL (Ubuntu/Origem). Resolve o erro "pool nao disponivel".
  [NEW] TrustNetwork integrada: rotas /api/trust/stats e /api/trust/nodes.
v9.9.13 (2026-10-22):
  [FIX CRÍTICO] /api/minhas-txs respeita ?limit= (default 200, max 500).
  [FIX] /api/miner/start agora persiste meta["miner_address"] no DB.
================================================================
"""
from __future__ import annotations

import os
import time
import json
import atexit
import hmac
import logging
import sys
import threading
import urllib.request
import urllib.error
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from functools import wraps
from pathlib import Path

from flask import Flask, request, jsonify
from flask_cors import CORS

from wallet import Wallet, WalletManager

try:
    from wallet import HDWalletManager
    _HAS_HD = True
except Exception as _hd_err:
    HDWalletManager = None
    _HAS_HD = False
    print(f"[server] HDWalletManager indisponível: {_hd_err}", file=sys.stderr)

from blockchain import Blockchain, make_coinbase, txid as calc_txid, signing_hash

try:
    from blockchain import MIN_RELAY_FEE
except ImportError:
    MIN_RELAY_FEE = 1000

try:
    from security import (
        require_rate_limit, audit_log, close_audit,
        hash_short, get_client_ip,
    )
    _HAS_SECURITY = True
except ImportError:
    _HAS_SECURITY = False

    def require_rate_limit(name):
        def deco(fn):
            return fn
        return deco

    def audit_log(event, data=None):
        pass

    def close_audit():
        pass

    def hash_short(v, n=12):
        return str(v)[:n] if v else ""

    def get_client_ip():
        return request.remote_addr or "0.0.0.0"


log = logging.getLogger("server")

BASE_DIR              = Path(__file__).resolve().parent
WEB_HOST              = os.environ.get("BRN_WEB_HOST", "127.0.0.1")
BEHIND_PROXY          = os.environ.get("BRN_BEHIND_PROXY", "0") == "1"
CORS_ORIGINS          = [
    o.strip() for o in os.environ.get("BRN_CORS_ORIGIN", "").split(",") if o.strip()
]
ADMIN_TOKEN           = os.environ.get("BRN_ADMIN_TOKEN", "").strip()
WALLETS_FILE          = str(BASE_DIR / "user_wallets.json")
CURRENT_WALLET_FILE   = str(BASE_DIR / "current_wallet.json")
FAUCET_AMOUNT_BRN     = 10
FAUCET_MAX_PER_ADDRESS = 3
FAUCET_COOLDOWN_S     = 60 * 60
CACHE_TTL_S           = 5

TXT_HISTORY_LIMIT_DEFAULT = 200
TXT_HISTORY_LIMIT_MAX     = 500

BOOTSTRAP_HTTP_PORT   = int(
    os.environ.get(
        "BRN_BOOTSTRAP_HTTP_PORT",
        os.environ.get("BRN_WEB_PORT", "5000"),
    )
)


def _notify_bootstrap_peers(addr: str, pubkey: str,
                             log_prefix: str = "") -> None:
    bootstrap_peers = os.environ.get("BRN_BOOTSTRAP_PEERS", "").split(",")
    for b in bootstrap_peers:
        b = b.strip()
        if not b:
            continue
        try:
            s = b
            scheme = "http"
            if "/" in s:
                s, _, scheme_part = s.partition("/")
                scheme = (scheme_part or "http").strip().lower()
                if scheme not in ("http", "https"):
                    scheme = "http"
            parts = s.split(":")
            if len(parts) < 2:
                continue
            host = parts[0]
            try:
                http_port = (
                    int(parts[2]) if len(parts) >= 3
                    else BOOTSTRAP_HTTP_PORT
                )
            except ValueError:
                http_port = BOOTSTRAP_HTTP_PORT

            url = f"{scheme}://{host}:{http_port}/api/bootstrap/add"
            req = urllib.request.Request(
                url,
                data=json.dumps({
                    "address": addr, "pubkey": pubkey,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "User-Agent": "brn-server/9.9.14"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                log.info(
                    f"[BOOTSTRAP] {log_prefix}aviso enviado para "
                    f"{host}:{http_port}"
                )
        except Exception as e:
            log.warning(
                f"[BOOTSTRAP] Nao foi possivel avisar {b} "
                f"({log_prefix}): {e}"
            )


# ============================================================
# CHAIN SINGLETON
# ============================================================
_CHAIN = None
CHAIN_LOCK = threading.RLock()


def set_chain(chain) -> None:
    global _CHAIN
    with CHAIN_LOCK:
        _CHAIN = chain
    log.info("server: chain injetada (height=%s)", chain.db.height())


def _get_chain():
    global _CHAIN
    if _CHAIN is not None:
        return _CHAIN
    with CHAIN_LOCK:
        if _CHAIN is None:
            from blockchain import Blockchain as _BC
            _CHAIN = _BC(str(BASE_DIR / "brn_v2_chain.db"))
            log.warning("server: chain NÃO injetada — criei uma local")
        return _CHAIN


class _LazyChain:
    def __getattr__(self, name):
        return getattr(_get_chain(), name)


CHAIN = _LazyChain()


# ============================================================
# P2P / POOL / SYNC / TRUST MANAGERS
# ============================================================
_P2P_MANAGER = None
_P2P_LOCK = threading.Lock()


def set_p2p_manager(p2p) -> None:
    global _P2P_MANAGER
    with _P2P_LOCK:
        _P2P_MANAGER = p2p
    log.info(f"server: P2PManager {'injetado' if p2p else 'removido'}")


def _get_p2p_manager():
    return _P2P_MANAGER


_POOL_MANAGER = None
_POOL_LOCK = threading.Lock()


def set_pool_manager(pool) -> None:
    global _POOL_MANAGER
    with _POOL_LOCK:
        _POOL_MANAGER = pool
    log.info(f"server: PoolManager {'injetado' if pool else 'removido'}")


def _get_pool_manager():
    return _POOL_MANAGER


_SYNC_MANAGER = None
_SYNC_LOCK = threading.Lock()


def set_sync_manager(sync) -> None:
    global _SYNC_MANAGER
    with _SYNC_LOCK:
        _SYNC_MANAGER = sync
    log.info("server: sync_manager injetado")


def _get_sync_manager():
    return _SYNC_MANAGER


_TRUST_NETWORK = None
_TRUST_LOCK = threading.Lock()


def set_trust_network(trust) -> None:
    global _TRUST_NETWORK
    with _TRUST_LOCK:
        _TRUST_NETWORK = trust
    log.info(f"server: TrustNetwork {'injetada' if trust else 'removida'}")


def _get_trust_network():
    return _TRUST_NETWORK


# ============================================================
# L2 MANAGER
# ============================================================
try:
    from l2_manager import L2Manager, L2Error
    import btc_config
    L2_ENABLED = True
except ImportError:
    L2_ENABLED = False
    L2Manager = None
    L2Error = Exception
    btc_config = None

_l2_mgr = None
_l2_lock = threading.Lock()


def _get_l2():
    global _l2_mgr
    if not L2_ENABLED:
        return None
    with _l2_lock:
        if _l2_mgr is None:
            try:
                try:
                    _l2_mgr = L2Manager(_get_chain().db, _get_chain())
                except TypeError:
                    _l2_mgr = L2Manager(_get_chain(), _get_chain().db)
                log.info("L2Manager inicializado")
            except Exception as e:
                log.warning(f"L2Manager erro: {e}")
                _l2_mgr = None
        return _l2_mgr


# ============================================================
# APP
# ============================================================
app = Flask(__name__)

if BEHIND_PROXY:
    try:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    except ImportError:
        pass

if CORS_ORIGINS:
    CORS(app, origins=CORS_ORIGINS)


# ============================================================
# ADMIN AUTH
# ============================================================
def _require_admin() -> bool:
    if not ADMIN_TOKEN:
        return True
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Bearer "):
        return False
    return hmac.compare_digest(hdr[7:], ADMIN_TOKEN)


def _admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _require_admin():
            audit_log("admin_denied", {
                "path": request.path, "ip": get_client_ip(),
            })
            return jsonify({"ok": False, "msg": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


# ============================================================
# CACHE DE SALDOS
# ============================================================
_cache_saldos: dict = {}
_cache_lock = threading.Lock()
_faucet_history: dict = {}


def _saldo_cache_get(addr):
    with _cache_lock:
        if addr in _cache_saldos:
            ts, val = _cache_saldos[addr]
            if time.time() - ts < CACHE_TTL_S:
                return val
    return None


def _saldo_cache_set(addr, val):
    with _cache_lock:
        _cache_saldos[addr] = (time.time(), val)


def _saldo_cache_invalidate(addr):
    with _cache_lock:
        _cache_saldos.pop(addr, None)


# ============================================================
# WALLETS FILE
# ============================================================
_wallets_lock = threading.Lock()


def carregar_wallets() -> dict:
    if not os.path.exists(WALLETS_FILE):
        return {}
    try:
        with open(WALLETS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def salvar_wallets(w: dict) -> None:
    with _wallets_lock:
        tmp = f"{WALLETS_FILE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(w, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, WALLETS_FILE)


def _registrar_pubkey(addr, pk):
    if not addr or not pk:
        return
    try:
        wallets = carregar_wallets()
        if wallets.get(addr, {}).get("public_key") != pk:
            wallets[addr] = {"public_key": pk}
            salvar_wallets(wallets)
    except Exception:
        pass


def auto_registrar_carteira_atual() -> dict:
    senha = (
        os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
        or os.environ.get("BRN_NODE_PASSWORD", "").strip()
    )
    if not senha:
        return {"ok": False, "msg": "BRN_WALLET_SESSION_PASS/BRN_NODE_PASSWORD vazias"}

    result = None
    try:
        _WM = WalletManager
        if hasattr(_WM, "load_current"):
            result = _WM.load_current(senha)
    except Exception:
        result = None

    if result is None:
        try:
            from secure_store import load_current_wallet
            result = load_current_wallet(senha)
        except Exception as e:
            return {"ok": False, "msg": f"secure_store falhou: {e}"}

    if not result or not result.get("ok"):
        return {"ok": False,
                "msg": (result or {}).get("msg", "current_wallet.enc não encontrado")}

    addr = (result.get("address") or "").strip()
    pk = (result.get("public_key")
          or result.get("pubkey")
          or result.get("pub_hex")
          or "").strip()
    if not addr or not pk:
        return {"ok": False, "msg": "current_wallet.enc sem address/public_key"}

    try:
        wallets = carregar_wallets()
        atual = wallets.get(addr, {}).get("public_key", "")
        if atual == pk:
            return {"ok": True, "msg": f"já registrado: {addr[:16]}..."}
        wallets[addr] = {"public_key": pk}
        salvar_wallets(wallets)
        return {"ok": True,
                "msg": f"registrado: {addr[:16]}... pub={pk[:16]}..."}
    except Exception as e:
        return {"ok": False, "msg": f"falha gravando user_wallets.json: {e}"}


def _pubkey_from_db_or_payload(addr: str, payload_pubkey: str = "",
                                payload_sk: str = "") -> str:
    if payload_pubkey:
        return payload_pubkey.strip()
    if payload_sk:
        try:
            _w = Wallet(private_key_hex=payload_sk.strip())
            if _w.address == addr:
                pk = _w.pub_hex
                _registrar_pubkey(addr, pk)
                return pk
        except Exception:
            pass
    try:
        if os.path.exists(CURRENT_WALLET_FILE):
            with open(CURRENT_WALLET_FILE, "r", encoding="utf-8") as f:
                cw = json.load(f)
            if (cw.get("address") or "").strip() == addr:
                pk = (cw.get("public_key") or cw.get("pubkey") or "").strip()
                if pk:
                    _registrar_pubkey(addr, pk)
                    return pk
    except Exception:
        pass
    try:
        wallets = carregar_wallets()
        entry = wallets.get(addr) or {}
        pk = (entry.get("public_key") or entry.get("pubkey") or "").strip()
        if pk:
            return pk
    except Exception:
        pass
    try:
        for u in _get_chain().db.get_utxos(addr):
            pk = (u.get("pubkey") or "").strip()
            if pk:
                return pk
    except Exception:
        pass
    return ""


# ============================================================
# HELPERS DE TX
# ============================================================
def _find_tx_in_mempool(txid_str: str):
    try:
        return _get_chain().db.get_mempool_tx(txid_str)
    except Exception:
        return None


def _find_tx_in_chain(txid_str: str):
    try:
        return _get_chain().db.get_tx_by_txid(txid_str)
    except Exception:
        return None, None


# ============================================================
# CLEANUP LOOP
# ============================================================
def _cleanup_loop():
    while True:
        time.sleep(300)
        agora = time.time()
        try:
            with _cache_lock:
                for a in [k for k, (ts, _) in _cache_saldos.items()
                          if agora - ts > CACHE_TTL_S * 10]:
                    del _cache_saldos[a]
            for a in [k for k, v in _faucet_history.items()
                      if not v or agora - v[-1] > FAUCET_COOLDOWN_S]:
                del _faucet_history[a]
        except Exception as e:
            log.warning(f"cleanup: {e}")


threading.Thread(target=_cleanup_loop, daemon=True,
                 name="CacheCleanup").start()


# ============================================================
# BOOTSTRAP / A/B TEST
# ============================================================
@app.route("/api/bootstrap/add", methods=["POST"])
@require_rate_limit("bootstrap_add")
def bootstrap_add():
    try:
        data = request.get_json(force=True) or {}
        addr = (data.get("address") or "").strip()
        pubkey = (data.get("pubkey") or "").strip()
        if not addr or not pubkey:
            return jsonify({"ok": False, "msg": "Dados incompletos"}), 400
        chain = _get_chain()
        chain.adicionar_na_fila(addr, pubkey)
        return jsonify({
            "ok": True,
            "fila_tamanho": len(chain.fila_mineracao),
            "rede_descentralizada": chain.rede_descentralizada,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/experiment/variant", methods=["GET"])
@require_rate_limit("experiment")
def get_experiment_variant():
    try:
        from ab_test import get_variant
        import security as _sec
        node_hash = getattr(_sec, "_node_id_hash", "unknown")
        return jsonify({"ok": True, "variant": get_variant(node_hash)})
    except Exception as e:
        return jsonify({"ok": False, "variant": "A", "msg": str(e)}), 500


# ============================================================
# CARTEIRA
# ============================================================
@app.route("/api/nova-carteira", methods=["POST"])
@require_rate_limit("nova_carteira")
def nova_carteira():
    try:
        w = Wallet()
        audit_log("wallet_created",
                  {"address": hash_short(w.address, 16)})
        dados = {
            "success": True,
            "address": w.address,
            "private_key": w.priv_hex,
            "public_key": w.pub_hex,
            "warning": "Guarde a chave privada.",
        }
        wallets = carregar_wallets()
        wallets[w.address] = {"public_key": w.pub_hex}
        salvar_wallets(wallets)
        return jsonify(dados)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/saldo/<address>", methods=["GET"])
def saldo(address):
    try:
        cached = _saldo_cache_get(address)
        if cached is None:
            utxos = _get_chain().db.get_utxos(address)
            cached = sum(u["amount"] for u in utxos)
            _saldo_cache_set(address, cached)
        return jsonify({
            "success": True,
            "address": address,
            "balance_sats": cached,
            "balance_brn": cached / 10**8,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/portfolio/<address>", methods=["GET"])
def portfolio(address):
    try:
        cached = _saldo_cache_get(address)
        if cached is None:
            utxos = _get_chain().db.get_utxos(address)
            cached = sum(u["amount"] for u in utxos)
            _saldo_cache_set(address, cached)
        return jsonify({"portfolio": {"BRN": cached / 10**8, "KYC": 0}})
    except Exception as e:
        return jsonify({"portfolio": {}, "error": str(e)}), 500


# ============================================================
# TX STATUS / HISTÓRICO
# ============================================================
@app.route("/api/tx-status/<txid_str>", methods=["GET"])
def tx_status(txid_str):
    try:
        if not txid_str or len(txid_str) != 64:
            return jsonify({"ok": False, "msg": "TXID invalido"}), 400
        mem_tx = _find_tx_in_mempool(txid_str)
        if mem_tx:
            return jsonify({
                "ok": True, "txid": txid_str, "status": "pending",
                "block_height": None, "confirmations": 0,
            })
        chain_tx, block_height = _find_tx_in_chain(txid_str)
        if chain_tx is not None:
            altura = _get_chain().db.height()
            confs = max(0, altura - block_height)
            return jsonify({
                "ok": True, "txid": txid_str, "status": "confirmed",
                "block_height": block_height, "confirmations": confs,
                "timestamp": chain_tx.get("timestamp", 0),
            })
        return jsonify({
            "ok": True, "txid": txid_str, "status": "not_found",
            "block_height": None, "confirmations": 0,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/verificar-recebimento/<address>/<txid_str>", methods=["GET"])
def verificar_recebimento(address, txid_str):
    try:
        if not WalletManager.validate_address(address):
            return jsonify({"ok": False, "msg": "Endereco invalido"}), 400
        if not txid_str or len(txid_str) != 64:
            return jsonify({"ok": False, "msg": "TXID invalido"}), 400
        mem_tx = _find_tx_in_mempool(txid_str)
        if mem_tx:
            valor_mem = sum(
                o.get("amount", 0) for o in mem_tx.get("outputs", [])
                if o.get("address") == address
            )
            return jsonify({
                "ok": True, "chegou": False, "status": "pending",
                "valor": valor_mem, "confirmacoes": 0, "block_height": None,
                "msg": ("Transacao esta na mempool." if valor_mem
                        else "Endereco nao esta nos outputs."),
            })
        chain_tx, block_height = _find_tx_in_chain(txid_str)
        if chain_tx is None:
            return jsonify({
                "ok": True, "chegou": False, "status": "not_found",
                "valor": 0, "confirmacoes": 0, "block_height": None,
                "msg": "TXID nao encontrado.",
            })
        valor = sum(
            o.get("amount", 0) for o in chain_tx.get("outputs", [])
            if o.get("address") == address
        )
        altura = _get_chain().db.height()
        confs = max(0, altura - block_height)
        chegou = valor > 0
        msg = (
            f"Confirmado no bloco #{block_height} com {confs} confirmacoes."
            if chegou else "Endereco nao esta nos outputs."
        )
        return jsonify({
            "ok": True, "chegou": chegou, "status": "confirmed",
            "valor": valor, "confirmacoes": confs,
            "block_height": block_height, "msg": msg,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/minhas-txs/<address>", methods=["GET"])
@app.route("/api/transacoes/<address>", methods=["GET"])
@require_rate_limit("minhas_txs")
def minhas_txs(address):
    try:
        if not WalletManager.validate_address(address):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400
        chain = _get_chain()
        try:
            current_height = chain.db.height()
        except Exception:
            current_height = 0
        if not hasattr(chain.db, "get_txs_for_address"):
            return jsonify({
                "ok": True, "address": address, "count": 0,
                "transactions": [],
                "msg": "Endpoint de histórico precisa do db.py v6.4+",
            })

        try:
            _limit = int(request.args.get("limit", TXT_HISTORY_LIMIT_DEFAULT))
        except (ValueError, TypeError):
            _limit = TXT_HISTORY_LIMIT_DEFAULT
        _limit = max(1, min(_limit, TXT_HISTORY_LIMIT_MAX))

        raw_list = chain.db.get_txs_for_address(address, limit=_limit)
        transactions = []
        for item in raw_list:
            tx = item["tx"]
            block_height = item["block_height"]
            is_mempool = item["is_mempool"]

            outputs_to_addr = sum(
                int(o.get("amount", 0) or 0)
                for o in tx.get("outputs", [])
                if o.get("address") == address
            )
            inputs_from_addr = 0
            for inp in tx.get("inputs", []):
                if inp.get("txid") == "0" * 64:
                    continue
                try:
                    row = chain.db.conn.execute(
                        "SELECT address, amount FROM utxos "
                        "WHERE txid = ? AND vout = ?",
                        (inp.get("txid"), inp.get("vout")),
                    ).fetchone()
                    if row and row["address"] == address:
                        inputs_from_addr += int(row["amount"] or 0)
                except Exception:
                    pass

            if inputs_from_addr > 0 and outputs_to_addr > 0:
                direction, amount = "self", 0
            elif outputs_to_addr > 0:
                direction, amount = "received", outputs_to_addr
            else:
                direction, amount = "sent", max(0, inputs_from_addr - outputs_to_addr)

            if is_mempool or block_height is None:
                status, confirmations = "pending", 0
            else:
                status = "confirmed"
                confirmations = max(0, current_height - block_height)

            transactions.append({
                "txid": tx.get("txid", ""),
                "direction": direction,
                "amount": amount,
                "status": status,
                "block_height": block_height,
                "confirmations": confirmations,
                "timestamp": int(tx.get("timestamp", 0) or 0),
                "num_inputs": len(tx.get("inputs", [])),
                "num_outputs": len(tx.get("outputs", [])),
            })

        return jsonify({
            "ok": True, "address": address,
            "count": len(transactions),
            "limit": _limit,
            "transactions": transactions,
        })
    except Exception as e:
        log.exception("minhas_txs falhou")
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# MEMPOOL / PEERS
# ============================================================
@app.route("/api/mempool", methods=["GET"])
@require_rate_limit("status")
def get_mempool():
    try:
        chain = _get_chain()
        txs = chain.db.all_mempool(limit=10000)
        resumo = []
        for t in txs:
            resumo.append({
                "txid": t.get("txid", ""),
                "num_inputs": len(t.get("inputs", [])),
                "num_outputs": len(t.get("outputs", [])),
                "total_out": sum(o.get("amount", 0)
                                  for o in t.get("outputs", [])),
                "timestamp": t.get("timestamp", 0),
                "nonce": t.get("nonce", 0),
            })
        return jsonify({"ok": True, "count": len(resumo), "txs": resumo})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/peers/discovered", methods=["GET"])
@require_rate_limit("peers_discovered")
def peers_discovered():
    try:
        chain = _get_chain()
        rows = chain.db.listar_peers(apenas_ativos=False)
        out, seen_ips = [], set()
        for p in rows:
            addr = (p.get("address") or "").strip()
            if not addr:
                continue
            ip = addr.rsplit(":", 1)[0] if ":" in addr else addr
            ip = ip.strip("[]")
            if not ip or ip.startswith("127.") or ip == "0.0.0.0" or ip in seen_ips:
                continue
            seen_ips.add(ip)
            out.append({
                "ip": ip,
                "address": addr,
                "node_id": (p.get("node_id") or "")[:16],
                "height": int(p.get("height", 0) or 0),
                "last_seen": int(p.get("last_seen", 0) or 0),
                "score": int(p.get("score", 0) or 0),
                "version": p.get("version", "?"),
            })
        out.sort(key=lambda x: x["last_seen"], reverse=True)
        return jsonify({"ok": True, "count": len(out), "peers": out})
    except Exception as e:
        log.exception("peers_discovered falhou")
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# TRANSFER
# ============================================================
@app.route("/api/transfer", methods=["POST"])
@app.route("/api/send", methods=["POST"])
@app.route("/api/enviar", methods=["POST"])
@require_rate_limit("transfer")
def transfer():
    try:
        data = request.get_json(force=True) or {}
        sender = (data.get("from") or "").strip()
        to = (data.get("to") or "").strip()
        asset_id = data.get("asset_id", "BRN")
        amount_raw = data.get("amount")
        sk = (request.headers.get("X-BRN-Sk", "")
              or data.get("private_key", ""))

        audit_log("transfer_attempt", {
            "from": hash_short(sender, 16),
            "to": hash_short(to, 16),
            "asset": asset_id,
        })

        if asset_id != "BRN":
            return jsonify({"ok": False, "msg": "So BRN."}), 400
        if not WalletManager.validate_address(sender):
            return jsonify({"ok": False, "msg": "Remetente invalido."}), 400
        if not WalletManager.validate_address(to):
            return jsonify({"ok": False, "msg": "Destinatario invalido."}), 400
        if sender == to:
            return jsonify({"ok": False, "msg": "Nao pode enviar para si."}), 400
        if amount_raw is None or str(amount_raw).strip() == "":
            return jsonify({"ok": False, "msg": "Valor vazio."}), 400

        try:
            amount_dec = Decimal(str(amount_raw).strip().replace(",", "."))
        except (InvalidOperation, ValueError, TypeError):
            return jsonify({"ok": False, "msg": "Valor invalido."}), 400

        if amount_dec <= 0:
            return jsonify({"ok": False, "msg": "Valor deve ser > 0."}), 400

        amount_dec = amount_dec.quantize(
            Decimal("0.00000001"), rounding=ROUND_DOWN)
        amount_sats = int(amount_dec * Decimal(10**8))
        if amount_sats <= 0:
            return jsonify({"ok": False, "msg": "Valor menor que 1 satoshi."}), 400

        try:
            w = Wallet(private_key_hex=sk)
        except Exception:
            return jsonify({"ok": False, "msg": "Chave privada invalida."}), 400

        if w.address != sender:
            return jsonify({"ok": False, "msg": "Chave privada nao corresponde."}), 400

        FEE = int(MIN_RELAY_FEE)
        chain = _get_chain()

        with CHAIN_LOCK:
            utxos = chain.db.get_utxos(sender)
            utxos.sort(key=lambda u: u["amount"], reverse=True)
            total, escolhidos = 0, []
            for u in utxos:
                escolhidos.append(u)
                total += u["amount"]
                if total >= amount_sats + FEE:
                    break

            if total < amount_sats + FEE:
                return jsonify({
                    "ok": False,
                    "msg": (f"Saldo insuficiente: tem {total/
