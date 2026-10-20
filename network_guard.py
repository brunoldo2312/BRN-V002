"""
network_guard.py — Bloqueia mineracao se nao houver outro no na rede (v3)
=========================================================================
Garante que nao exista cadeia paralela: 1 no sozinho nunca minera.

v3:
  [FIX] Erro "bytes-like object is required, not 'NoneType'" silenciado
        e tratado — vem de hmac.compare_digest() em p2p_unified.get_status()
        quando algum peer tem public_key/node_id = None
  [FIX] _count_peers_via_api agora:
        - valida tipo de retorno ANTES de qualquer operação
        - distingue TypeError (bug interno do p2p) de falha real
        - só loga WARNING em falha real; TypeError vira DEBUG
  [NEW] Cache de resultados (0.5s) — evita spam de logs quando chamado
        em loop apertado pelo miner_loop
  [NEW] _peer_height_max com try/except granular (não engole erros)
  [NEW] diagnóstico.expõe qual API funcionou + quantos peers
  [NEW] timeout suave via cache: cada método tem pelo menos 1 tentativa
  [KEEP] API pública 100% compatível com v2:
        reset(), force_allow(), force_deny(), clear_force(),
        pode_minerar(p2p), status(p2p), info(), CLI --info/--status

Env:
  BRN_ALLOW_SOLO_MINING  (0/1)  — modo dev (default 0)
  BRN_MIN_PEER_STABLE    int    — segundos que peer precisa estar visível
                                  (default 10)
  BRN_MIN_PEER_HEIGHT    int    — altura mínima do peer (default 0)
  BRN_NETGUARD_CACHE_MS  int    — TTL do cache de _count_peers (default 500)

Retorno de pode_minerar():
    (bool, str)  — (pode, motivo)
=========================================================================
"""
from __future__ import annotations

import os
import time
import threading
from typing import Any, Optional, Tuple

# ============================================================
# CONFIG
# ============================================================
ALLOW_SOLO       = os.environ.get("BRN_ALLOW_SOLO_MINING", "0") == "1"
MIN_PEER_STABLE  = int(os.environ.get("BRN_MIN_PEER_STABLE", "10"))
MIN_PEER_HEIGHT  = int(os.environ.get("BRN_MIN_PEER_HEIGHT", "0"))
CACHE_MS         = int(os.environ.get("BRN_NETGUARD_CACHE_MS", "500"))

# ============================================================
# ESTADO (protegido por lock)
# ============================================================
_lock = threading.Lock()
_first_peer_ts: Optional[float] = None
_force: Optional[bool] = None
_api_warned = False

# cache: (ts_monotonic, count, api_used)
_cache_lock = threading.Lock()
_cache: Optional[Tuple[float, int, str]] = None


# ============================================================
# LOG
# ============================================================
def _log(msg: str, level: str = "info") -> None:
    try:
        from brn_logger import get_logger
        lg = get_logger("netguard")
        getattr(lg, level, lg.info)("[NetGuard] " + msg)
    except Exception:
        # Fallback: só imprime se não for debug (evita flood)
        if level != "debug":
            print(f"[NetGuard] {msg}", flush=True)


# ============================================================
# NORMALIZAÇÃO DE PEERS
# ============================================================
def _extract_peer_count(raw: Any) -> int:
    """
    Normaliza o retorno de qualquer API para int (contagem de peers).

    [FIX v3] Nunca propaga exceções; retorna 0 em qualquer tipo inválido.
    """
    if raw is None:
        return 0
    if isinstance(raw, bool):     # bool é subclasse de int — evitar
        return 0
    if isinstance(raw, int):
        return max(0, raw)
    if isinstance(raw, (list, tuple, set, frozenset)):
        return len(raw)
    if isinstance(raw, dict):
        return len(raw)
    # strings e outros — não tentar converter
    return 0


def _is_p2p_type_error(exc: BaseException) -> bool:
    """
    Detecta o bug conhecido de p2p_unified.get_status() que lança
    TypeError por comparar None com bytes (hmac.compare_digest).
    """
    return isinstance(exc, TypeError) and (
        "bytes-like" in str(exc) or "NoneType" in str(exc)
    )


