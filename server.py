"""server.py — Servidor Web/API do nó BRN
Versão: 13.9.26
================================================================
Este arquivo expõe a API HTTP (porta 5000) que a GUI (app_wallet_v3)
e o main.py consomem.
================================================================
v13.9.26 (2026-11-23) — COMPATIBILIDADE COM app_wallet_v3 v3.23 + GUI v3.23:
  [NEW] GET /api/mine/gate?force=1 — fonte de verdade do gate de
        mineração. Retorna {mining_allowed, reason, detail{sk_installed,
        local_pk, validators_ready, validators_reason, fast_sync_active,
        checks}}. O app_wallet_v3 usa isso em _verify_sk_on_node().
  [NEW] GET /api/status agora inclui:
          - mining_allowed, mining_gate_reason
          - validators_ready, validators_reason
          - fast_sync_active, fast_sync_until
          - wallet_sk_installed
        A UI (index_wallet.html v3.23) e o app_wallet_v3 v3.23
        dependem desses campos.
  [NEW] GET /api/consensus/validators — retorna {ok, validators[],
        required_sigs, all_required, n_trust, n_active, local_pk,
        local_pk_full, mining_allowed, gate_reason}.
        O app_wallet_v3 v3.23 tenta esse endpoint primeiro e cai
        para /api/mine/gate se 404.
  [NEW] GET /api/block/<height>/signatures — retorna {ok, signatures[],
        signatures_count, required, all_signed}.
        Usado pelo app_wallet_v3.get_multi_sig_status().
  [NEW] GET /api/trust/nodes?limit=N — lista nós do trust_network
        (public_key, trust_score, is_validator, connected, last_seen,
        peer_addr).
  [NEW] GET /api/trust/stats — resumo {total, validators, connected,
        top_score}.
  [NEW] POST /api/wallet/set_validator_sk — recebe {sk_hex, address,
        pubkey}, instala no chain via set_local_validator_sk() e
        retorna {ok, sk_installed, pubkey, propagated, peers_notified,
        in_trust, multisig_ready, warnings, errors}.
  [NEW] GET /api/peers/score — lista peers com trust_score (usado pela
        GUI antiga).
  [NEW] GET /api/blocks?start=N&limit=M — paginação de blocos (usado
        pela GUI/explorer).
  [NEW] GET /api/latest — últimos 10 blocos (compat com index.html).
  [NEW] GET /api/block/<height> — bloco individual.
  [NEW] GET /api/fee-estimate — {low, medium, high, min_relay_fee}.
  [NEW] GET /api/work — {height, cumulative_work}.
  [NEW] GET /api/minhas-txs/<address>?limit=N — histórico de txs.
  [NEW] GET /api/tx-status/<txid> — status da tx.
  [NEW] GET /api/verificar-recebimento/<addr>/<txid> — confirma se tx
        chegou no endereço.
  [NEW] GET /api/nova-carteira (POST) — cria carteira via Wallet().
        Opcionalmente instala SK automaticamente no nó.
  [NEW] GET /api/portfolio/<address> — saldo + KYC.
  [NEW] GET /api/saldo/<address> — alias para balance.
  [NEW] POST /api/transfer, /api/send, /api/enviar — envio de BRN.
  [NEW] POST /api/miner/start, /api/miner/stop, /api/miner/status.
  [NEW] POST /api/pool/start_local, /api/pool/stop_local,
        GET /api/pool/local_status.
  [NEW] GET /api/sync/status — status detalhado de sync.
  [NEW] Rota /wallet/<peer_id> via Flask (fallback se FastAPI não
        estiver montado).
  [KEEP] Compatibilidade total com main.py, FastAPI bridge e
         app_wallet_v3.
================================================================
"""
from __future__ import annotations

import io as _io
import json
import logging
import os
import threading
import time
from decimal import Decimal, ROUND_DOWN
from functools import wraps
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

# ============================================================
# ESTADO GLOBAL (injetado pelo main.py)
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
    _http_sync_state = state or {}


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
    """Retorna PoolClient se estiver rodando (usado pelo pool/local_status)."""
    try:
        from miner_pool_client import get_pool_client
        return get_pool_client()
    except Exception:
        return None


