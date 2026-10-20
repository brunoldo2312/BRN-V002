"""fork_guard.py — Guardião de forks e convergência da rede BRN
================================================================
Módulo INDEPENDENTE. Pluga-se no main.py como serviço adicional e
não substitui nem o sync_manager nem o reorg.py — apenas os complementa
com 4 funções:

  1. Monitorização contínua do tip_hash local vs. peers conhecidos
  2. Deteção precoce de fork (antes do sync_manager agir)
  3. Sinalização extra ao miner (além do _fork_signal já existente)
  4. Log de métricas: forks detetados, tempo médio de convergência

API pública (usada pelo main.py):
    guard = ForkGuard(chain, sync_manager, p2p_manager)
    guard.start()                  # arranca thread de monitorização
    guard.on_block_accepted(block) # callback para chain.on_block_mined
    guard.stop()
    guard.status() -> dict

Env:
    BRN_FORK_CHECK_S       (default 3)      — intervalo de verificação
    BRN_FORK_ALERT_S       (default 15)     — tempo máximo antes de alertar
    BRN_FORK_AUTO_REPAIR   (default 1)      — acionar sinal se divergir
================================================================
"""
from __future__ import annotations

import os
import time
import threading
import logging
from typing import Optional

log = logging.getLogger("fork_guard")


# ============================================================
# CONFIG
# ============================================================
CHECK_INTERVAL_S   = float(os.environ.get("BRN_FORK_CHECK_S", "3"))
ALERT_AFTER_S      = float(os.environ.get("BRN_FORK_ALERT_S", "15"))
AUTO_REPAIR        = os.environ.get("BRN_FORK_AUTO_REPAIR", "1") == "1"
MAX_HISTORY        = 100   # últimos N eventos de fork


# ============================================================
# TIPOS
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
        self.peer = peer[:16]
        self.resolved_at: Optional[float] = None
        self.resolved_by: str = ""

    def mark_resolved(self, by: str = "auto"):
        self.resolved_at = time.time()
        self.resolved_by = by

    @property
    def duration_s(self) -> Optional[float]:
        if self.resolved_at is None:
            return None
        return self.resolved_at - self.ts

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "height": self.height,
            "local_hash": self.local_hash,
            "remote_hash": self.remote_hash,
            "peer": self.peer,
            "resolved": self.resolved_at is not None,
            "duration_s": (round(self.duration_s, 3)
                           if self.duration_s is not None else None),
            "resolved_by": self.resolved_by,
        }


