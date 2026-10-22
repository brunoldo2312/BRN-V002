"""
sync_manager.py — Sincronização + Reorg automático (v8.4.2)
================================================================
v8.4.2 (2026-10-22):
  [NEW] Imutabilidade por checkpoint. Reorg que tenta reverter
        bloco <= último checkpoint é BLOQUEADO.
  [FIX] _detect_fork retorna None se DB vazio (local_h < 1).
        Evita loop "sem ancestral comum" em primeiro sync.
v8.4.1:
  [FIX] _fetch_chain_from aceita lotes menores que o pedido.
        Antes, pedia 200, recebia 50, e falhava com "download falhou".
================================================================
"""
from __future__ import annotations

import json
import os
import socket
import time
import threading
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("sync")

NETWORK_MAGIC      = b"BRN5"
MAX_MSG_SIZE       = 8 * 1024 * 1024
DEFAULT_TIMEOUT    = 10.0
DEFAULT_BATCH_SIZE = int(os.environ.get("BRN_SYNC_BATCH", "50"))
DEFAULT_INTERVAL_S = float(os.environ.get("BRN_SYNC_INTERVAL", "5"))
BATCH_DELAY_S      = float(os.environ.get("BRN_SYNC_BATCH_DELAY", "0.06"))

REORG_LOOKBACK       = int(os.environ.get("BRN_REORG_LOOKBACK", "500"))
REORG_WORK_THRESHOLD = float(os.environ.get("BRN_REORG_WORK_THRESHOLD", "1.0"))
REORG_FETCH_BATCH    = int(os.environ.get("BRN_REORG_FETCH_BATCH", "200"))
REORG_MAX_FAILS      = int(os.environ.get("BRN_REORG_MAX_FAILS", "3"))
REORG_COOLDOWN_S     = float(os.environ.get("BRN_REORG_COOLDOWN_S", "60"))

try:
    from blockchain import block_hash as _block_hash_fn, meets_difficulty as _meets_diff_fn
    _HAS_HELPERS = True
except ImportError:
    _block_hash_fn = None
    _meets_diff_fn = None
    _HAS_HELPERS = False
    log.warning("[sync] blockchain helpers ausentes")


@dataclass
class PeerStatus:
    node_id: str
    ip: str
    port: int
    height: int = 0
    cumulative_work: int = 0
    last_seen: float = field(default_factory=time.time)
    latency_ms: float = 0.0

    @property
    def addr(self) -> str:
        return f"{self.ip}:{self.port}"


@dataclass
class SyncStatus:
    mode: str = "full"
    local_height: int = 0
    target_height: int = 0
    synced: bool = False
    syncing: bool = False
    progress: float = 0.0
    active_peer: Optional[str] = None
    peers_known: int = 0
    peers_synced: int = 0
    last_error: str = ""
    forking: bool = False
    reorgs_total: int = 0
    last_reorg_height: int = -1


