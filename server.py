"""server.py — Servidor Web/API do nó BRN
Versão: 13.9.25
================================================================
Este arquivo expõe a API HTTP (porta 5000) que a GUI (app_wallet_v3)
e o main.py consomem.
================================================================
"""
from __future__ import annotations
import logging
import os
from typing import Any, Dict, Optional

from flask import Flask, jsonify, request, redirect
from flask_cors import CORS

log = logging.getLogger("server")

# ============================================================
# ESTADO GLOBAL
# ============================================================
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

_chain = None
_p2p_manager = None
_pool_manager = None
_sync_manager = None
_trust_network = None
_sk_validation_state: Dict[str, Any] = {}
_http_sync_state: Dict[str, Any] = {}
_BRN_PEER_ID = ""

# ============================================================
# INJEÇÃO DE DEPENDÊNCIAS (chamadas pelo main.py)
# ============================================================
def set_chain(chain) -> None:
    global _chain
    _chain = chain
    log.info("server: chain injetada (height=%s)", 
             getattr(_chain.db, "height", lambda: -1)())

def set_peer_id(peer_id: str) -> None:
    global _BRN_PEER_ID
    _BRN_PEER_ID = peer_id
    log.info("[server] peer_id=%s", _BRN_PEER_ID)

def set_p2p_manager(p2p) -> None:
    global _p2p_manager
    _p2p_manager = p2p

def set_pool_manager(pool) -> None:
    global _pool_manager
    _pool_manager = pool

def set_sync_manager(sync) -> None:
    global _sync_manager
    _sync_manager = sync

def set_trust_network(trust) -> None:
    global _trust_network
    _trust_network = trust

def set_sk_validation_state(state: dict) -> None:
    global _sk_validation_state
    _sk_validation_state = state

def set_http_sync_state(state: dict) -> None:
    global _http_sync_state
    _http_sync_state = state

# ============================================================
# ROTAS BÁSICAS (HEALTH E STATUS)
# ============================================================
@app.route("/health", methods=["GET"])
def health():
    h = -1
    if _chain and hasattr(_chain, "db"):
        try:
            h = _chain.db.height()
        except Exception:
            pass
    return jsonify({
        "ok": True,
        "height": h,
        "peer_id": _BRN_PEER_ID,
        "l2_enabled": True,
        "chain_injected": _chain is not None,
        "sync_injected": _sync_manager is not None,
        "version": "13.9.25"
    })

@app.route("/api/peer_id", methods=["GET"])
def api_peer_id():
    return jsonify({"ok": True, "peer_id": _BRN_PEER_ID})

@app.route("/api/status", methods=["GET"])
def api_status():
    if not _chain:
        return jsonify({"ok": False, "msg": "chain nao injetada"}), 503
    
    try:
        h = _chain.db.height()
        tip = _chain.db.tip_hash()
        # Conta peers
        n_peers = 0
        if _chain.db and hasattr(_chain.db, "listar_peers"):
            n_peers = len(_chain.db.listar_peers(apenas_ativos=True) or [])
            
        return jsonify({
            "name": "BrunoCoin",
            "ticker": "BRN",
            "height": h,
            "tip_hash": tip,
            "peers": n_peers,
            "reward": _chain.current_reward(max(h, 1)),
            "difficulty": _chain.current_difficulty(),
            "mempool": _chain.db.mempool_count() if hasattr(_chain.db, "mempool_count") else 0
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

@app.route("/api/chain-info", methods=["GET"])
def api_chain_info():
    if not _chain:
        return jsonify({"ok": False, "msg": "chain nao injetada"}), 503
    try:
        h = _chain.db.height()
        return jsonify({
            "success": True,
            "name": "BrunoCoin",
            "height": h,
            "tip_hash": _chain.db.tip_hash(),
            "reward": _chain.current_reward(max(h, 1)),
            "difficulty": _chain.current_difficulty()
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

@app.route("/api/sync-info", methods=["GET"])
def api_sync_info():
    # Retorna o estado do Fast-Sync / Bootstrap para a GUI
    state = dict(_http_sync_state) if _http_sync_state else {}
    state.update({
        "sk_installed": _sk_validation_state.get("ok", False),
        "validators_ready": _sk_validation_state.get("validators_ready", False),
        "auto_mine_status": _sk_validation_state.get("auto_mine_status", "idle"),
        "bridge": {
            "wif_configured": bool(os.environ.get("BRN_BTC_WIF")),
            "receive_address": os.environ.get("BRN_BTC_RECEIVE_ADDRESS", ""),
            "network": os.environ.get("BRN_BTC_NETWORK", "testnet")
        }
    })
    return jsonify(state)

# ============================================================
# ROTAS DO POOL / MINERAÇÃO (chamadas pelo main.py)
# ============================================================
@app.route("/api/pool/local_status", methods=["GET"])
def api_pool_local_status():
    running = False
    if _pool_manager and hasattr(_pool_manager, "is_local_running"):
        running = _pool_manager.is_local_running()
    return jsonify({"ok": True, "running": running})

@app.route("/api/pool/start_local", methods=["POST"])
def api_pool_start_local():
    data = request.get_json(silent=True) or {}
    addr = data.get("address")
    pubkey = data.get("pubkey")
    
    if not addr or not pubkey:
        return jsonify({"ok": False, "msg": "address e pubkey obrigatorios"}), 400
        
    if not _pool_manager:
        return jsonify({"ok": False, "msg": "PoolManager nao injetado (no e cliente)"}), 409
        
    try:
        if hasattr(_pool_manager, "start_local"):
            r = _pool_manager.start_local(addr, pubkey)
            return jsonify(r)
        return jsonify({"ok": False, "msg": "metodo start_local indisponivel"}), 501
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

@app.route("/api/pool/stop_local", methods=["POST"])
def api_pool_stop_local():
    if _pool_manager and hasattr(_pool_manager, "stop_local"):
        try:
            _pool_manager.stop_local()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)}), 500
    return jsonify({"ok": False, "msg": "PoolManager indisponivel"}), 409

# ============================================================
# ROTA DA GUI (WALLET)
# ============================================================
@app.route("/wallet", methods=["GET"])
@app.route("/wallet/<peer_id>", methods=["GET"])
def serve_wallet(peer_id=None):
    # Tenta redirecionar para o index da carteira ou para o FastAPI
    try:
        from server_fastapi import app as fastapi_app
        # Se FastAPI estiver rodando, ele cuida da rota. 
        # Aqui eh apenas fallback.
    except ImportError:
        pass

    # Fallback: serve o HTML diretamente se existir
    wallet_path = os.path.join(os.path.dirname(__file__), "index_wallet.html")
    if os.path.exists(wallet_path):
        from flask import send_file
        return send_file(wallet_path)
        
    return jsonify({
        "ok": False, 
        "msg": "index_wallet.html nao encontrado no servidor Flask.",
        "peer_id": _BRN_PEER_ID
    }), 404

# ============================================================
# ROTAS DE DEBUG E DIAGNOSTICO
# ============================================================
@app.route("/api/mining_gate", methods=["GET"])
def api_mining_gate():
    return jsonify({
        "ok": True,
        "sk_installed": _sk_validation_state.get("ok", False),
        "mining_allowed": _sk_validation_state.get("validators_ready", False),
        "reason": _sk_validation_state.get("validators_reason", ""),
        "detail": _sk_validation_state
    })

def run_server(host="0.0.0.0", port=5000):
    app.run(host=host, port=port, threaded=True, use_reloader=False)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("[server] Rodando em modo standalone (teste).")
    run_server()
