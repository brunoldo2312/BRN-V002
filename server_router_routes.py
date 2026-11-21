"""server_router_routes.py — Rotas /admin/origin/* (Plano B+)
================================================================
Drop-in. Chame no final do server.py:

    from server_router_routes import register_router_routes
    register_router_routes(app)

Endpoints:
  GET  /admin/origin/state     - estado atual (útil para monitor)
  POST /admin/origin/promote   - vira origin (com verify_primary)
  POST /admin/origin/demote    - volta a standby

Autenticação: Authorization: Bearer <BRN_ADMIN_TOKEN>
================================================================
"""
from __future__ import annotations
import os
import time
import hmac
import logging
import threading
import urllib.error
import urllib.request
from functools import wraps
from flask import request, jsonify

log = logging.getLogger("server_router")

_ROUTER_STATE = {
    "is_promoted": False,
    "promoted_at": 0,
    "promoted_by": "",
    "last_action": "",
    "last_action_at": 0,
    "original_env_is_origin": os.environ.get("BRN_IS_ORIGIN", "0"),
}
_STATE_LOCK = threading.RLock()


def _admin_ok() -> bool:
    token = os.environ.get("BRN_ADMIN_TOKEN", "").strip()
    if not token:
        return True  # dev sem token
    hdr = request.headers.get("Authorization", "")
    return hdr.startswith("Bearer ") and hmac.compare_digest(hdr[7:], token)