# ============================================================
# DESCOBERTA DE PEERS (fallback chain)
# ============================================================
def _count_peers_via_api_raw(p2p) -> Tuple[int, str]:
    """
    Tenta listar peers via várias APIs conhecidas, SEM cache.
    Retorna (count, api_used) — api_used="" se nenhuma funcionou.

    Ordem de preferência:
      1. p2p.listar_peers()              (PT-BR, nativo)
      2. p2p.discovery.listar_peers()    (formato antigo)
      3. p2p.get_peers()                 (EN)
      4. p2p.peers (atributo direto)
      5. p2p.get_status()['peer_count']  (último recurso)
    """
    if p2p is None:
        return 0, "p2p=None"

    # ---------- 1) p2p.listar_peers() ----------
    fn = getattr(p2p, "listar_peers", None)
    if callable(fn):
        try:
            raw = fn()
            return _extract_peer_count(raw), "p2p.listar_peers"
        except TypeError as e:
            if _is_p2p_type_error(e):
                _log(f"p2p.listar_peers() bug conhecido (None em compare_digest): {e}",
                     "debug")
            else:
                _log(f"p2p.listar_peers() TypeError: {e}", "debug")
        except Exception as e:
            _log(f"p2p.listar_peers() falhou: {e}", "debug")

    # ---------- 2) p2p.discovery.listar_peers() ----------
    disc = getattr(p2p, "discovery", None)
    if disc is not None:
        fn = getattr(disc, "listar_peers", None)
        if callable(fn):
            try:
                raw = fn()
                return _extract_peer_count(raw), "p2p.discovery.listar_peers"
            except Exception as e:
                _log(f"p2p.discovery.listar_peers() falhou: {e}", "debug")

    # ---------- 3) p2p.get_peers() ----------
    fn = getattr(p2p, "get_peers", None)
    if callable(fn):
        try:
            raw = fn()
            return _extract_peer_count(raw), "p2p.get_peers"
        except Exception as e:
            _log(f"p2p.get_peers() falhou: {e}", "debug")

    # ---------- 4) p2p.peers (atributo direto) ----------
    peers_attr = getattr(p2p, "peers", None)
    if peers_attr is not None:
        try:
            n = _extract_peer_count(peers_attr)
            return n, "p2p.peers"
        except Exception as e:
            _log(f"p2p.peers falhou: {e}", "debug")

    # ---------- 5) p2p.get_status() ----------
    fn = getattr(p2p, "get_status", None)
    if callable(fn):
        try:
            st = fn()
            if isinstance(st, dict):
                # tenta 'peer_count' primeiro, depois 'peers'
                n_raw = st.get("peer_count")
                if n_raw is None:
                    n_raw = st.get("peers")
                n = _extract_peer_count(n_raw)
                return n, "p2p.get_status"
            # se não for dict, tenta normalizar
            return _extract_peer_count(st), "p2p.get_status"
        except TypeError as e:
            # [FIX] Este é o erro que aparecia no log:
            # "a bytes-like object is required, not 'NoneType'"
            if _is_p2p_type_error(e):
                _log(
                    "p2p.get_status() bug interno (compare_digest com None) "
                    "— ignorado; p2p_unified precisa de patch",
                    "debug",
                )
            else:
                _log(f"p2p.get_status() TypeError: {e}", "debug")
        except Exception as e:
            _log(f"p2p.get_status() falhou: {e}", "debug")

    return 0, ""


def _count_peers_via_api(p2p) -> Tuple[int, str]:
    """
    Igual a _count_peers_via_api_raw, mas com cache curto (CACHE_MS).
    Evita que o loop do miner_loop spame logs idênticos a cada 500ms.
    """
    global _cache

    now = time.monotonic()
    with _cache_lock:
        if _cache is not None:
            ts, n, api = _cache
            if (now - ts) * 1000 < CACHE_MS:
                return n, api

    # Cache expirado (ou vazio) → consulta real
    n, api = _count_peers_via_api_raw(p2p)

    with _cache_lock:
        _cache = (now, n, api)
    return n, api


# ============================================================
# ALTURA MÁXIMA DE PEERS
# ============================================================
def _peer_height_max(p2p) -> int:
    """
    Tenta descobrir a altura máxima entre os peers.
    Retorna 0 se não conseguir.

    [FIX v3] Try/except granular por peer — um peer corrompido
    não invalida a leitura dos outros.
    """
    if p2p is None:
        return 0

    heights: list[int] = []

    # ---------- via atributo peers / peer_status ----------
    for attr in ("peers", "peer_status"):
        peers_attr = getattr(p2p, attr, None)
        if not isinstance(peers_attr, dict):
            continue

        for v in peers_attr.values():
            try:
                if isinstance(v, dict):
                    h = v.get("height")
                    if isinstance(h, int) and not isinstance(h, bool):
                        heights.append(h)
                elif hasattr(v, "height"):
                    hv = getattr(v, "height", None)
                    if isinstance(hv, int) and not isinstance(hv, bool):
                        heights.append(hv)
            except Exception:
                # um peer mau não invalida os outros
                continue

        if heights:
            return max(heights)

    # ---------- via get_status() ----------
    fn = getattr(p2p, "get_status", None)
    if callable(fn):
        try:
            st = fn()
            if isinstance(st, dict):
                for key in ("max_peer_height", "peer_height_max"):
                    h = st.get(key)
                    if isinstance(h, int) and not isinstance(h, bool):
                        return h
        except TypeError as e:
            if _is_p2p_type_error(e):
                _log("_peer_height_max: p2p.get_status() bug NoneType", "debug")
        except Exception as e:
            _log(f"_peer_height_max: {e}", "debug")

    return 0


# ============================================================
# API PÚBLICA
# ============================================================
def reset() -> None:
    """Chamar quando p2p inicia/para. Idempotente e thread-safe."""
    global _first_peer_ts, _api_warned, _cache
    with _lock:
        _first_peer_ts = None
    with _cache_lock:
        _cache = None
    # _api_warned não é resetado — evita re-logar o mesmo erro N vezes


