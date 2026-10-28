"""sync_manager.py — Sincronizador automático de blockchain (v2.1)
================================================================
Versão HTTP — funciona de QUALQUER lugar (LAN, Tailscale, internet).

Substitui a versão P2P anterior que dependia de multicast/UDP.
O P2P continua para transações, mas o sync da blockchain é feito
via HTTP (porta 5000), que é universal e à prova de firewall.

Como funciona:
  1. Lê peers de bootstrap_peers.json + BRN_BOOTSTRAP_PEERS
  2. GET /api/chain-info em cada peer para ver a altura
  3. Se peer tem mais blocos: GET /api/blocks?start=N&limit=100
  4. Valida cada bloco com chain.accept_block() (PoW, merkle, auth)
  5. Repete a cada SYNC_INTERVAL segundos

Compatível com fork_guard.py (PeerStatus tem .addr e .height).
v2.1 (2026-10-27):
  [FIX] Impede que o sync tente importar blocos fora de ordem
        (o que causava o erro "bloco #0 rejeitado: prev_hash incorreto"
        quando o banco local oscilava rapidamente).
================================================================
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

log = logging.getLogger("sync_manager")

BASE_DIR = Path(__file__).resolve().parent

# Configurações
SYNC_INTERVAL = float(os.environ.get("BRN_SYNC_INTERVAL", "15"))
SYNC_BATCH = int(os.environ.get("BRN_SYNC_BATCH", "100"))
PEER_HTTP_TIMEOUT = float(os.environ.get("BRN_SYNC_PEER_TIMEOUT", "10"))
HTTP_PORT = int(os.environ.get("BRN_PEER_HTTP_PORT", "5000"))
MAX_RETRIES = int(os.environ.get("BRN_SYNC_RETRIES", "3"))


# ============================================================
# PEER STATUS (compatível com fork_guard.py)
# ============================================================
class PeerStatus:
    """Estado observado de um peer. fork_guard usa .addr e .height."""

    def __init__(self, addr: str):
        self.addr = addr
        self.height = -1
        self.tip_hash = ""
        self.work = 0
        self.last_seen = 0.0
        self.last_error = ""
        self.failures = 0
        self.verified = False

    @property
    def alive(self) -> bool:
        return self.last_seen > 0 and (time.time() - self.last_seen) < 300


# ============================================================
# CHAIN SYNC MANAGER
# ============================================================
class ChainSyncManager:
    """Sincroniza a blockchain via HTTP. Funciona de qualquer lugar."""

    def __init__(self, chain, db, p2p=None):
        self.chain = chain
        self.db = db
        self.p2p = p2p

        # Interface exigida por fork_guard.py
        self.peer_lock = threading.RLock()
        self.peers: dict = {}

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
        self._current_target = 0

        if not _HAS_REQUESTS:
            log.error("requests nao instalado - sync HTTP indisponivel")

        log.info("ChainSyncManager v2.1 (HTTP) inicializado")

    # ============================================================
    # CICLO DE VIDA
    # ============================================================
    def start_sync_loop(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="ChainSync",
        )
        self._thread.start()
        log.info("ChainSync loop iniciado (HTTP, interval=%.0fs)", SYNC_INTERVAL)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        log.info("ChainSync parado")

    def set_sync_mode(self, mode: str) -> None:
        if mode not in ("full", "pull", "off"):
            log.warning("sync_mode invalido: %s", mode)
            return
        self._mode = mode
        log.info("sync_mode = %s", mode)

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
        if not _HAS_REQUESTS:
            return

        self._refresh_peer_list()

        best = self._probe_peers()
        if best is None:
            return

        local_h = self._safe_height()
        if best.height <= local_h:
            with self._state_lock:
                self._current_target = best.height
                self._active_peer = None
                self._last_error = ""
            return

        with self._state_lock:
            self._syncing = True
            self._active_peer = best.addr
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
        addrs = set()

        # 1. bootstrap_peers.json
        try:
            bp = BASE_DIR / "bootstrap_peers.json"
            if bp.exists():
                data = json.loads(bp.read_text(encoding="utf-8-sig"))
                if isinstance(data, list):
                    for x in data:
                        x = str(x).strip()
                        if x:
                            addrs.add(x)
                elif isinstance(data, dict):
                    for x in data.get("peers", []):
                        x = str(x).strip()
                        if x:
                            addrs.add(x)
        except Exception as e:
            log.debug("bootstrap_peers.json: %s", e)

        # 2. Env var
        for p in os.environ.get("BRN_BOOTSTRAP_PEERS", "").split(","):
            p = p.strip()
            if p:
                addrs.add(p)

        # 3. p2p.discovery (se disponível, como fallback)
        if self.p2p is not None:
            try:
                disc = getattr(self.p2p, "discovery", None)
                if disc and hasattr(disc, "listar_peers"):
                    for x in disc.listar_peers() or []:
                        x = str(x).strip()
                        if x:
                            addrs.add(x)
            except Exception:
                pass

        with self.peer_lock:
            for addr in addrs:
                if not addr or ":" not in addr:
                    continue
                if addr not in self.peers:
                    self.peers[addr] = PeerStatus(addr=addr)

    def _probe_peers(self) -> Optional[PeerStatus]:
        with self.peer_lock:
            addrs = list(self.peers.keys())

        best: Optional[PeerStatus] = None
        for addr in addrs:
            if ":" not in addr:
                continue
            ip = addr.rpartition(":")[0]

            try:
                r = requests.get(
                    f"http://{ip}:{HTTP_PORT}/api/chain-info",
                    timeout=PEER_HTTP_TIMEOUT,
                    proxies={"http": None, "https": None},
                    headers={"User-Agent": "brn-sync/2.1"},
                )
                if r.status_code != 200:
                    with self.peer_lock:
                        p = self.peers.get(addr)
                        if p:
                            p.failures += 1
                            p.last_error = f"HTTP {r.status_code}"
                    continue

                data = r.json()
                height = int(data.get("height", -1))
                tip = str(data.get("tip_hash", ""))
                work = int(data.get("cumulative_work", 0) or 0)

                with self.peer_lock:
                    p = self.peers.get(addr)
                    if p:
                        p.height = height
                        p.tip_hash = tip
                        p.work = work
                        p.last_seen = time.time()
                        p.failures = 0
                        p.last_error = ""

                if height >= 0:
                    if best is None or height > best.height:
                        best = self.peers.get(addr)
            except Exception as e:
                with self.peer_lock:
                    p = self.peers.get(addr)
                    if p:
                        p.failures += 1
                        p.last_error = str(e)[:80]
                continue

        return best

    # ============================================================
    # SINCRONIZAÇÃO
    # ============================================================
    def _sync_from_peer(self, peer: PeerStatus) -> None:
        ip = peer.addr.rpartition(":")[0]
        target_h = peer.height
        cursor = self._safe_height() + 1
        imported = 0
        t0 = time.time()

        if cursor > target_h:
            return

        log.info("sync %s: baixando %d-%d", peer.addr, cursor, target_h)

        while cursor <= target_h and not self._stop.is_set():
            end = min(cursor + SYNC_BATCH, target_h + 1)

            blocks = None
            for attempt in range(MAX_RETRIES):
                if self._stop.is_set():
                    return
                try:
                    r = requests.get(
                        f"http://{ip}:{HTTP_PORT}/api/blocks"
                        f"?start={cursor}&limit={SYNC_BATCH}",
                        timeout=30,
                        proxies={"http": None, "https": None},
                        headers={"User-Agent": "brn-sync/2.1"},
                    )
                    if r.status_code == 200:
                        blocks = r.json().get("blocks", [])
                        break
                    log.debug("batch %d-%d HTTP %d", cursor, end, r.status_code)
                except Exception as e:
                    log.debug("batch %d-%d tentativa %d: %s",
                              cursor, end, attempt + 1, e)
                time.sleep(1)

            if not blocks:
                log.warning("falha ao baixar batch %d-%d de %s",
                            cursor, end, peer.addr)
                with self._state_lock:
                    self._last_error = f"batch {cursor}-{end} falhou"
                return

            for blk in blocks:
                if self._stop.is_set():
                    return
                
                # [FIX] Valida se o bloco veio fora de ordem
                if blk.get("height") != cursor:
                    log.warning("sync: bloco fora de ordem (esperado #%d, recebido #%d)", cursor, blk.get("height"))
                    return  # Aborta esse sync. Na próxima rodada ele tenta de novo.
                
                try:
                    ok, msg = self.chain.accept_block(blk)
                except Exception as e:
                    ok, msg = False, str(e)
                if not ok:
                    log.warning("bloco #%s rejeitado: %s", blk.get("height"), msg)
                    with self._state_lock:
                        self._last_error = f"#{blk.get('height')}: {msg}"
                    return
                imported += 1
                cursor += 1

            # Log de progresso
            elapsed = time.time() - t0
            if elapsed > 0:
                rate = imported / elapsed
                pct = (cursor - 1) / target_h * 100 if target_h > 0 else 100
                if imported % 500 == 0 or cursor > target_h:
                    log.info("sync %s: %d/%d (%.1f%%) — %.0f bl/s",
                             peer.addr, cursor - 1, target_h, pct, rate)

        log.info("sync %s: +%d blocos (h=%d)", peer.addr, imported, self._safe_height())

    # ============================================================
    # HELPERS
    # ============================================================
    def _safe_height(self) -> int:
        try:
            return int(self.chain.db.height())
        except Exception:
            return -1

    def get_status(self) -> dict:
        local_h = self._safe_height()
        with self._state_lock:
            target_h = max(self._current_target, local_h)
            if target_h > 0:
                progress = min(100.0, (local_h / target_h) * 100.0)
            else:
                progress = 100.0
            synced = local_h >= target_h
            return {
                "synced": synced,
                "syncing": self._syncing,
                "forking": self._forking,
                "progress": round(progress, 2),
                "local_height": local_h,
                "target_height": target_h,
                "eta_segundos": None,
                "eta_legivel": "sincronizado" if synced else "sincronizando...",
                "active_peer": self._active_peer,
                "peers_known": len(self.peers),
                "reorgs_total": self._reorgs_total,
                "last_reorg_height": self._last_reorg_height,
                "last_error": self._last_error,
            }