class ChainSyncManager:

    def __init__(self, blockchain, db, p2p_manager,
                 send_fn: Optional[Callable[[str, int, dict, float],
                                             Optional[dict]]] = None):
        self.chain = blockchain
        self.db = db
        self.p2p = p2p_manager
        self._send_fn = send_fn

        self.status = SyncStatus()
        self.peers: Dict[str, PeerStatus] = {}
        self.peer_lock = threading.Lock()
        self.sync_lock = threading.Lock()

        self.batch_size = DEFAULT_BATCH_SIZE
        self.spv_headers_only = False

        self._stop_sync = threading.Event()
        self.sync_thread: Optional[threading.Thread] = None

        self._has_save_header = hasattr(self.db, "save_header")

        existing_sig = getattr(blockchain, "_fork_signal", None)
        if existing_sig is None:
            existing_sig = threading.Event()
            try:
                blockchain._fork_signal = existing_sig
            except Exception:
                pass
        self._fork_signal = existing_sig

        self._reorg_fails: Dict[str, dict] = {}
        self._reorg_fails_lock = threading.Lock()

    def set_sync_mode(self, mode: str = "full") -> None:
        if mode not in ("full", "spv"):
            raise ValueError("Modo inválido: use 'full' ou 'spv'")
        if mode == "spv" and not self._has_save_header:
            log.warning("Modo SPV sem db.save_header — caindo para FULL")
            mode = "full"
        self.spv_headers_only = (mode == "spv")
        self.status.mode = mode

    def register_peer(self, node_id: str, ip: str, port: int,
                      height: int = 0, cumulative_work: int = 0) -> None:
        if not node_id:
            return
        with self.peer_lock:
            existing = self.peers.get(node_id)
            if existing:
                existing.height = height
                existing.cumulative_work = cumulative_work
                existing.last_seen = time.time()
                existing.ip = ip
                existing.port = port
            else:
                self.peers[node_id] = PeerStatus(
                    node_id=node_id, ip=ip, port=port,
                    height=height, cumulative_work=cumulative_work,
                )
            self.status.peers_known = len(self.peers)

    def unregister_peer(self, node_id: str) -> None:
        with self.peer_lock:
            self.peers.pop(node_id, None)
            self.status.peers_known = len(self.peers)
        with self._reorg_fails_lock:
            self._reorg_fails.pop(node_id, None)

    def fork_in_progress(self) -> bool:
        return self._fork_signal.is_set()

    def _safe_height(self) -> int:
        try:
            h = int(self.chain.db.height())
        except Exception:
            return -1
        return max(-1, h)

    def _local_work(self) -> int:
        try:
            return int(self.chain.cumulative_work())
        except Exception:
            return 0

    def _validate_chain_segment(
        self, blocks: List[Dict], start_height: int, expected_prev_hash: str,
    ) -> Tuple[bool, str]:
        if not blocks:
            return False, "lista de blocos vazia"

        if not _HAS_HELPERS:
            expected_h = start_height
            prev = expected_prev_hash
            for blk in blocks:
                if not isinstance(blk, dict):
                    return False, f"bloco em #{expected_h} não é dict"
                if int(blk.get("height", -1)) != expected_h:
                    return False, f"height {blk.get('height')} != esperado {expected_h}"
                if blk.get("prev_hash") != prev:
                    return False, f"prev_hash diverge em #{expected_h}"
                prev = blk.get("hash", "")
                expected_h += 1
            return True, ""

        expected_h = start_height
        prev = expected_prev_hash
        for blk in blocks:
            if not isinstance(blk, dict):
                return False, f"bloco em #{expected_h} não é dict"

            h = int(blk.get("height", -1))
            if h != expected_h:
                return False, f"height {h} != esperado {expected_h}"

            if blk.get("prev_hash") != prev:
                return False, f"prev_hash diverge em #{expected_h}"

            try:
                h_calc = _block_hash_fn(
                    blk["prev_hash"], blk["merkle"], blk["timestamp"],
                    blk["nonce"], blk["difficulty"],
                )
            except KeyError as e:
                return False, f"campo ausente em #{expected_h}: {e}"
            except Exception as e:
                return False, f"block_hash falhou em #{expected_h}: {e}"

            if h_calc != blk.get("hash"):
                return False, f"hash inválido em #{expected_h}"

            try:
                if not _meets_diff_fn(blk["hash"], blk["difficulty"]):
                    return False, f"PoW insuficiente em #{expected_h}"
            except Exception as e:
                return False, f"meets_difficulty falhou em #{expected_h}: {e}"

            prev = blk["hash"]
            expected_h += 1

        return True, ""

    def _peer_is_blocked(self, peer_id: str) -> bool:
        with self._reorg_fails_lock:
            entry = self._reorg_fails.get(peer_id)
            if not entry:
                return False
            return entry.get("blocked_until", 0) > time.time()

    def _peer_record_fail(self, peer_id: str) -> None:
        now = time.time()
        with self._reorg_fails_lock:
            entry = self._reorg_fails.setdefault(peer_id, {
                "fails": 0, "blocked_until": 0, "first_fail": now
            })
            entry["fails"] += 1
            entry["last_fail"] = now
            if entry["fails"] >= REORG_MAX_FAILS:
                entry["blocked_until"] = now + REORG_COOLDOWN_S
                log.warning(
                    f"[REORG] peer {peer_id[:12]} com {entry['fails']} "
                    f"falhas seguidas — cooldown por {REORG_COOLDOWN_S:.0f}s"
                )

    def _peer_record_success(self, peer_id: str) -> None:
        with self._reorg_fails_lock:
            self._reorg_fails.pop(peer_id, None)

    def _detect_fork(self) -> Optional[PeerStatus]:
        local_work = self._local_work()
        local_h = self._safe_height()

        if local_h < 1:
            return None

        best = None
        best_work = local_work

        with self.peer_lock:
            snapshot = list(self.peers.values())

        for p in snapshot:
            if p.height <= local_h:
                continue
            if p.cumulative_work == 0 and local_work > 0:
                continue
            if self._peer_is_blocked(p.node_id):
                continue
            threshold_factor = 1.0 + (REORG_WORK_THRESHOLD / 100.0)
            if p.cumulative_work > best_work * threshold_factor:
                best = p
                best_work = p.cumulative_work

        if best is not None:
            log.warning(
                f"[REORG] detectado: peer {best.node_id[:12]} "
                f"h={best.height} work={best.cumulative_work} | "
                f"local h={local_h} work={local_work}"
            )
        return best

    def _fetch_chain_from(self, peer: PeerStatus, start: int, end: int,
                          strict: bool = True) -> Optional[List[Dict]]:
        all_blocks = []
        cursor = start
        empties = 0

        while cursor < end:
            batch_end = min(cursor + REORG_FETCH_BATCH, end)
            try:
                resp = self._do_send(
                    peer.ip, peer.port,
                    {"type": "get_blocks_range",
                     "start": cursor, "end": batch_end},
                    DEFAULT_TIMEOUT * 2,
                )
            except Exception as e:
                log.warning(f"_fetch_chain_from({cursor}-{batch_end}): {e}")
                return None if strict else all_blocks

            if not resp or "blocks" not in resp:
                return None if strict else all_blocks

            batch = resp["blocks"]
            if not isinstance(batch, list):
                return None if strict else all_blocks
            if not batch:
                empties += 1
                if empties >= 2:
                    break
                continue

            all_blocks.extend(batch)
            cursor += len(batch)
            empties = 0

        return all_blocks

    def _resolve_fork(self, peer: PeerStatus) -> bool:
        peer_id = peer.node_id
        if self._peer_is_blocked(peer_id):
            return False

        with self.sync_lock:
            local_h = self._safe_height()
            if peer.height <= local_h:
                log.debug(
                    f"[REORG] ignorando — peer_h={peer.height} <= "
                    f"local_h={local_h}"
                )
                return False

            log.warning(
                f"[REORG] resolvendo fork com {peer_id[:12]} "
                f"(peer_h={peer.height} work={peer.cumulative_work}, "
                f"local_h={local_h} work={self._local_work()})"
            )

            self.status.forking = True
            self._fork_signal.set()

            try:
                target_h = peer.height
                start = max(0, local_h - REORG_LOOKBACK)
                end = target_h + 1

                log.info(f"[REORG] baixando #{start}...#{target_h} do peer")
                all_blocks = self._fetch_chain_from(
                    peer, start, end, strict=True
                )

                if all_blocks is None:
                    log.warning("[REORG] download falhou (strict)")
                    self._peer_record_fail(peer_id)
                    return False
                if not all_blocks:
                    log.warning("[REORG] nenhum bloco recebido")
                    self._peer_record_fail(peer_id)
                    return False

                common_h = -1
                common_hash = ""
                sorted_by_h = sorted(
                    all_blocks, key=lambda b: int(b.get("height", -1)),
                    reverse=True,
                )
                for blk in sorted_by_h:
                    h = int(blk.get("height", -1))
                    if h < 0:
                        continue
                    local_blk = self.db.get_block(h)
                    if local_blk and local_blk["hash"] == blk.get("hash"):
                        common_h = h
                        common_hash = blk["hash"]
                        break

                if common_h < 0:
                    log.warning(
                        f"[REORG] sem ancestral comum nos últimos "
                        f"{len(all_blocks)} blocos"
                    )
                    self._peer_record_fail(peer_id)
                    return False

                new_blocks = sorted(
                    [
                        b for b in all_blocks
                        if common_h < int(b.get("height", -1)) <= target_h
                    ],
                    key=lambda b: int(b.get("height", -1)),
                )

                if not new_blocks:
                    log.warning(
                        f"[REORG] nenhum bloco novo (comum={common_h})"
                    )
                    self._peer_record_fail(peer_id)
                    return False

                try:
                    from blockchain import (
                        get_highest_checkpoint_height,
                        validate_reorg_immutability,
                    )
                    highest_cp = get_highest_checkpoint_height()
                    if highest_cp >= 0 and common_h < highest_cp:
                        log.error(
                            f"[REORG] BLOQUEADO: ancestral comum #{common_h} < "
                            f"checkpoint #{highest_cp}. "
                            f"Blocos <= #{highest_cp} sao IMUTAVEIS."
                        )
                        self._peer_record_fail(peer_id)
                        return False
                    ok_imm, msg_imm = validate_reorg_immutability(new_blocks)
                    if not ok_imm:
                        log.error(f"[REORG] BLOQUEADO: {msg_imm}")
                        self._peer_record_fail(peer_id)
                        return False
                except ImportError:
                    pass

                ok, motivo = self._validate_chain_segment(
                    new_blocks,
                    start_height=common_h + 1,
                    expected_prev_hash=common_hash,
                )
                if not ok:
                    log.warning(
                        f"[REORG] cadeia nova INVÁLIDA — peer malicioso? "
                        f"{motivo}"
                    )
                    self._peer_record_fail(peer_id)
                    return False

                log.warning(
                    f"[REORG] ancestral #{common_h}, "
                    f"nova cadeia com {len(new_blocks)} blocos (validada)"
                )

                if not hasattr(self.chain, "reorg_to"):
                    log.error("[REORG] blockchain sem método reorg_to()")
                    self._peer_record_fail(peer_id)
                    return False

                ok, msg = self.chain.reorg_to(new_blocks)
                if ok:
                    self.status.reorgs_total += 1
                    self.status.last_reorg_height = common_h
                    log.warning(f"[REORG] OK — fork resolvido em #{common_h} ({msg})")
                    self._peer_record_success(peer_id)
                    return True
                else:
                    log.warning(f"[REORG] reorg_to falhou: {msg}")
                    self._peer_record_fail(peer_id)
                    return False

            except Exception as e:
                log.exception(f"[REORG] exceção: {e}")
                self._peer_record_fail(peer_id)
                return False
            finally:
                self.status.forking = False
                self._fork_signal.clear()

    def _refresh_peers_from_p2p(self) -> int:
        p2p = self.p2p
        if p2p is None:
            return 0
        count = 0
        disc = getattr(p2p, "discovery", None)
        if disc is not None and hasattr(disc, "listar_peers_com_altura"):
            try:
                for p in disc.listar_peers_com_altura() or []:
                    addr = p.get("address", "")
                    if not addr or ":" not in addr:
                        continue
                    ip, _, port_s = addr.rpartition(":")
                    try:
                        port = int(port_s)
                    except ValueError:
                        continue
                    height = int(p.get("height", 0) or 0)
                    work = int(p.get("work", 0) or 0)
                    self.register_peer(addr, ip, port, height, work)
                    count += 1
            except Exception as e:
                log.debug(f"_refresh_peers_from_p2p: {e}")
        return count

    def _do_send(self, ip: str, port: int, msg: dict,
                 timeout: float = DEFAULT_TIMEOUT) -> Optional[dict]:
        if self._send_fn is not None:
            try:
                return self._send_fn(ip, port, msg, timeout)
            except Exception:
                return None
        try:
            from p2p_unified import P2PClient
            resp, _ = P2PClient.send_message(ip, port, msg, timeout=timeout)
            return resp
        except Exception as e:
            log.debug(f"P2PClient falhou: {e}")
            return None

    def request_block_batch(self, peer: PeerStatus,
                            start_height: int, count: int) -> List[Dict]:
        payload = {
            "type": "get_blocks_range",
            "start": start_height,
            "end": start_height + count,
        }
        resp = self._do_send(peer.ip, peer.port, payload, DEFAULT_TIMEOUT)
        if not resp or "blocks" not in resp:
            return []
        blocks = resp["blocks"]
        return blocks if isinstance(blocks, list) else []

    def _block_exists(self, height: int) -> bool:
        try:
            return self.db.get_block(height) is not None
        except Exception:
            return False

    def apply_block_batch(self, blocks: List[Dict]) -> Tuple[int, int]:
        applied = 0
        failed = 0
        for block_data in blocks:
            try:
                h = block_data.get("height")
                if h is not None and self._block_exists(h):
                    applied += 1
                    continue
                ok, msg = self.chain.accept_block(block_data)
                if ok:
                    applied += 1
                else:
                    log.warning(f"accept_block #{h}: {msg}")
                    failed += 1
                    break
            except Exception as e:
                log.warning(f"aplicar #{block_data.get('height')}: {e}")
                failed += 1
                break
        return applied, failed

    def sync_with_peer(self, peer: PeerStatus) -> bool:
        local_h = self._safe_height()
        self.status.local_height = local_h
        self.status.target_height = peer.height
        self.status.last_error = ""

        if local_h >= peer.height:
            self.status.synced = True
            return True

        if local_h >= 0:
            peer_block = self._fetch_chain_from(
                peer, local_h, local_h + 1, strict=True
            )
            if peer_block is None or not peer_block:
                self._peer_record_fail(peer.node_id)
                self.status.last_error = f"peer não devolveu bloco #{local_h}"
                return False
            local_block = self.db.get_block(local_h)
            if local_block and peer_block[0].get("hash") != local_block["hash"]:
                log.debug(
                    f"sync_with_peer: fork detectado em #{local_h}, "
                    f"deixando _detect_fork resolver"
                )
                return False

        while not self._stop_sync.is_set():
            local_h = self._safe_height()
            if local_h >= peer.height:
                break

            current = local_h + 1
            end = min(current + self.batch_size - 1, peer.height)
            count = end - current + 1

            blocks = self.request_block_batch(peer, current, count)
            if not blocks:
                self.status.last_error = f"lote {current}..{end} vazio"
                if self._stop_sync.wait(timeout=2):
                    break
                continue

            applied, failed = self.apply_block_batch(blocks)

            self.status.local_height = self._safe_height()
            target = max(1, self.status.target_height)
            self.status.progress = min(100.0, self.status.local_height / target * 100.0)

            log.info(
                f"Sync {self.status.local_height}/{peer.height} "
                f"({self.status.progress:.1f}%) — +{applied} ok, {failed} falhas"
            )

            if applied == 0:
                break
            if failed > 0:
                break
            if self._stop_sync.wait(timeout=BATCH_DELAY_S):
                break

        if not self._stop_sync.is_set():
            self.status.synced = (self._safe_height() >= peer.height)
        return self.status.synced

    def start_sync_loop(self) -> None:
        if self.sync_lock.locked():
            log.debug("sync loop já está rodando")
            return
        self._stop_sync.clear()
        self.status.syncing = True

        def _loop():
            try:
                while not self._stop_sync.is_set():
                    try:
                        self._refresh_peers_from_p2p()

                        fork_peer = self._detect_fork()
                        if fork_peer is not None:
                            self._resolve_fork(fork_peer)
                            continue

                        best = self._best_peer_by_work()
                        if best is not None:
                            self.status.active_peer = best.node_id
                            self.sync_with_peer(best)
                        else:
                            self.status.synced = True
                            self.status.local_height = self._safe_height()
                            self.status.target_height = self.status.local_height
                    except Exception as e:
                        log.warning(f"sync loop erro: {e}")
                        self.status.last_error = str(e)
                    if self._stop_sync.wait(timeout=DEFAULT_INTERVAL_S):
                        break
            finally:
                self.status.syncing = False
                self.status.active_peer = None

        self.sync_thread = threading.Thread(
            target=_loop, daemon=True, name="ChainSync"
        )
        self.sync_thread.start()
        log.info("ChainSyncManager v8.4.2 iniciado")

    def _best_peer_by_work(self) -> Optional[PeerStatus]:
        local_h = self._safe_height()
        local_work = self._local_work()
        with self.peer_lock:
            candidates = [
                p for p in self.peers.values()
                if p.height > local_h
                and not self._peer_is_blocked(p.node_id)
                and (p.cumulative_work > 0 or local_work == 0)
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: (p.cumulative_work, p.height))

    def stop(self) -> None:
        self._stop_sync.set()
        if self.sync_thread and self.sync_thread.is_alive():
            self.sync_thread.join(timeout=5)

    def get_status(self) -> Dict:
        local = self._safe_height()
        target = local
        active = None
        with self.peer_lock:
            peers = list(self.peers.values())
        for p in peers:
            if p.height > target:
                target = p.height
                active = p.addr
        progress = 100.0 if target <= 0 else min(100.0, local / target * 100.0)
        synced = (local >= target and target >= 0)
        blocked = []
        with self._reorg_fails_lock:
            now = time.time()
            for pid, entry in self._reorg_fails.items():
                if entry.get("blocked_until", 0) > now:
                    blocked.append({
                        "peer": pid[:12],
                        "seconds_left": int(entry["blocked_until"] - now),
                        "fails": entry.get("fails", 0),
                    })
        return {
            "mode":                self.status.mode,
            "local_height":        local,
            "target_height":       target,
            "synced":              synced,
            "syncing":             self.status.syncing or self.status.forking,
            "forking":             self.status.forking,
            "reorgs_total":        self.status.reorgs_total,
            "last_reorg_height":   self.status.last_reorg_height,
            "progress":            round(progress, 2),
            "active_peer":         active,
            "peers_known":         len(peers),
            "peers_synced":        sum(1 for p in peers if p.height <= local),
            "last_error":          self.status.last_error,
            "blocos_faltando":     max(0, target - local),
            "blocos_por_segundo":  0.0,
            "velocidade_media":    0.0,
            "eta_segundos":        None,
            "peers_in_cooldown":   blocked,
        }

    def __enter__(self):
        self.start_sync_loop()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False
