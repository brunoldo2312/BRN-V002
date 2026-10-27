"""sync_manager.py — ChainSyncManager do BRN (v1.0)
================================================================
Reconstruído a partir de main.py v13.7.1 + server.py v9.9.17 +
fork_guard.py v2.0, após detectar que o sync_manager.py no disco
foi SOBRESCRITO com o conteúdo de chain_validator.py v2.2.

Interface consumida por:
  - main.py:       ChainSyncManager(chain, db, p2p)
                   .set_sync_mode("full")
                   .start_sync_loop()
                   .stop()
  - server.py:     .get_status()  → dict com 13 chaves
  - fork_guard.py: .peer_lock (RLock)
                   .peers      (dict[addr → PeerStatus])

PeerStatus tem .addr, .height, .tip_hash, .last_seen, .work — todos
os campos que fork_guard.py usa via getattr().

Env:
  BRN_SYNC_INTERVAL       default 15s   intervalo entre ciclos
  BRN_SYNC_BATCH          default 100   blocos por requisição
  BRN_SYNC_PEER_TIMEOUT   default 10s   timeout por peer
  BRN_SYNC_REORG_DEPTH    default 200   profundidade máxima de reorg
  BRN_SYNC_PEER_STALE     default 300s  peer morto após
================================================================
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("sync_manager")


SYNC_INTERVAL   = float(os.environ.get("BRN_SYNC_INTERVAL", "15"))
SYNC_BATCH      = int(os.environ.get("BRN_SYNC_BATCH", "100"))
PEER_TIMEOUT    = float(os.environ.get("BRN_SYNC_PEER_TIMEOUT", "10"))
REORG_MAX_DEPTH = int(os.environ.get("BRN_SYNC_REORG_DEPTH", "200"))
PEER_STALE_S    = float(os.environ.get("BRN_SYNC_PEER_STALE", "300"))

MAX_REORGS_PER_SYNC = 3


# ============================================================
# PEER STATUS (interface para fork_guard)
# ============================================================
@dataclass
class PeerStatus:
    """Estado observado de um peer. Atualizado pelo loop de sync."""
    addr: str
    height: int = -1
    tip_hash: str = ""
    work: int = 0
    last_seen: float = 0.0
    last_error: str = ""
    failures: int = 0
    verified: bool = False

    @property
    def alive(self) -> bool:
        return self.last_seen > 0 and (time.time() - self.last_seen) < PEER_STALE_S

    def update_from_chain_height(self, resp: dict) -> None:
        self.height = int(resp.get("height", -1))
        self.tip_hash = str(resp.get("hash", ""))
        self.work = int(resp.get("work", 0))
        self.last_seen = time.time()
        self.failures = 0
        self.last_error = ""


# ============================================================
# CHAIN SYNC MANAGER
# ============================================================
class ChainSyncManager:
    """Sincroniza a blockchain com peers via P2P, com reorg automático."""

    def __init__(self, chain, db, p2p):
        if chain is None or db is None or p2p is None:
            raise ValueError("chain, db e p2p sao obrigatorios")

        self.chain = chain
        self.db = db
        self.p2p = p2p

        # Interface exigida por fork_guard.py
        self.peer_lock = threading.RLock()
        self.peers: dict[str, PeerStatus] = {}

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._mode = "full"

        self._state_lock = threading.RLock()
        self._syncing = False
        self._forking = False
        self._active_peer: Optional[str] = None
        self._reorgs_total = 0
        self._last_reorg_height = -1
        self._last_error = ""
        self._current_progress = 100.0
        self._current_local = 0
        self._current_target = 0

        log.info("ChainSyncManager inicializado (modo=%s)", self._mode)

    # ============================================================
    # CICLO DE VIDA
    # ============================================================
    def set_sync_mode(self, mode: str) -> None:
        if mode not in ("full", "pull", "off"):
            raise ValueError(f"modo invalido: {mode}")
        self._mode = mode
        log.info("sync_mode = %s", mode)

    def start_sync_loop(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="ChainSync",
        )
        self._thread.start()
        log.info("ChainSync loop iniciado (interval=%.1fs)", SYNC_INTERVAL)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        log.info("ChainSync parado")

    # ============================================================
    # STATUS (formato exigido por server.py)
    # ============================================================
    def get_status(self) -> dict:
        local_h = self._safe_height()
        with self._state_lock:
            syncing = self._syncing
            forking = self._forking
            target_h = max(self._current_target, local_h)
            if target_h > 0:
                progress = min(100.0, (local_h / target_h) * 100.0)
            else:
                progress = 100.0
            synced = local_h >= target_h

            active_peer = self._active_peer
            reorgs = self._reorgs_total
            last_reorg = self._last_reorg_height
            last_error = self._last_error

        with self.peer_lock:
            peers_known = sum(1 for p in self.peers.values() if p.alive)

        eta_s = None
        eta_str = "sincronizado"
        if not synced and syncing:
            eta_str = "calculando..."

        return {
            "synced": synced,
            "syncing": syncing,
            "forking": forking,
            "progress": round(progress, 2),
            "local_height": local_h,
            "target_height": target_h,
            "eta_segundos": eta_s,
            "eta_legivel": eta_str,
            "active_peer": active_peer,
            "peers_known": peers_known,
            "reorgs_total": reorgs,
            "last_reorg_height": last_reorg,
            "last_error": last_error,
        }

    # ============================================================
    # LOOP PRINCIPAL
    # ============================================================
    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._mode == "off":
                if self._stop.wait(SYNC_INTERVAL):
                    break
                continue
            try:
                self._sync_once()
            except Exception as e:
                log.exception("sync_once falhou: %s", e)
                with self._state_lock:
                    self._last_error = str(e)
            if self._stop.wait(SYNC_INTERVAL):
                break

    def _sync_once(self) -> None:
        self._refresh_peer_list()

        best = self._probe_peers()
        if best is None:
            with self._state_lock:
                self._current_target = self._safe_height()
                self._current_progress = 100.0
                self._syncing = False
            return

        local_h = self._safe_height()
        if best.height <= local_h:
            with self._state_lock:
                self._current_target = best.height
                self._current_progress = 100.0
                self._syncing = False
                self._active_peer = None
            return

        with self._state_lock:
            self._syncing = True
            self._active_peer = best.addr
            self._current_local = local_h
            self._current_target = best.height

        try:
            self._sync_from_peer(best)
        finally:
            with self._state_lock:
                self._syncing = False

    # ============================================================
    # PEERS
    # ============================================================
    def _refresh_peer_list(self) -> None:
        try:
            addrs = list(self.p2p.discovery.listar_peers() or [])
        except Exception as e:
            log.debug("listar_peers falhou: %s", e)
            return

        now = time.time()
        with self.peer_lock:
            for addr in addrs:
                if not addr or ":" not in addr:
                    continue
                if addr not in self.peers:
                    self.peers[addr] = PeerStatus(addr=addr)
            dead = [
                a for a, p in self.peers.items()
                if p.last_seen > 0 and (now - p.last_seen) > PEER_STALE_S * 2
            ]
            for a in dead:
                del self.peers[a]

    def _probe_peers(self) -> Optional[PeerStatus]:
        from p2p_unified import P2PClient

        with self.peer_lock:
            addrs = list(self.peers.keys())

        best: Optional[PeerStatus] = None
        for addr in addrs:
            if ":" not in addr:
                continue
            try:
                ip, port_s = addr.rsplit(":", 1)
                port = int(port_s)
            except ValueError:
                continue

            try:
                resp, _ = P2PClient.get_chain_height(
                    ip, port, timeout=PEER_TIMEOUT,
                )
            except Exception as e:
                with self.peer_lock:
                    p = self.peers.get(addr)
                    if p:
                        p.failures += 1
                        p.last_error = str(e)
                continue

            if not resp:
                with self.peer_lock:
                    p = self.peers.get(addr)
                    if p:
                        p.failures += 1
                continue

            with self.peer_lock:
                p = self.peers.get(addr)
                if p is None:
                    continue
                p.update_from_chain_height(resp)
                if best is None or p.height > best.height:
                    best = p

        return best

    # ============================================================
    # SINCRONIZAÇÃO
    # ============================================================
    def _sync_from_peer(self, peer: PeerStatus) -> None:
        from p2p_unified import P2PClient

        try:
            ip, port_s = peer.addr.rsplit(":", 1)
            port = int(port_s)
        except ValueError:
            return

        target_h = peer.height
        applied = 0
        reorg_count = 0
        t0 = time.time()

        cursor = self._safe_height() + 1
        while cursor <= target_h and not self._stop.is_set():
            end = min(cursor + SYNC_BATCH, target_h + 1)
            try:
                resp, _ = P2PClient.get_blocks_range(
                    ip, port, cursor, end,
                    timeout=PEER_TIMEOUT * 2,
                )
            except Exception as e:
                with self._state_lock:
                    self._last_error = f"get_blocks_range: {e}"
                return

            if not resp or "blocks" not in resp:
                with self._state_lock:
                    self._last_error = "resposta invalida (sem blocks)"
                return

            lote = resp.get("blocks") or []
            if not lote:
                break

            reorg_triggered = False
            for blk in lote:
                if self._stop.is_set():
                    return
                h = blk.get("height", -1)
                current_h = self._safe_height()
                if h <= current_h:
                    continue  # já temos
                if h != current_h + 1:
                    if self._mode == "full" and self._try_reorg(ip, port, peer):
                        reorg_triggered = True
                        break
                    with self._state_lock:
                        self._last_error = (
                            f"bloco fora de ordem: recebido #{h}, "
                            f"esperado #{current_h + 1}"
                        )
                    return

                try:
                    ok, msg = self.chain.accept_block(blk)
                except Exception as e:
                    ok, msg = False, str(e)

                if not ok:
                    if self._mode == "full" and self._try_reorg(ip, port, peer):
                        reorg_triggered = True
                        break
                    with self._state_lock:
                        self._last_error = f"#{h}: {msg}"
                    return

                applied += 1

            if reorg_triggered:
                reorg_count += 1
                if reorg_count > MAX_REORGS_PER_SYNC:
                    with self._state_lock:
                        self._last_error = (
                            f"mais de {MAX_REORGS_PER_SYNC} reorgs no mesmo sync"
                        )
                    return
                cursor = self._safe_height() + 1
            else:
                cursor = end

            with self._state_lock:
                self._current_local = self._safe_height()
                if target_h > 0:
                    self._current_progress = min(
                        100.0,
                        (self._current_local / target_h) * 100.0,
                    )

        with self._state_lock:
            self._current_progress = 100.0
            self._current_local = self._safe_height()
            self._current_target = target_h
            self._active_peer = None

        if applied > 0:
            log.info(
                "sync OK: +%d blocos em %.1fs (h=%d, peer=%s)",
                applied, time.time() - t0,
                self._safe_height(), peer.addr,
            )

    # ============================================================
    # REORG
    # ============================================================
    def _try_reorg(self, ip: str, port: int, peer: PeerStatus) -> bool:
        if self._mode != "full":
            return False

        try:
            from reorg import reorg_to
            from p2p_unified import P2PClient
        except ImportError:
            return False

        with self._state_lock:
            self._forking = True
        log.warning("tentando reorg com %s", peer.addr)

        try:
            local_h = self._safe_height()
            # Baixa no máximo REORG_MAX_DEPTH*2 + 50 blocos para trás
            start = max(0, local_h - REORG_MAX_DEPTH * 2 - 50)
            resp, _ = P2PClient.get_blocks_range(
                ip, port, start, peer.height + 1,
                timeout=PEER_TIMEOUT * 3,
            )
            if not resp or "blocks" not in resp:
                return False

            result = reorg_to(
                self.chain, resp["blocks"],
                max_depth=REORG_MAX_DEPTH,
            )
            ok, msg = result.to_tuple()
            if ok:
                with self._state_lock:
                    self._reorgs_total += 1
                    self._last_reorg_height = int(
                        getattr(result, "fork_height", -1)
                    )
                log.warning("reorg OK: %s", msg)
                return True

            log.warning("reorg falhou: %s", msg)
            return False
        except Exception as e:
            log.exception("reorg exception: %s", e)
            return False
        finally:
            with self._state_lock:
                self._forking = False

    # ============================================================
    # HELPERS
    # ============================================================
    def _safe_height(self) -> int:
        try:
            return int(self.chain.db.height())
        except Exception:
            return -1