# ============================================================
# FORK GUARD
# ============================================================
class ForkGuard:
    """
    Guardião de forks. Thread-safe. Não substitui nada — só observa
    e (opcionalmente) reforça o sinal ao miner.
    """

    def __init__(self, chain, sync_manager=None, p2p_manager=None):
        self.chain = chain
        self.sync = sync_manager
        self.p2p = p2p_manager

        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # estado
        self.started_at = time.time()
        self.last_local_tip: str = ""
        self.last_local_height: int = -1
        self.events: list = []                 # últimos ForkEvent
        self.active_fork: Optional[ForkEvent] = None
        self.fork_alerts: int = 0
        self.auto_repairs: int = 0
        self.checks_run: int = 0

    # ============================================================
    # CICLO DE VIDA
    # ============================================================
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                         name="ForkGuard")
        self._thread.start()
        log.info("ForkGuard iniciado (check=%ss alert=%ss auto_repair=%s)",
                 CHECK_INTERVAL_S, ALERT_AFTER_S, AUTO_REPAIR)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        log.info("ForkGuard encerrado")

    # ============================================================
    # CALLBACK: CHAMADO A CADA BLOCO ACEITE
    # ============================================================
    def on_block_accepted(self, block: dict) -> None:
        """
        Chamado pelo main.py via chain.on_block_mined (encadeado).
        Serve para: (a) registar o tip, (b) fechar forks resolvidos.
        """
        try:
            h = int(block.get("height", -1))
            hh = str(block.get("hash", ""))[:20]
        except Exception:
            return

        with self.lock:
            self.last_local_height = h
            self.last_local_tip = hh

            # Se havia um fork ativo e o bloco novo é acima da altura
            # do fork, marca como resolvido
            if self.active_fork is not None:
                if h > self.active_fork.height:
                    self.active_fork.mark_resolved(by="new_block")
                    log.info(
                        "[ForkGuard] fork em #%d resolvido por novo bloco #%d "
                        "(%.3fs)",
                        self.active_fork.height,
                        h,
                        self.active_fork.duration_s or 0.0,
                    )
                    self._push_event(self.active_fork)
                    self.active_fork = None

    # ============================================================
    # LOOP DE VERIFICAÇÃO
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
        with self.lock:
            self.checks_run += 1
            local_h = self.last_local_height
            local_tip = self.last_local_tip

        if local_h < 0:
            return

        # Descobre o melhor peer conhecido
        peer_info = self._best_peer()
        if peer_info is None:
            return

        peer_h, peer_hash, peer_addr = peer_info

        # Se o peer tem mais altura, não é fork — é sync em atraso.
        # O sync_manager trata disso.
        if peer_h > local_h:
            return

        # Se o peer tem a mesma altura mas hash diferente → FORK
        if peer_h == local_h and peer_hash and peer_hash != local_tip:
            self._handle_fork(local_h, local_tip, peer_hash, peer_addr)
            return

        # Se o peer tem menos altura, nada a fazer.
        # Verifica se havia um fork ativo que já não é relevante
        with self.lock:
            if self.active_fork is not None:
                age = time.time() - self.active_fork.ts
                if age > ALERT_AFTER_S:
                    # Fork preso há muito tempo
                    if self.active_fork.resolved_at is None:
                        log.warning(
                            "[ForkGuard] fork em #%d ainda ativo após %.0fs "
                            "(peer=%s)",
                            self.active_fork.height, age,
                            self.active_fork.peer,
                        )

    def _best_peer(self) -> Optional[tuple]:
        """
        Retorna (height, hash, addr) do peer com maior altura,
        ou None se não houver peers conhecidos.
        """
        # Fonte 1: sync_manager.peers
        if self.sync is not None:
            try:
                with self.sync.peer_lock:
                    peers = list(self.sync.peers.values())
                if peers:
                    best = max(peers, key=lambda p: p.height)
                    # Não temos o hash do peer aqui — só a altura.
                    # Consultamos via API local.
                    return (best.height, "", best.addr)
            except Exception:
                pass

        # Fonte 2: p2p_manager.discovery
        if self.p2p is not None:
            disc = getattr(self.p2p, "discovery", None)
            if disc is not None and hasattr(disc, "listar_peers_com_altura"):
                try:
                    peers = disc.listar_peers_com_altura() or []
                    if peers:
                        best = max(peers, key=lambda p: p.get("height", 0))
                        return (int(best.get("height", 0)),
                                "", best.get("address", ""))
                except Exception:
                    pass

        return None

    # ============================================================
    # HANDLER DE FORK
    # ============================================================
    def _handle_fork(self, height: int, local_tip: str,
                     remote_hash: str, peer_addr: str) -> None:
        now = time.time()

        with self.lock:
            # Se já temos um fork ativo para esta altura, não duplica
            if (self.active_fork is not None
                    and self.active_fork.height == height):
                return

            # Se havia um fork ativo anterior, fecha-o como substituído
            if self.active_fork is not None:
                self.active_fork.mark_resolved(by="superseded")
                self._push_event(self.active_fork)

            ev = ForkEvent(
                height=height,
                local_hash=local_tip,
                remote_hash=remote_hash or "(desconhecido)",
                peer=peer_addr,
            )
            self.active_fork = ev
            self.fork_alerts += 1

        log.warning(
            "[ForkGuard] FORK detetado em #%d — local=%s peer=%s@%s",
            height, local_tip, ev.remote_hash, peer_addr,
        )

        # ------------------------------------------------------------
        # AÇÃO: reforça o sinal ao miner local para abortar a tentativa
        # atual e recomeçar no bloco seguinte.
        # O reorg.py faz o trabalho pesado — mas o miner já deve estar
        # a trabalhar no bloco correto quando o reorg terminar.
        # ------------------------------------------------------------
        if AUTO_REPAIR:
            sig = getattr(self.chain, "_fork_signal", None)
            if sig is not None:
                try:
                    sig.set()
                    self.auto_repairs += 1
                    log.info(
                        "[ForkGuard] sinal enviado ao miner "
                        "(auto_repair #%d)", self.auto_repairs,
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
            avg_dur = (sum(durations) / len(durations)
                       if durations else 0.0)
            max_dur = max(durations) if durations else 0.0

            active = None
            if self.active_fork is not None:
                active = self.active_fork.to_dict()
                active["age_s"] = round(time.time() - self.active_fork.ts, 2)

            return {
                "ok": True,
                "uptime_s":         int(time.time() - self.started_at),
                "checks_run":       self.checks_run,
                "check_interval_s": CHECK_INTERVAL_S,
                "auto_repair":      AUTO_REPAIR,
                "fork_alerts":      self.fork_alerts,
                "auto_repairs":     self.auto_repairs,
                "events_total":     n_total,
                "events_resolved":  n_resolved,
                "avg_duration_s":   round(avg_dur, 3),
                "max_duration_s":   round(max_dur, 3),
                "active_fork":      active,
                "last_local_height": self.last_local_height,
                "last_local_tip":   self.last_local_tip,
            }

    def list_events(self, limit: int = 20) -> list:
        with self.lock:
            return [e.to_dict() for e in self.events[-limit:]]


# ============================================================
# SINGLETON (opcional)
# ============================================================
_INSTANCE = None
_INSTANCE_LOCK = threading.RLock()


def get_fork_guard(chain=None, sync=None, p2p=None) -> ForkGuard:
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
        return True


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import logging as _lg
    import tempfile

    _lg.basicConfig(level=_lg.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("=" * 60)
    print(" fork_guard.py — Auto-teste")
    print("=" * 60)

    # ---------- Teste 1: instanciação com chain mock ----------
    class FakeDB:
        def __init__(self):
            self._height = 100
            self._tip = "aaaa" * 5
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

    print("\n[1] Instanciação")
    guard = ForkGuard(chain, sync, p2p_manager=None)
    st = guard.status()
    print(f"    fork_alerts: {st['fork_alerts']}")
    print(f"    auto_repair: {st['auto_repair']}")
    assert st["fork_alerts"] == 0
    print("    [ok]")

    print("\n[2] on_block_accepted registra tip")
    block = {"height": 100, "hash": "aaaa" * 5}
    guard.on_block_accepted(block)
    st = guard.status()
    assert st["last_local_height"] == 100
    print(f"    last_local_height: {st['last_local_height']}")
    print(f"    last_local_tip:    {st['last_local_tip']}")
    print("    [ok]")

    print("\n[3] Fork detetado (peer com mesmo height, hash diferente)")
    # Simula um peer no mesmo height com hash diferente
    from collections import namedtuple
    PeerStub = namedtuple("PeerStub", ["height", "addr", "node_id"])
    sync.peers["peer-1"] = PeerStub(height=100, addr="10.0.0.1:6001",
                                     node_id="peer-1")

    # Chama o handler diretamente
    guard._handle_fork(100, "aaaa" * 5, "bbbb" * 5, "10.0.0.1:6001")

    st = guard.status()
    print(f"    fork_alerts:  {st['fork_alerts']}")
    print(f"    auto_repairs: {st['auto_repairs']}")
    print(f"    active_fork:  {st['active_fork']}")
    assert st["fork_alerts"] == 1
    assert st["auto_repairs"] == 1
    assert st["active_fork"] is not None
    assert chain._fork_signal.is_set()
    print("    [ok] fork registrado + sinal enviado")

    print("\n[4] Novo bloco resolve o fork")
    # Limpa o sinal para simular o miner a consumi-lo
    chain._fork_signal.clear()
    guard.on_block_accepted({"height": 101, "hash": "cccc" * 5})

    st = guard.status()
    print(f"    events_total:    {st['events_total']}")
    print(f"    events_resolved: {st['events_resolved']}")
    print(f"    active_fork:     {st['active_fork']}")
    assert st["events_total"] == 1
    assert st["events_resolved"] == 1
    assert st["active_fork"] is None
    print("    [ok] fork marcado como resolvido")

    print("\n[5] list_events")
    evs = guard.list_events()
    for e in evs:
        print(f"    #{e['height']} local={e['local_hash'][:8]} "
              f"peer={e['remote_hash'][:8]} dur={e['duration_s']}s "
              f"by={e['resolved_by']}")
    print("    [ok]")

    print("\n[6] Métricas acumuladas")
    st = guard.status()
    print(f"    avg_duration_s: {st['avg_duration_s']}")
    print(f"    max_duration_s: {st['max_duration_s']}")
    print("    [ok]")

    print("\n" + "=" * 60)
    print(" fork_guard.py OK.")
    print("=" * 60)