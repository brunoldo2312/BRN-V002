"""origin_manager.py — Promoção/rebaixamento de Origin (v1.0)
================================================================
Estado persistido em ChainDB meta (sobrevive a restart):
  brn_origin_override       "1" | "0"
  brn_origin_promoted_at    unix ts
  brn_origin_promoted_by    peer_id
  brn_origin_promoted_reason texto

Rotas (registrar no server.py):
  GET  /api/node/origin/status
  POST /api/node/origin/promote
  POST /api/node/origin/demote

Integração no main.py (antes de Blockchain):
    from origin_manager import apply_origin_override_at_boot
    is_origin = apply_origin_override_at_boot(db_path, is_origin)
================================================================
"""
from __future__ import annotations
import os
import time
import logging
import sqlite3
from pathlib import Path
from typing import Optional

log = logging.getLogger("origin_manager")

# ============================================================
# DB helpers (sem depender de ChainDB — funciona antes do boot)
# ============================================================
def _meta_get(db_path: str, key: str) -> Optional[str]:
    p = Path(db_path) if os.path.isabs(db_path) else Path(__file__).parent / db_path
    if not p.exists():
        return None
    try:
        c = sqlite3.connect(str(p), timeout=5)
        try:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None
        finally:
            c.close()
    except Exception:
        return None


def _meta_set(db_path: str, key: str, value: str) -> bool:
    p = Path(db_path) if os.path.isabs(db_path) else Path(__file__).parent / db_path
    try:
        c = sqlite3.connect(str(p), timeout=5)
        try:
            c.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            c.commit()
            return True
        finally:
            c.close()
    except Exception as e:
        log.error("[meta] set %s falhou: %s", key, e)
        return False


def _meta_del(db_path: str, key: str) -> bool:
    p = Path(db_path) if os.path.isabs(db_path) else Path(__file__).parent / db_path
    try:
        c = sqlite3.connect(str(p), timeout=5)
        try:
            c.execute("DELETE FROM meta WHERE key = ?", (key,))
            c.commit()
            return True
        finally:
            c.close()
    except Exception:
        return False


# ============================================================
# APLICAÇÃO NO BOOT (chamado pelo main.py ANTES de Blockchain)
# ============================================================
def apply_origin_override_at_boot(db_path: str, is_origin_from_env: bool) -> bool:
    """
    Lê o override persistido e ajusta o estado. Retorna o `is_origin` final.
    Se override=1: força is_origin=True e remove BRN_POOL_URL (para de
    tentar sync de um Origin morto).
    """
    override = _meta_get(db_path, "brn_origin_override")
    if override == "1":
        os.environ["BRN_IS_ORIGIN"] = "1"
        # Remove POOL_URL para não tentar sincronizar de um Origin morto
        old_url = os.environ.pop("BRN_POOL_URL", None)
        log.warning("=" * 64)
        log.warning(" [ORIGIN OVERRIDE] Este nó está promovido a Origin")
        log.warning("  promoted_at  : %s",
                    _meta_get(db_path, "brn_origin_promoted_at") or "?")
        log.warning("  promoted_by  : %s",
                    _meta_get(db_path, "brn_origin_promoted_by") or "?")
        log.warning("  reason       : %s",
                    _meta_get(db_path, "brn_origin_promoted_reason") or "?")
        if old_url:
            log.warning("  BRN_POOL_URL ignorado: %s", old_url)
        log.warning("=" * 64)
        return True

    if override == "0":
        # Explicitamente demovido — não usa env mesmo se env disser sim
        os.environ["BRN_IS_ORIGIN"] = "0"
        log.info("[ORIGIN OVERRIDE] Este nó foi demovido explicitamente a cliente")
        return False

    return is_origin_from_env


# ============================================================
# PROMOÇÃO EM RUNTIME
# ============================================================
def _get_db_path(chain) -> str:
    try:
        return os.environ.get("BRN_DB", "brn_v2_chain.db")
    except Exception:
        return "brn_v2_chain.db"


def _count_alive_peers(chain) -> int:
    """Peers com last_seen < 5min."""
    try:
        peers = chain.db.listar_peers(apenas_ativos=False) or []
        now = time.time()
        n = 0
        for p in peers:
            try:
                ls = float(p.get("last_seen", 0) or 0)
                addr = (p.get("address") or "").strip()
                ip = addr.rsplit(":", 1)[0] if ":" in addr else addr
                if ls > 0 and (now - ls) < 300 and not ip.startswith("127."):
                    n += 1
            except Exception:
                pass
        return n
    except Exception:
        return 0


def _check_sk(chain) -> dict:
    """Verifica se o nó tem SK para assinar."""
    try:
        sk = getattr(chain, "_local_validator_sk", None)
        if sk is None:
            return {"ok": False, "reason": "no_sk",
                    "msg": "chain._local_validator_sk is None — desbloqueie a carteira"}
        pk = getattr(sk, "pub_hex", "") or ""
        if not pk:
            return {"ok": False, "reason": "sk_invalid",
                    "msg": "SK carregada mas sem pub_hex"}
        return {"ok": True, "pubkey": pk}
    except Exception as e:
        return {"ok": False, "reason": "exception", "msg": str(e)}