def _admin(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not _admin_ok():
            return jsonify({"ok": False, "msg": "unauthorized"}), 401
        return fn(*a, **kw)
    return wrapper


def _probe_url(url: str, timeout: float = 5.0) -> tuple:
    """Retorna (ok, reason). Probe /health do URL dado."""
    try:
        req = urllib.request.Request(f"{url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, "ok"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, str(e.reason)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def register_router_routes(app) -> None:
    # Acesso lazy a globais do server.py (evita circular import no boot)
    def _srv():
        import server
        return server

    # ---------- GET /admin/origin/state ----------
    @app.route("/admin/origin/state", methods=["GET"])
    @_admin
    def api_origin_state():
        srv = _srv()
        with _STATE_LOCK:
            st = dict(_ROUTER_STATE)
        env_origin = os.environ.get("BRN_IS_ORIGIN", "0") == "1"
        effective = env_origin or st["is_promoted"]

        # Estado do pool local (nós promovidos têm PoolManager; standby não)
        pool = srv._get_pool_manager()
        pool_ready = pool is not None

        # Trust atual
        trust = srv._get_trust_network()
        trust_is_origin = False
        try:
            trust_is_origin = bool(getattr(trust, "is_origin", False))
        except Exception:
            pass

        return jsonify({
            "ok": True,
            "router_id": os.environ.get("BRN_ROUTER_ID", ""),
            "env_is_origin": env_origin,
            "router_promoted": st["is_promoted"],
            "effective_origin": effective,
            "promoted_at": st["promoted_at"],
            "promoted_by": st["promoted_by"],
            "last_action": st["last_action"],
            "last_action_at": st["last_action_at"],
            "pool_manager_ready": pool_ready,
            "trust_is_origin": trust_is_origin,
            "strict_multisig": os.environ.get("BRN_STRICT_MULTISIG", "0") == "1",
            "network_secret_set": bool(os.environ.get("BRN_NETWORK_SECRET", "").strip()),
        })

    # ---------- POST /admin/origin/promote ----------
    @app.route("/admin/origin/promote", methods=["POST"])
    @_admin
    def api_origin_promote():
        srv = _srv()
        data = request.get_json(silent=True) or {}
        by = (data.get("by") or "unknown").strip()
        verify_primary = (data.get("verify_primary") or "").strip().rstrip("/")
        primary_fails = int(data.get("primary_fails", 0) or 0)

        # --- ANTI-SPLIT-BRAIN: re-verifica por conta própria ---
        if verify_primary:
            ok, reason = _probe_url(verify_primary, timeout=5.0)
            if ok:
                log.error(
                    "[ROUTER] promote RECUSADO: primary %s está acessível "
                    "daqui (%s) — possível split brain",
                    verify_primary, reason,
                )
                return jsonify({
                    "ok": False,
                    "msg": (
                        f"RECUSADO: primary {verify_primary} está acessível "
                        f"deste nó ({reason}). Possível split brain — "
                        f"não vou promover."
                    ),
                    "reason": "primary_reachable",
                }), 409
            log.warning(
                "[ROUTER] verify_primary OK: %s inacessível daqui também (%s)",
                verify_primary, reason,
            )

        # --- Idempotência ---
        with _STATE_LOCK:
            if _ROUTER_STATE["is_promoted"]:
                return jsonify({
                    "ok": True,
                    "already_promoted": True,
                    "promoted_at": _ROUTER_STATE["promoted_at"],
                    "promoted_by": _ROUTER_STATE["promoted_by"],
                })
            _ROUTER_STATE["is_promoted"] = True
            _ROUTER_STATE["promoted_at"] = int(time.time())
            _ROUTER_STATE["promoted_by"] = by
            _ROUTER_STATE["last_action"] = "promote"
            _ROUTER_STATE["last_action_at"] = int(time.time())

        actions_done = []
        errors = []

        # --- 1. Set BRN_IS_ORIGIN=1 em runtime ---
        os.environ["BRN_IS_ORIGIN"] = "1"
        actions_done.append("env_updated")

        # --- 2. Cria PoolManager local (para servir pool, não mais proxy) ---
        try:
            if srv._get_pool_manager() is None:
                from mining_pool import PoolManager
                chain = srv._get_chain()
                pool = PoolManager(chain)
                srv.set_pool_manager(pool)
                actions_done.append("pool_manager_created")
                log.warning("[ROUTER] PoolManager local criado — agora serve pool")
            else:
                actions_done.append("pool_manager_already_present")
        except ImportError as e:
            errors.append(f"PoolManager indisponível: {e}")
            log.error("[ROUTER] PoolManager import falhou: %s", e)
        except Exception as e:
            errors.append(f"PoolManager instanciar: {e}")
            log.exception("[ROUTER] PoolManager instanciar falhou")

        # --- 3. Trust: tenta re-ativar como origin ---
        try:
            trust = srv._get_trust_network()
            if trust is not None:
                if hasattr(trust, "set_is_origin"):
                    try:
                        trust.set_is_origin(True)
                        actions_done.append("trust_set_is_origin")
                    except Exception as e:
                        errors.append(f"trust.set_is_origin: {e}")
                else:
                    try:
                        trust.is_origin = True
                        actions_done.append("trust_is_origin_attr")
                    except Exception as e:
                        errors.append(f"trust.is_origin=: {e}")

                # Tenta re-assinar trust (não falha se método ausente)
                for meth in ("sign_and_save", "sign", "flush"):
                    fn = getattr(trust, meth, None)
                    if callable(fn):
                        try:
                            fn()
                            actions_done.append(f"trust_{meth}")
                            break
                        except Exception as e:
                            log.warning("[ROUTER] trust.%s falhou: %s", meth, e)
        except Exception as e:
            errors.append(f"trust_reactivate: {e}")

        # --- 4. Invalida cache de mining gate ---
        try:
            srv._invalidate_mining_gate_cache()
            actions_done.append("gate_cache_invalidated")
        except Exception:
            pass

        # --- 5. Broadcast via P2P (se disponível) ---
        try:
            p2p = srv._get_p2p_manager()
            if p2p is not None and hasattr(p2p, "broadcast_origin_change"):
                try:
                    p2p.broadcast_origin_change(
                        new_origin=srv._BRN_PEER_ID or "unknown",
                        reason=f"promoted_by_{by}",
                    )
                    actions_done.append("p2p_broadcast")
                except Exception as e:
                    errors.append(f"p2p_broadcast: {e}")
        except Exception:
            pass

        log.warning("=" * 60)
        log.warning("[ROUTER] PROMOVIDO A ORIGIN por %s", by)
        log.warning("[ROUTER] Ações: %s", ", ".join(actions_done))
        if errors:
            log.warning("[ROUTER] Erros parciais: %s", errors)
        log.warning("=" * 60)

        return jsonify({
            "ok": True,
            "promoted_at": _ROUTER_STATE["promoted_at"],
            "promoted_by": by,
            "actions_done": actions_done,
            "errors": errors,
        })

    # ---------- POST /admin/origin/demote ----------
    @app.route("/admin/origin/demote", methods=["POST"])
    @_admin
    def api_origin_demote():
        srv = _srv()
        data = request.get_json(silent=True) or {}
        by = (data.get("by") or "unknown").strip()

        with _STATE_LOCK:
            if not _ROUTER_STATE["is_promoted"]:
                return jsonify({"ok": True, "already_standby": True})
            _ROUTER_STATE["is_promoted"] = False
            _ROUTER_STATE["promoted_at"] = 0
            _ROUTER_STATE["promoted_by"] = ""
            _ROUTER_STATE["last_action"] = "demote"
            _ROUTER_STATE["last_action_at"] = int(time.time())
            original_env = _ROUTER_STATE.get("original_env_is_origin", "0")

        actions_done = []
        errors = []

        # --- 1. Restaura BRN_IS_ORIGIN ao valor original ---
        # Se o operador setou =1 manualmente no boot, respeitamos.
        # Só zeramos se era 0 originalmente (router que subiu pra 1).
        if original_env != "1":
            os.environ["BRN_IS_ORIGIN"] = "0"
            actions_done.append("env_restored")
        else:
            log.info("[ROUTER] BRN_IS_ORIGIN=%s original — não mexo", original_env)
            actions_done.append("env_preserved")

        # --- 2. Destrói PoolManager local (volta a ser proxy) ---
        try:
            if srv._get_pool_manager() is not None:
                srv.set_pool_manager(None)
                actions_done.append("pool_manager_destroyed")
                log.warning("[ROUTER] PoolManager local removido — volta a proxy")
        except Exception as e:
            errors.append(f"pool_destroy: {e}")

        # --- 3. Trust volta a standby ---
        try:
            trust = srv._get_trust_network()
            if trust is not None:
                if hasattr(trust, "set_is_origin"):
                    try:
                        trust.set_is_origin(False)
                        actions_done.append("trust_set_is_origin_false")
                    except Exception as e:
                        errors.append(f"trust.set_is_origin(False): {e}")
                else:
                    try:
                        trust.is_origin = False
                        actions_done.append("trust_is_origin_attr_false")
                    except Exception as e:
                        errors.append(f"trust.is_origin=False: {e}")
        except Exception as e:
            errors.append(f"trust_deactivate: {e}")

        # --- 4. Invalida mining gate ---
        try:
            srv._invalidate_mining_gate_cache()
            actions_done.append("gate_cache_invalidated")
        except Exception:
            pass

        log.warning("=" * 60)
        log.warning("[ROUTER] REBAIXADO A STANDBY por %s", by)
        log.warning("[ROUTER] Ações: %s", ", ".join(actions_done))
        log.warning("=" * 60)

        return jsonify({
            "ok": True,
            "actions_done": actions_done,
            "errors": errors,
        })

    log.info("[ROUTER] rotas /admin/origin/* registradas")
