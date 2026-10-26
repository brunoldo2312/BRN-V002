"""
server.py — Backend HTTP do nó BRN (v9.9.16)
================================================================
v9.9.16 (2026-10-25):
  [FIX CRÍTICO] Re-adicionada a rota /api/minhas-txs que havia sido
                removida por engano na v9.9.15, causando erro 404.
  [FIX] Mantidas as rotas /api/sync-info e /api/miner/status.
v9.9.15 (2026-10-24):
  [NEW] Integração robusta com trust_system.py e proxy do pool.
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
        def deco(fn): return fn
        return deco

    def audit_log(event, data=None): pass
    def close_audit(): pass
    def hash_short(v, n=12): return str(v)[:n] if v else ""
    def get_client_ip(): return request.remote_addr or "0.0.0.0"


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


def _notify_bootstrap_peers(addr: str, pubkey: str, log_prefix: str = "") -> None:
    bootstrap_peers = os.environ.get("BRN_BOOTSTRAP_PEERS", "").split(",")
    for b in bootstrap_peers:
        b = b.strip()
        if not b: continue
        try:
            s = b
            scheme = "http"
            if "/" in s:
                s, _, scheme_part = s.partition("/")
                scheme = (scheme_part or "http").strip().lower()
                if scheme not in ("http", "https"): scheme = "http"
            parts = s.split(":")
            if len(parts) < 2: continue
            host = parts[0]
            try:
                http_port = int(parts[2]) if len(parts) >= 3 else BOOTSTRAP_HTTP_PORT
            except ValueError:
                http_port = BOOTSTRAP_HTTP_PORT

            url = f"{scheme}://{host}:{http_port}/api/bootstrap/add"
            req = urllib.request.Request(
                url,
                data=json.dumps({"address": addr, "pubkey": pubkey}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "brn-server/9.9.16"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                log.info(f"[BOOTSTRAP] {log_prefix}aviso enviado para {host}:{http_port}")
        except Exception as e:
            log.warning(f"[BOOTSTRAP] Nao foi possivel avisar {b} ({log_prefix}): {e}")


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
    if _CHAIN is not None: return _CHAIN
    with CHAIN_LOCK:
        if _CHAIN is None:
            from blockchain import Blockchain as _BC
            _CHAIN = _BC(str(BASE_DIR / "brn_v2_chain.db"))
            log.warning("server: chain NÃO injetada — criei uma local")
        return _CHAIN

class _LazyChain:
    def __getattr__(self, name): return getattr(_get_chain(), name)

CHAIN = _LazyChain()


# ============================================================
# P2P / POOL / SYNC / TRUST MANAGERS
# ============================================================
_P2P_MANAGER = None
_P2P_LOCK = threading.Lock()
def set_p2p_manager(p2p) -> None:
    global _P2P_MANAGER
    with _P2P_LOCK: _P2P_MANAGER = p2p
    log.info(f"server: P2PManager {'injetado' if p2p else 'removido'}")
def _get_p2p_manager(): return _P2P_MANAGER


_POOL_MANAGER = None
_POOL_LOCK = threading.Lock()
def set_pool_manager(pool) -> None:
    global _POOL_MANAGER
    with _POOL_LOCK: _POOL_MANAGER = pool
    log.info(f"server: PoolManager {'injetado' if pool else 'removido'}")
def _get_pool_manager(): return _POOL_MANAGER


_SYNC_MANAGER = None
_SYNC_LOCK = threading.Lock()
def set_sync_manager(sync) -> None:
    global _SYNC_MANAGER
    with _SYNC_LOCK: _SYNC_MANAGER = sync
    log.info("server: sync_manager injetado")
def _get_sync_manager(): return _SYNC_MANAGER


_TRUST_NETWORK = None
_TRUST_LOCK = threading.Lock()
def set_trust_network(trust) -> None:
    global _TRUST_NETWORK
    with _TRUST_LOCK: _TRUST_NETWORK = trust
    log.info(f"server: TrustNetwork {'injetada' if trust else 'removida'}")
def _get_trust_network(): return _TRUST_NETWORK


def _register_miner_in_trust(pubkey: str, address: str) -> None:
    trust = _get_trust_network()
    if trust is None: return
    try:
        if hasattr(trust, "ensure_peer_entry"):
            trust.ensure_peer_entry(pubkey)
            trust.update_score(pubkey, "heartbeat_ok")
        elif hasattr(trust, "register_node"):
            trust.register_node(node_id=pubkey, address=address, public_key=pubkey)
            trust.mark_connected(pubkey)
    except Exception as e:
        log.warning(f"[Trust] Falha ao registrar minerador {pubkey[:12]}: {e}")


# ============================================================
# APP
# ============================================================
app = Flask(__name__)

if BEHIND_PROXY:
    try:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    except ImportError: pass

if CORS_ORIGINS:
    CORS(app, origins=CORS_ORIGINS)


# ============================================================
# ADMIN AUTH
# ============================================================
def _require_admin() -> bool:
    if not ADMIN_TOKEN: return True
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Bearer "): return False
    return hmac.compare_digest(hdr[7:], ADMIN_TOKEN)

def _admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _require_admin():
            audit_log("admin_denied", {"path": request.path, "ip": get_client_ip()})
            return jsonify({"ok": False, "msg": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


# ============================================================
# CACHE DE SALDOS E WALLETS
# ============================================================
_cache_saldos: dict = {}
_cache_lock = threading.Lock()
_faucet_history: dict = {}

def _saldo_cache_get(addr):
    with _cache_lock:
        if addr in _cache_saldos:
            ts, val = _cache_saldos[addr]
            if time.time() - ts < CACHE_TTL_S: return val
    return None

def _saldo_cache_set(addr, val):
    with _cache_lock: _cache_saldos[addr] = (time.time(), val)

def _saldo_cache_invalidate(addr):
    with _cache_lock: _cache_saldos.pop(addr, None)


_wallets_lock = threading.Lock()

def carregar_wallets() -> dict:
    if not os.path.exists(WALLETS_FILE): return {}
    try:
        with open(WALLETS_FILE, encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def salvar_wallets(w: dict) -> None:
    with _wallets_lock:
        tmp = f"{WALLETS_FILE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(w, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, WALLETS_FILE)

def _registrar_pubkey(addr, pk):
    if not addr or not pk: return
    try:
        wallets = carregar_wallets()
        if wallets.get(addr, {}).get("public_key") != pk:
            wallets[addr] = {"public_key": pk}
            salvar_wallets(wallets)
    except Exception: pass


def auto_registrar_carteira_atual() -> dict:
    senha = (
        os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
        or os.environ.get("BRN_NODE_PASSWORD", "").strip()
    )
    if not senha: return {"ok": False, "msg": "senhas vazias"}

    result = None
    try:
        if hasattr(WalletManager, "load_current"): result = WalletManager.load_current(senha)
    except Exception: result = None

    if result is None:
        try:
            from secure_store import load_current_wallet
            result = load_current_wallet(senha)
        except Exception as e: return {"ok": False, "msg": f"secure_store falhou: {e}"}

    if not result or not result.get("ok"):
        return {"ok": False, "msg": "current_wallet.enc não encontrado"}

    addr = (result.get("address") or "").strip()
    pk = (result.get("public_key") or result.get("pubkey") or result.get("pub_hex") or "").strip()
    if not addr or not pk: return {"ok": False, "msg": "sem address/public_key"}

    try:
        wallets = carregar_wallets()
        if wallets.get(addr, {}).get("public_key") == pk: return {"ok": True, "msg": f"já registrado: {addr[:16]}"}
        wallets[addr] = {"public_key": pk}
        salvar_wallets(wallets)
        return {"ok": True, "msg": f"registrado: {addr[:16]}"}
    except Exception as e: return {"ok": False, "msg": f"falha: {e}"}


def _pubkey_from_db_or_payload(addr: str, payload_pubkey: str = "", payload_sk: str = "") -> str:
    if payload_pubkey: return payload_pubkey.strip()
    if payload_sk:
        try:
            _w = Wallet(private_key_hex=payload_sk.strip())
            if _w.address == addr:
                pk = _w.pub_hex
                _registrar_pubkey(addr, pk)
                return pk
        except Exception: pass
    try:
        if os.path.exists(CURRENT_WALLET_FILE):
            with open(CURRENT_WALLET_FILE, "r", encoding="utf-8") as f:
                cw = json.load(f)
            if (cw.get("address") or "").strip() == addr:
                pk = (cw.get("public_key") or cw.get("pubkey") or "").strip()
                if pk:
                    _registrar_pubkey(addr, pk)
                    return pk
    except Exception: pass
    try:
        wallets = carregar_wallets()
        entry = wallets.get(addr) or {}
        pk = (entry.get("public_key") or entry.get("pubkey") or "").strip()
        if pk: return pk
    except Exception: pass
    try:
        for u in _get_chain().db.get_utxos(addr):
            pk = (u.get("pubkey") or "").strip()
            if pk: return pk
    except Exception: pass
    return ""


def _find_tx_in_mempool(txid_str: str):
    try: return _get_chain().db.get_mempool_tx(txid_str)
    except Exception: return None


def _find_tx_in_chain(txid_str: str):
    try: return _get_chain().db.get_tx_by_txid(txid_str)
    except Exception: return None, None


# ============================================================
# CLEANUP LOOP
# ============================================================
def _cleanup_loop():
    while True:
        time.sleep(300)
        agora = time.time()
        try:
            with _cache_lock:
                for a in [k for k, (ts, _) in _cache_saldos.items() if agora - ts > CACHE_TTL_S * 10]:
                    del _cache_saldos[a]
            for a in [k for k, v in _faucet_history.items() if not v or agora - v[-1] > FAUCET_COOLDOWN_S]:
                del _faucet_history[a]
        except Exception as e:
            log.warning(f"cleanup: {e}")

threading.Thread(target=_cleanup_loop, daemon=True, name="CacheCleanup").start()


# ============================================================
# ROTAS BÁSICAS (BOOTSTRAP / CARTEIRA / TRANSFER / HISTÓRICO)
# ============================================================
@app.route("/api/bootstrap/add", methods=["POST"])
@require_rate_limit("bootstrap_add")
def bootstrap_add():
    try:
        data = request.get_json(force=True) or {}
        addr = (data.get("address") or "").strip()
        pubkey = (data.get("pubkey") or "").strip()
        if not addr or not pubkey: return jsonify({"ok": False, "msg": "Dados incompletos"}), 400
        chain = _get_chain()
        chain.adicionar_na_fila(addr, pubkey)
        _register_miner_in_trust(pubkey, addr)
        return jsonify({
            "ok": True, "fila_tamanho": len(chain.fila_mineracao),
            "rede_descentralizada": chain.rede_descentralizada,
        })
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/nova-carteira", methods=["POST"])
@require_rate_limit("nova_carteira")
def nova_carteira():
    try:
        w = Wallet()
        audit_log("wallet_created", {"address": hash_short(w.address, 16)})
        dados = {"success": True, "address": w.address, "private_key": w.priv_hex, "public_key": w.pub_hex, "warning": "Guarde a chave."}
        wallets = carregar_wallets()
        wallets[w.address] = {"public_key": w.pub_hex}
        salvar_wallets(wallets)
        return jsonify(dados)
    except Exception as e: return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/saldo/<address>", methods=["GET"])
def saldo(address):
    try:
        cached = _saldo_cache_get(address)
        if cached is None:
            utxos = _get_chain().db.get_utxos(address)
            cached = sum(u["amount"] for u in utxos)
            _saldo_cache_set(address, cached)
        return jsonify({"success": True, "address": address, "balance_sats": cached, "balance_brn": cached / 10**8})
    except Exception as e: return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/portfolio/<address>", methods=["GET"])
def portfolio(address):
    try:
        cached = _saldo_cache_get(address)
        if cached is None:
            utxos = _get_chain().db.get_utxos(address)
            cached = sum(u["amount"] for u in utxos)
            _saldo_cache_set(address, cached)
        return jsonify({"portfolio": {"BRN": cached / 10**8, "KYC": 0}})
    except Exception as e: return jsonify({"portfolio": {}, "error": str(e)}), 500


@app.route("/api/minhas-txs/<address>", methods=["GET"])
@app.route("/api/transacoes/<address>", methods=["GET"])
@require_rate_limit("minhas_txs")
def minhas_txs(address):
    try:
        if not WalletManager.validate_address(address): return jsonify({"ok": False, "msg": "Endereco invalido."}), 400
        chain = _get_chain()
        current_height = chain.db.height()
        
        try: _limit = int(request.args.get("limit", TXT_HISTORY_LIMIT_DEFAULT))
        except: _limit = TXT_HISTORY_LIMIT_DEFAULT
        _limit = max(1, min(_limit, TXT_HISTORY_LIMIT_MAX))

        raw_list = chain.db.get_txs_for_address(address, limit=_limit)
        transactions = []
        for item in raw_list:
            tx = item["tx"]
            block_height = item["block_height"]
            is_mempool = item["is_mempool"]

            outputs_to_addr = sum(int(o.get("amount", 0) or 0) for o in tx.get("outputs", []) if o.get("address") == address)
            inputs_from_addr = 0
            for inp in tx.get("inputs", []):
                if inp.get("txid") == "0" * 64: continue
                try:
                    row = chain.db.conn.execute("SELECT address, amount FROM utxos WHERE txid = ? AND vout = ?", (inp.get("txid"), inp.get("vout"))).fetchone()
                    if row and row["address"] == address: inputs_from_addr += int(row["amount"] or 0)
                except Exception: pass

            if inputs_from_addr > 0 and outputs_to_addr > 0: direction, amount = "self", 0
            elif outputs_to_addr > 0: direction, amount = "received", outputs_to_addr
            else: direction, amount = "sent", max(0, inputs_from_addr - outputs_to_addr)

            if is_mempool or block_height is None: status, confirmations = "pending", 0
            else:
                status = "confirmed"
                confirmations = max(0, current_height - block_height)

            transactions.append({
                "txid": tx.get("txid", ""), "direction": direction, "amount": amount, "status": status,
                "block_height": block_height, "confirmations": confirmations, "timestamp": int(tx.get("timestamp", 0) or 0),
            })

        return jsonify({"ok": True, "address": address, "count": len(transactions), "limit": _limit, "transactions": transactions})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/transfer", methods=["POST"])
@app.route("/api/send", methods=["POST"])
@app.route("/api/enviar", methods=["POST"])
@require_rate_limit("transfer")
def transfer():
    try:
        data = request.get_json(force=True) or {}
        sender = (data.get("from") or "").strip()
        to = (data.get("to") or "").strip()
        amount_raw = data.get("amount")
        sk = (request.headers.get("X-BRN-Sk", "") or data.get("private_key", ""))

        if not WalletManager.validate_address(sender) or not WalletManager.validate_address(to):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400
        if sender == to: return jsonify({"ok": False, "msg": "Nao pode enviar para si."}), 400

        try:
            amount_dec = Decimal(str(amount_raw).strip().replace(",", ".")).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            amount_sats = int(amount_dec * Decimal(10**8))
        except: return jsonify({"ok": False, "msg": "Valor invalido."}), 400

        if amount_sats <= 0: return jsonify({"ok": False, "msg": "Valor deve ser > 0."}), 400

        try:
            w = Wallet(private_key_hex=sk)
            if w.address != sender: raise ValueError()
        except: return jsonify({"ok": False, "msg": "Chave privada invalida."}), 400

        FEE = int(MIN_RELAY_FEE)
        chain = _get_chain()

        with CHAIN_LOCK:
            utxos = chain.db.get_utxos(sender)
            utxos.sort(key=lambda u: u["amount"], reverse=True)
            total, escolhidos = 0, []
            for u in utxos:
                escolhidos.append(u)
                total += u["amount"]
                if total >= amount_sats + FEE: break

            if total < amount_sats + FEE:
                return jsonify({"ok": False, "msg": f"Saldo insuficiente: tem {total/1e8:.8f} BRN."}), 400

            troco = total - amount_sats - FEE
            outputs = [{"address": to, "amount": amount_sats, "pubkey": ""}]
            if troco > 0: outputs.append({"address": sender, "amount": troco, "pubkey": ""})

            inputs = [{"txid": u["txid"], "vout": u["vout"], "pubkey": w.pub_hex, "signature": ""} for u in escolhidos]
            nonce = chain.db.get_nonce_for_pubkey(w.pub_hex)
            tx = {"txid": "", "inputs": inputs, "outputs": outputs, "timestamp": int(time.time()), "locktime": 0, "nonce": nonce}
            
            h = signing_hash(tx)
            for inp in tx["inputs"]: inp["signature"] = w.sign(h)
            tx["txid"] = calc_txid(tx)
            ok, msg = chain.submit_tx(tx)

        if not ok: return jsonify({"ok": False, "msg": msg}), 400

        p2p = _get_p2p_manager()
        if p2p:
            try: p2p.broadcast_tx(tx)
            except Exception as e: log.warning(f"broadcast_tx falhou: {e}")

        _saldo_cache_invalidate(sender)
        _saldo_cache_invalidate(to)

        return jsonify({"ok": True, "txid": tx["txid"], "msg": f"Aceita na mempool ({amount_dec} BRN)."})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# STATUS E SYNC
# ============================================================
@app.route("/api/status", methods=["GET"])
def status():
    try:
        chain = _get_chain()
        peer_count = 0
        p2p = _get_p2p_manager()
        if p2p is not None:
            try: peer_count = int(p2p.get_status().get("peer_count", 0))
            except: pass
        if peer_count == 0:
            try: peer_count = chain.db.contar_peers(apenas_ativos=True)
            except: pass

        return jsonify({
            "name": "BrunoCoin", "ticker": "BRN", "height": chain.db.height(),
            "tip_hash": chain.db.tip_hash(), "utxos": chain.db.count_utxos(),
            "mempool": len(chain.db.all_mempool(limit=1000)), "peers": peer_count,
            "reward": chain.current_reward(chain.db.height() + 1), "difficulty": chain.current_difficulty(),
            "fila_tamanho": len(chain.fila_mineracao), "rede_descentralizada": chain.rede_descentralizada,
        })
    except Exception as e: return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/chain-info", methods=["GET"])
def chain_info():
    try:
        chain = _get_chain()
        return jsonify({
            "success": True, "name": "BrunoCoin", "ticker": "BRN",
            "height": chain.db.height(), "tip_hash": chain.db.tip_hash(),
            "reward": chain.current_reward(chain.db.height() + 1),
            "difficulty": chain.current_difficulty(),
        })
    except Exception as e: return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sync-info", methods=["GET"])
def sync_info():
    result = {
        "ok": True,
        "sync": {"percent": 100.0, "height": 0, "target": 0, "peers": 0, "work": 0},
        "miner_target": "",
    }
    try:
        chain = _get_chain()
        local_h = chain.db.height()
        result["sync"]["height"] = local_h
        result["sync"]["peers"] = chain.db.contar_peers(apenas_ativos=False)
        
        target_h = local_h
        todos = chain.db.listar_peers(apenas_ativos=False)
        heights = [int(p["height"]) for p in todos if p.get("height")]
        if heights: target_h = max([local_h] + heights)
        
        result["sync"]["target"] = target_h
        if target_h > 0:
            result["sync"]["percent"] = min(100.0, round(local_h / target_h * 100, 2))

        result["miner_target"] = chain.db.get_meta("miner_address") or ""
    except Exception as e:
        result["ok"] = False
        result["erro"] = str(e)

    return jsonify(result), 200


@app.route("/api/sync/status", methods=["GET"])
def sync_status():
    sync = _get_sync_manager()
    if sync is not None:
        try: data = sync.get_status()
        except Exception: data = {}
    else:
        try:
            local_h = _get_chain().db.height()
            data = {"local_height": local_h, "target_height": local_h, "synced": True, "syncing": False, "progress": 100.0}
        except Exception:
            data = {"local_height": 0, "target_height": 0, "synced": False, "syncing": False, "progress": 0.0}

    return jsonify({
        "ok": True,
        "synced": bool(data.get("synced", True)),
        "syncing": bool(data.get("syncing", False)),
        "forking": bool(data.get("forking", False)),
        "local_height": int(data.get("local_height", 0)),
        "target_height": int(data.get("target_height", 0)),
        "progress": float(data.get("progress", 100.0)),
        "eta_legivel": data.get("eta_legivel", "sincronizado"),
    }), 200


# ============================================================
# MINERADOR
# ============================================================
@app.route("/api/miner/start", methods=["POST"])
@require_rate_limit("miner_start")
def miner_start():
    try:
        data = request.get_json(force=True) or {}
        addr = (data.get("validator_address") or data.get("address") or "").strip()
        pubkey = _pubkey_from_db_or_payload(addr, data.get("miner_pubkey", ""), data.get("private_key", ""))

        if not WalletManager.validate_address(addr): return jsonify({"ok": False, "msg": "Endereco invalido."}), 400
        if not pubkey: return jsonify({"ok": False, "msg": "Pubkey desconhecida."}), 400

        chain = _get_chain()
        chain.adicionar_na_fila(addr, pubkey)
        try: chain.db.set_meta("miner_address", addr)
        except Exception: pass

        _notify_bootstrap_peers(addr, pubkey, log_prefix="fila ")
        _register_miner_in_trust(pubkey, addr)

        from miner_loop import get_miner
        m = get_miner(chain)
        is_origin_val = getattr(chain, "is_origin", (os.environ.get("BRN_IS_ORIGIN", "0") == "1"))
        
        try: ok, msg = m.start(addr, pubkey, is_origin=bool(is_origin_val))
        except TypeError: ok, msg = m.start(addr, pubkey)

        if not ok: return jsonify({"ok": False, "msg": msg, **m.status()}), 400
        
        st = m.status()
        st["rede_descentralizada"] = chain.rede_descentralizada
        st["fila_tamanho"] = len(chain.fila_mineracao)
        return jsonify({"ok": True, "msg": "Minerador iniciado.", **st})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/miner/status", methods=["GET"])
def miner_status():
    try:
        from miner_loop import get_miner
        chain = _get_chain()
        m = get_miner(chain)
        st = m.status()
        return jsonify({
            "running": st.get("running", False),
            "address": st.get("address", ""),
            "pubkey": st.get("pubkey", ""),
            "blocks_mined": st.get("blocks_mined", 0),
            "last_height": st.get("last_height"),
            "uptime": st.get("uptime", 0),
            "hashrate": st.get("hashrate", 0.0),
            "height": chain.db.height(),
            "difficulty": chain.current_difficulty(),
            "last_error": st.get("last_error", ""),
            "rede_descentralizada": chain.rede_descentralizada,
            "fila_tamanho": len(chain.fila_mineracao),
        })
    except Exception as e:
        return jsonify({"running": False, "error": str(e)}), 200


# ============================================================
# POOL (COM PROXY PARA CLIENTES)
# ============================================================
def _proxy_pool_request(path: str, method: str = "POST") -> tuple:
    remote_pool = os.environ.get("BRN_POOL_URL", "").strip().rstrip("/")
    if not remote_pool or not remote_pool.startswith("http"):
        return jsonify({"ok": False, "msg": "pool nao disponivel e BRN_POOL_URL nao configurada"}), 503

    url = f"{remote_pool}{path}"
    try:
        if method == "POST":
            data = request.get_json(force=True) or {}
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "brn-server-proxy/1.0"},
                method="POST"
            )
        else:
            req = urllib.request.Request(url, method="GET")

        with urllib.request.urlopen(req, timeout=100) as resp:
            return jsonify(json.loads(resp.read().decode("utf-8"))), resp.status
            
    except urllib.error.HTTPError as e:
        try: return jsonify(json.loads(e.read().decode("utf-8"))), e.code
        except: return jsonify({"ok": False, "msg": f"HTTP {e.code} no pool remoto"}), e.code
    except urllib.error.URLError as e:
        return jsonify({"ok": False, "msg": f"Nao consegui conectar ao Origem ({remote_pool}): {e.reason}"}), 502
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Falha no proxy do pool: {e}"}), 502


@app.route("/api/pool/register", methods=["POST"])
@require_rate_limit("pool_register")
def pool_register():
    pool = _get_pool_manager()
    if pool is None: return _proxy_pool_request("/api/pool/register", "POST")
    try:
        data = request.get_json(force=True) or {}
        address = (data.get("address") or "").strip()
        pubkey = (data.get("pubkey") or "").strip()
        miner_id = (data.get("miner_id") or "").strip() or None

        if not address.startswith("brn1"): return jsonify({"ok": False, "msg": "address invalido"}), 400
        if not pubkey: return jsonify({"ok": False, "msg": "pubkey obrigatoria"}), 400

        chain = _get_chain()
        chain.adicionar_na_fila(address, pubkey)
        try: chain.db.set_meta("miner_address", address)
        except Exception: pass

        _notify_bootstrap_peers(address, pubkey, log_prefix="pool ")
        _register_miner_in_trust(pubkey, address)

        r = pool.register(address, pubkey, miner_id=miner_id)
        return jsonify(r), (200 if r.get("ok") else 400)
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/pool/work", methods=["POST"])
@require_rate_limit("pool_work")
def pool_work():
    pool = _get_pool_manager()
    if pool is None: return _proxy_pool_request("/api/pool/work", "POST")
    try:
        data = request.get_json(force=True) or {}
        miner_id = (data.get("miner_id") or "").strip()
        if not miner_id: return jsonify({"ok": False, "msg": "miner_id obrigatorio"}), 400
        r = pool.get_work(miner_id)
        return jsonify(r), (200 if r.get("ok") else 404)
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/pool/submit", methods=["POST"])
@require_rate_limit("pool_submit")
def pool_submit():
    pool = _get_pool_manager()
    if pool is None: return _proxy_pool_request("/api/pool/submit", "POST")
    try:
        data = request.get_json(force=True) or {}
        miner_id = (data.get("miner_id") or "").strip()
        nonce = data.get("nonce")
        hash_hex = (data.get("hash") or "").strip()

        if not miner_id: return jsonify({"ok": False, "msg": "miner_id obrigatorio"}), 400
        if nonce is None: return jsonify({"ok": False, "msg": "nonce obrigatorio"}), 400
        try: nonce = int(nonce)
        except: return jsonify({"ok": False, "msg": "nonce invalido"}), 400

        r = pool.submit_share(miner_id, nonce, hash_hex)
        return jsonify(r), (200 if r.get("ok") else 400)
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/pool/heartbeat", methods=["POST"])
@require_rate_limit("pool_heartbeat")
def pool_heartbeat():
    pool = _get_pool_manager()
    if pool is None: return _proxy_pool_request("/api/pool/heartbeat", "POST")
    try:
        data = request.get_json(force=True) or {}
        miner_id = (data.get("miner_id") or "").strip()
        hashrate = float(data.get("hashrate", 0) or 0)
        hashes_total = int(data.get("hashes_total", 0) or 0)

        if not miner_id: return jsonify({"ok": False, "msg": "miner_id obrigatorio"}), 400
        r = pool.heartbeat(miner_id, hashrate, hashes_total)
        return jsonify(r), (200 if r.get("ok") else 404)
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/pool/stats", methods=["GET"])
@require_rate_limit("pool_stats")
def pool_stats():
    pool = _get_pool_manager()
    if pool is None: return _proxy_pool_request("/api/pool/stats", "GET")
    try: return jsonify(pool.stats())
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/pool/miners", methods=["GET"])
@require_rate_limit("pool_miners")
def pool_miners():
    pool = _get_pool_manager()
    if pool is None: return _proxy_pool_request("/api/pool/miners", "GET")
    try: return jsonify({"ok": True, "miners": pool.miners_list()})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# TRUST NETWORK
# ============================================================
@app.route("/api/trust/stats", methods=["GET"])
@require_rate_limit("trust_stats")
def trust_stats():
    trust = _get_trust_network()
    if trust is None: return jsonify({"ok": False, "msg": "TrustNetwork indisponivel"}), 503
    try:
        if hasattr(trust, "peers"):
            peers = trust.peers
            total = len(peers)
            trusted = sum(1 for p in peers.values() if p.get("status") == "trusted")
            banned = sum(1 for p in peers.values() if p.get("status") == "banned")
            return jsonify({"ok": True, "total_nodes": total, "validators_count": trusted, "banned_nodes": banned})
        else:
            return jsonify({"ok": True, **trust.get_network_stats()})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/trust/nodes", methods=["GET"])
@require_rate_limit("trust_nodes")
def trust_nodes():
    trust = _get_trust_network()
    if trust is None: return jsonify({"ok": False, "msg": "TrustNetwork indisponivel"}), 503
    try:
        limit = int(request.args.get("limit", 100))
        if hasattr(trust, "peers"):
            nodes = []
            for pubkey, data in trust.peers.items():
                nodes.append({
                    "node_id": pubkey, "address": "desconhecido", "trust_score": data.get("score", 0),
                    "trust_level": data.get("status", "new"), "is_validator": data.get("status") == "trusted"
                })
            nodes.sort(key=lambda x: x["trust_score"], reverse=True)
            return jsonify({"ok": True, "count": len(nodes), "nodes": nodes[:limit]})
        else:
            from dataclasses import asdict
            nodes_raw = list(trust.nodes.values()) if hasattr(trust, "nodes") else []
            nodes = [asdict(n) for n in nodes_raw]
            nodes.sort(key=lambda x: x.get("trust_score", 0), reverse=True)
            return jsonify({"ok": True, "count": len(nodes), "nodes": nodes[:limit]})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# BLOCOS / PEERS / HEALTH
# ============================================================
@app.route("/api/blocks", methods=["GET"])
def api_get_blocks():
    try:
        start = int(request.args.get("start", 0))
        limit = int(request.args.get("limit", 100))
        limit = min(max(limit, 1), 5000)
        blocks = _get_chain().db.get_blocks_range(start, start + limit)
        return jsonify({"blocks": blocks, "start": start, "count": len(blocks)})
    except Exception as e: return jsonify({"error": str(e)}), 500


@app.route("/api/peers/discovered", methods=["GET"])
@require_rate_limit("peers_discovered")
def peers_discovered():
    try:
        chain = _get_chain()
        rows = chain.db.listar_peers(apenas_ativos=False)
        out, seen_ips = [], set()
        for p in rows:
            addr = (p.get("address") or "").strip()
            if not addr: continue
            ip = addr.rsplit(":", 1)[0] if ":" in addr else addr
            if ip.startswith("127.") or ip == "0.0.0.0" or ip in seen_ips: continue
            seen_ips.add(ip)
            out.append({"ip": ip, "address": addr, "height": int(p.get("height", 0) or 0), "last_seen": int(p.get("last_seen", 0) or 0)})
        out.sort(key=lambda x: x["last_seen"], reverse=True)
        return jsonify({"ok": True, "count": len(out), "peers": out})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    try:
        return jsonify({
            "ok": True, "height": _get_chain().db.height(), "version": "9.9.16",
            "chain_injected": _CHAIN is not None, "pool_injected": _POOL_MANAGER is not None,
            "trust_injected": _TRUST_NETWORK is not None,
        })
    except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500


@atexit.register
def _shutdown():
    log.info("encerrando server…")
    try: close_audit()
    except: pass
# ============================================================
# [PATCH v9.9.17] Endpoints críticos faltando
# ============================================================

@app.route("/api/miner/stop", methods=["POST"])
@require_rate_limit("miner_stop")
def miner_stop():
    try:
        from miner_loop import get_miner
        m = get_miner(_get_chain())
        ok, msg = m.stop()
        audit_log("miner_stop_ok" if ok else "miner_stop_failed",
                  {"ip": get_client_ip(), "msg": msg})
        return jsonify({"ok": ok, "msg": msg, **m.status()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/miner/status", methods=["GET"])
def miner_status():
    try:
        from miner_loop import get_miner
        chain = _get_chain()
        st = get_miner(chain).status()

        # Conta validadores ativos (Top 5) para multi-sig
        validators_active = 0
        trust = _get_trust_network()
        if trust is not None:
            try:
                validators_active = len(trust.get_top_validators(count=5))
            except Exception:
                pass

        return jsonify({
            **st,
            "height": chain.db.height(),
            "difficulty": chain.current_difficulty(),
            "validators_active": validators_active,
            "signatures_required": validators_active,
            "signatures_collected": st.get("signatures_collected", 0),
            "rede_descentralizada": chain.rede_descentralizada,
            "fila_tamanho": len(chain.fila_mineracao),
        })
    except Exception as e:
        return jsonify({"running": False, "error": str(e)}), 200


@app.route("/api/pool/disconnect", methods=["POST"])
@require_rate_limit("pool_disconnect")
def pool_disconnect():
    pool = _get_pool_manager()
    if pool is None:
        return _proxy_pool_request("/api/pool/disconnect", "POST")
    try:
        data = request.get_json(force=True) or {}
        miner_id = (data.get("miner_id") or "").strip()
        if not miner_id:
            return jsonify({"ok": False, "msg": "miner_id obrigatorio"}), 400
        r = pool.disconnect(miner_id)
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/sync-info", methods=["GET"])
def sync_info():
    result = {
        "ok": True,
        "sync": {"percent": 100.0, "height": 0, "target": 0, "peers": 0},
        "miner_target": "",
        "bridge": {"onramp_ativo": False, "taxa": 0, "btc_cofre": ""},
    }
    chain = _get_chain()
    try:
        local_h = chain.db.height()
        result["sync"]["height"] = local_h
    except Exception as e:
        result["ok"] = False
        result["erro"] = f"height: {e}"
        return jsonify(result), 200

    try:
        result["sync"]["peers"] = chain.db.contar_peers(apenas_ativos=False)
    except Exception:
        result["sync"]["peers"] = 0

    target_h = local_h
    try:
        todos = chain.db.listar_peers(apenas_ativos=False)
        heights = [int(p.get("height", 0) or 0) for p in (todos or []) if isinstance(p, dict)]
        if heights:
            target_h = max([target_h] + heights)
    except Exception:
        pass
    result["sync"]["target"] = target_h

    try:
        if target_h > 0:
            result["sync"]["percent"] = min(100.0, round(local_h / target_h * 100, 2))
    except Exception:
        pass

    try:
        result["miner_target"] = chain.db.get_meta("miner_address") or ""
    except Exception:
        pass

    try:
        if L2_ENABLED and btc_config is not None:
            result["bridge"]["onramp_ativo"] = True
            result["bridge"]["taxa"] = int(getattr(btc_config, "BRN_PER_BTC", 0) or 0)
            result["bridge"]["btc_cofre"] = getattr(btc_config, "BTC_RECEIVE_ADDRESS", "") or ""
    except Exception:
        pass

    return jsonify(result), 200


@app.route("/api/sync/status", methods=["GET"])
def sync_status():
    try:
        sync = _get_sync_manager()
        if sync is None:
            chain = _get_chain()
            h = chain.db.height()
            return jsonify({
                "ok": True, "synced": True, "syncing": False, "forking": False,
                "progress": 100.0, "local_height": h, "target_height": h,
                "blocos_faltando": 0, "eta_legivel": "sincronizado",
                "last_error": "",
            })
        data = sync.get_status()

        # [FIX] Fallback se retornar -1
        local_h = int(data.get("local_height", 0))
        if local_h < 0:
            local_h = _get_chain().db.height()
            data["local_height"] = local_h
            data["synced"] = True
            data["progress"] = 100.0

        return jsonify({
            "ok": True,
            "synced": bool(data.get("synced", True)),
            "syncing": bool(data.get("syncing", False)),
            "forking": bool(data.get("forking", False)),
            "progress": round(float(data.get("progress", 100.0)), 2),
            "local_height": local_h,
            "target_height": int(data.get("target_height", local_h)),
            "blocos_faltando": max(0, int(data.get("target_height", local_h)) - local_h),
            "eta_segundos": data.get("eta_segundos"),
            "eta_legivel": data.get("eta_legivel", ""),
            "active_peer": data.get("active_peer"),
            "peers_known": int(data.get("peers_known", 0)),
            "reorgs_total": int(data.get("reorgs_total", 0)),
            "last_reorg_height": int(data.get("last_reorg_height", -1)),
            "last_error": data.get("last_error", ""),
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/faucet", methods=["POST"])
@require_rate_limit("faucet")
@_admin_required
def faucet():
    try:
        data = request.get_json(force=True) or {}
        addr = (data.get("address") or "").strip()
        if not WalletManager.validate_address(addr):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400

        agora = time.time()
        hist = _faucet_history.setdefault(addr, [])
        hist[:] = [t for t in hist if agora - t < FAUCET_COOLDOWN_S]

        if len(hist) >= FAUCET_MAX_PER_ADDRESS:
            return jsonify({"ok": False, "msg": "Limite atingido."}), 429
        if hist and agora - hist[-1] < FAUCET_COOLDOWN_S:
            falta = int(FAUCET_COOLDOWN_S - (agora - hist[-1]))
            return jsonify({"ok": False, "msg": f"Aguarde {falta}s."}), 429

        miner_pubkey = _pubkey_from_db_or_payload(
            addr, data.get("pubkey", ""), data.get("private_key", ""),
        )
        if not miner_pubkey:
            return jsonify({"ok": False, "msg": "Pubkey desconhecida."}), 400

        chain = _get_chain()
        with CHAIN_LOCK:
            block = chain.mine_block(addr, miner_pubkey)

        if not block:
            return jsonify({"ok": False, "msg": "Falha ao minerar."}), 500

        hist.append(agora)
        _saldo_cache_invalidate(addr)

        reward_sats = 0
        try:
            cb = block["transactions"][0]
            reward_sats = sum(o.get("amount", 0) for o in cb.get("outputs", [])
                              if o.get("address") == addr)
        except Exception:
            pass

        return jsonify({
            "ok": True, "msg": f"Faucet enviado! +{reward_sats/1e8} BRN",
            "txid": block["transactions"][0]["txid"],
            "amount": reward_sats / 1e8,
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

        chain = _get_chain()
        mem_tx = chain.db.get_mempool_tx(txid_str)
        if mem_tx:
            valor = sum(o.get("amount", 0) for o in mem_tx.get("outputs", [])
                        if o.get("address") == address)
            return jsonify({
                "ok": True, "chegou": False, "status": "pending",
                "valor": valor, "confirmacoes": 0, "block_height": None,
                "msg": "Transacao esta na mempool." if valor else "Endereco nao esta nos outputs.",
            })

        chain_tx, block_height = chain.db.get_tx_by_txid(txid_str)
        if chain_tx is None:
            return jsonify({
                "ok": True, "chegou": False, "status": "not_found",
                "valor": 0, "confirmacoes": 0, "block_height": None,
                "msg": "TXID nao encontrado.",
            })

        valor = sum(o.get("amount", 0) for o in chain_tx.get("outputs", [])
                    if o.get("address") == address)
        altura = chain.db.height()
        confs = max(0, altura - block_height)
        chegou = valor > 0
        return jsonify({
            "ok": True, "chegou": chegou, "status": "confirmed",
            "valor": valor, "confirmacoes": confs, "block_height": block_height,
            "msg": f"Confirmado no bloco #{block_height} com {confs} confirmacoes." if chegou else "Endereco nao esta nos outputs.",
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/block/<int:height>", methods=["GET"])
def api_get_block(height):
    try:
        block = _get_chain().db.get_block(height)
        if block:
            return jsonify(block)
        return jsonify({"error": "Bloco não encontrado"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# [NEW v9.9.17] Multi-sig / Top 5 validadores
# ============================================================
@app.route("/api/consensus/validators", methods=["GET"])
@require_rate_limit("status")
def consensus_validators():
    try:
        trust = _get_trust_network()
        if trust is None:
            return jsonify({"ok": False, "msg": "TrustNetwork indisponivel"}), 503

        top5_pubkeys = trust.get_top_validators(count=5) if hasattr(trust, "get_top_validators") else []

        # Encontra os NodeRecords que correspondem a essas pubkeys
        nodes = list(trust.nodes.values()) if hasattr(trust, "nodes") else []
        validators = []
        for n in nodes:
            if getattr(n, "public_key", "") in top5_pubkeys:
                validators.append({
                    "pubkey": n.public_key[:32] + "...",
                    "pubkey_full": n.public_key,
                    "node_id": n.node_id,
                    "address": getattr(n, "address", ""),
                    "trust_score": round(getattr(n, "trust_score", 0), 1),
                    "trust_level": getattr(n, "trust_level", "new"),
                    "connected": getattr(n, "connected", False),
                })
        validators.sort(key=lambda x: -x["trust_score"])

        return jsonify({
            "ok": True,
            "required_sigs": len(top5_pubkeys),
            "all_required": True,  # REQUIRE_ALL_SIGS no blockchain v9.4.0
            "validators": validators[:5],
        })
    except Exception as e:
        log.exception("consensus_validators falhou")
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/block/<int:height>/signatures", methods=["GET"])
@require_rate_limit("status")
def block_signatures(height):
    try:
        block = _get_chain().db.get_block(height)
        if not block:
            return jsonify({"ok": False, "msg": "Bloco não encontrado"}), 404

        sigs = block.get("validators_sig", [])
        if not isinstance(sigs, list):
            sigs = []

        return jsonify({
            "ok": True,
            "height": height,
            "block_hash": block.get("hash", ""),
            "signatures_count": len(sigs),
            "signatures": [
                {
                    "pubkey": (s or {}).get("pubkey", "")[:32] + "...",
                    "pubkey_full": (s or {}).get("pubkey", ""),
                    "signature": (s or {}).get("signature", "")[:64] + "...",
                }
                for s in sigs
            ],
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# [FIX] /api/status com validators_active
# ============================================================
# SUBSTITUIR o retorno de /api/status (procure "fila_tamanho": len(chain.fila_mineracao))
# Adicione esta linha no jsonify:
#     "validators_active": (len(_get_trust_network().get_top_validators(5))
#                           if _get_trust_network() and hasattr(_get_trust_network(), "get_top_validators")
#                           else 0),

if __name__ == "__main__":
    port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    print(f"BRN Server v9.9.16 (Proxy + Trust + History)")
    app.run(host=WEB_HOST, port=port, debug=False, threaded=True)