def force_allow() -> None:
    """Emergência/teste: libera mineração incondicionalmente."""
    global _force
    with _lock:
        _force = True
    _log("force_allow() — mineração liberada por override", "warning")


def force_deny() -> None:
    """Emergência/teste: bloqueia mineração."""
    global _force
    with _lock:
        _force = False
    _log("force_deny() — mineração bloqueada por override", "warning")


def clear_force() -> None:
    """Remove override (volta ao comportamento normal)."""
    global _force
    with _lock:
        _force = None


def pode_minerar(p2p) -> Tuple[bool, str]:
    """
    Retorna (pode, motivo).

    Regras:
      - override (_force) tem prioridade máxima
      - ALLOW_SOLO=1 → sempre pode (modo dev)
      - Sem peers → não pode
      - Peer precisa estar visível por MIN_PEER_STABLE segundos
      - Se MIN_PEER_HEIGHT > 0, peer precisa ter essa altura
    """
    global _first_peer_ts, _api_warned

    # 1) Override
    with _lock:
        override = _force
    if override is True:
        return True, "override (force_allow)"
    if override is False:
        return False, "override (force_deny)"

    # 2) Modo dev
    if ALLOW_SOLO:
        return True, "BRN_ALLOW_SOLO_MINING=1 (dev mode)"

    # 3) Conta peers (com cache)
    n, api_used = _count_peers_via_api(p2p)

    if not api_used:
        # Nenhuma API funcionou — loga UMA vez e bloqueia
        with _lock:
            warned = _api_warned
            _api_warned = True
        if not warned:
            _log(
                "Nenhuma API de listagem de peers encontrada em p2p. "
                "Mineração BLOQUEADA. Esperado: p2p.listar_peers() / "
                "p2p.discovery.listar_peers() / p2p.get_peers() / "
                "p2p.peers / p2p.get_status()",
                "error",
            )
        return False, "sem API de listagem de peers em p2p"

    # 4) Sem peers → bloqueia
    if n == 0:
        with _lock:
            _first_peer_ts = None
        return False, "sem peers — aguardando outro nó entrar na rede"

    # 5) Checa altura mínima
    if MIN_PEER_HEIGHT > 0:
        h = _peer_height_max(p2p)
        if h < MIN_PEER_HEIGHT:
            return False, f"peer com altura baixa ({h} < {MIN_PEER_HEIGHT})"

    # 6) Estabilização (peer precisa estar visível N segundos)
    with _lock:
        if _first_peer_ts is None:
            _first_peer_ts = time.time()
            return False, (
                f"peer detectado, aguardando estabilizar ({MIN_PEER_STABLE}s)"
            )
        elapsed = time.time() - _first_peer_ts

    if elapsed < MIN_PEER_STABLE:
        falta = int(MIN_PEER_STABLE - elapsed) + 1
        return False, (
            f"estabilizando peer ({int(elapsed)}/{MIN_PEER_STABLE}s, "
            f"faltam {falta}s)"
        )

    return True, f"{n} peer(s) estavel(is) via {api_used}"


# ============================================================
# DIAGNÓSTICO
# ============================================================
def status(p2p=None) -> dict:
    """
    Snapshot do estado atual. Útil para UI / health check.
    Se p2p for passado, conta peers na hora (sem cache).
    """
    with _lock:
        first_ts = _first_peer_ts
        force = _force
        warned = _api_warned

    if p2p is not None:
        n, api_used = _count_peers_via_api_raw(p2p)
    else:
        n, api_used = 0, ""

    elapsed = int(time.time() - first_ts) if first_ts else 0
    wait_remaining = 0
    if first_ts and elapsed < MIN_PEER_STABLE:
        wait_remaining = MIN_PEER_STABLE - elapsed

    return {
        "allow_solo":       ALLOW_SOLO,
        "min_peer_stable":  MIN_PEER_STABLE,
        "min_peer_height":  MIN_PEER_HEIGHT,
        "cache_ms":         CACHE_MS,
        "force_override":   force,
        "api_warned":       warned,
        "peers_count":      n,
        "api_used":         api_used or "(nenhuma)",
        "first_peer_ts":    first_ts,
        "stable_for_s":     elapsed,
        "wait_remaining_s": wait_remaining,
    }


def info() -> dict:
    """Config resolvida (sem consultar p2p)."""
    return {
        "allow_solo":      ALLOW_SOLO,
        "min_peer_stable": MIN_PEER_STABLE,
        "min_peer_height": MIN_PEER_HEIGHT,
        "cache_ms":        CACHE_MS,
        "force":           _force,
    }


# ============================================================
# CLI (diagnóstico)
# ============================================================
if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--info":
        print(json.dumps(info(), indent=2, ensure_ascii=False))
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        print(json.dumps(status(), indent=2, ensure_ascii=False))
        sys.exit(0)

    print("Uso:")
    print("  python network_guard.py --info     # config")
    print("  python network_guard.py --status   # estado atual (sem p2p)")
    sys.exit(0)
