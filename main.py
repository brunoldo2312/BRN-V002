"""server.py — Servidor HTTP Flask do nó BRN (v10.0)
================================================================
Expõe a interface esperada por main.py v13.6.3:
    app                         — Flask app
    set_chain(chain)            — injeta Blockchain
    set_p2p_manager(p2p)        — injeta P2PManager
    set_sync_manager(sync)      — injeta ChainSyncManager
    set_pool_manager(pool)      — injeta PoolManager
    set_trust_network(trust)    — injeta TrustNetwork
    auto_registrar_carteira_atual() -> dict
================================================================
"""
from __future__ import annotations

import os
import time
import logging
import threading
from decimal import Decimal, ROUND_DOWN
from functools import wraps

from flask import Flask, request, jsonify
from flask_cors import CORS

log = logging.getLogger("server")

app = Flask(__name__)
CORS(app)

_state = {"chain": None, "p2p": None, "sync": None, "pool": None, "trust": None}
_lock = threading.RLock()


def set_chain(chain):
    with _lock: _state["chain"] = chain
    log.info("server: chain injetada (altura=%s)", chain.db.height() if chain else -1)


def set_p2p_manager(p2p):
    with _lock: _state["p2p"] = p2p
    log.info("server: p2p injetado")


def set_sync_manager(sync):
    with _lock: _state["sync"] = sync
    log.info("server: sync injetado")


def set_pool_manager(pool):
    with _lock: _state["pool"] = pool
    log.info("server: pool injetado")


def set_trust_network(trust):
    with _lock: _state["trust"] = trust
    log.info("server: trust injetado")


def _get(k):
    with _lock: return _state[k]


# Rate limiting (graceful se security.py ausente)
try:
    from security import require_rate_limit
except ImportError:
    def require_rate_limit(_k):
        def deco(fn): return fn
        return deco


def _body():
    try: return request.get_json(force=True, silent=True) or {}
    except Exception: return {}


def _err(msg, status=400):
    r = jsonify({"ok": False, "error": msg, "msg": msg})
    r.status_code = status
    return r


def _ok(**kw):
    return jsonify({"ok": True, **kw})


# ============================================================
# HEALTH / STATUS
# ============================================================
@app.route("/health")
def health():
    chain = _get("chain")
    if chain is None:
        return jsonify({"ok": False, "error": "chain nao injetada"}), 503
    try:
        return jsonify({"ok": True, "height": chain.db.height(),
                        "version": "10.0", "ts": int(time.time())})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/status")
@require_rate_limit("status")
def api_status():
    chain = _get("chain")
    if chain is None:
        return _err("chain nao injetada", 503)
    try:
        h = chain.db.height()
        return jsonify({
            "height": h,
            "tip_hash": chain.db.tip_hash() or "",
            "mempool": len(chain.db.all_mempool(limit=1000)),
            "utxos": chain.db.count_utxos(),
            "difficulty": chain.current_difficulty(),
            "reward": chain.current_reward(h + 1),
            "l2_enabled": False,
            "pool_enabled": _get("pool") is not None,
            "fila_tamanho": len(getattr(chain, "fila_mineracao", {})),
            "rede_descentralizada": getattr(chain, "rede_descentralizada", False),
        })
    except Exception as e:
        log.exception("api_status")
        return _err(str(e), 500)


