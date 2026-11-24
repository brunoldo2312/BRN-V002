"""server.py — Servidor Web/API do nó BRN
Versão: 13.9.27
================================================================
v13.9.27 (2026-11-24) — DESCOBERTA DE PEER MAIS ALTO + SYNC FORÇADO:
  [NEW] GET /api/peers — lista peers ordenados por altura DESC.
  [NEW] GET /api/sync/peer-max — pergunta via P2P a altura de cada peer.
  [NEW] POST /api/sync/force — força sync com o peer mais alto.
  [NEW] POST /api/sync/auto — descobre peer mais alto e dispara sync.
  [NEW] GET /api/sync/status melhorado com max_peer_height e peer_mais_alto.
  [KEEP] v13.9.26: /api/mine/gate, /api/consensus/validators,
         /api/block/<h>/signatures, /api/trust/nodes, etc.
================================================================
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request, send_file, redirect
from flask_cors import CORS

log = logging.getLogger("server")

BASE_DIR = Path(__file__).resolve().parent
CURRENT_WALLET_FILE = str(BASE_DIR / "current_wallet.json")
WALLETS_FILE = str(BASE_DIR / "user_wallets.json")
TXT_HISTORY_LIMIT_DEFAULT = 200
TXT_HISTORY_LIMIT_MAX = 500
MIN_RELAY_FEE = 1000

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
_last_force_sync: Dict[str, Any] = {"ts": 0.0, "peer": None, "ok": False}


# ============================================================
# INJEÇÃO
# ============================================================
def set_chain(chain) -> None:
    global _chain
    _chain = chain
    h = -1
    try:
        h = _chain.db.height()
    except Exception:
        pass
    log.info("server: chain injetada (height=%s)", h)


def set_peer_id(peer_id: str) -> None:
    global _BRN_PEER_ID
    _BRN_PEER_ID = peer_id or ""
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
    _sk_validation_state = state or {}


def set_http_sync_state(state: dict) -> None:
    global _http_sync_state
    state = state or {}
    state["last_force_sync"] = _last_force_sync
    _http_sync_state = state


def _get_chain():
    return _chain


def _get_p2p_manager():
    return _p2p_manager


def _get_pool_manager():
    return _pool_manager


def _get_sync_manager():
    return _sync_manager


def _get_trust_network():
    return _trust_network


def _get_pool_client_local():
    try:
        from miner_pool_client import get_pool_client
        return get_pool_client()
    except Exception:
        return None


# ============================================================
# Helpers de gate
# ============================================================
def _build_gate_detail() -> dict:
    chain = _get_chain()
    h = -1
    if chain is not None:
        try:
            h = int(chain.db.height())
        except Exception:
            pass

    sk_installed = False
    local_pk = ""
    active = []
    if chain is not None:
        sk = getattr(chain, "_local_validator_sk", None)
        if sk is not None:
            sk_installed = True
            try:
                local_pk = getattr(sk, "pub_hex", "") or ""
            except Exception:
                pass
        try:
            active = list(chain._get_active_validators() or [])
        except Exception:
            active = []

    n_active = len(active)
    n_trust = 0
    if chain is not None:
        try:
            n_trust = len(chain._get_trust_validators() or [])
        except Exception:
            n_trust = 0

    if n_active > 0:
        validators_ready = True
        validators_reason = f"{n_active} validador(es) ativo(s)"
    else:
        validators_reason = (
            "trust_network vazio — bootstrap bypass ativo"
            if n_trust == 0 else
            f"{n_trust} no trust mas 0 conectados"
        )
        validators_ready = False

    fast_sync_until = int(os.environ.get("BRN_FAST_SYNC_UNTIL", "0") or "0")
    fast_sync_active = bool(fast_sync_until > 0 and h < fast_sync_until)

    pc = _get_pool_client_local()
    pool_client_running = False
    if pc is not None:
        try:
            pool_client_running = bool(pc.is_running())
        except Exception:
            pass

    checks = {
        "height": {"ok": h >= 0, "current": h, "required": 0},
        "sk": {"ok": sk_installed,
               "local_pk": (local_pk[:16] + "...") if local_pk else ""},
        "validators": {
            "ok": validators_ready,
            "reason": validators_reason,
            "n_active": n_active,
            "n_trust": n_trust,
        },
        "fast_sync": {"ok": not fast_sync_active, "until": fast_sync_until},
        "pool_client": {"running": pool_client_running},
    }

    return {
        "current_height": h,
        "fast_sync_until": fast_sync_until,
        "fast_sync_active": fast_sync_active,
        "sk_installed": sk_installed,
        "local_pk": (local_pk[:16] + "...") if local_pk else "",
        "local_pk_full": local_pk,
        "validators_ready": validators_ready,
        "validators_reason": validators_reason,
        "validators_detail": {
            "n_active": n_active,
            "n_trust": n_trust,
            "active": active,
        },
        "checks": checks,
        "pool_client_running": pool_client_running,
    }


def _compute_mining_allowed() -> tuple:
    detail = _build_gate_detail()
    if detail["fast_sync_active"]:
        return False, f"fast_sync_below_{detail['fast_sync_until']}", detail
    if not detail["sk_installed"]:
        return False, "no_sk", detail
    if not detail["validators_ready"]:
        return False, "validators_not_synced", detail
    return True, "ok", detail


# ============================================================
# Helpers de sync
# ============================================================
def _list_peers_from_db() -> List[Dict[str, Any]]:
    """Lê peers do SQLite, normaliza e ordena por altura DESC."""
    if _chain is None or not hasattr(_chain.db, "listar_peers"):
        return []
    try:
        raw = _chain.db.listar_peers(apenas_ativos=False) or []
    except Exception as e:
        log.warning("_list_peers_from_db falhou: %s", e)
        return []

    peers = []
    now = time.time()
    for p in raw:
        if not isinstance(p, dict):
            continue
        addr = (p.get("address") or "").strip()
        if not addr:
            continue
        ip = addr.rsplit(":", 1)[0] if ":" in addr else addr
        try:
            ph = int(p.get("height", 0) or 0)
        except (ValueError, TypeError):
            ph = 0
        try:
            ls = float(p.get("last_seen", 0) or 0)
        except (ValueError, TypeError):
            ls = 0.0
        age = int(now - ls) if ls > 0 else -1
        peers.append({
            "address": addr,
            "ip": ip,
            "height": ph,
            "last_seen": int(ls) if ls > 0 else 0,
            "age_s": age,
            "active": (age >= 0 and age < 300),
            "node_id": p.get("node_id", ""),
            "version": p.get("version", ""),
            "public_key": p.get("public_key", ""),
        })
    peers.sort(key=lambda x: x["height"], reverse=True)
    return peers


def _probe_peer_height(peer_addr: str, timeout: float = 2.0) -> Optional[int]:
    """Pergunta a um peer via P2P qual é a altura dele."""
    try:
        from p2p_unified import P2PClient
    except ImportError:
        return None
    try:
        ip, port_s = peer_addr.rsplit(":", 1)
        port = int(port_s)
    except Exception:
        return None
    try:
        resp, _ = P2PClient.get_chain_height(ip, port, timeout=timeout)
        if resp and isinstance(resp, dict):
            try:
                return int(resp.get("height", 0) or 0)
            except (ValueError, TypeError):
                return None
    except Exception:
        return None
    return None


def _find_highest_peer(probe_p2p: bool = True,
                      timeout_per_peer: float = 2.0) -> Dict[str, Any]:
    """
    Retorna o peer com maior altura. Se probe_p2p=True, pergunta
    a altura em tempo real via P2P (mais confiável); senão usa o DB.
    """
    local_h = -1
    if _chain is not None:
        try:
            local_h = int(_chain.db.height())
        except Exception:
            local_h = -1

    peers = _list_peers_from_db()
    best = None
    best_h = -1

    for p in peers:
        addr = p["address"]
        if p["ip"].startswith("127.") or addr == "0.0.0.0:0":
            continue
        height_used = p["height"]
        probed_ok = False
        if probe_p2p:
            probed = _probe_peer_height(addr, timeout=timeout_per_peer)
            if probed is not None:
                height_used = probed
                probed_ok = True
        if height_used > best_h:
            best_h = height_used
            best = {
                "address": addr,
                "ip": p["ip"],
                "height": height_used,
                "height_db": p["height"],
                "probed_ok": probed_ok,
                "active": p["active"],
            }

    return {
        "ok": best is not None,
        "local_height": local_h,
        "max_peer_height": best_h,
        "peer_mais_alto": best,
        "delta": (best_h - local_h) if (best is not None and local_h >= 0) else None,
        "peers_total": len(peers),
    }


def _do_force_sync(peer_addr: Optional[str] = None,
                   auto: bool = True) -> Dict[str, Any]:
    """Força sync com um peer (ou descobre o mais alto se auto=True)."""
    global _last_force_sync

    p2p = _get_p2p_manager()
    if p2p is None:
        return {"ok": False, "msg": "p2p nao injetado", "peer": None}
    if _chain is None:
        return {"ok": False, "msg": "chain nao injetada", "peer": None}

    chosen = peer_addr
    if not chosen and auto:
        found = _find_highest_peer(probe_p2p=True)
        if not found.get("ok") or not found.get("peer_mais_alto"):
            return {"ok": False, "msg": "nenhum peer disponivel",
                    "peer": None, "found": found}
        chosen = found["peer_mais_alto"]["address"]
        if found.get("delta") is not None and found["delta"] <= 0:
            _last_force_sync = {"ts": time.time(), "peer": chosen,
                                "ok": True, "msg": "local ja e o mais alto"}
            return {"ok": True, "msg": "local ja e o mais alto",
                    "peer": chosen, "found": found}

    if not chosen:
        return {"ok": False, "msg": "peer nao informado e auto=False",
                "peer": None}

    try:
        ip, port_s = chosen.rsplit(":", 1)
        port = int(port_s)
    except Exception:
        return {"ok": False, "msg": f"peer invalido: {chosen}", "peer": None}

    result = {"ok": False, "msg": "sync falhou", "peer": chosen}
    try:
        if hasattr(p2p, "sync_with_peer"):
            r = p2p.sync_with_peer(ip, port)
            if isinstance(r, tuple) and len(r) >= 1:
                result["sync_result"] = str(r[0])
                result["sync_msg"] = str(r[1]) if len(r) > 1 else ""
                result["ok"] = str(r[0]).lower() in ("ok", "ok_blocks", "up_to_date")
            elif isinstance(r, dict):
                result.update(r)
                result.setdefault("ok", True)
            else:
                result["ok"] = True
        elif hasattr(p2p, "_sync_with_retry"):
            threading.Thread(
                target=p2p._sync_with_retry, args=(ip, port),
                daemon=True, name=f"ForceSync-{ip}"
            ).start()
            result["ok"] = True
            result["msg"] = "sync disparado em background"
        else:
            result["msg"] = "p2p sem metodo de sync (sync_with_peer/_sync_with_retry)"
    except Exception as e:
        result["ok"] = False
        result["msg"] = f"excecao: {e}"

    _last_force_sync = {"ts": time.time(), "peer": chosen,
                        "ok": result["ok"], "msg": result["msg"]}
    return result


# ============================================================
# [1] Health + status
# ============================================================
@app.route("/health", methods=["GET"])
def health():
    h = -1
    if _chain is not None:
        try:
            h = _chain.db.height()
        except Exception:
            pass

    chain_sk = False
    if _chain is not None:
        try:
            chain_sk = getattr(_chain, "_local_validator_sk", None) is not None
        except Exception:
            pass

    active = []
    required = 0
    local_pk = ""
    if _chain is not None:
        try:
            active = list(_chain._get_active_validators() or [])
            required = int(_chain._required_signatures(active))
        except Exception:
            pass
        sk = getattr(_chain, "_local_validator_sk", None)
        if sk is not None:
            try:
                local_pk = getattr(sk, "pub_hex", "") or ""
            except Exception:
                pass

    return jsonify({
        "ok": True,
        "height": h,
        "peer_id": _BRN_PEER_ID,
        "l2_enabled": True,
        "chain_injected": _chain is not None,
        "sync_injected": _sync_manager is not None,
        "wallet_sk_installed": chain_sk,
        "validators": {
            "n_trust": len(active),
            "n_active": len(active),
            "required_sigs": required,
            "bootstrap_would_fire": len(active) < 2,
            "local_pk": (local_pk[:16] + "...") if local_pk else "",
        },
        "version": "13.9.27",
    })


@app.route("/api/peer_id", methods=["GET"])
def api_peer_id():
    return jsonify({"ok": True, "peer_id": _BRN_PEER_ID})


@app.route("/api/status", methods=["GET"])
def api_status():
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada"}), 503

    try:
        h = _chain.db.height()
        tip = _chain.db.tip_hash()
        n_peers = 0
        if hasattr(_chain.db, "listar_peers"):
            try:
                n_peers = len(_chain.db.listar_peers(apenas_ativos=True) or [])
            except Exception:
                pass

        allowed, reason, detail = _compute_mining_allowed()

        return jsonify({
            "name": "BrunoCoin",
            "ticker": "BRN",
            "height": h,
            "tip_hash": tip,
            "peers": n_peers,
            "reward": _chain.current_reward(max(h, 1)),
            "difficulty": _chain.current_difficulty(),
            "mempool": _chain.db.mempool_count()
                if hasattr(_chain.db, "mempool_count") else 0,
            "utxos": _chain.db.count_utxos()
                if hasattr(_chain.db, "count_utxos") else 0,
            "peer_id": _BRN_PEER_ID,
            "fila_tamanho": len(getattr(_chain, "fila_mineracao", {}) or {}),
            "rede_descentralizada": bool(
                getattr(_chain, "rede_descentralizada", False)),
            "mining_allowed": allowed,
            "mining_gate_reason": reason,
            "validators_ready": detail["validators_ready"],
            "validators_reason": detail["validators_reason"],
            "fast_sync_active": detail["fast_sync_active"],
            "fast_sync_until": detail["fast_sync_until"],
            "wallet_sk_installed": detail["sk_installed"],
        })
    except Exception as e:
        log.exception("api_status falhou")
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# [2] GATE DE MINERAÇÃO
# ============================================================
@app.route("/api/mine/gate", methods=["GET"])
def api_mine_gate():
    try:
        force = request.args.get("force", "0") in ("1", "true", "True")
        allowed, reason, detail = _compute_mining_allowed()
        return jsonify({
            "ok": True,
            "mining_allowed": allowed,
            "reason": reason,
            "detail": detail,
            "force": force,
            "generated_at": int(time.time()),
        })
    except Exception as e:
        log.exception("api_mine_gate falhou")
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# [3] CONSENSO
# ============================================================
@app.route("/api/consensus/validators", methods=["GET"])
def api_consensus_validators():
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada",
                        "validators": [], "required_sigs": 0,
                        "all_required": True}), 503

    try:
        trust = _get_trust_network()
        n_trust = 0
        if trust is not None and hasattr(trust, "nodes"):
            try:
                n_trust = len(trust.nodes)
            except Exception:
                pass

        active = []
        try:
            active = list(_chain._get_active_validators() or [])
        except Exception:
            pass

        required_sigs = 0
        all_required = False
        try:
            required_sigs = int(_chain._required_signatures(active))
            all_required = bool(required_sigs == len(active) and len(active) > 0)
        except Exception:
            pass

        local_pk = ""
        local_pk_installed = False
        sk = getattr(_chain, "_local_validator_sk", None)
        if sk is not None:
            local_pk_installed = True
            try:
                local_pk = getattr(sk, "pub_hex", "") or ""
            except Exception:
                pass

        allowed, reason, _ = _compute_mining_allowed()
        bootstrap_would_fire = (len(active) < 2)

        return jsonify({
            "ok": True,
            "validators": active,
            "required_sigs": required_sigs,
            "all_required": all_required,
            "n_trust": n_trust,
            "n_active": len(active),
            "local_pk": (local_pk[:16] + "...") if local_pk else "",
            "local_pk_full": local_pk,
            "local_pk_installed": local_pk_installed,
            "bootstrap_would_fire": bootstrap_would_fire,
            "mining_allowed": allowed,
            "gate_reason": reason,
            "require_all_sigs": bool(
                os.environ.get("BRN_REQUIRE_ALL_SIGS", "1") == "1"),
            "validator_quorum": float(
                os.environ.get("BRN_VALIDATOR_QUORUM", "0.67")),
        })
    except Exception as e:
        log.exception("api_consensus_validators falhou")
        return jsonify({"ok": False, "msg": str(e),
                        "validators": [], "required_sigs": 0,
                        "all_required": True}), 500


@app.route("/api/block/<int:height>/signatures", methods=["GET"])
def api_block_signatures(height: int):
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada",
                        "signatures": [], "signatures_count": 0}), 503

    try:
        blk = _chain.db.get_block(int(height))
        if not blk:
            return jsonify({"ok": False,
                            "msg": f"bloco #{height} nao encontrado",
                            "signatures": [], "signatures_count": 0}), 404

        sigs = blk.get("validators_sig") or []
        n_sigs = len(sigs) if isinstance(sigs, list) else 0

        required = 0
        try:
            active = list(_chain._get_active_validators() or [])
            required = int(_chain._required_signatures(active))
        except Exception:
            pass

        local_pk = ""
        sk = getattr(_chain, "_local_validator_sk", None)
        if sk is not None:
            try:
                local_pk = getattr(sk, "pub_hex", "") or ""
            except Exception:
                pass

        return jsonify({
            "ok": True,
            "height": int(height),
            "signatures": sigs,
            "signatures_count": n_sigs,
            "required": required,
            "all_signed": (n_sigs >= required) if required > 0 else True,
            "local_pk": (local_pk[:16] + "...") if local_pk else "",
            "local_pk_full": local_pk,
            "hash": blk.get("hash", ""),
            "timestamp": blk.get("timestamp", 0),
        })
    except Exception as e:
        log.exception("api_block_signatures falhou")
        return jsonify({"ok": False, "msg": str(e),
                        "signatures": [], "signatures_count": 0}), 500


# ============================================================
# [4] TRUST NETWORK
# ============================================================
@app.route("/api/trust/nodes", methods=["GET"])
def api_trust_nodes():
    try:
        limit = int(request.args.get("limit", "100"))
    except (ValueError, TypeError):
        limit = 100
    limit = max(1, min(limit, 500))

    trust = _get_trust_network()
    if trust is None or not hasattr(trust, "nodes"):
        return jsonify({"ok": True, "nodes": [], "count": 0,
                        "msg": "trust_network nao injetado"})

    try:
        nodes = []
        for pk, node in (trust.nodes or {}).items():
            try:
                nodes.append({
                    "public_key": pk,
                    "trust_score": float(getattr(node, "trust_score", 0) or 0),
                    "is_validator": bool(getattr(node, "is_validator", False)),
                    "connected": bool(getattr(node, "connected", False)),
                    "last_seen": float(getattr(node, "last_seen", 0) or 0),
                    "peer_addr": getattr(node, "peer_addr", "") or "",
                    "address": getattr(node, "address", "") or "",
                    "status": (
                        "trusted" if getattr(node, "is_validator", False) else "new"
                    ),
                })
            except Exception:
                continue
        nodes.sort(key=lambda x: x["trust_score"], reverse=True)
        return jsonify({
            "ok": True,
            "nodes": nodes[:limit],
            "count": len(nodes),
            "limit": limit,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "nodes": [], "count": 0}), 500


@app.route("/api/trust/stats", methods=["GET"])
def api_trust_stats():
    trust = _get_trust_network()
    if trust is None or not hasattr(trust, "nodes"):
        return jsonify({"ok": True, "total": 0, "validators": 0,
                        "connected": 0, "top_score": 0.0})
    try:
        total = len(trust.nodes)
        validators = sum(1 for n in trust.nodes.values()
                         if getattr(n, "is_validator", False))
        connected = sum(1 for n in trust.nodes.values()
                        if getattr(n, "connected", False))
        top_score = 0.0
        for n in trust.nodes.values():
            try:
                s = float(getattr(n, "trust_score", 0) or 0)
                if s > top_score:
                    top_score = s
            except Exception:
                pass
        return jsonify({
            "ok": True, "total": total, "validators": validators,
            "connected": connected, "top_score": top_score,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# [5] WALLET — SK
# ============================================================
@app.route("/api/wallet/set_validator_sk", methods=["POST"])
def api_wallet_set_validator_sk():
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada",
                        "sk_installed": False}), 503

    try:
        data = request.get_json(silent=True) or {}
        sk_hex = (data.get("sk_hex") or "").strip()
        address = (data.get("address") or "").strip()
        pubkey = (data.get("pubkey") or "").strip()

        if not sk_hex:
            return jsonify({"ok": False, "msg": "sk_hex vazio",
                            "sk_installed": False}), 400
        if len(sk_hex) != 64:
            return jsonify({"ok": False,
                            "msg": f"sk_hex deve ter 64 chars (tem {len(sk_hex)})",
                            "sk_installed": False}), 400
        try:
            int(sk_hex, 16)
        except ValueError:
            return jsonify({"ok": False, "msg": "sk_hex não é hex",
                            "sk_installed": False}), 400

        try:
            from wallet import Wallet as _W
            wobj = _W(private_key_hex=sk_hex)
        except Exception as e:
            return jsonify({"ok": False, "msg": f"SK inválida: {e}",
                            "sk_installed": False}), 400

        propagated = False
        peers_notified = 0
        warnings = []
        errors = []

        try:
            from wallet_integrator import integrate as _wi_integrate
            r = _wi_integrate(
                _chain, sk_hex,
                address=address, pubkey=pubkey or wobj.pub_hex,
                logger=log,
            )
            if r.get("ok"):
                propagated = bool(r.get("propagated", False))
                peers_notified = int(r.get("peers_notified", 0) or 0)
                warnings.extend(r.get("warnings") or [])
            else:
                warnings.append(f"wallet_integrator: {r.get('msg', 'falhou')}")
        except ImportError:
            pass
        except Exception as e:
            warnings.append(f"wallet_integrator exception: {e}")

        try:
            _chain.set_local_validator_sk(wobj)
        except Exception as e:
            errors.append(f"set_local_validator_sk: {e}")

        sk_now = getattr(_chain, "_local_validator_sk", None)
        sk_installed = sk_now is not None

        in_trust = False
        multisig_ready = False
        try:
            active = list(_chain._get_active_validators() or [])
            in_trust = (wobj.pub_hex in active)
            req = int(_chain._required_signatures(active))
            multisig_ready = (req > 0)
        except Exception:
            pass

        return jsonify({
            "ok": True,
            "sk_installed": sk_installed,
            "pubkey": wobj.pub_hex,
            "address": wobj.address,
            "in_trust": in_trust,
            "multisig_ready": multisig_ready,
            "propagated": propagated,
            "peers_notified": peers_notified,
            "warnings": warnings,
            "errors": errors,
        })
    except Exception as e:
        log.exception("api_wallet_set_validator_sk falhou")
        return jsonify({"ok": False, "msg": str(e),
                        "sk_installed": False}), 500


# ============================================================
# [6] CARTEIRA
# ============================================================
@app.route("/api/nova-carteira", methods=["POST"])
def api_nova_carteira():
    try:
        from wallet import Wallet
        w = Wallet()
        data = {
            "success": True,
            "address": w.address,
            "private_key": w.priv_hex,
            "public_key": w.pub_hex,
            "warning": "Guarde a chave privada em local seguro.",
        }

        try:
            pw = (os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
                  or os.environ.get("BRN_NODE_PASSWORD", "").strip())
            if pw:
                from secure_store import save_current_wallet
                r = save_current_wallet(pw, {
                    "address": w.address,
                    "private_key": w.priv_hex,
                    "public_key": w.pub_hex,
                })
                if r.get("ok"):
                    data["wallet_saved"] = True
        except Exception as e:
            data["wallet_saved"] = False
            data["wallet_saved_error"] = str(e)

        try:
            if _chain is not None:
                _chain.set_local_validator_sk(w)
                data["sk_installed"] = True
                log.info("[nova-carteira] SK instalada: %s...", w.address[:16])
            else:
                data["sk_installed"] = False
        except Exception as e:
            data["sk_installed"] = False
            data["sk_installed_error"] = str(e)

        return jsonify(data)
    except Exception as e:
        log.exception("api_nova_carteira falhou")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/portfolio/<address>", methods=["GET"])
def api_portfolio(address: str):
    if _chain is None:
        return jsonify({"portfolio": {}, "error": "chain nao injetada"}), 503
    try:
        if not hasattr(_chain.db, "get_utxos"):
            return jsonify({"portfolio": {"BRN": 0, "KYC": 0}})
        utxos = _chain.db.get_utxos(address)
        total = sum(int(u.get("amount", 0) or 0) for u in utxos)
        return jsonify({
            "portfolio": {
                "address": address,
                "BRN": total / 1e8,
                "KYC": 0,
                "balance_sats": total,
                "utxos": len(utxos),
            }
        })
    except Exception as e:
        return jsonify({"portfolio": {}, "error": str(e)}), 500


@app.route("/api/saldo/<address>", methods=["GET"])
@app.route("/api/balance/<address>", methods=["GET"])
def api_saldo(address: str):
    if _chain is None:
        return jsonify({"ok": False, "error": "chain nao injetada"}), 503
    try:
        utxos = _chain.db.get_utxos(address)
        total = sum(int(u.get("amount", 0) or 0) for u in utxos)
        return jsonify({
            "success": True,
            "address": address,
            "balance_sats": total,
            "balance_brn": total / 1e8,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/minhas-txs/<address>", methods=["GET"])
@app.route("/api/transacoes/<address>", methods=["GET"])
def api_minhas_txs(address: str):
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada"}), 503
    try:
        try:
            limit = int(request.args.get("limit", TXT_HISTORY_LIMIT_DEFAULT))
        except (ValueError, TypeError):
            limit = TXT_HISTORY_LIMIT_DEFAULT
        limit = max(1, min(limit, TXT_HISTORY_LIMIT_MAX))

        current_height = _chain.db.height()
        raw_list = _chain.db.get_txs_for_address(address, limit=limit)

        transactions = []
        for item in raw_list:
            tx = item["tx"]
            block_height = item["block_height"]
            is_mempool = item["is_mempool"]

            outputs_to_addr = sum(
                int(o.get("amount", 0) or 0)
                for o in (tx.get("outputs") or [])
                if o.get("address") == address
            )

            inputs_from_addr = 0
            for inp in (tx.get("inputs") or []):
                if inp.get("txid") == "0" * 64:
                    continue
                try:
                    u = _chain.db.get_utxo(inp.get("txid"), int(inp.get("vout", 0)))
                    if u and u.get("address") == address:
                        inputs_from_addr += int(u.get("amount", 0) or 0)
                except Exception:
                    pass

            if inputs_from_addr > 0 and outputs_to_addr > 0:
                direction = "self"
                amount = 0
            elif outputs_to_addr > 0:
                direction = "received"
                amount = outputs_to_addr
            else:
                direction = "sent"
                amount = max(0, inputs_from_addr - outputs_to_addr)

            if is_mempool or block_height is None:
                status = "pending"
                confirmations = 0
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
            "ok": True,
            "address": address,
            "count": len(transactions),
            "limit": limit,
            "transactions": transactions,
        })
    except Exception as e:
        log.exception("api_minhas_txs falhou")
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/tx-status/<txid>", methods=["GET"])
def api_tx_status(txid: str):
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada"}), 503
    try:
        tx = None
        height = None
        if hasattr(_chain.db, "get_tx_by_txid"):
            tx, height = _chain.db.get_tx_by_txid(txid)
        if tx is not None and height is not None and height >= 0:
            h = _chain.db.height()
            return jsonify({
                "ok": True,
                "txid": txid,
                "status": "confirmed",
                "block_height": height,
                "confirmations": max(0, h - height),
                "timestamp": int(tx.get("timestamp", 0) or 0),
            })
        mem = None
        if hasattr(_chain.db, "get_mempool_tx"):
            mem = _chain.db.get_mempool_tx(txid)
        if mem is not None:
            return jsonify({
                "ok": True,
                "txid": txid,
                "status": "pending",
                "block_height": None,
                "confirmations": 0,
                "timestamp": int(mem.get("timestamp", 0) or 0),
            })
        return jsonify({
            "ok": True,
            "txid": txid,
            "status": "not_found",
            "block_height": None,
            "confirmations": 0,
            "timestamp": 0,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/verificar-recebimento/<address>/<txid>", methods=["GET"])
def api_verificar_recebimento(address: str, txid: str):
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada"}), 503
    try:
        from wallet import WalletManager
        if not WalletManager.validate_address(address):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400
    except Exception:
        pass

    try:
        tx = None
        height = None
        if hasattr(_chain.db, "get_tx_by_txid"):
            tx, height = _chain.db.get_tx_by_txid(txid)
        if tx is None:
            mem = None
            if hasattr(_chain.db, "get_mempool_tx"):
                mem = _chain.db.get_mempool_tx(txid)
            if mem is not None:
                return jsonify({
                    "ok": True,
                    "chegou": False,
                    "status": "pending",
                    "valor": 0,
                    "confirmacoes": 0,
                    "block_height": None,
                    "msg": "Transacao na mempool, aguardando mineracao.",
                })
            return jsonify({
                "ok": True,
                "chegou": False,
                "status": "not_found",
                "valor": 0,
                "confirmacoes": 0,
                "block_height": None,
                "msg": "TX nao encontrada.",
            })

        received = 0
        for o in (tx.get("outputs") or []):
            if o.get("address") == address:
                received += int(o.get("amount", 0) or 0)

        h = _chain.db.height()
        confs = max(0, h - height) if height is not None else 0

        if received <= 0:
            return jsonify({
                "ok": True,
                "chegou": False,
                "status": "confirmed",
                "valor": 0,
                "confirmacoes": confs,
                "block_height": height,
                "msg": f"TX confirmada no bloco #{height}, mas sem valor para {address[:16]}…",
            })

        return jsonify({
            "ok": True,
            "chegou": True,
            "status": "confirmed",
            "valor": received,
            "confirmacoes": confs,
            "block_height": height,
            "msg": f"Confirmado no bloco #{height} com {confs} confirmacoes.",
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# [7] TRANSFERIR
# ============================================================
class _NoopCM:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _noop_cm():
    return _NoopCM()


@app.route("/api/transfer", methods=["POST"])
@app.route("/api/send", methods=["POST"])
@app.route("/api/enviar", methods=["POST"])
def api_transfer():
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada"}), 503
    try:
        data = request.get_json(silent=True) or {}
        sender = (data.get("from") or "").strip()
        to = (data.get("to") or "").strip()
        amount_raw = data.get("amount")
        sk = (request.headers.get("X-BRN-Sk", "")
              or data.get("private_key", "")
              or data.get("sk", ""))
        pk_payload = (data.get("pubkey", "")
                      or data.get("public_key", "")
                      or data.get("miner_pubkey", ""))

        from wallet import Wallet as _W
        from wallet import WalletManager

        if not WalletManager.validate_address(sender):
            return jsonify({"ok": False, "msg": "Endereco 'from' invalido."}), 400
        if not WalletManager.validate_address(to):
            return jsonify({"ok": False, "msg": "Endereco 'to' invalido."}), 400
        if sender == to:
            return jsonify({"ok": False, "msg": "Nao pode enviar para si mesmo."}), 400

        try:
            amount_dec = Decimal(str(amount_raw).replace(",", ".")).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN)
            amount_sats = int(amount_dec * Decimal(10 ** 8))
        except Exception:
            return jsonify({"ok": False, "msg": "Valor invalido."}), 400

        if amount_sats <= 0:
            return jsonify({"ok": False, "msg": "Valor deve ser > 0."}), 400

        try:
            w = _W(private_key_hex=sk)
            if w.address != sender:
                raise ValueError("chave privada não corresponde ao from")
        except Exception:
            return jsonify({"ok": False, "msg": "Chave privada invalida."}), 400

        FEE = int(MIN_RELAY_FEE)
        lock = _chain.db.lock if hasattr(_chain.db, "lock") else _noop_cm()
        with lock:
            utxos = _chain.db.get_utxos(sender)
            utxos.sort(key=lambda u: int(u.get("amount", 0) or 0), reverse=True)
            total = 0
            escolhidos = []
            for u in utxos:
                escolhidos.append(u)
                total += int(u.get("amount", 0) or 0)
                if total >= amount_sats + FEE:
                    break

            if total < amount_sats + FEE:
                return jsonify({
                    "ok": False,
                    "msg": f"Saldo insuficiente: tem {total/1e8:.8f} BRN, "
                           f"precisa {(amount_sats+FEE)/1e8:.8f} BRN.",
                }), 400

            troco = total - amount_sats - FEE
            outputs = [{"address": to, "amount": amount_sats, "pubkey": ""}]
            if troco > 0:
                outputs.append({
                    "address": sender,
                    "amount": troco,
                    "pubkey": w.pub_hex,
                })

            inputs = [{
                "txid": u["txid"],
                "vout": int(u["vout"]),
                "pubkey": w.pub_hex,
                "signature": "",
            } for u in escolhidos]

            nonce = _chain.db.get_nonce_for_pubkey(w.pub_hex)
            tx = {
                "txid": "",
                "inputs": inputs,
                "outputs": outputs,
                "timestamp": int(time.time()),
                "locktime": 0,
                "nonce": nonce,
            }

            try:
                from blockchain import signing_hash as _sh, txid as _calc
                h = _sh(tx)
                for inp in tx["inputs"]:
                    inp["signature"] = w.sign(h)
                tx["txid"] = _calc(tx)
            except Exception as e:
                return jsonify({"ok": False,
                                "msg": f"Falha ao assinar tx: {e}"}), 500

            ok, msg = _chain.submit_tx(tx)
            if not ok:
                return jsonify({"ok": False, "msg": msg}), 400

            p2p = _get_p2p_manager()
            if p2p is not None and hasattr(p2p, "broadcast_tx"):
                try:
                    p2p.broadcast_tx(tx)
                except Exception as e:
                    log.warning("broadcast_tx falhou: %s", e)

            return jsonify({
                "ok": True,
                "txid": tx["txid"],
                "msg": f"Aceita na mempool ({amount_dec} BRN).",
                "amount_brn": float(amount_dec),
                "fee_sats": FEE,
            })
    except Exception as e:
        log.exception("api_transfer falhou")
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# [8] MINER
# ============================================================
@app.route("/api/miner/start", methods=["POST"])
def api_miner_start():
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada"}), 503
    try:
        allowed, reason, detail = _compute_mining_allowed()
        if not allowed:
            return jsonify({
                "ok": False,
                "msg": f"Mineração bloqueada: {reason}",
                "mining_allowed": False,
                "gate": {"reason": reason, "detail": detail},
            }), 409

        data = request.get_json(silent=True) or {}
        addr = (data.get("validator_address")
                or data.get("address")
                or "").strip()
        pubkey = (data.get("miner_pubkey")
                  or data.get("pubkey")
                  or data.get("public_key")
                  or "").strip()

        if not addr:
            return jsonify({"ok": False, "msg": "Endereco obrigatorio."}), 400
        if not pubkey:
            return jsonify({"ok": False, "msg": "Pubkey obrigatoria."}), 400

        try:
            from wallet import WalletManager
            if not WalletManager.validate_address(addr):
                return jsonify({"ok": False, "msg": "Endereco invalido."}), 400
        except Exception:
            pass

        _chain.adicionar_na_fila(addr, pubkey)
        try:
            _chain.db.set_meta("miner_address", addr)
        except Exception:
            pass

        from miner_loop import get_miner
        m = get_miner(_chain)
        is_origin_val = bool(getattr(_chain, "is_origin", False))
        try:
            ok, msg = m.start(addr, pubkey, is_origin=is_origin_val)
        except TypeError:
            ok, msg = m.start(addr, pubkey)

        if not ok:
            st = m.status() if hasattr(m, "status") else {}
            return jsonify({"ok": False, "msg": msg, **st}), 400

        st = m.status() if hasattr(m, "status") else {}
        st["rede_descentralizada"] = bool(
            getattr(_chain, "rede_descentralizada", False))
        st["fila_tamanho"] = len(getattr(_chain, "fila_mineracao", {}) or {})
        return jsonify({"ok": True, "msg": "Minerador iniciado.", **st})
    except Exception as e:
        log.exception("api_miner_start falhou")
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/miner/status", methods=["GET"])
def api_miner_status():
    if _chain is None:
        return jsonify({"running": False, "error": "chain nao injetada"}), 200
    try:
        from miner_loop import get_miner
        m = get_miner(_chain)
        st = m.status() if hasattr(m, "status") else {}
        active = 0
        trust = _get_trust_network()
        if trust is not None:
            try:
                active = len(trust.get_top_validators(count=5))
            except Exception:
                pass
        return jsonify({
            **st,
            "height": _chain.db.height(),
            "difficulty": _chain.current_difficulty(),
            "validators_active": active,
            "signatures_required": active,
            "signatures_collected": st.get("signatures_collected", 0),
            "rede_descentralizada": bool(
                getattr(_chain, "rede_descentralizada", False)),
            "fila_tamanho": len(getattr(_chain, "fila_mineracao", {}) or {}),
        })
    except Exception as e:
        return jsonify({"running": False, "error": str(e)}), 200


@app.route("/api/miner/stop", methods=["POST"])
def api_miner_stop():
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada"}), 503
    try:
        from miner_loop import get_miner
        m = get_miner(_chain)
        ok, msg = m.stop()
        st = m.status() if hasattr(m, "status") else {}
        return jsonify({"ok": ok, "msg": msg, **st})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# [9] POOL
# ============================================================
@app.route("/api/pool/local_status", methods=["GET"])
def api_pool_local_status():
    pc = _get_pool_client_local()
    if pc is None:
        return jsonify({"ok": False, "running": False,
                        "msg": "PoolClient nao injetado"}), 200
    try:
        st = pc.status() if hasattr(pc, "status") else {}
        return jsonify({"ok": True, **st})
    except Exception as e:
        return jsonify({"ok": False, "running": False, "msg": str(e)}), 200


@app.route("/api/pool/start_local", methods=["POST"])
def api_pool_start_local():
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada"}), 503
    try:
        allowed, reason, detail = _compute_mining_allowed()
        if not allowed:
            return jsonify({
                "ok": False,
                "msg": f"Mineração bloqueada: {reason}",
                "mining_allowed": False,
                "gate": {"reason": reason, "detail": detail},
            }), 409

        data = request.get_json(silent=True) or {}
        addr = (data.get("address") or "").strip()
        pubkey = (data.get("pubkey") or "").strip()
        if not addr or not pubkey:
            return jsonify({"ok": False,
                            "msg": "address e pubkey obrigatorios"}), 400

        pc = _get_pool_client_local()
        if pc is None:
            try:
                from miner_pool_client import PoolClient
            except ImportError as e:
                return jsonify({"ok": False,
                                "msg": f"PoolClient indisponivel: {e}"}), 503

            pool_url = (os.environ.get("BRN_POOL_URL", "") or "").strip()
            if not pool_url:
                pool_url = "http://127.0.0.1:5000"
            try:
                pc = PoolClient(address=addr, pubkey=pubkey, pool_url=pool_url)
            except TypeError:
                pc = PoolClient(addr, pubkey, pool_url=pool_url)

            try:
                from miner_pool_client import set_pool_client as _set_pc
                _set_pc(pc)
            except Exception:
                pass

        start_fn = None
        for name in ("start_mining", "start"):
            if hasattr(pc, name):
                start_fn = getattr(pc, name)
                break
        if start_fn is None:
            return jsonify({"ok": False,
                            "msg": "PoolClient sem metodo de inicio"}), 500

        try:
            ok, msg = start_fn()
        except TypeError:
            ok, msg = start_fn(addr, pubkey)

        st = pc.status() if hasattr(pc, "status") else {}
        return jsonify({"ok": bool(ok), "msg": msg, **st})
    except Exception as e:
        log.exception("api_pool_start_local falhou")
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/pool/stop_local", methods=["POST"])
def api_pool_stop_local():
    pc = _get_pool_client_local()
    if pc is None:
        return jsonify({"ok": False, "msg": "PoolClient nao injetado"}), 503
    try:
        stop_fn = None
        for name in ("stop_mining", "stop", "disconnect"):
            if hasattr(pc, name):
                stop_fn = getattr(pc, name)
                break
        if stop_fn is None:
            return jsonify({"ok": False,
                            "msg": "PoolClient sem metodo de parada"}), 500
        try:
            ok, msg = stop_fn()
        except TypeError:
            r = stop_fn()
            ok = True if r is None else bool(r)
            msg = "parado"
        st = pc.status() if hasattr(pc, "status") else {}
        return jsonify({"ok": bool(ok), "msg": msg, **st})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# [10] BLOCOS
# ============================================================
@app.route("/api/blocks", methods=["GET"])
def api_blocks():
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada"}), 503
    try:
        try:
            start = int(request.args.get("start", "0"))
        except (ValueError, TypeError):
            start = 0
        try:
            limit = int(request.args.get("limit", "20"))
        except (ValueError, TypeError):
            limit = 20
        limit = max(1, min(limit, 500))

        if hasattr(_chain.db, "get_blocks_range"):
            blocks = _chain.db.get_blocks_range(start, start + limit)
        elif hasattr(_chain.db, "list_blocks"):
            blocks = _chain.db.list_blocks(start, limit)
        else:
            blocks = []

        return jsonify({"ok": True, "blocks": blocks,
                        "start": start, "limit": limit})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "blocks": []}), 500


@app.route("/api/latest", methods=["GET"])
def api_latest():
    if _chain is None:
        return jsonify([])
    try:
        h = _chain.db.height()
        if h < 0:
            return jsonify([])
        start = max(0, h - 9)
        if hasattr(_chain.db, "get_blocks_range"):
            blocks = _chain.db.get_blocks_range(start, h + 1)
        elif hasattr(_chain.db, "list_blocks"):
            blocks = _chain.db.list_blocks(start, 10)
        else:
            blocks = []
        return jsonify(blocks)
    except Exception:
        return jsonify([])


@app.route("/api/block/<int:height>", methods=["GET"])
def api_block(height: int):
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada"}), 503
    try:
        blk = _chain.db.get_block(int(height))
        if not blk:
            return jsonify({"ok": False,
                            "msg": f"Bloco #{height} nao encontrado"}), 404
        return jsonify(blk)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/fee-estimate", methods=["GET"])
def api_fee_estimate():
    if _chain is None:
        return jsonify({"success": False, "error": "chain nao injetada"}), 503
    try:
        if hasattr(_chain, "estimate_fee"):
            low = int(_chain.estimate_fee("low"))
            medium = int(_chain.estimate_fee("medium"))
            high = int(_chain.estimate_fee("high"))
        else:
            low = medium = high = MIN_RELAY_FEE
        return jsonify({
            "success": True,
            "low": low, "medium": medium, "high": high,
            "min_relay_fee": MIN_RELAY_FEE,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/work", methods=["GET"])
def api_work():
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada"}), 503
    try:
        h = _chain.db.height()
        work = 0
        if hasattr(_chain, "cumulative_work"):
            work = int(_chain.cumulative_work())
        return jsonify({
            "ok": True,
            "height": h,
            "cumulative_work": work,
            "difficulty": _chain.current_difficulty(),
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# [11] PEERS + SYNC
# ============================================================
@app.route("/api/peers", methods=["GET"])
def api_peers():
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada",
                        "peers": [], "count": 0}), 503
    try:
        try:
            limit = int(request.args.get("limit", "100"))
        except (ValueError, TypeError):
            limit = 100
        limit = max(1, min(limit, 500))

        peers = _list_peers_from_db()
        local_h = -1
        try:
            local_h = int(_chain.db.height())
        except Exception:
            pass

        max_peer_h = max((p["height"] for p in peers), default=0)
        best_peer = peers[0] if peers else None

        return jsonify({
            "ok": True,
            "peers": peers[:limit],
            "count": len(peers),
            "active": sum(1 for p in peers if p["active"]),
            "local_height": local_h,
            "max_peer_height": max_peer_h,
            "peer_mais_alto": (
                {"address": best_peer["address"],
                 "height": best_peer["height"]}
                if best_peer else None
            ),
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e),
                        "peers": [], "count": 0}), 500


@app.route("/api/sync-info", methods=["GET"])
def api_sync_info():
    state = dict(_http_sync_state) if _http_sync_state else {}
    h = -1
    if _chain is not None:
        try:
            h = _chain.db.height()
        except Exception:
            pass
    state.update({
        "sk_installed": _sk_validation_state.get("ok", False),
        "validators_ready": _sk_validation_state.get("validators_ready", False),
        "auto_mine_status": _sk_validation_state.get("auto_mine_status", "idle"),
        "height": h,
        "bridge": {
            "wif_configured": bool(os.environ.get("BRN_BTC_WIF")),
            "receive_address": os.environ.get("BRN_BTC_RECEIVE_ADDRESS", ""),
            "network": os.environ.get("BRN_BTC_NETWORK", "testnet"),
        },
    })
    return jsonify(state)


@app.route("/api/sync/status", methods=["GET"])
def api_sync_status():
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada"}), 503
    try:
        h = _chain.db.height()
        sync = _get_sync_manager()
        peers = _list_peers_from_db()
        max_peer_h = max((p["height"] for p in peers), default=h)
        best_peer = peers[0] if peers else None

        if sync is not None and hasattr(sync, "get_status"):
            data = sync.get_status()
            return jsonify({
                "ok": True,
                "synced": bool(data.get("synced", True)),
                "syncing": bool(data.get("syncing", False)),
                "forking": bool(data.get("forking", False)),
                "progress": float(data.get("progress", 100.0)),
                "local_height": h,
                "target_height": int(data.get("target_height", h)),
                "max_peer_height": max_peer_h,
                "peer_mais_alto": (
                    {"address": best_peer["address"],
                     "height": best_peer["height"]}
                    if best_peer else None
                ),
                "blocos_faltando": max(0, int(data.get("target_height", h)) - h),
                "eta_legivel": data.get("eta_legivel", ""),
                "active_peer": data.get("active_peer"),
                "last_error": data.get("last_error", ""),
                "sync_running": True,
                "last_force_sync": _last_force_sync,
            })
        return jsonify({
            "ok": True, "synced": True, "syncing": False, "forking": False,
            "progress": 100.0, "local_height": h, "target_height": h,
            "max_peer_height": max_peer_h,
            "peer_mais_alto": (
                {"address": best_peer["address"],
                 "height": best_peer["height"]}
                if best_peer else None
            ),
            "blocos_faltando": 0, "eta_legivel": "sincronizado",
            "last_error": "",
            "sync_running": False,
            "last_force_sync": _last_force_sync,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/sync/peer-max", methods=["GET"])
def api_sync_peer_max():
    """Pergunta a cada peer conhecido (via P2P) qual é a altura dele."""
    try:
        probe = request.args.get("probe", "1") in ("1", "true", "True")
        timeout = float(request.args.get("timeout", "2.0"))
    except (ValueError, TypeError):
        probe, timeout = True, 2.0
    try:
        found = _find_highest_peer(probe_p2p=probe, timeout_per_peer=timeout)
        return jsonify({"ok": True, **found})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/sync/force", methods=["POST"])
def api_sync_force():
    """Força sync com um peer (ou descobre o mais alto se auto=True)."""
    data = request.get_json(silent=True) or {}
    peer = (data.get("peer") or request.args.get("peer") or "").strip() or None
    auto = bool(data.get("auto", True))
    try:
        r = _do_force_sync(peer_addr=peer, auto=auto)
        return jsonify(r), (200 if r.get("ok") else 400)
    except Exception as e:
        log.exception("api_sync_force falhou")
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/sync/auto", methods=["POST"])
def api_sync_auto():
    """Descobre peer mais alto e dispara sync."""
    try:
        r = _do_force_sync(peer_addr=None, auto=True)
        return jsonify(r), (200 if r.get("ok") else 400)
    except Exception as e:
        log.exception("api_sync_auto falhou")
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# [12] WALLET GUI
# ============================================================
@app.route("/wallet", methods=["GET"])
def serve_wallet_root():
    wallet_path = os.path.join(os.path.dirname(__file__), "index_wallet.html")
    if os.path.exists(wallet_path):
        return send_file(wallet_path)
    return jsonify({
        "ok": False,
        "msg": "index_wallet.html nao encontrado.",
        "peer_id": _BRN_PEER_ID,
    }), 404


@app.route("/wallet/<peer_id>", methods=["GET"])
def serve_wallet_peer(peer_id: str):
    if _BRN_PEER_ID and peer_id != _BRN_PEER_ID:
        return jsonify({
            "ok": False,
            "msg": f"peer_id {peer_id} nao e deste no (e {_BRN_PEER_ID})",
        }), 404

    wallet_path = os.path.join(os.path.dirname(__file__), "index_wallet.html")
    if not os.path.exists(wallet_path):
        return jsonify({
            "ok": False,
            "msg": "index_wallet.html nao encontrado.",
        }), 404

    try:
        with open(wallet_path, "r", encoding="utf-8") as f:
            html = f.read()
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

    if 'name="brn-peer-id"' not in html:
        html = html.replace(
            "</head>",
            f'<meta name="brn-peer-id" content="{peer_id}">\n</head>',
            1,
        )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ============================================================
# [13] RUN standalone
# ============================================================
def run_server(host="0.0.0.0", port=5000):
    app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("[server] Rodando em modo standalone (debug).")
    print("[server] Use main.py em producao.")
    run_server()
