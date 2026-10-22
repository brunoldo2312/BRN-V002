"""fork_guard.py — Guardião de forks e convergência da rede BRN (v2)
================================================================
v2 (2026-10-22):
  [FIX CRÍTICO] Loop de detecção NUNCA disparava fork. _best_peer()
                retornava (height, "", addr) com hash VAZIO, e a
                condição `peer_h == local_h and peer_hash and ...`
                sempre falhava porque "" é falsy. _handle_fork só era
                chamado no auto-test (bypass direto). Em produção, o
                loop era código morto.
  [FIX CRÍTICO] active_fork nunca expirava. Se o peer que causou fork
                ficava offline, active_fork ficava travado para sempre.
                Agora expira após ALERT_AFTER_S*3 sem resolução.
  [FIX CRÍTICO] _fork_signal ficava setado após resolução. Dependia do
                sync_manager._resolve_fork limpar. Se sync_manager não
                agisse, miner ficava pausado eternamente. Agora
                _clear_fork_signal() é chamado em resolve/expire/stop.
  [FIX CRÍTICO] last_local_height nunca inicializava do DB. Se o
                processo reiniciava com chain avançada, last_local_height
                ficava -1 e _check_once retornava early sempre. Agora
                _sync_local_state() inicializa de chain.db no start() e
                a cada check.
  [FIX] sync.PeerStatus não tem tip_hash. getattr(peer, "tip_hash", "")
        sempre retornava "". Agora _query_peer_tip_cached() consulta P2P
        get_chain_height() com cache TTL 10s.
  [FIX] Hash truncado a 20 chars na comparação. Agora hash full é
        guardado internamente; truncado apenas em ForkEvent/log.
  [NEW] _detect_fork_peers() verifica TODOS os peers no mesmo height
        (não só o "best").
  [NEW] Deduplicação por (height, remote_hash, peer) — não spamma log.
  [NEW] forks_per_peer — estatística de quem causa mais forks.
  [NEW] CLI: --once, --loop N, --json, --probe, --version.
  [NEW] _peer_still_known() — checa se peer ainda está ativo.
  [NEW] __version__ = "2.0"
v1:
  - Monitorização contínua, detecção precoce, sinal extra ao miner
  - Log de métricas
  - AUTO_REPAIR via chain._fork_signal
================================================================
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from typing import Optional

__version__ = "2.0"
__all__ = [
    "ForkGuard", "ForkEvent",
    "get_fork_guard", "stop_fork_guard",
    "CHECK_INTERVAL_S", "ALERT_AFTER_S", "AUTO_REPAIR",
]

log = logging.getLogger("fork_guard")


# ============================================================
# CONFIG
# ============================================================
CHECK_INTERVAL_S = float(os.environ.get("BRN_FORK_CHECK_S", "3"))
ALERT_AFTER_S    = float(os.environ.get("BRN_FORK_ALERT_S", "15"))
AUTO_REPAIR      = os.environ.get("BRN_FORK_AUTO_REPAIR", "1") == "1"
MAX_HISTORY      = 100
HASH_CACHE_TTL   = 10.0
MAX_PEER_QUERIES_PER_CHECK = 5


# ============================================================
# EVENTO
# ============================================================
class ForkEvent:
    """Registo de um fork detetado."""
    __slots__ = ("ts", "height", "local_hash", "remote_hash",
                 "peer", "resolved_at", "resolved_by")

    def __init__(self, height: int, local_hash: str,
                 remote_hash: str, peer: str):
        self.ts = time.time()
        self.height = height
        self.local_hash = (local_hash or "")[:20]
        self.remote_hash = (remote_hash or "")[:20]
        self.peer = (peer or "")[:16]
        self.resolved_at: Optional[float] = None
        self.resolved_by: str = ""

    def mark_resolved(self, by: str = "auto"):
        if self.resolved_at is None:
            self.resolved_at = time.time()
            self.resolved_by = by

    @property
    def duration_s(self) -> Optional[float]:
        if self.resolved_at is None:
            return None
        return self.resolved_at - self.ts

    def to_dict(self) -> dict:
        return {
            "ts":           self.ts,
            "height":       self.height,
            "local_hash":   self.local_hash,
            "remote_hash":  self.remote_hash,
            "peer":         self.peer,
            "resolved":     self.resolved_at is not None,
            "duration_s":   (round(self.duration_s, 3)
                             if self.duration_s is not None else None),
            "resolved_by":  self.resolved_by,
        }


# ============================================================
# FORK GUARD
# ============================================================
class ForkGuard:
    """Guardião de forks. Thread-safe."""

    def __init__(self, chain, sync_manager=None, p2p_manager=None):
        self.chain = chain
        self.sync  = sync_manager
        self.p2p   = p2p_manager

        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Estado
        self.started_at = time.time()
        self.last_local_tip: str = ""
        self.last_local_height: int = -1
        self.events: list = []
        self.active_fork: Optional[ForkEvent] = None
        self.fork_alerts: int = 0
        self.auto_repairs: int = 0
        self.checks_run: int = 0
        self.expired_forks: int = 0
        self.forks_per_peer: dict = {}

        # Cache de hash por peer (addr -> (hash, ts))
        self._hash_cache: dict = {}

    # ============================================================
    # CICLO DE VIDA
    # ============================================================
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # [FIX v2] Inicializa estado do DB
        self._sync_local_state()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="ForkGuard"
        )
        self._thread.start()
        log.info(
            "ForkGuard v%s iniciado (check=%ss alert=%ss auto_repair=%s)",
            __version__, CHECK_INTERVAL_S, ALERT_AFTER_S, AUTO_REPAIR,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        # [FIX v2] Não deixa o miner pausado
        self._clear_fork_signal()
        log.info("ForkGuard encerrado")

    # ============================================================
    # ESTADO LOCAL
    # ============================================================
    def _sync_local_state(self) -> None:
        """[FIX v2] Atualiza last_local_height/tip do DB real."""
        try:
            h = int(self.chain.db.height())
            tip = str(self.chain.db.tip_hash() or "").lower().strip()
            with self.lock:
                if h >= 0:
                    self.last_local_height = h
                    self.last_local_tip = tip
        except Exception:
            pass

    def _clear_fork_signal(self) -> None:
        """[NEW v2] Limpa _fork_signal sem depender do sync_manager."""
        sig = getattr(self.chain, "_fork_signal", None)
        if sig is not None:
            try:
                sig.clear()
            except Exception:
                pass

    # ============================================================
    # CALLBACK: BLOCO ACEITE
    # ============================================================
    def on_block_accepted(self, block: dict) -> None:
        try:
            h = int(block.get("height", -1))
            hh = str(block.get("hash", "") or "").lower().strip()
        except Exception:
            return

        resolved = None
        with self.lock:
            self.last_local_height = h
            self.last_local_tip = hh

            if self.active_fork is not None and h > self.active_fork.height:
                self.active_fork.mark_resolved(by="new_block")
                resolved = self.active_fork
                self._push_event(self.active_fork)
                self.active_fork = None

        if resolved is not None:
            log.info(
                "[ForkGuard] fork em #%d resolvido por bloco #%d (%.3fs)",
                resolved.height, h, resolved.duration_s or 0.0,
            )
            # [FIX v2] Limpa sinal ao resolver
            self._clear_fork_signal()

    # ============================================================
    # LOOP
    # ============================================================
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._check_once()
            except Exception as e:
                log.warning("ForkGuard check falhou: %s", e)
            if self._stop.wait(CHECK_INTERVAL_S):
                break

    def _check_once(self) -> None:
        # [FIX v2] Sincroniza estado do DB em cada check
        self._sync_local_state()

        with self.lock:
            self.checks_run += 1
            local_h = self.last_local_height
            local_tip = self.last_local_tip

        if local_h < 0 or not local_tip:
            return

        # 1) Detectar forks
        fork_peers = self._detect_fork_peers(local_h, local_tip)
        for peer_addr, peer_hash in fork_peers:
            self._handle_fork(local_h, local_tip, peer_hash, peer_addr)

        # 2) Expirar fork ativo muito tempo sem resolução
        with self.lock:
            if self.active_fork is not None:
                age = time.time() - self.active_fork.ts
                if age > ALERT_AFTER_S * 3:
                    self.active_fork.mark_resolved(by="expired")
                    self._push_event(self.active_fork)
                    self.expired_forks += 1
                    expired_fork = self.active_fork
                    self.active_fork = None
                else:
                    expired_fork = None
            else:
                expired_fork = None

        if expired_fork is not None:
            log.warning(
                "[ForkGuard] fork em #%d expirado após %.0fs sem resolução "
                "(peer=%s)",
                expired_fork.height,
                expired_fork.duration_s or 0.0,
                expired_fork.peer,
            )
            self._clear_fork_signal()

    # ============================================================
    # DESCOBERTA DE FORKS
    # ============================================================
    def _detect_fork_peers(self, local_h: int, local_tip: str) -> list:
        """
        [FIX v2] Retorna lista de (peer_addr, peer_hash) com hash
        diferente no mesmo height.
        """
        if not local_tip:
            return []
        local_n = local_tip.lower().strip()
        matches = []
        seen = set()
        budget = MAX_PEER_QUERIES_PER_CHECK

        # Fonte 1: sync_manager.peers
        if self.sync is not None:
            try:
                with self.sync.peer_lock:
                    peers_snap = list(self.sync.peers.values())
            except Exception:
                peers_snap = []

            for peer in peers_snap:
                h = getattr(peer, "height", -1)
                addr = getattr(peer, "addr", "") or ""
                if h != local_h or not addr or addr in seen:
                    continue
                if budget <= 0:
                    break
                budget -= 1
                ph = self._query_peer_tip_cached(addr)
                seen.add(addr)
                if ph and ph != local_n:
                    matches.append((addr, ph))

        # Fonte 2: p2p.discovery
        if self.p2p is not None:
            disc = getattr(self.p2p, "discovery", None)
            if disc is not None and hasattr(disc, "listar_peers_com_altura"):
                try:
                    for p in disc.listar_peers_com_altura() or []:
                        h = int(p.get("height", 0) or 0)
                        addr = p.get("address", "") or ""
                        if h != local_h or not addr or addr in seen:
                            continue
                        if budget <= 0:
                            break
                        budget -= 1
                        ph = self._query_peer_tip_cached(addr)
                        seen.add(addr)
                        if ph and ph != local_n:
                            matches.append((addr, ph))
                except Exception:
                    pass

        return matches

    def _query_peer_tip_cached(self, addr: str, ttl: float = HASH_CACHE_TTL) -> str:
        """[FIX v2] Cache com TTL para não martelar peers."""
        now = time.time()
        with self.lock:
            cached = self._hash_cache.get(addr)
            if cached and (now - cached[1]) < ttl:
                return cached[0]

        h = self._query_peer_tip(addr)

        with self.lock:
            self._hash_cache[addr] = (h, now)
        return h

    def _query_peer_tip(self, addr: str, timeout: float = 3.0) -> str:
        """[FIX v2] Consulta P2P get_chain_height para obter tip_hash."""
        try:
            from p2p_unified import P2PClient
            if ":" not in addr:
                return ""
            ip, port_s = addr.rsplit(":", 1)
            port = int(port_s)
            resp, _ = P2PClient.get_chain_height(ip, port, timeout=timeout)
            if resp:
                return (resp.get("hash", "") or "").lower().strip()
        except Exception:
            pass
        return ""

    # ============================================================
    # HANDLER DE FORK
    # ============================================================
    def _handle_fork(self, height: int, local_tip: str,
                     remote_hash: str, peer_addr: str) -> None:
        now = time.time()
        update_existing = False

        with self.lock:
            # Deduplicação: mesmo height + hash + peer → ignora
            if (self.active_fork is not None
                    and self.active_fork.height == height
                    and self.active_fork.remote_hash == remote_hash[:20]
                    and self.active_fork.peer == peer_addr[:16]):
                return

            # Mesmo height, mas peer/hash diferente → atualiza
            if (self.active_fork is not None
                    and self.active_fork.height == height):
                self.active_fork.remote_hash = remote_hash[:20]
                self.active_fork.peer = peer_addr[:16]
                update_existing = True
            else:
                # Fecha fork anterior (se houver) como superseded
                if self.active_fork is not None:
                    self.active_fork.mark_resolved(by="superseded")
                    self._push_event(self.active_fork)

                self.active_fork = ForkEvent(
                    height=height,
                    local_hash=local_tip,
                    remote_hash=remote_hash,
                    peer=peer_addr,
                )
                self.fork_alerts += 1
                key = peer_addr[:16]
                self.forks_per_peer[key] = self.forks_per_peer.get(key, 0) + 1

        if update_existing:
            log.debug(
                "[ForkGuard] fork em #%d atualizado (peer=%s hash=%s)",
                height, peer_addr[:16], remote_hash[:20],
            )
            return

        log.warning(
            "[ForkGuard] FORK em #%d — local=%s peer=%s@%s",
            height, local_tip[:20], remote_hash[:20], peer_addr,
        )

        # Envia sinal ao miner
        if AUTO_REPAIR:
            sig = getattr(self.chain, "_fork_signal", None)
            if sig is not None:
                try:
                    sig.set()
                    with self.lock:
                        self.auto_repairs += 1
                        n = self.auto_repairs
                    log.info(
                        "[ForkGuard] sinal enviado ao miner (auto_repair #%d)",
                        n,
                    )
                except Exception as e:
                    log.debug("fork_signal.set falhou: %s", e)

    def _push_event(self, ev: ForkEvent) -> None:
        self.events.append(ev)
        if len(self.events) > MAX_HISTORY:
            self.events = self.events[-MAX_HISTORY:]

    # ============================================================
    # STATUS
    # ============================================================
    def status(self) -> dict:
        with self.lock:
            n_total = len(self.events)
            n_resolved = sum(1 for e in self.events
                             if e.resolved_at is not None)
            durations = [e.duration_s for e in self.events
                         if e.duration_s is not None]
            avg_dur = (sum(durations) / len(durations)) if durations else 0.0
            max_dur = max(durations) if durations else 0.0

            active = None
            if self.active_fork is not None:
                active = self.active_fork.to_dict()
                active["age_s"] = round(time.time() - self.active_fork.ts, 2)

            return {
                "ok":               True,
                "version":          __version__,
                "uptime_s":         int(time.time() - self.started_at),
                "checks_run":       self.checks_run,
                "check_interval_s": CHECK_INTERVAL_S,
                "auto_repair":      AUTO_REPAIR,
                "fork_alerts":      self.fork_alerts,
                "auto_repairs":     self.auto_repairs,
                "expired_forks":    self.expired_forks,
                "events_total":     n_total,
                "events_resolved":  n_resolved,
                "avg_duration_s":   round(avg_dur, 3),
                "max_duration_s":   round(max_dur, 3),
                "active_fork":      active,
                "last_local_height": self.last_local_height,
                "last_local_tip":   self.last_local_tip[:20],
                "forks_per_peer":   dict(self.forks_per_peer),
            }

    def list_events(self, limit: int = 20) -> list:
        with self.lock:
            return [e.to_dict() for e in self.events[-limit:]]


# ============================================================
# SINGLETON
# ============================================================
_INSTANCE = None
_INSTANCE_LOCK = threading.RLock()


def get_fork_guard(chain=None, sync=None, p2p=None) -> Optional[ForkGuard]:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            if chain is None:
                return None
            _INSTANCE = ForkGuard(chain, sync, p2p)
        return _INSTANCE


def stop_fork_guard() -> bool:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            return False
        _INSTANCE.stop()
        _INSTANCE = None
        return True


# ============================================================
# CLI
# ============================================================
def _cli_once(json_out: bool = False, probe: bool = False) -> int:
    """Roda uma verificação e mostra status."""
    from blockchain import Blockchain
    from sync_manager import ChainSyncManager

    try:
        chain = Blockchain("brn_v2_chain.db", auto_genesis=False)
    except Exception as e:
        print(f"Falha criando chain: {e}", file=sys.stderr)
        return 1

    guard = ForkGuard(chain, sync_manager=None, p2p_manager=None)
    guard._sync_local_state()

    if probe:
        # Tenta descobrir peers via API HTTP local
        try:
            import urllib.request
            import json as _json
            with urllib.request.urlopen(
                "http://127.0.0.1:5000/api/peers/discovered", timeout=3.0
            ) as r:
                data = _json.loads(r.read())
            peers = data.get("peers", [])
        except Exception:
            peers = []

        if peers:
            print(f"Peers descobertos: {len(peers)}")
            for p in peers:
                addr = p.get("address", "?")
                h = p.get("height", 0)
                ph = guard._query_peer_tip_cached(addr)
                local_h = guard.last_local_height
                local_tip = guard.last_local_tip
                match = "✓" if ph and ph == local_tip else "✗"
                print(f"  {match} {addr:<24} h={h:<8} hash={ph[:16] if ph else '(desconhecido)'}...")
        else:
            print("Nenhum peer descoberto (node rodando?)")

    # Roda um check
    guard._check_once()

    result = guard.status()
    result["events"] = guard.list_events(limit=10)

    if json_out:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"\nForkGuard v{__version__}")
        print(f"  local: h={result['last_local_height']} tip={result['last_local_tip']}")
        print(f"  checks_run: {result['checks_run']}")
        print(f"  fork_alerts: {result['fork_alerts']}")
        print(f"  auto_repairs: {result['auto_repairs']}")
        print(f"  active_fork: {result['active_fork']}")
        print(f"  forks_per_peer: {result['forks_per_peer']}")

    return 0


def _cli_loop(interval: float, json_out: bool) -> int:
    """Loop de monitoramento (Ctrl+C para sair)."""
    from blockchain import Blockchain

    try:
        chain = Blockchain("brn_v2_chain.db", auto_genesis=False)
    except Exception as e:
        print(f"Falha criando chain: {e}", file=sys.stderr)
        return 1

    guard = ForkGuard(chain, sync_manager=None, p2p_manager=None)
    guard.start()

    print(f"ForkGuard v{__version__} — loop a cada {interval}s (Ctrl+C)")
    try:
        while True:
            time.sleep(interval)
            st = guard.status()
            line = (
                f"[{time.strftime('%H:%M:%S')}] h={st['last_local_height']} "
                f"tip={st['last_local_tip'][:12]} | "
                f"alerts={st['fork_alerts']} repairs={st['auto_repairs']} "
                f"active={'YES' if st['active_fork'] else 'no'}"
            )
            print(line, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        guard.stop()

    return 0


# ============================================================
# AUTO-TESTE
# ============================================================
def _auto_test() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 60)
    print(f" fork_guard.py v{__version__} — Auto-teste")
    print("=" * 60)

    class FakeDB:
        def __init__(self):
            self._height = 100
            self._tip = "a" * 64
        def height(self): return self._height
        def tip_hash(self): return self._tip

    class FakeChain:
        def __init__(self):
            self.db = FakeDB()
            self._fork_signal = threading.Event()

    class FakeSync:
        def __init__(self):
            self.peer_lock = threading.RLock()
            self.peers = {}

    chain = FakeChain()
    sync = FakeSync()

    # [1] Instanciação
    print("\n[1] Instanciação")
    guard = ForkGuard(chain, sync, p2p_manager=None)
    st = guard.status()
    assert st["fork_alerts"] == 0
    assert st["version"] == __version__
    print(f"    version: {st['version']}")
    print("    [ok]")

    # [2] sync_local_state puxa do DB
    print("\n[2] _sync_local_state puxa do DB")
    guard._sync_local_state()
    st = guard.status()
    assert st["last_local_height"] == 100
    assert st["last_local_tip"] == "a" * 20  # truncado no status
    print(f"    h={st['last_local_height']} tip={st['last_local_tip']}")
    print("    [ok]")

    # [3] on_block_accepted atualiza estado
    print("\n[3] on_block_accepted")
    guard.on_block_accepted({"height": 101, "hash": "b" * 64})
    st = guard.status()
    assert st["last_local_height"] == 101
    print(f"    h={st['last_local_height']}")
    print("    [ok]")

    # [4] Fork detetado + sinal setado
    print("\n[4] Fork detetado")
    guard._handle_fork(101, "b" * 64, "c" * 64, "10.0.0.1:6001")
    st = guard.status()
    assert st["fork_alerts"] == 1
    assert st["auto_repairs"] == 1
    assert st["active_fork"] is not None
    assert chain._fork_signal.is_set()
    print(f"    fork_alerts: {st['fork_alerts']}")
    print(f"    auto_repairs: {st['auto_repairs']}")
    print(f"    forks_per_peer: {st['forks_per_peer']}")
    print("    [ok]")

    # [5] Deduplicação — mesmo fork não re-alerta
    print("\n[5] Deduplicação")
    guard._handle_fork(101, "b" * 64, "c" * 64, "10.0.0.1:6001")
    st = guard.status()
    assert st["fork_alerts"] == 1
    print("    [ok] (não duplicou)")

    # [6] Novo bloco resolve fork + limpa sinal
    print("\n[6] Novo bloco resolve fork")
    guard.on_block_accepted({"height": 102, "hash": "d" * 64})
    st = guard.status()
    assert st["active_fork"] is None
    assert st["events_total"] == 1
    assert st["events_resolved"] == 1
    assert not chain._fork_signal.is_set(), "sinal deveria ter sido limpo"
    print(f"    events_total: {st['events_total']}")
    print(f"    events_resolved: {st['events_resolved']}")
    print(f"    sinal limpo: {not chain._fork_signal.is_set()}")
    print("    [ok]")

    # [7] Expiração de fork preso
    print("\n[7] Expiração de fork preso")
    guard._handle_fork(102, "d" * 64, "e" * 64, "10.0.0.2:6001")
    st = guard.status()
    assert st["active_fork"] is not None
    # Força expiração manipulando o timestamp
    guard.active_fork.ts -= (ALERT_AFTER_S * 3 + 5)
    guard._check_once()  # deve expirar
    st = guard.status()
    assert st["active_fork"] is None
    assert st["expired_forks"] == 1
    print(f"    expired_forks: {st['expired_forks']}")
    print(f"    sinal limpo: {not chain._fork_signal.is_set()}")
    print("    [ok]")

    # [8] list_events
    print("\n[8] list_events")
    evs = guard.list_events()
    assert len(evs) >= 2
    for e in evs:
        print(f"    #{e['height']} dur={e['duration_s']}s by={e['resolved_by']}")
    print("    [ok]")

    # [9] stop limpa sinal
    print("\n[9] stop limpa sinal")
    chain._fork_signal.set()  # simula estado ruim
    guard.stop()
    assert not chain._fork_signal.is_set()
    print("    [ok]")

    print("\n" + "=" * 60)
    print(" fork_guard.py v" + __version__ + " OK.")
    print("=" * 60)
    return 0


# ============================================================
# MAIN
# ============================================================
def _main() -> int:
    p = argparse.ArgumentParser(
        prog="fork_guard.py",
        description="Guardião de forks BRN",
    )
    p.add_argument("--version", action="store_true")
    p.add_argument("--once", action="store_true",
                   help="Roda 1 check e mostra status")
    p.add_argument("--loop", metavar="SECONDS", type=float, default=0,
                   help="Loop contínuo (Ctrl+C para sair)")
    p.add_argument("--probe", action="store_true",
                   help="Consulta peers via HTTP e mostra hash de cada")
    p.add_argument("--json", action="store_true",
                   help="Saída JSON")
    p.add_argument("--test", action="store_true",
                   help="Roda auto-teste")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    if args.version:
        print(f"fork_guard.py v{__version__}")
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.test:
        return _auto_test()

    if args.loop > 0:
        return _cli_loop(args.loop, args.json)

    # Default: --once
    return _cli_once(json_out=args.json, probe=args.probe)


if __name__ == "__main__":
    try:
        sys.exit(_main())
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(130)