# ============================================================
# Helpers de gate de mineração
# ============================================================
def _build_gate_detail() -> dict:
    """Monta o detail completo do gate de mineração. Lê do chain + state."""
    chain = _get_chain()
    h = -1
    if chain is not None:
        try:
            h = int(chain.db.height())
        except Exception:
            pass

    # SK instalada?
    sk_installed = False
    local_pk = ""
    if chain is not None:
        sk = getattr(chain, "_local_validator_sk", None)
        if sk is not None:
            sk_installed = True
            try:
                local_pk = getattr(sk, "pub_hex", "") or ""
            except Exception:
                pass

    # Trust
    trust = _get_trust_network()
    n_trust = 0
    n_active = 0
    validators_ready = False
    validators_reason = ""
    if chain is not None:
        try:
            active = chain._get_active_validators()
            n_active = len(active)
        except Exception:
            active = []
        try:
            tv = chain._get_trust_validators()
            n_trust = len(tv)
        except Exception:
            tv = []
        if n_active > 0:
            validators_ready = True
            validators_reason = f"{n_active} validador(es) ativo(s)"
        else:
            validators_reason = (
                "trust_network vazio — bootstrap bypass ativo"
                if n_trust == 0 else
                f"{n_trust} no trust mas 0 conectados"
            )

    # Fast-sync
    fast_sync_until = int(os.environ.get("BRN_FAST_SYNC_UNTIL", "0") or "0")
    fast_sync_active = bool(fast_sync_until > 0 and h < fast_sync_until)

    # Pool client local
    pc = _get_pool_client_local()
    pool_client_running = False
    if pc is not None:
        try:
            pool_client_running = bool(pc.is_running())
        except Exception:
            pass

    checks = {
        "height": {"ok": h >= 0, "current": h, "required": 0},
        "sk": {"ok": sk_installed, "local_pk": (local_pk[:16] + "...") if local_pk else ""},
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
            "active": list(active) if 'active' in dir() else [],
        },
        "checks": checks,
        "pool_client_running": pool_client_running,
    }