@app.route("/api/chain-info")
def api_chain_info():
    chain = _get("chain")
    if chain is None:
        return _err("chain nao injetada", 503)
    try:
        h = chain.db.height()
        return jsonify({
            "ok": True, "height": h,
            "tip_hash": chain.db.tip_hash() or "",
            "difficulty": chain.current_difficulty(),
            "reward": chain.current_reward(h + 1),
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route("/api/work")
@require_rate_limit("work")
def api_work():
    chain = _get("chain")
    if chain is None:
        return _err("chain nao injetada", 503)
    try:
        return jsonify({"ok": True, "cumulative_work": chain.cumulative_work()})
    except Exception as e:
        return _err(str(e), 500)


@app.route("/api/sync-info")
@require_rate_limit("sync_info")
def api_sync_info():
    chain = _get("chain")
    sync = _get("sync")
    if chain is None:
        return jsonify({"ok": False, "erro": "chain nao injetada"}), 503
    try:
        local_h = chain.db.height()
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500
    if sync is None:
        return jsonify({"ok": True, "sync": {
            "height": local_h, "target": local_h, "percent": 100.0,
            "synced": True, "syncing": False,
        }})
    try:
        s = sync.get_status()
        return jsonify({"ok": True, "sync": {
            "height": s.get("local_height", local_h),
            "target": s.get("target_height", local_h),
            "percent": s.get("progress", 100.0),
            "synced": s.get("synced", False),
            "syncing": s.get("syncing", False),
            "active_peer": s.get("active_peer"),
            "last_error": s.get("last_error", ""),
        }})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.route("/api/sync/status")
def api_sync_status():
    chain = _get("chain")
    sync = _get("sync")
    if chain is None:
        return _err("chain nao injetada", 503)
    if sync is None:
        h = chain.db.height()
        return jsonify({"synced": True, "syncing": False, "progress": 100.0,
                        "local_height": h, "target_height": h})
    try:
        return jsonify(sync.get_status())
    except Exception as e:
        return _err(str(e), 500)


# ============================================================
# BLOCKS
# ============================================================
def _ser_block(b):
    return {
        "height": b.get("height"),
        "hash": b.get("hash", ""),
        "prev_hash": b.get("prev_hash", ""),
        "timestamp": b.get("timestamp"),
        "nonce": b.get("nonce"),
        "merkle": b.get("merkle", ""),
        "difficulty": b.get("difficulty"),
        "transactions": [
            {"txid": t.get("txid", ""),
             "inputs": t.get("inputs", []),
             "outputs": t.get("outputs", [])}
            for t in b.get("transactions", [])
        ],
    }


@app.route("/api/blocks")
@require_rate_limit("list_blocks")
def api_blocks():
    chain = _get("chain")
    if chain is None:
        return jsonify({"error": "chain nao injetada"}), 503
    try:
        start = int(request.args.get("start", 0))
        limit = max(1, min(int(request.args.get("limit", 15)), 100))
        h = chain.db.height()
        end = min(h, start + limit - 1)
        blocks = []
        for i in range(start, end + 1):
            b = chain.db.get_block(i)
            if b:
                blocks.append(_ser_block(b))
        return jsonify({"blocks": blocks, "start": start, "count": len(blocks)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/block/<int:height>")
@require_rate_limit("get_block")
def api_block(height):
    chain = _get("chain")
    if chain is None:
        return jsonify({"error": "chain nao injetada"}), 503
    try:
        b = chain.db.get_block(height)
        if not b:
            return jsonify({"error": "bloco nao encontrado"}), 404
        return jsonify(_ser_block(b))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/latest")
def api_latest():
    chain = _get("chain")
    if chain is None:
        return jsonify({"error": "chain nao injetada"}), 503
    try:
        h = chain.db.height()
        out = []
        for i in range(max(0, h - 9), h + 1):
            b = chain.db.get_block(i)
            if b:
                out.append(_ser_block(b))
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# PORTFOLIO
# ============================================================
@app.route("/api/portfolio/<address>")
@require_rate_limit("portfolio")
def api_portfolio(address):
    chain = _get("chain")
    if chain is None:
        return jsonify({"error": "chain nao injetada"}), 503
    try:
        from bech32 import validate_address
        if not validate_address(address):
            return jsonify({"error": "endereco invalido"}), 400
        utxos = chain.db.get_utxos(address)
        total = sum(int(u.get("amount", 0)) for u in utxos)
        return jsonify({"portfolio": {"BRN": total, "KYC": 0},
                        "utxos_count": len(utxos)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# WALLET
# ============================================================
@app.route("/api/nova-carteira", methods=["POST"])
@require_rate_limit("nova_carteira")
def api_nova_carteira():
    try:
        from wallet import Wallet
        w = Wallet()
        return jsonify({"success": True, "address": w.address,
                        "private_key": w.priv_hex, "public_key": w.pub_hex})
    except Exception as e:
        log.exception("nova-carteira")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# TRANSFER
# ============================================================
@app.route("/api/transfer", methods=["POST"])
@require_rate_limit("transfer")
def api_transfer():
    chain = _get("chain")
    if chain is None:
        return jsonify({"ok": False, "error": "chain nao injetada"}), 503
    b = _body()
    try:
        from_addr = (b.get("from") or "").strip()
        to_addr = (b.get("to") or "").strip()
        priv_hex = (b.get("private_key") or "").strip()
        pub_hex = (b.get("pubkey") or "").strip()
        asset = b.get("asset_id", "BRN")
        if asset not in ("BRN", ""):
            return jsonify({"ok": False, "error": "apenas BRN"}), 400

        from bech32 import validate_address
        if not validate_address(from_addr):
            return jsonify({"ok": False, "error": "from invalido"}), 400
        if not validate_address(to_addr):
            return jsonify({"ok": False, "error": "to invalido"}), 400
        if not priv_hex or not pub_hex:
            return jsonify({"ok": False, "error": "priv/pub obrigatorios"}), 400

        UNIT = 10 ** 8
        try:
            amount = int((Decimal(str(b.get("amount", "0"))) * UNIT)
                         .quantize(Decimal("1"), rounding=ROUND_DOWN))
        except Exception:
            return jsonify({"ok": False, "error": "amount invalido"}), 400
        if amount <= 0:
            return jsonify({"ok": False, "error": "amount <= 0"}), 400

        from crypto import pubkey_to_address
        if pubkey_to_address(pub_hex) != from_addr:
            return jsonify({"ok": False, "error": "pubkey nao corresponde a from"}), 400

        utxos = chain.db.get_utxos(from_addr)
        if not utxos:
            return jsonify({"ok": False, "error": "sem UTXOs"}), 400

        MIN_FEE = 1000
        selected, s = [], 0
        for u in utxos:
            selected.append(u)
            s += int(u["amount"])
            if s >= amount + MIN_FEE:
                break
        if s < amount + MIN_FEE:
            return jsonify({"ok": False, "error": "saldo insuficiente"}), 400

        change = s - amount - MIN_FEE
        nonce = chain.db.get_nonce_for_pubkey(pub_hex)

        inputs = [{"txid": u["txid"], "vout": int(u["vout"]),
                   "pubkey": pub_hex, "signature": ""} for u in selected]
        outputs = [{"address": to_addr, "amount": amount, "pubkey": ""}]
        if change > 0:
            outputs.append({"address": from_addr, "amount": change, "pubkey": ""})

        tx = {"inputs": inputs, "outputs": outputs,
              "timestamp": int(time.time()), "locktime": 0, "nonce": nonce}

        from blockchain import txid as calc_txid, signing_hash
        tx["txid"] = calc_txid(tx)

        from crypto import sign_ecdsa
        sig_hex = sign_ecdsa(bytes.fromhex(priv_hex), signing_hash(tx)).hex()
        for inp in tx["inputs"]:
            inp["signature"] = sig_hex

        ok, msg = chain.submit_tx(tx)
        if not ok:
            return jsonify({"ok": False, "error": msg}), 400

        p2p = _get("p2p")
        if p2p is not None and hasattr(p2p, "broadcast_tx"):
            try: p2p.broadcast_tx(tx)
            except Exception: pass

        return jsonify({"ok": True, "txid": tx["txid"]})
    except Exception as e:
        log.exception("api_transfer")
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# TRANSACTIONS
# ============================================================
@app.route("/api/tx-status/<txid>")
@require_rate_limit("tx_status")
def api_tx_status(txid):
    chain = _get("chain")
    if chain is None:
        return jsonify({"ok": False, "error": "chain nao injetada"}), 503
    try:
        h = chain.get_transaction_block_height(txid)
        if h is None or h < 0:
            if chain.db.has_mempool(txid):
                return jsonify({"ok": True, "status": "pending", "height": None})
            return jsonify({"ok": False, "status": "not_found"}), 404
        return jsonify({"ok": True, "status": "confirmed", "height": h,
                        "confirmations": chain.count_confirmations(txid)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/minhas-txs/<address>")
@require_rate_limit("minhas_txs")
def api_minhas_txs(address):
    chain = _get("chain")
    if chain is None:
        return jsonify({"ok": False, "error": "chain nao injetada"}), 503
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 500))
        fn = getattr(chain.db, "get_txs_for_address", None)
        if fn is None:
            return jsonify({"ok": True, "transactions": [], "count": 0,
                            "note": "endpoint indisponivel no db"})
        rows = fn(address, limit=limit) or []
        return jsonify({"ok": True, "transactions": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# MINING
# ============================================================
@app.route("/api/miner/start", methods=["POST"])
@require_rate_limit("miner_start")
def api_miner_start():
    chain = _get("chain")
    if chain is None:
        return jsonify({"ok": False, "error": "chain nao injetada"}), 503
    b = _body()
    address = (b.get("address") or "").strip()
    pubkey = (b.get("miner_pubkey") or b.get("pubkey") or "").strip()
    if not address or not pubkey:
        return jsonify({"ok": False, "error": "address e miner_pubkey obrigatorios"}), 400
    try:
        from miner_loop import get_miner
        ok, msg = get_miner(chain).start(address, pubkey)
        return jsonify({"ok": ok, "msg": msg})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/miner/stop", methods=["POST"])
@require_rate_limit("miner_stop")
def api_miner_stop():
    chain = _get("chain")
    if chain is None:
        return jsonify({"ok": False, "error": "chain nao injetada"}), 503
    try:
        from miner_loop import get_miner
        ok, msg = get_miner(chain).stop()
        return jsonify({"ok": ok, "msg": msg})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/miner/status")
@require_rate_limit("miner_status")
def api_miner_status():
    chain = _get("chain")
    if chain is None:
        return jsonify({"running": False, "error": "chain nao injetada"}), 503
    try:
        from miner_loop import get_miner
        return jsonify(get_miner(chain).status())
    except Exception as e:
        return jsonify({"running": False, "error": str(e)}), 500


# ============================================================
# PEERS / BOOTSTRAP
# ============================================================
@app.route("/api/peers")
def api_peers():
    p2p = _get("p2p")
    if p2p is None:
        return jsonify({"ok": True, "peers": [], "count": 0, "active": 0})
    try:
        st = p2p.get_status()
        peers = st.get("peers", []) if isinstance(st, dict) else []
        return jsonify({"ok": True, "peers": peers, "count": len(peers),
                        "active": st.get("peer_count", len(peers))})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/bootstrap/add", methods=["POST"])
@require_rate_limit("bootstrap_add")
def api_bootstrap_add():
    chain = _get("chain")
    if chain is None:
        return jsonify({"ok": False, "error": "chain nao injetada"}), 503
    b = _body()
    address = (b.get("address") or "").strip()
    pubkey = (b.get("pubkey") or "").strip()
    if not address or not pubkey:
        return jsonify({"ok": False, "error": "address e pubkey obrigatorios"}), 400
    try:
        chain.adicionar_na_fila(address, pubkey)
        return jsonify({"ok": True,
                        "fila_tamanho": len(chain.fila_mineracao),
                        "rede_descentralizada": chain.rede_descentralizada})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# POOL
# ============================================================
@app.route("/api/pool/register", methods=["POST"])
@require_rate_limit("pool_register")
def api_pool_register():
    pool = _get("pool")
    if pool is None:
        return jsonify({"ok": False, "error": "pool indisponivel"}), 503
    b = _body()
    try:
        return jsonify(pool.register(
            address=(b.get("address") or "").strip(),
            pubkey=(b.get("pubkey") or "").strip(),
            miner_id=b.get("miner_id"),
        ))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/pool/work", methods=["POST"])
@require_rate_limit("pool_work")
def api_pool_work():
    pool = _get("pool")
    if pool is None:
        return jsonify({"ok": False, "error": "pool indisponivel"}), 503
    try:
        return jsonify(pool.get_work(miner_id=_body().get("miner_id", "")))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/pool/submit", methods=["POST"])
@require_rate_limit("pool_submit")
def api_pool_submit():
    pool = _get("pool")
    if pool is None:
        return jsonify({"ok": False, "error": "pool indisponivel"}), 503
    b = _body()
    try:
        return jsonify(pool.submit_share(
            miner_id=b.get("miner_id", ""),
            nonce=int(b.get("nonce", 0)),
            hash_hex=b.get("hash", ""),
        ))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/pool/heartbeat", methods=["POST"])
@require_rate_limit("pool_heartbeat")
def api_pool_heartbeat():
    pool = _get("pool")
    if pool is None:
        return jsonify({"ok": False, "error": "pool indisponivel"}), 503
    b = _body()
    try:
        return jsonify(pool.heartbeat(
            miner_id=b.get("miner_id", ""),
            hashrate=float(b.get("hashrate", 0)),
            hashes_total=int(b.get("hashes_total", 0)),
        ))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/pool/disconnect", methods=["POST"])
@require_rate_limit("pool_disconnect")
def api_pool_disconnect():
    pool = _get("pool")
    if pool is None:
        return jsonify({"ok": False, "error": "pool indisponivel"}), 503
    try:
        return jsonify(pool.disconnect(miner_id=_body().get("miner_id", "")))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/pool/stats")
@require_rate_limit("pool_stats")
def api_pool_stats():
    pool = _get("pool")
    if pool is None:
        return jsonify({"ok": False, "error": "pool indisponivel"}), 503
    try:
        return jsonify(pool.stats())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/pool/miners")
@require_rate_limit("pool_miners")
def api_pool_miners():
    pool = _get("pool")
    if pool is None:
        return jsonify({"ok": False, "error": "pool indisponivel"}), 503
    try:
        return jsonify({"ok": True, "miners": pool.miners_list()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# TRUST
# ============================================================
@app.route("/api/trust/stats")
@require_rate_limit("trust_stats")
def api_trust_stats():
    trust = _get("trust")
    if trust is None:
        return jsonify({"ok": False, "error": "trust indisponivel"}), 503
    try:
        return jsonify(trust.get_network_stats())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/trust/nodes")
@require_rate_limit("trust_nodes")
def api_trust_nodes():
    trust = _get("trust")
    if trust is None:
        return jsonify({"ok": False, "error": "trust indisponivel"}), 503
    try:
        return jsonify({"ok": True, "nodes": trust.list_all_nodes()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# AUTO-REGISTRO DA CARTEIRA ATUAL
# ============================================================
def auto_registrar_carteira_atual() -> dict:
    chain = _get("chain")
    if chain is None:
        return {"ok": False, "msg": "chain nao injetada"}
    try:
        pw = (os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
              or os.environ.get("BRN_NODE_PASSWORD", "").strip())
        if not pw:
            return {"ok": False, "msg": "senha nao configurada"}
        from secure_store import load_current_wallet
        w = load_current_wallet(pw)
        if not w or not w.get("ok"):
            return {"ok": False, "msg": "current_wallet.enc nao decifrou"}
        addr = (w.get("address") or "").strip()
        pub = (w.get("public_key") or w.get("pub_hex") or w.get("pubkey") or "").strip()
        if not addr.startswith("brn1") or not pub:
            return {"ok": False, "msg": "address/pubkey ausentes"}
        fn = getattr(chain.db, "register_pubkey", None)
        if fn is None:
            return {"ok": False, "msg": "db.register_pubkey indisponivel"}
        fn(pub, addr)
        return {"ok": True, "msg": f"{addr[:16]}... ({pub[:16]}...)"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}