def _clean_trust_dead(chain) -> dict:
    """Tenta remover validadores inativos do trust. Retorna removidos."""
    try:
        trust = getattr(chain, "trust_net", None)
        if trust is None:
            return {"removed": 0, "msg": "trust_net ausente"}
        if hasattr(trust, "cleanup_inactive"):
            n = trust.cleanup_inactive() or 0
            return {"removed": int(n), "msg": "cleanup_inactive()"}
        # Fallback: iterar nodes e remover os com last_seen > 1h
        removed = 0
        try:
            nodes = getattr(trust, "nodes", {}) or {}
            now = time.time()
            to_remove = []
            for pk, node in list(nodes.items()):
                try:
                    ls = float(getattr(node, "last_seen", 0) or 0)
                    if ls > 0 and (now - ls) > 3600:
                        to_remove.append(pk)
                except Exception:
                    pass
            for pk in to_remove:
                try:
                    if hasattr(trust, "remove_node"):
                        trust.remove_node(pk)
                        removed += 1
                    elif hasattr(trust, "ban_node"):
                        trust.ban_node(pk, reason="origin_promotion_cleanup")
                        removed += 1
                except Exception:
                    pass
            if hasattr(trust, "flush"):
                try: trust.flush()
                except Exception: pass
        except Exception:
            pass
        return {"removed": removed, "msg": "fallback_iterate"}
    except Exception as e:
        return {"removed": 0, "msg": str(e)}


def promote_to_origin(chain, reason: str = "manual",
                      force: bool = False) -> dict:
    """
    Promove ESTE nó a Origin.

    Regras:
      - Requer SK carregada (a menos que force=True)
      - Avisa se há peers vivos (pode causar split-brain)
      - Limpa trust de validadores inativos
      - Persiste override no DB
      - Aplica mudanças em runtime (env + chain + trust)
    """
    started = time.time()
    db_path = _get_db_path(chain)

    # 1. Verifica SK
    sk_state = _check_sk(chain)
    if not sk_state["ok"] and not force:
        return {
            "ok": False,
            "msg": f"Sem SK — promoção bloqueada. {sk_state['msg']}. "
                   f"Use force=true para forçar (NÃO vai conseguir minerar).",
            "sk": sk_state,
        }

    # 2. Alerta se há peers vivos (pode ser split-brain)
    alive = _count_alive_peers(chain)
    warning = ""
    if alive > 0:
        warning = (
            f"{alive} peer(s) vivo(s) — promover agora pode causar "
            f"split-brain se o Origin real voltar. Considere aguardar "
            f"o Origin estabilizar ou usar force=true consciente."
        )
        log.warning("[ORIGIN] %s", warning)

    # 3. Limpa trust de mortos
    trust_clean = _clean_trust_dead(chain)

    # 4. Aplica runtime
    old_pool_url = os.environ.get("BRN_POOL_URL", "")
    os.environ["BRN_IS_ORIGIN"] = "1"
    os.environ.pop("BRN_POOL_URL", None)
    os.environ.pop("BRN_POOL_URL_LOCAL", None)

    # Chain attributes
    try: chain.is_origin = True
    except Exception: pass

    # Trust attributes
    try:
        if getattr(chain, "trust_net", None) is not None:
            chain.trust_net.is_origin = True
    except Exception: pass

    # Invalida mining gate cache (se server.py estiver carregado)
    try:
        import server as _srv
        if hasattr(_srv, "_invalidate_mining_gate_cache"):
            _srv._invalidate_mining_gate_cache()
            log.info("[ORIGIN] mining gate cache invalidado")
    except Exception:
        pass

    # 5. Persiste
    peer_id = os.environ.get("BRN_PEER_ID", "unknown")
    _meta_set(db_path, "brn_origin_override", "1")
    _meta_set(db_path, "brn_origin_promoted_at", str(int(time.time())))
    _meta_set(db_path, "brn_origin_promoted_by", peer_id)
    _meta_set(db_path, "brn_origin_promoted_reason", reason)

    # 6. Tenta parar sync HTTP (se sync_manager tiver método)
    sync_stopped = False
    try:
        sync = getattr(chain, "_sync_manager", None)
        if sync is None:
            try:
                import server as _srv
                sync = _srv._get_sync_manager()
            except Exception:
                pass
        if sync is not None and hasattr(sync, "disable_http_sync"):
            sync.disable_http_sync()
            sync_stopped = True
            log.info("[ORIGIN] sync_manager.disable_http_sync() chamado")
        elif sync is not None and hasattr(sync, "stop"):
            # NÃO parar o sync inteiro — só HTTP. Preferimos não parar.
            log.warning("[ORIGIN] sync_manager sem disable_http_sync() — "
                        "pode continuar tentando %s. Considere reiniciar.",
                        old_pool_url or "(nenhum)")
    except Exception as e:
        log.debug("[ORIGIN] tentativa de parar sync falhou: %s", e)

    # 7. Broadcast P2P (informativo — peers podem atualizar bootstrap)
    broadcast_ok = False
    try:
        p2p = getattr(chain, "p2p", None)
        if p2p is not None and hasattr(p2p, "broadcast_origin_announce"):
            p2p.broadcast_origin_announce(peer_id, reason)
            broadcast_ok = True
    except Exception:
        pass

    elapsed = round(time.time() - started, 2)
    log.warning("[ORIGIN] PROMOVIDO (peer=%s, reason=%s, %.2fs)",
                peer_id, reason, elapsed)

    return {
        "ok": True,
        "peer_id": peer_id,
        "promoted_at": int(time.time()),
        "reason": reason,
        "sk": sk_state,
        "alive_peers": alive,
        "trust_cleaned": trust_clean,
        "old_pool_url": old_pool_url,
        "sync_http_stopped": sync_stopped,
        "broadcast_ok": broadcast_ok,
        "warning": warning,
        "restart_required": False,
        "msg": "Origin promovido em runtime. Se /api/mine/gate ainda bloquear, "
               "reinicie o nó (o override está persistido no DB).",
    }


