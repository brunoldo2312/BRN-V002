"""
server.py — Backend HTTP do nó BRN (v9.9.18)
================================================================
v9.9.18 (2026-10-28):
  [NEW] Suporte ao auto-registro P2P de validadores:
        - /api/consensus/validators lê os validadores que
          se anunciaram via P2P (register_validator).
        - Registra a pubkey do validador automaticamente no
          trust_net se ela ainda não estiver lá.
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

# ============================================================
# CONFIG
# ============================================================
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


# ============================================================
# BOOTSTRAP NOTIFY
# ============================================================
def _notify_bootstrap_peers(addr: str, pubkey: str, log_prefix: str = "") -> None:
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
                http_port = int(parts[2]) if len(parts) >= 3 else BOOTSTRAP_HTTP_PORT
            except ValueError:
                http_port = BOOTSTRAP_HTTP_PORT

            url = f"{scheme}://{host}:{http_port}/api/bootstrap/add"
            req = urllib.request.Request(
                url,
                data=json.dumps({"address": addr, "pubkey": pubkey}).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "User-Agent": "brn-server/9.9.18"},
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
# MANAGERS
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
# _register_miner_in_trust
# ============================================================
def _register_miner_in_trust(pubkey: str, address: str) -> None:
    """Registra o minerador no Trust Network (prefere trust_network.register_node)."""
    trust = _get_trust_network()
    if trust is None:
        return
    try:
        if hasattr(trust, "register_node"):
            trust.register_node(node_id=pubkey, address=address, public_key=pubkey)
            if hasattr(trust, "mark_connected"):
                trust.mark_connected(pubkey)
            return
        if hasattr(trust, "ensure_peer_entry"):
            trust.ensure_peer_entry(pubkey, address=address)
    except Exception as e:
        log.warning(f"[Trust] Falha ao registrar minerador {pubkey[:12]}: {e}")


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
            audit_log("admin_denied", {"path": request.path, "ip": get_client_ip()})
            return jsonify({"ok": False, "msg": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


# ============================================================
# CACHE / WALLETS
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
        return {"ok": False, "msg": "senhas vazias"}

    result = None
    try:
        if hasattr(WalletManager, "load_current"):
            result = WalletManager.load_current(senha)
    except Exception:
        result = None

    if result is None:
        try:
            from secure_store import load_current_wallet
            result = load_current_wallet(senha)
        except Exception as e:
            return {"ok": False, "msg": f"secure_store falhou: {e}"}

    if not result or not result.get("ok"):
        return {"ok": False, "msg": "current_wallet.enc não encontrado"}

    addr = (result.get("address") or "").strip()
    pk = (result.get("public_key") or result.get("pubkey")
          or result.get("pub_hex") or "").strip()
    if not addr or not pk:
        return {"ok": False, "msg": "sem address/public_key"}

    try:
        wallets = carregar_wallets()
        if wallets.get(addr, {}).get("public_key") == pk:
            return {"ok": True, "msg": f"já registrado: {addr[:16]}"}
        wallets[addr] = {"public_key": pk}
        salvar_wallets(wallets)
        return {"ok": True, "msg": f"registrado: {addr[:16]}"}
    except Exception as e:
        return {"ok": False, "msg": f"falha: {e}"}


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
# BOOTSTRAP
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
        _register_miner_in_trust(pubkey, addr)
        return jsonify({
            "ok": True,
            "fila_tamanho": len(chain.fila_mineracao),
            "rede_descentralizada": chain.rede_descentralizada,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# CARTEIRA
# ============================================================
@app.route("/api/nova-carteira", methods=["POST"])
@require_rate_limit("nova_carteira")
def nova_carteira():
    try:
        w = Wallet()
        audit_log("wallet_created", {"address": hash_short(w.address, 16)})
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
            valor = sum(o.get("amount", 0) for o in mem_tx.get("outputs", [])
                        if o.get("address") == address)
            return jsonify({
                "ok": True, "chegou": False, "status": "pending",
                "valor": valor, "confirmacoes": 0, "block_height": None,
                "msg": ("Transacao esta na mempool." if valor
                        else "Endereco nao esta nos outputs."),
            })
        chain_tx, block_height = _find_tx_in_chain(txid_str)
        if chain_tx is None:
            return jsonify({
                "ok": True, "chegou": False, "status": "not_found",
                "valor": 0, "confirmacoes": 0, "block_height": None,
                "msg": "TXID nao encontrado.",
            })
        valor = sum(o.get("amount", 0) for o in chain_tx.get("outputs", [])
                    if o.get("address") == address)
        altura = _get_chain().db.height()
        confs = max(0, altura - block_height)
        chegou = valor > 0
        msg = (f"Confirmado no bloco #{block_height} com {confs} confirmacoes."
               if chegou else "Endereco nao esta nos outputs.")
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
        current_height = chain.db.height()

        try:
            _limit = int(request.args.get("limit", TXT_HISTORY_LIMIT_DEFAULT))
        except Exception:
            _limit = TXT_HISTORY_LIMIT_DEFAULT
        _limit = max(1, min(_limit, TXT_HISTORY_LIMIT_MAX))

        if not hasattr(chain.db, "get_txs_for_address"):
            return jsonify({"ok": True, "address": address,
                            "count": 0, "transactions": []})

        raw_list = chain.db.get_txs_for_address(address, limit=_limit)
        transactions = []
        for item in raw_list:
            tx = item["tx"]
            block_height = item["block_height"]
            is_mempool = item["is_mempool"]

            outputs_to_addr = sum(
                int(o.get("amount", 0) or 0) for o in tx.get("outputs", [])
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
                direction, amount = "sent", max(
                    0, inputs_from_addr - outputs_to_addr)

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
            })

        return jsonify({
            "ok": True, "address": address,
            "count": len(transactions), "limit": _limit,
            "transactions": transactions,
        })
    except Exception as e:
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
                "total_out": sum(o.get("amount", 0) for o in t.get("outputs", [])),
                "timestamp": t.get("timestamp", 0),
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
            if ip.startswith("127.") or ip == "0.0.0.0" or ip in seen_ips:
                continue
            seen_ips.add(ip)
            out.append({
                "ip": ip,
                "address": addr,
                "height": int(p.get("height", 0) or 0),
                "last_seen": int(p.get("last_seen", 0) or 0),
            })
        out.sort(key=lambda x: x["last_seen"], reverse=True)
        return jsonify({"ok": True, "count": len(out), "peers": out})
    except Exception as e:
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
        amount_raw = data.get("amount")
        sk = (request.headers.get("X-BRN-Sk", "")
              or data.get("private_key", ""))

        if not WalletManager.validate_address(sender) or \
           not WalletManager.validate_address(to):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400
        if sender == to:
            return jsonify({"ok": False, "msg": "Nao pode enviar para si."}), 400

        try:
            amount_dec = Decimal(
                str(amount_raw).strip().replace(",", ".")
            ).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            amount_sats = int(amount_dec * Decimal(10**8))
        except Exception:
            return jsonify({"ok": False, "msg": "Valor invalido."}), 400

        if amount_sats <= 0:
            return jsonify({"ok": False, "msg": "Valor deve ser > 0."}), 400

        try:
            w = Wallet(private_key_hex=sk)
            if w.address != sender:
                raise ValueError()
        except Exception:
            return jsonify({"ok": False, "msg": "Chave privada invalida."}), 400

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
                    "msg": f"Saldo insuficiente: tem {total/1e8:.8f} BRN.",
                }), 400

            troco = total - amount_sats - FEE
            outputs = [{"address": to, "amount": amount_sats, "pubkey": ""}]
            if troco > 0:
                outputs.append(
                    {"address": sender, "amount": troco, "pubkey": ""})

            inputs = [{
                "txid": u["txid"], "vout": u["vout"],
                "pubkey": w.pub_hex, "signature": "",
            } for u in escolhidos]

            nonce = chain.db.get_nonce_for_pubkey(w.pub_hex)
            tx = {
                "txid": "", "inputs": inputs, "outputs": outputs,
                "timestamp": int(time.time()), "locktime": 0, "nonce": nonce,
            }
            h = signing_hash(tx)
            for inp in tx["inputs"]:
                inp["signature"] = w.sign(h)
            tx["txid"] = calc_txid(tx)
            ok, msg = chain.submit_tx(tx)

        if not ok:
            return jsonify({"ok": False, "msg": msg}), 400

        p2p = _get_p2p_manager()
        if p2p:
            try:
                p2p.broadcast_tx(tx)
            except Exception as e:
                log.warning(f"broadcast_tx falhou: {e}")

        _saldo_cache_invalidate(sender)
        _saldo_cache_invalidate(to)

        return jsonify({
            "ok": True,
            "txid": tx["txid"],
            "msg": f"Aceita na mempool ({amount_dec} BRN).",
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# MINE (1 bloco)
# ============================================================
@app.route("/api/mine", methods=["POST"])
@require_rate_limit("mine")
def mine():
    try:
        data = request.get_json(force=True) or {}
        miner = (data.get("validator_address")
                 or data.get("address") or "").strip()

        if not WalletManager.validate_address(miner):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400

        miner_pubkey = _pubkey_from_db_or_payload(
            miner,
            data.get("miner_pubkey", "") or data.get("pubkey", ""),
            data.get("private_key", "") or data.get("privatekey", ""),
        )
        if not miner_pubkey:
            return jsonify({"ok": False, "msg": "Pubkey desconhecida."}), 400

        chain = _get_chain()
        with CHAIN_LOCK:
            block = chain.mine_block(miner, miner_pubkey)

        if not block:
            return jsonify({"ok": False, "msg": "Falha ao minerar."}), 500

        _saldo_cache_invalidate(miner)

        p2p = _get_p2p_manager()
        if p2p:
            try:
                p2p.broadcast_block(block)
            except Exception:
                pass

        return jsonify({
            "ok": True,
            "msg": f"Bloco #{block['height']} minerado!",
            "block": {
                "height": block["height"],
                "hash": block["hash"],
                "txs": len(block["transactions"]),
                "difficulty": block["difficulty"],
                "nonce": block["nonce"],
            },
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# MINER (start / stop / status)
# ============================================================
@app.route("/api/miner/start", methods=["POST"])
@require_rate_limit("miner_start")
def miner_start():
    try:
        data = request.get_json(force=True) or {}
        addr = (data.get("validator_address")
                or data.get("address") or "").strip()
        pubkey = _pubkey_from_db_or_payload(
            addr,
            data.get("miner_pubkey", "") or data.get("pubkey", ""),
            data.get("private_key", "") or data.get("privatekey", ""),
        )

        if not WalletManager.validate_address(addr):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400
        if not pubkey:
            return jsonify({"ok": False, "msg": "Pubkey desconhecida."}), 400

        chain = _get_chain()
        chain.adicionar_na_fila(addr, pubkey)
        try:
            chain.db.set_meta("miner_address", addr)
        except Exception:
            pass

        _notify_bootstrap_peers(addr, pubkey, log_prefix="fila ")
        _register_miner_in_trust(pubkey, addr)

        from miner_loop import get_miner
        m = get_miner(chain)
        is_origin_val = getattr(
            chain, "is_origin",
            os.environ.get("BRN_IS_ORIGIN", "0") == "1",
        )

        try:
            ok, msg = m.start(addr, pubkey, is_origin=bool(is_origin_val))
        except TypeError:
            ok, msg = m.start(addr, pubkey)

        if not ok:
            return jsonify({"ok": False, "msg": msg, **m.status()}), 400

        st = m.status()
        st["rede_descentralizada"] = chain.rede_descentralizada
        st["fila_tamanho"] = len(chain.fila_mineracao)
        return jsonify({"ok": True, "msg": "Minerador iniciado.", **st})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/miner/stop", methods=["POST"])
@require_rate_limit("miner_stop")
def miner_stop():
    try:
        from miner_loop import get_miner
        m = get_miner(_get_chain())
        ok, msg = m.stop()
        return jsonify({"ok": ok, "msg": msg, **m.status()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/miner/status", methods=["GET"])
def miner_status():
    try:
        from miner_loop import get_miner
        chain = _get_chain()
        m = get_miner(chain)
        st = m.status()

        validators_active = 0
        trust = _get_trust_network()
        if trust is not None:
            try:
                if hasattr(trust, "get_top_validators"):
                    validators_active = len(trust.get_top_validators(count=5))
            except Exception:
                pass

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
            "validators_active": validators_active,
            "signatures_required": validators_active,
            "signatures_collected": st.get("signatures_collected", 0),
            "rede_descentralizada": chain.rede_descentralizada,
            "fila_tamanho": len(chain.fila_mineracao),
        })
    except Exception as e:
        return jsonify({"running": False, "error": str(e)}), 200


# ============================================================
# FAUCET
# ============================================================
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
            addr,
            data.get("pubkey", "") or data.get("miner_pubkey", ""),
            data.get("private_key", "") or data.get("privatekey", ""),
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
            "ok": True,
            "msg": f"Faucet enviado! +{reward_sats/1e8} BRN",
            "txid": block["transactions"][0]["txid"],
            "amount": reward_sats / 1e8,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# POOL (com proxy para clientes)
# ============================================================
def _proxy_pool_request(path: str, method: str = "POST") -> tuple:
    remote_pool = os.environ.get("BRN_POOL_URL", "").strip().rstrip("/")
    if not remote_pool or not remote_pool.startswith("http"):
        return jsonify({
            "ok": False,
            "msg": "pool nao disponivel e BRN_POOL_URL nao configurada",
        }), 503

    url = f"{remote_pool}{path}"
    try:
        if method == "POST":
            data = request.get_json(force=True) or {}
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "User-Agent": "brn-server-proxy/1.0"},
                method="POST",
            )
        else:
            req = urllib.request.Request(url, method="GET")

        with urllib.request.urlopen(req, timeout=100) as resp:
            return jsonify(json.loads(resp.read().decode("utf-8"))), resp.status
    except urllib.error.HTTPError as e:
        try:
            return jsonify(json.loads(e.read().decode("utf-8"))), e.code
        except Exception:
            return jsonify({"ok": False, "msg": f"HTTP {e.code} no pool remoto"}), e.code
    except urllib.error.URLError as e:
        return jsonify({
            "ok": False,
            "msg": f"Nao consegui conectar ao Origem ({remote_pool}): {e.reason}",
        }), 502
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Falha no proxy do pool: {e}"}), 502


@app.route("/api/pool/register", methods=["POST"])
@require_rate_limit("pool_register")
def pool_register():
    pool = _get_pool_manager()
    if pool is None:
        return _proxy_pool_request("/api/pool/register", "POST")
    try:
        data = request.get_json(force=True) or {}
        address = (data.get("address") or "").strip()
        pubkey = (data.get("pubkey") or "").strip()
        miner_id = (data.get("miner_id") or "").strip() or None

        if not address.startswith("brn1"):
            return jsonify({"ok": False, "msg": "address invalido"}), 400
        if not pubkey:
            return jsonify({"ok": False, "msg": "pubkey obrigatoria"}), 400

        chain = _get_chain()
        chain.adicionar_na_fila(address, pubkey)
        try:
            chain.db.set_meta("miner_address", address)
        except Exception:
            pass

        _notify_bootstrap_peers(address, pubkey, log_prefix="pool ")
        _register_miner_in_trust(pubkey, address)

        r = pool.register(address, pubkey, miner_id=miner_id)
        return jsonify(r), (200 if r.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/pool/work", methods=["POST"])
@require_rate_limit("pool_work")
def pool_work():
    pool = _get_pool_manager()
    if pool is None:
        return _proxy_pool_request("/api/pool/work", "POST")
    try:
        data = request.get_json(force=True) or {}
        miner_id = (data.get("miner_id") or "").strip()
        if not miner_id:
            return jsonify({"ok": False, "msg": "miner_id obrigatorio"}), 400
        r = pool.get_work(miner_id)
        return jsonify(r), (200 if r.get("ok") else 404)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/pool/submit", methods=["POST"])
@require_rate_limit("pool_submit")
def pool_submit():
    pool = _get_pool_manager()
    if pool is None:
        return _proxy_pool_request("/api/pool/submit", "POST")
    try:
        data = request.get_json(force=True) or {}
        miner_id = (data.get("miner_id") or "").strip()
        nonce = data.get("nonce")
        hash_hex = (data.get("hash") or "").strip()

        if not miner_id:
            return jsonify({"ok": False, "msg": "miner_id obrigatorio"}), 400
        if nonce is None:
            return jsonify({"ok": False, "msg": "nonce obrigatorio"}), 400
        try:
            nonce = int(nonce)
        except Exception:
            return jsonify({"ok": False, "msg": "nonce invalido"}), 400

        r = pool.submit_share(miner_id, nonce, hash_hex)
        return jsonify(r), (200 if r.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/pool/heartbeat", methods=["POST"])
@require_rate_limit("pool_heartbeat")
def pool_heartbeat():
    pool = _get_pool_manager()
    if pool is None:
        return _proxy_pool_request("/api/pool/heartbeat", "POST")
    try:
        data = request.get_json(force=True) or {}
        miner_id = (data.get("miner_id") or "").strip()
        hashrate = float(data.get("hashrate", 0) or 0)
        hashes_total = int(data.get("hashes_total", 0) or 0)

        if not miner_id:
            return jsonify({"ok": False, "msg": "miner_id obrigatorio"}), 400

        r = pool.heartbeat(miner_id, hashrate, hashes_total)
        return jsonify(r), (200 if r.get("ok") else 404)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


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


@app.route("/api/pool/stats", methods=["GET"])
@require_rate_limit("pool_stats")
def pool_stats():
    pool = _get_pool_manager()
    if pool is None:
        return _proxy_pool_request("/api/pool/stats", "GET")
    try:
        return jsonify(pool.stats())
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/pool/miners", methods=["GET"])
@require_rate_limit("pool_miners")
def pool_miners():
    pool = _get_pool_manager()
    if pool is None:
        return _proxy_pool_request("/api/pool/miners", "GET")
    try:
        return jsonify({"ok": True, "miners": pool.miners_list()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# BLOCOS
# ============================================================
@app.route("/api/blocks", methods=["GET"])
def api_get_blocks():
    try:
        start = int(request.args.get("start", 0))
        limit = int(request.args.get("limit", 100))
        limit = min(max(limit, 1), 5000)
        blocks = _get_chain().db.get_blocks_range(start, start + limit)
        return jsonify({"blocks": blocks, "start": start, "count": len(blocks)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/block/<int:height>", methods=["GET"])
def api_get_block(height):
    try:
        block = _get_chain().db.get_block(height)
        if block:
            return jsonify(block)
        return jsonify({"error": "Bloco não encontrado"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/latest", methods=["GET"])
def api_latest():
    try:
        return jsonify(_get_chain().db.latest_blocks(n=10))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# CHAIN INFO / STATUS / WORK / NONCE / FEE
# ============================================================
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
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
def status():
    try:
        chain = _get_chain()
        peer_count = 0
        p2p = _get_p2p_manager()
        if p2p is not None:
            try:
                peer_count = int(p2p.get_status().get("peer_count", 0))
            except Exception:
                pass
        if peer_count == 0:
            try:
                peer_count = chain.db.contar_peers(apenas_ativos=True)
            except Exception:
                pass

        validators_active = 0
        trust = _get_trust_network()
        if trust is not None:
            try:
                if hasattr(trust, "get_top_validators"):
                    validators_active = len(trust.get_top_validators(count=5))
            except Exception:
                pass

        return jsonify({
            "name": "BrunoCoin", "ticker": "BRN",
            "height": chain.db.height(),
            "tip_hash": chain.db.tip_hash(),
            "utxos": chain.db.count_utxos(),
            "mempool": len(chain.db.all_mempool(limit=1000)),
            "peers": peer_count,
            "reward": chain.current_reward(chain.db.height() + 1),
            "difficulty": chain.current_difficulty(),
            "fila_tamanho": len(chain.fila_mineracao),
            "rede_descentralizada": chain.rede_descentralizada,
            "validators_active": validators_active,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/fee-estimate", methods=["GET"])
def fee_estimate():
    try:
        chain = _get_chain()
        return jsonify({
            "success": True,
            "low": chain.estimate_fee("low"),
            "medium": chain.estimate_fee("medium"),
            "high": chain.estimate_fee("high"),
            "min_relay_fee": int(MIN_RELAY_FEE),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/work", methods=["GET"])
def work():
    try:
        chain = _get_chain()
        return jsonify({
            "success": True,
            "height": chain.db.height(),
            "cumulative_work": chain.cumulative_work(),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/nonce/<pubkey>", methods=["GET"])
def get_nonce(pubkey):
    try:
        return jsonify({
            "success": True, "pubkey": pubkey,
            "next_nonce": _get_chain().db.get_nonce_for_pubkey(pubkey),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# HD WALLET
# ============================================================
@app.route("/api/hd/create", methods=["POST"])
@require_rate_limit("hd_create")
def hd_create():
    if not _HAS_HD:
        return jsonify({
            "ok": False, "msg": "HDWalletManager nao existe em wallet.py",
        }), 503
    try:
        data = request.get_json(force=True) or {}
        strength = int(data.get("strength", 128))
        if strength not in (128, 160, 192, 224, 256):
            return jsonify({"ok": False, "msg": "strength invalido"}), 400
        result = HDWalletManager.create(strength=strength)
        _registrar_pubkey(result["address"], result.get("public_key", ""))
        resp = jsonify({"ok": True, "warning": "GUARDE o mnemonico.", **result})
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hd/derive", methods=["POST"])
@require_rate_limit("hd_derive")
def hd_derive():
    if not _HAS_HD:
        return jsonify({
            "ok": False, "msg": "HDWalletManager nao existe em wallet.py",
        }), 503
    try:
        data = request.get_json(force=True) or {}
        mn = data.get("mnemonic", "").strip()
        index = int(data.get("index", 0))
        if not HDWalletManager.validate_mnemonic(mn):
            return jsonify({"ok": False, "msg": "Mnemonico invalido"}), 400
        result = HDWalletManager.from_mnemonic(mn, index=index)
        _registrar_pubkey(result["address"], result.get("public_key", ""))
        resp = jsonify({"ok": True, **result})
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# PEERS SCORE / PEERS
# ============================================================
@app.route("/api/peers/score", methods=["GET"])
def peers_score():
    try:
        chain = _get_chain()
        return jsonify({
            "success": True,
            "peers": chain.db.listar_peers(apenas_ativos=False),
            "banned": chain.db.listar_peers_maus(score_min=-100),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/peers", methods=["GET"])
def peers():
    try:
        chain = _get_chain()
        return jsonify({
            "count": chain.db.contar_peers(apenas_ativos=False),
            "active": chain.db.contar_peers(apenas_ativos=True),
            "peers": chain.db.listar_peers(apenas_ativos=False),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# TRUST NETWORK
# ============================================================
@app.route("/api/trust/stats", methods=["GET"])
@require_rate_limit("trust_stats")
def trust_stats():
    trust = _get_trust_network()
    if trust is None:
        return jsonify({"ok": False, "msg": "TrustNetwork indisponivel"}), 503
    try:
        if hasattr(trust, "peers"):
            peers = trust.peers
            total = len(peers)
            trusted = sum(1 for p in peers.values() if p.get("status") == "trusted")
            banned = sum(1 for p in peers.values() if p.get("status") == "banned")
            return jsonify({
                "ok": True, "total_nodes": total,
                "connected_nodes": total,
                "validators_count": trusted,
                "banned_nodes": banned,
                "avg_trust": 100.0,
            })
        stats = trust.get_network_stats()
        return jsonify({"ok": True, **stats})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/trust/nodes", methods=["GET"])
@require_rate_limit("trust_nodes")
def trust_nodes():
    trust = _get_trust_network()
    if trust is None:
        return jsonify({"ok": False, "msg": "TrustNetwork indisponivel"}), 503
    try:
        limit = int(request.args.get("limit", 100))

        if hasattr(trust, "peers"):
            nodes = []
            for pubkey, data in trust.peers.items():
                nodes.append({
                    "node_id": pubkey,
                    "address": "desconhecido",
                    "trust_score": data.get("score", 0),
                    "trust_level": data.get("status", "new"),
                    "is_validator": data.get("status") == "trusted",
                })
            nodes.sort(key=lambda x: x["trust_score"], reverse=True)
            return jsonify({"ok": True, "count": len(nodes), "nodes": nodes[:limit]})

        from dataclasses import asdict
        nodes_raw = list(trust.nodes.values()) if hasattr(trust, "nodes") else []
        nodes = [asdict(n) for n in nodes_raw]
        nodes.sort(key=lambda x: x.get("trust_score", 0), reverse=True)
        return jsonify({"ok": True, "count": len(nodes), "nodes": nodes[:limit]})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# SYNC
# ============================================================
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
        heights = [int(p.get("height", 0) or 0)
                   for p in (todos or []) if isinstance(p, dict)]
        if heights:
            target_h = max([target_h] + heights)
    except Exception:
        pass
    result["sync"]["target"] = target_h

    try:
        if target_h > 0:
            result["sync"]["percent"] = min(
                100.0, round(local_h / target_h * 100, 2))
    except Exception:
        pass

    try:
        result["miner_target"] = chain.db.get_meta("miner_address") or ""
    except Exception:
        pass

    try:
        if L2_ENABLED and btc_config is not None:
            result["bridge"]["onramp_ativo"] = True
            result["bridge"]["taxa"] = int(
                getattr(btc_config, "BRN_PER_BTC", 0) or 0)
            result["bridge"]["btc_cofre"] = (
                getattr(btc_config, "BTC_RECEIVE_ADDRESS", "") or "")
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
            }), 200

        data = sync.get_status()

        local_h = int(data.get("local_height", 0))
        if local_h < 0:
            local_h = _get_chain().db.height()
            data["local_height"] = local_h
            data["synced"] = True
            data["progress"] = 100.0

        target_h = int(data.get("target_height", local_h))
        if target_h < 0:
            target_h = local_h

        return jsonify({
            "ok": True,
            "synced": bool(data.get("synced", True)),
            "syncing": bool(data.get("syncing", False)),
            "forking": bool(data.get("forking", False)),
            "progress": round(float(data.get("progress", 100.0)), 2),
            "local_height": local_h,
            "target_height": target_h,
            "blocos_faltando": max(0, target_h - local_h),
            "eta_segundos": data.get("eta_segundos"),
            "eta_legivel": data.get("eta_legivel", "sincronizado"),
            "active_peer": data.get("active_peer"),
            "peers_known": int(data.get("peers_known", 0)),
            "reorgs_total": int(data.get("reorgs_total", 0)),
            "last_reorg_height": int(data.get("last_reorg_height", -1)),
            "last_error": data.get("last_error", ""),
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# CONTRATOS
# ============================================================
@app.route("/api/contract/deploy", methods=["POST"])
@require_rate_limit("contract_deploy")
def contract_deploy():
    try:
        data = request.get_json(force=True) or {}
        owner = (data.get("owner") or "").strip()
        code = data.get("code")
        metadata = data.get("metadata")

        if not WalletManager.validate_address(owner):
            return jsonify({"ok": False, "msg": "owner invalido"}), 400
        if not isinstance(code, dict):
            return jsonify({"ok": False, "msg": "code deve ser dict"}), 400

        try:
            from contracts import ContractVM, deploy
        except ImportError as e:
            return jsonify({
                "ok": False,
                "msg": f"contracts.py nao encontrado: {e}",
            }), 500

        ok, msg = ContractVM.validate(code)
        if not ok:
            return jsonify({"ok": False, "msg": msg}), 400
        r = deploy(_get_chain(), owner, code, metadata)
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/contract/call", methods=["POST"])
@require_rate_limit("contract_call")
def contract_call():
    try:
        data = request.get_json(force=True) or {}
        contract_id = (data.get("contract_id") or "").strip()
        caller = (data.get("caller") or "").strip()
        args = data.get("args") or {}

        if not contract_id:
            return jsonify({"ok": False, "msg": "contract_id obrigatorio"}), 400

        try:
            from contracts import call as _call
        except ImportError as e:
            return jsonify({
                "ok": False,
                "msg": f"contracts.py nao encontrado: {e}",
            }), 500

        r = _call(_get_chain(), contract_id, caller, args)
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/contract/<contract_id>", methods=["GET"])
def contract_info(contract_id):
    try:
        chain = _get_chain()
        c = chain.db.contract_get(contract_id)
        if not c:
            return jsonify({"ok": False, "msg": "contrato nao existe"}), 404
        return jsonify({
            "ok": True,
            "contract": c,
            "state": chain.db.contract_get_state(contract_id),
            "events": chain.db.contract_get_events(contract_id, limit=100),
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/contracts", methods=["GET"])
def contracts_list():
    try:
        chain = _get_chain()
        owner = (request.args.get("owner") or "").strip() or None
        return jsonify({
            "ok": True,
            "contracts": chain.db.contract_list(owner=owner, limit=1000),
            "total": chain.db.contract_count(),
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# L2
# ============================================================
@app.route("/api/l2/quote", methods=["GET"])
def l2_quote():
    l2 = _get_l2()
    if not l2:
        return jsonify({"error": "L2 desabilitado"}), 503
    try:
        sats = int(request.args.get("btc_sats", 100000))
        q = l2.quote(sats)
        q["btc_address"] = btc_config.BTC_RECEIVE_ADDRESS
        q["network"] = btc_config.BTC_NETWORK
        q["min_confirmations"] = btc_config.BTC_MIN_CONFIRMATIONS
        return jsonify(q)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/l2/create", methods=["POST"])
@require_rate_limit("l2_create")
def l2_create():
    l2 = _get_l2()
    if not l2:
        return jsonify({"error": "L2 desabilitado"}), 503
    try:
        data = request.get_json(force=True) or {}
        btc_sats = int(data.get("btc_sats", 0))
        buyer = (data.get("buyer_brn") or "").strip()

        if not buyer:
            return jsonify({"error": "buyer_brn obrigatorio"}), 400
        if not WalletManager.validate_address(buyer):
            return jsonify({"error": "buyer_brn invalido"}), 400
        if btc_sats <= 0:
            return jsonify({"error": "btc_sats deve ser > 0"}), 400

        order = l2.create_order(btc_sats, buyer)
        return jsonify(order)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/l2/order/<escrow_id>", methods=["GET"])
def l2_order(escrow_id):
    l2 = _get_l2()
    if not l2:
        return jsonify({"error": "L2 desabilitado"}), 503
    try:
        order = l2.get_order(escrow_id)
        if not order:
            return jsonify({"error": "escrow not found"}), 404
        status_map = {
            "OPEN": "Aguardando BTC no endereco",
            "BTC_DETECTED": "BTC confirmado via RPC",
            "RELEASED": "Minerado! BRN creditado na carteira",
            "EXPIRED": "Expirado (24h)",
            "CANCELLED": "Cancelado",
        }
        _status = order.get("status", "UNKNOWN")
        order["status_desc"] = status_map.get(_status, _status)
        return jsonify(order)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/l2/list", methods=["GET"])
def l2_list():
    l2 = _get_l2()
    if not l2:
        return jsonify({"error": "L2 desabilitado"}), 503
    try:
        buyer = request.args.get("buyer")
        status = request.args.get("status", "OPEN")
        chain = _get_chain()
        if not hasattr(chain.db, "get_l2_escrows"):
            return jsonify({"error": "db.py nao tem get_l2_escrows"}), 500
        if buyer:
            escrows = [e for e in chain.db.get_l2_escrows()
                       if e.get("buyer") == buyer]
        elif status != "ALL":
            escrows = chain.db.get_l2_escrows(status=status)
        else:
            escrows = chain.db.get_l2_escrows()
        return jsonify(escrows[:1000])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/l2/claim", methods=["POST"])
@require_rate_limit("l2_claim")
def l2_claim():
    l2 = _get_l2()
    if not l2:
        return jsonify({"error": "L2 desabilitado"}), 503

    try:
        data = request.get_json(force=True) or {}
        btc_txid = (data.get("btc_txid") or "").strip()
        buyer_brn = (data.get("buyer_brn") or "").strip()

        if not btc_txid or not buyer_brn:
            return jsonify({
                "error": "btc_txid e buyer_brn obrigatorios",
            }), 400
        if not WalletManager.validate_address(buyer_brn):
            return jsonify({"error": "buyer_brn invalido"}), 400

        try:
            if _get_chain().db.is_btc_txid_used(btc_txid):
                return jsonify({"error": "txid BTC ja utilizado"}), 400
        except Exception:
            pass

        escrows = l2.list_orders(buyer=buyer_brn, status="OPEN")
        if not escrows:
            return jsonify({
                "error": "Nenhuma ordem em aberto para este endereco",
            }), 404
        escrow = escrows[0]

        expected_sats = int(
            escrow.get("btc_expected_sats")
            or escrow.get("btc_amount_sats") or 0
        )
        if expected_sats <= 0:
            return jsonify({"error": "escrow sem valor BTC"}), 500
        min_ok = int(expected_sats * 0.99)
        target_addr = escrow.get("btc_address", "")
        min_confs = int(getattr(btc_config, "BTC_MIN_CONFIRMATIONS", 3))
        network = getattr(btc_config, "BTC_NETWORK", "mainnet")

        import requests as _rq

        actual_sats = None
        confs = 0
        last_err = ""

        for base in getattr(btc_config, "BTC_RPCS", []):
            try:
                base = base.rstrip("/")
                if network == "testnet":
                    base = base.replace("/api", "/testnet/api")

                r = _rq.get(f"{base}/tx/{btc_txid}", timeout=15)
                if r.status_code == 404:
                    last_err = "txid nao encontrado"
                    continue
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    continue

                txdata = r.json()
                status = txdata.get("status") or {}
                if not status.get("confirmed"):
                    return jsonify({
                        "error": "tx BTC ainda nao confirmada",
                    }), 400

                r2 = _rq.get(f"{base}/blocks/tip/height", timeout=10)
                tip = int(r2.text.strip()) if r2.status_code == 200 else 0
                block_h = int(status.get("block_height", 0))
                confs = max(0, tip - block_h + 1) if tip and block_h else 0

                if confs < min_confs:
                    return jsonify({
                        "error": f"aguardando {min_confs} confs (tem {confs})",
                        "confs": confs, "min_confs": min_confs,
                    }), 400

                actual_sats = sum(
                    int(v.get("value", 0))
                    for v in txdata.get("vout", [])
                    if v.get("scriptpubkey_address") == target_addr
                )
                if actual_sats > 0:
                    break
                else:
                    last_err = "txid nao paga o escrow"

            except Exception as _e:
                last_err = str(_e)
                continue

        if not actual_sats:
            return jsonify({
                "error": last_err or "txid invalido",
            }), 400

        if actual_sats < min_ok:
            return jsonify({
                "error": (f"BTC insuficiente: pago {actual_sats/1e8:.8f} BTC, "
                          f"esperado minimo {min_ok/1e8:.8f} BTC"),
                "btc_pago_sats": actual_sats,
                "btc_esperado_sats": expected_sats,
            }), 400

        res = l2.apply_btc_deposit(
            escrow_id=escrow["escrow_id"],
            btc_txid=btc_txid,
            btc_amount_sats=actual_sats,
        )

        if not res.get("ok"):
            return jsonify({
                "error": res.get("msg", "falha ao processar claim"),
            }), 400

        brn_recebido_sats = int(
            escrow.get("brn_amount")
            or escrow.get("brn_amount_sats") or 0
        )

        return jsonify({
            "ok": True,
            "msg": "Claim processado! Aguardando mineracao.",
            "escrow_id": escrow["escrow_id"],
            "btc_pago_sats": actual_sats,
            "btc_pago_btc": actual_sats / 1e8,
            "btc_esperado_sats": expected_sats,
            "confs": confs,
            "brn_recebido": brn_recebido_sats / 1e8,
            "txid": res.get("txid"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/l2/stats", methods=["GET"])
def l2_stats():
    try:
        if not L2_ENABLED:
            return jsonify({"error": "L2 desabilitado"}), 503
        l2 = _get_l2()
        s = l2.stats() if l2 else {
            "open": 0, "btc_detected": 0, "released": 0,
        }
        s["btc_address"] = getattr(btc_config, "BTC_RECEIVE_ADDRESS", "")
        s["network"] = getattr(btc_config, "BTC_NETWORK", "")
        s["rate"] = f"1 BTC = {getattr(btc_config, 'BRN_PER_BTC', 0)} BRN"
        return jsonify(s)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/l2/cancel/<escrow_id>", methods=["POST"])
@require_rate_limit("l2_cancel")
def l2_cancel(escrow_id):
    if not L2_ENABLED:
        return jsonify({"error": "L2 desabilitado"}), 503
    try:
        chain = _get_chain()
        if not hasattr(chain.db, "get_l2_escrow_by_id"):
            return jsonify({
                "error": "db.py nao tem get_l2_escrow_by_id",
            }), 500
        escrow = chain.db.get_l2_escrow_by_id(escrow_id)
        if not escrow:
            return jsonify({"error": "escrow not found"}), 404
        if escrow.get("status") != "OPEN":
            return jsonify({
                "error": f"so pode cancelar OPEN, status {escrow.get('status')}",
            }), 400
        chain.db.update_l2_escrow_status(escrow_id, "CANCELLED")
        return jsonify({
            "ok": True, "escrow_id": escrow_id, "status": "CANCELLED",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# CONSENSUS / MULTI-SIG
# ============================================================
@app.route("/api/consensus/validators", methods=["GET"])
@require_rate_limit("status")
def consensus_validators():
    try:
        trust = _get_trust_network()
        if trust is None:
            return jsonify({"ok": False, "msg": "TrustNetwork indisponivel"}), 503

        top5_pubkeys = []
        if hasattr(trust, "get_top_validators"):
            try:
                top5_pubkeys = trust.get_top_validators(count=5)
            except Exception as e:
                log.warning(f"get_top_validators falhou: {e}")

        nodes = list(trust.nodes.values()) if hasattr(trust, "nodes") else []
        validators = []
        for n in nodes:
            pk = getattr(n, "public_key", "")
            if pk and pk in top5_pubkeys:
                validators.append({
                    "pubkey": pk[:32] + "...",
                    "pubkey_full": pk,
                    "node_id": getattr(n, "node_id", ""),
                    "address": getattr(n, "address", ""),
                    "trust_score": round(getattr(n, "trust_score", 0), 1),
                    "trust_level": getattr(n, "trust_level", "new"),
                    "connected": getattr(n, "connected", False),
                })
        validators.sort(key=lambda x: -x["trust_score"])

        return jsonify({
            "ok": True,
            "required_sigs": len(top5_pubkeys),
            "all_required": True,
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
# HEALTH / ROOT
# ============================================================
@app.route("/health", methods=["GET"])
def health():
    try:
        return jsonify({
            "ok": True,
            "height": _get_chain().db.height(),
            "version": "9.9.18",
            "chain_injected": _CHAIN is not None,
            "p2p_injected": _P2P_MANAGER is not None,
            "pool_injected": _POOL_MANAGER is not None,
            "sync_injected": _SYNC_MANAGER is not None,
            "trust_injected": _TRUST_NETWORK is not None,
            "l2_enabled": L2_ENABLED,
            "hd_wallet": _HAS_HD,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "name": "BRN Node API",
        "version": "9.9.18",
        "mode": "modo2_aberto",
        "l2_enabled": L2_ENABLED,
        "pool_enabled": _POOL_MANAGER is not None,
        "admin_protected": bool(ADMIN_TOKEN),
    })


# ============================================================
# SHUTDOWN
# ============================================================
@atexit.register
def _shutdown():
    log.info("encerrando server…")
    try:
        close_audit()
    except Exception:
        pass


# ============================================================
# MAIN (standalone)
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    print(f"BRN Server v9.9.18 (Proxy + Trust + Multi-Sig + History)")
    print(f"  → http://{WEB_HOST}:{port}")
    print(f"  L2: {'ATIVO' if L2_ENABLED else 'DESABILITADO'}")
    print(f"  Admin token: {'SIM' if ADMIN_TOKEN else 'NÃO'}")
    print(f"  HDWallet: {'SIM' if _HAS_HD else 'NÃO'}")

    reg = auto_registrar_carteira_atual()
    if reg.get("ok"):
        print(f"[server] auto-registro pubkey: {reg['msg']}")
    else:
        print(f"[server] auto-registro pubkey: pulado ({reg.get('msg')})")

    app.run(host=WEB_HOST, port=port, debug=False, threaded=True)