def _compute_mining_allowed() -> tuple:
    """Retorna (allowed: bool, reason: str, detail: dict)."""
    detail = _build_gate_detail()

    if detail["fast_sync_active"]:
        return False, f"fast_sync_below_{detail['fast_sync_until']}", detail
    if not detail["sk_installed"]:
        return False, "no_sk", detail
    if not detail["validators_ready"]:
        return False, "validators_not_synced", detail

    return True, "ok", detail


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

    return jsonify({
        "ok": True,
        "height": h,
        "peer_id": _BRN_PEER_ID,
        "l2_enabled": True,
        "chain_injected": _chain is not None,
        "sync_injected": _sync_manager is not None,
        "wallet_sk_installed": chain_sk,
        "validators": {
            "n_trust": 0,
            "n_active": 0,
            "required_sigs": 0,
            "bootstrap_would_fire": False,
            "local_pk": "",
        },
        "version": "13.9.26",
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

        # Gate + campos extras
        allowed, reason, detail = _compute_mining_allowed()

        return jsonify({
            "name": "BrunoCoin",
            "ticker": "BRN",
            "height": h,
            "tip_hash": tip,
            "peers": n_peers,
            "reward": _chain.current_reward(max(h, 1)),
            "difficulty": _chain.current_difficulty(),
            "mempool": _chain.db.mempool_count() if hasattr(_chain.db, "mempool_count") else 0,
            "utxos": _chain.db.count_utxos() if hasattr(_chain.db, "count_utxos") else 0,
            "peer_id": _BRN_PEER_ID,
            "fila_tamanho": len(getattr(_chain, "fila_mineracao", {}) or {}),
            "rede_descentralizada": bool(getattr(_chain, "rede_descentralizada", False)),
            # ============================================================
            # [v13.9.26] Campos do gate de mineração (vistos pela GUI)
            # ============================================================
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
# [2] GATE DE MINERAÇÃO (fonte de verdade do app_wallet_v3)
# ============================================================
@app.route("/api/mine/gate", methods=["GET"])
def api_mine_gate():
    """
    Fonte de verdade do gate de mineração.

    Query params:
      force=1  → ignora cache (sempre recalcula)

    Retorna:
      {
        ok: bool,
        mining_allowed: bool,
        reason: str,             # ok | no_sk | validators_not_synced | fast_sync_below_N
        detail: {
          current_height: int,
          sk_installed: bool,
          local_pk: str,         # truncado
          local_pk_full: str,    # completo (para o app_wallet_v3 comparar)
          validators_ready: bool,
          validators_reason: str,
          fast_sync_active: bool,
          fast_sync_until: int,
          checks: {...},
        }
      }
    """
    try:
        # O parâmetro force é lido mas o cálculo é sempre fresco
        # (não há cache aqui; o cache é feito dentro do _build_gate_detail
        # via env vars, mas o _check_mining_gates do main.py é o que cacheia)
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
# [3] CONSENSO — validadores + assinaturas do bloco
# ============================================================
@app.route("/api/consensus/validators", methods=["GET"])
def api_consensus_validators():
    """
    Lista validadores ativos + requerimentos de assinatura.

    Retorna:
      {
        ok: bool,
        validators: list[str],        # pubkeys hex completos
        required_sigs: int,
        all_required: bool,
        n_trust: int,
        n_active: int,
        local_pk: str,                # truncado
        local_pk_full: str,           # completo
        local_pk_installed: bool,
        bootstrap_would_fire: bool,
        mining_allowed: bool,
        gate_reason: str,
      }
    """
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
            "require_all_sigs": bool(os.environ.get("BRN_REQUIRE_ALL_SIGS", "1") == "1"),
            "validator_quorum": float(os.environ.get("BRN_VALIDATOR_QUORUM", "0.67")),
        })
    except Exception as e:
        log.exception("api_consensus_validators falhou")
        return jsonify({"ok": False, "msg": str(e),
                        "validators": [], "required_sigs": 0,
                        "all_required": True}), 500


@app.route("/api/block/<int:height>/signatures", methods=["GET"])
def api_block_signatures(height: int):
    """
    Retorna as assinaturas do bloco <height>.

    Retorna:
      {
        ok: bool,
        height: int,
        signatures: [{"pubkey": str, "signature": str}],
        signatures_count: int,
        required: int,
        all_signed: bool,
        local_pk: str,           # truncado
        local_pk_full: str,      # completo
      }
    """
    if _chain is None:
        return jsonify({"ok": False, "msg": "chain nao injetada",
                        "signatures": [], "signatures_count": 0}), 503

    try:
        blk = _chain.db.get_block(int(height))
        if not blk:
            return jsonify({"ok": False, "msg": f"bloco #{height} nao encontrado",
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
    """Lista nós do trust_network."""
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
    """Resumo do trust_network."""
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
# [5] WALLET — instalar SK no nó (chamado pelo app_wallet_v3)
# ============================================================
@app.route("/api/wallet/set_validator_sk", methods=["POST"])
def api_wallet_set_validator_sk():
    """
    Instala SK no nó. Chamado pelo app_wallet_v3._sync_sk_to_chain().

    Body: {sk_hex, address, pubkey}
    """
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

        # Tenta via wallet_integrator primeiro (propaga pra peers)
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

        # Fallback direto: seta no chain
        try:
            _chain.set_local_validator_sk(wobj)
        except Exception as e:
            errors.append(f"set_local_validator_sk: {e}")

        # Confirma que está no chain
        sk_now = getattr(_chain, "_local_validator_sk", None)
        sk_installed = sk_now is not None

        # Verifica se está no trust
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
# [6] CARTEIRA — criar, portfolio, saldo, histórico
# ============================================================
@app.route("/api/nova-carteira", methods=["POST"])
def api_nova_carteira():
    """Cria carteira nova e (opcionalmente) instala SK no nó."""
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

        # Tenta salvar current_wallet.enc
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

        # Instala SK no nó
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
                    u = _chain.db.get