def demote_from_origin(chain, reason: str = "manual") -> dict:
    """Reverte para cliente. Restaura BRN_POOL_URL se foi perdido."""
    started = time.time()
    db_path = _get_db_path(chain)

    os.environ["BRN_IS_ORIGIN"] = "0"
    try: chain.is_origin = False
    except Exception: pass
    try:
        if getattr(chain, "trust_net", None) is not None:
            chain.trust_net.is_origin = False
    except Exception: pass

    _meta_set(db_path, "brn_origin_override", "0")
    _meta_set(db_path, "brn_origin_demoted_at", str(int(time.time())))
    _meta_set(db_path, "brn_origin_demoted_reason", reason)

    try:
        import server as _srv
        if hasattr(_srv, "_invalidate_mining_gate_cache"):
            _srv._invalidate_mining_gate_cache()
    except Exception:
        pass

    pool_url = os.environ.get("BRN_POOL_URL", "").strip()
    elapsed = round(time.time() - started, 2)
    log.warning("[ORIGIN] REBAIXADO a cliente (reason=%s, %.2fs)",
                reason, elapsed)

    return {
        "ok": True,
        "demoted_at": int(time.time()),
        "reason": reason,
        "pool_url_current": pool_url,
        "restart_required": not bool(pool_url),
        "msg": ("Cliente em runtime. Configure BRN_POOL_URL para apontar "
                "ao novo Origin, ou reinicie se vazio." if not pool_url
                else "Cliente. BRN_POOL_URL mantido."),
    }


def origin_status(chain) -> dict:
    """Estado atual completo — útil para diagnóstico."""
    db_path = _get_db_path(chain)
    sk_state = _check_sk(chain)

    pool_url = os.environ.get("BRN_POOL_URL", "").strip()
    pool_reachable = None
    if pool_url:
        try:
            import urllib.request
            req = urllib.request.Request(pool_url.rstrip("/") + "/health",
                                          method="GET")
            with urllib.request.urlopen(req, timeout=2) as r:
                pool_reachable = (r.status == 200)
        except Exception:
            pool_reachable = False

    try: local_h = int(chain.db.height())
    except Exception: local_h = -1

    try:
        trust = getattr(chain, "trust_net", None)
        trust_is_origin = bool(getattr(trust, "is_origin", False)) if trust else False
        trust_nodes = len(getattr(trust, "nodes", {}) or {}) if trust else 0
    except Exception:
        trust_is_origin = False
        trust_nodes = 0

    # Mining gate
    try:
        import server as _srv
        gate = _srv._check_mining_gates(chain, force_refresh=True)
    except Exception as e:
        gate = {"mining_allowed": False, "reason": f"gate unavailable: {e}",
                "detail": {}}

    return {
        "ok": True,
        "peer_id": os.environ.get("BRN_PEER_ID", ""),
        "is_origin_env": os.environ.get("BRN_IS_ORIGIN", "0") == "1",
        "is_origin_override": _meta_get(db_path, "brn_origin_override"),
        "promoted_at": _meta_get(db_path, "brn_origin_promoted_at"),
        "promoted_by": _meta_get(db_path, "brn_origin_promoted_by"),
        "promoted_reason": _meta_get(db_path, "brn_origin_promoted_reason"),
        "demoted_at": _meta_get(db_path, "brn_origin_demoted_at"),
        "demoted_reason": _meta_get(db_path, "brn_origin_demoted_reason"),
        "sk": sk_state,
        "pool_url": pool_url,
        "pool_reachable": pool_reachable,
        "local_height": local_h,
        "alive_peers": _count_alive_peers(chain),
        "trust_is_origin": trust_is_origin,
        "trust_nodes": trust_nodes,
        "mining_gate": {
            "allowed": bool(gate.get("mining_allowed", False)),
            "reason": gate.get("reason", ""),
            "checks": (gate.get("detail") or {}).get("checks", {}),
        },
    }
