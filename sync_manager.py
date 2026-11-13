"""
sync_manager.py — Sincronização + Reorg automático + Aguarda Origin (v9.4.1)
================================================================
v9.4.1 (2026-11-13) — PAINEL HONESTO SOBRE FAST-SYNC:
  [FIX] get_status() agora NÃO reporta synced=True quando o nó ainda
        está abaixo de BRN_FAST_SYNC_UNTIL. Antes, num cliente com
        DB em 4520 e fast-sync até 6000, target=local (só 1 peer, ele
        mesmo) → synced=True, progress=100%. O painel /sync e
        /api/sync/status mostravam "sincronizado" enquanto a mineração
        continuava bloqueada pelo mining gate (que checa
        fast_sync_until). Confusão idêntica à que o server.py v9.9.35
        corrigiu em /api/sync-info e /api/sync/live, mas não em
        /api/sync/status (que passa por aqui).
  [FIX] get_status() retorna fast_sync_active, fast_sync_until e
        fast_sync_percent para o painel /sync poder exibir
        banner "Fast-sync até #6000 (4520/6000 = 75.3%)".
  [FIX] sync_trust_network() docstring movida para o topo (estava
        depois do `if is_origin: return False`, virando string morta
        que o Python ignora).
  [KEEP] v9.4: aguarda Origin online, convergence check, sync do
        trust assinado, sync paralelo.
  [KEEP] v9.3-v9.0: tudo.
================================================================
"""
from __future__ import annotations

import json
import os
import socket
import time
import threading
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("sync")

NETWORK_MAGIC      = b"BRN5"
MAX_MSG_SIZE       = 8 * 1024 * 1024
DEFAULT_TIMEOUT    = 10.0
DEFAULT_BATCH_SIZE = int(os.environ.get("BRN_SYNC_BATCH", "50"))
DEFAULT_INTERVAL_S = float(os.environ.get("BRN_SYNC_INTERVAL", "5"))
BATCH_DELAY_S      = float(os.environ.get("BRN_SYNC_BATCH_DELAY", "0.06"))
PARALLEL_WORKERS   = int(os.environ.get("BRN_SYNC_PARALLEL_WORKERS", "4"))
TRUST_SYNC_INTERVAL= float(os.environ.get("BRN_TRUST_SYNC_INTERVAL", "60"))

BRN_TRUST_REQUIRE_SIGNATURE = (
    os.environ.get("BRN_TRUST_REQUIRE_SIGNATURE", "1") == "1"
)

REORG_LOOKBACK       = int(os.environ.get("BRN_REORG_LOOKBACK", "500"))
REORG_WORK_THRESHOLD = float(os.environ.get("BRN_REORG_WORK_THRESHOLD", "1.0"))
REORG_FETCH_BATCH    = int(os.environ.get("BRN_REORG_FETCH_BATCH", "200"))
REORG_MAX_FAILS      = int(os.environ.get("BRN_REORG_MAX_FAILS", "3"))
REORG_COOLDOWN_S     = float(os.environ.get("BRN_REORG_COOLDOWN_S", "60"))

# [v9.4] Aguardar Origin e convergência
ORIGIN_WAIT_INTERVAL = float(os.environ.get("BRN_ORIGIN_WAIT_S", "30"))
ORIGIN_WAIT_TIMEOUT  = float(os.environ.get("BRN_ORIGIN_TIMEOUT_S", "0"))  # 0 = infinito
CONVERGENCE_CYCLES   = int(os.environ.get("BRN_CONVERGENCE_CYCLES", "3"))
CONVERGENCE_INTERVAL = float(os.environ.get("BRN_CONVERGENCE_INTERVAL", "10"))

# [v9.4.1] Fast-sync awareness
FAST_SYNC_UNTIL = int(os.environ.get("BRN_FAST_SYNC_UNTIL", "0") or "0")

try:
    from blockchain import block_hash as _block_hash_fn, meets_difficulty as _meets_diff_fn
    _HAS_HELPERS = True
except ImportError:
    _block_hash_fn = None
    _meets_diff_fn = None
    _HAS_HELPERS = False
    log.warning("[sync] blockchain helpers ausentes")

try:
    from crypto import Ed25519PublicKey
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False
    log.warning("[sync] crypto.Ed25519PublicKey indisponível — sync de trust "
                "SERÁ REJEITADO.")


@dataclass
class PeerStatus:
    node_id: str
    ip: str
    port: int
    height: int = 0
    cumulative_work: int = 0
    last_seen: float = field(default_factory=time.time)
    latency_ms: float = 0.0
    is_origin: bool = False

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
    parallel_workers: int = PARALLEL_WORKERS
    origin_online: bool = False
    waiting_origin: bool = False
    convergence_cycles: int = 0


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
        self._last_trust_sync = 0.0

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

        # [v9.4] Estado do Origin
        self._origin_url = os.environ.get("BRN_POOL_URL", "").strip().rstrip("/")
        self._origin_wait_start: float = 0.0
        self._last_origin_check: float = 0.0

        # [v9.4.1] Fast-sync (lido uma vez, mas compatível com mudança
        # runtime via env se o operador setar antes do boot)
        self._fast_sync_until = FAST_SYNC_UNTIL

    def set_sync_mode(self, mode: str = "full") -> None:
        if mode not in ("full", "spv"):
            raise ValueError("Modo inválido: use 'full' ou 'spv'")
        if mode == "spv" and not self._has_save_header:
            log.warning("Modo SPV sem db.save_header — caindo para FULL")
            mode = "full"
        self.spv_headers_only = (mode == "spv")
        self.status.mode = mode

    def register_peer(self, node_id: str, ip: str, port: int,
                      height: int = 0, cumulative_work: int = 0,
                      is_origin: bool = False) -> None:
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
                existing.is_origin = is_origin
            else:
                self.peers[node_id] = PeerStatus(
                    node_id=node_id, ip=ip, port=port,
                    height=height, cumulative_work=cumulative_work,
                    is_origin=is_origin,
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

            if not _HAS_HELPERS:
                prev = blk.get("hash", "")
                expected_h += 1
                continue

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

    # =================================================================
    # [v9.4] AGUARDAR ORIGIN ONLINE
    # =================================================================
    def _check_origin_online(self) -> bool:
        """Testa HTTP do Origin com timeout de 3s."""
        if not self._origin_url:
            return False

        try:
            req = urllib.request.Request(
                f"{self._origin_url}/api/status",
                headers={"User-Agent": "brn-sync/9.4.1"},
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    def _wait_origin_online(self, max_wait_s: float = None) -> bool:
        """
        Aguarda o Origin ficar online. Se max_wait_s=None usa
        ORIGIN_WAIT_TIMEOUT (0=infinito).
        """
        if not self._origin_url:
            return False

        t_start = time.time()
        if max_wait_s is None:
            max_wait_s = ORIGIN_WAIT_TIMEOUT

        self.status.waiting_origin = True
        self._origin_wait_start = t_start
        first_attempt = True

        while not self._stop_sync.is_set():
            online = self._check_origin_online()
            self.status.origin_online = online
            self._last_origin_check = time.time()

            if online:
                if not first_attempt:
                    waited = int(time.time() - t_start)
                    log.info(
                        "[SYNC] Origin voltou online após %ds — iniciando sync",
                        waited,
                    )
                self.status.waiting_origin = False
                return True

            if first_attempt:
                log.warning(
                    "[SYNC] Origin offline (%s) — aguardando...",
                    self._origin_url,
                )
                first_attempt = False

            waited = time.time() - t_start
            if int(waited) % 120 < int(ORIGIN_WAIT_INTERVAL):
                log.info("[SYNC] Aguardando Origin há %ds...", int(waited))

            if max_wait_s > 0 and (time.time() - t_start) > max_wait_s:
                log.warning(
                    "[SYNC] Origin não voltou em %ds — seguindo sem ele",
                    int(max_wait_s),
                )
                self.status.waiting_origin = False
                return False

            if self._stop_sync.wait(ORIGIN_WAIT_INTERVAL):
                break

        self.status.waiting_origin = False
        return False

    # =================================================================
    # [v9.4] CONSULTAR ALTURA DE TODOS OS PEERS
    # =================================================================
    def _check_peer_heights(self) -> dict:
        peers_with_height = {}
        with self.peer_lock:
            peers_snapshot = list(self.peers.values())

        if not peers_snapshot:
            return peers_with_height

        for peer in peers_snapshot:
            if self._stop_sync.is_set():
                break
            try:
                resp = self._do_send(
                    peer.ip, peer.port,
                    {"type": "get_chain_height"},
                    DEFAULT_TIMEOUT,
                )
                if resp:
                    h = int(resp.get("height", 0) or 0)
                    w = int(resp.get("work", 0) or 0)
                    with self.peer_lock:
                        if peer.node_id in self.peers:
                            self.peers[peer.node_id].height = h
                            self.peers[peer.node_id].cumulative_work = w
                            self.peers[peer.node_id].last_seen = time.time()
                    peers_with_height[peer.addr] = (h, w)
            except Exception as e:
                log.debug("[SYNC] peer %s altura falhou: %s", peer.addr, e)

        return peers_with_height

    # =================================================================
    # [v9.4] AGUARDAR CONVERGÊNCIA
    # =================================================================
    def _wait_convergence(self, target_height: int) -> bool:
        if CONVERGENCE_CYCLES <= 0:
            return True

        self.status.convergence_cycles = 0
        consecutive = 0

        for cycle in range(CONVERGENCE_CYCLES * 3):
            if self._stop_sync.is_set():
                return False

            peers_h = self._check_peer_heights()
            if not peers_h:
                if self._stop_sync.wait(CONVERGENCE_INTERVAL):
                    return False
                continue

            at_target = sum(
                1 for h, _w in peers_h.values()
                if abs(h - target_height) <= 1
            )
            total = len(peers_h)
            ratio = at_target / max(1, total)

            if ratio >= 0.6:
                consecutive += 1
                self.status.convergence_cycles = consecutive
                log.info(
                    "[SYNC] convergência %d/%d: %d/%d peers em #%d",
                    consecutive, CONVERGENCE_CYCLES,
                    at_target, total, target_height,
                )
                if consecutive >= CONVERGENCE_CYCLES:
                    log.info(
                        "[SYNC] ✅ convergiu — %d peers na altura #%d",
                        at_target, target_height,
                    )
                    return True
            else:
                consecutive = 0
                self.status.convergence_cycles = 0
                log.info(
                    "[SYNC] aguardando convergência: %d/%d peers em #%d (precisa 60%%)",
                    at_target, total, target_height,
                )

            if self._stop_sync.wait(CONVERGENCE_INTERVAL):
                return False

        log.warning(
            "[SYNC] convergência não atingida após %d ciclos",
            CONVERGENCE_CYCLES * 3,
        )
        return False

    # =================================================================
    # SINCRONIZAÇÃO DO TRUST NETWORK VIA HTTP
    # =================================================================
    def sync_trust_network(self) -> bool:
        """Baixa o trust_network.json do Origin e atualiza a memória local."""
        if getattr(self.chain, "is_origin", False):
            return False
        origin_url = self._origin_url
        if not origin_url or not origin_url.startswith("http"):
            return False

        now = time.time()
        if now - self._last_trust_sync < TRUST_SYNC_INTERVAL:
            return False

        self._last_trust_sync = now
        url = f"{origin_url}/api/trust/download"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "brn-sync/9.4.1"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if not isinstance(data, dict):
                log.error("[TRUST-SYNC] payload não é dict — rejeitando")
                return False

            sig = data.pop("signature", None)
            origin_pub = data.pop("origin_pubkey", None)
            data.pop("saved_at", None)
            data.pop("client_written", None)

            if not sig or not origin_pub:
                if BRN_TRUST_REQUIRE_SIGNATURE:
                    log.error(
                        "[TRUST-SYNC] REJEITADO: payload sem assinatura "
                        "do Origin (sig=%s origin_pub=%s)",
                        bool(sig), bool(origin_pub),
                    )
                    return False
                log.warning(
                    "[TRUST-SYNC] ATENÇÃO: aceitando trust SEM assinatura "
                    "(BRN_TRUST_REQUIRE_SIGNATURE=0)"
                )

            if sig and origin_pub:
                if not _HAS_CRYPTO:
                    log.error("[TRUST-SYNC] crypto indisponível — rejeitando")
                    return False
                payload = json.dumps(
                    data, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                try:
                    pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(origin_pub))
                    pk.verify(bytes.fromhex(sig), payload)
                except Exception as e:
                    log.error("[TRUST-SYNC] Assinatura do Origin INVÁLIDA: %s", e)
                    return False

            nodes_payload = data.get("nodes")
            if not isinstance(nodes_payload, dict):
                log.error(
                    "[TRUST-SYNC] payload.nodes não é dict (%s)",
                    type(nodes_payload).__name__,
                )
                return False

            trust = getattr(self.chain, "trust_net", None)
            if trust is None:
                log.debug("[TRUST-SYNC] chain.trust_net ausente")
                return False

            if getattr(trust, "is_origin", False):
                log.warning(
                    "[TRUST-SYNC] este nó é Origin mas BRN_POOL_URL está setado "
                    "para %s. Ignorando.", origin_url,
                )
                return False

            from trust_network import NodeRecord
            merged = 0
            rejected = 0
            with trust._lock:
                for nid, nd in nodes_payload.items():
                    if not isinstance(nd, dict):
                        rejected += 1
                        continue
                    try:
                        trust.nodes[nid] = NodeRecord.from_dict(nd)
                        merged += 1
                    except Exception:
                        rejected += 1
                trust._mark_dirty()
                trust.flush()

            if rejected:
                log.warning(
                    "[TRUST-SYNC] %d nó(s) ignorado(s) (de %d total)",
                    rejected, len(nodes_payload),
                )
            log.info(
                "[TRUST-SYNC] %d nós atualizados do Origin "
                "(assinatura=%s)", merged,
                "válida" if sig and origin_pub else "AUSENTE",
            )
            return True

        except urllib.error.URLError as e:
            log.debug(f"[TRUST-SYNC] Conexão falhou: {e}")
            return False
        except Exception as e:
            log.debug(f"[TRUST-SYNC] falhou: {e}")
            return False

    # =================================================================
    # SYNC PARALELO DE BLOCOS
    # =================================================================
    def _fetch_blocks_parallel(
        self,
        peers: List[PeerStatus],
        start: int,
        end: int,
        chunk_size: int = 20,
    ) -> List[Dict]:
        if not peers or start >= end:
            return []

        chunks = []
        for cursor in range(start, end, chunk_size):
            chunk_end = min(cursor + chunk_size, end)
            chunks.append((cursor, chunk_end))

        log.info(
            f"[SYNC-PARALELO] {len(chunks)} chunk(s) de {chunk_size} blocos, "
            f"{len(peers)} peer(s)"
        )

        def fetch_chunk(chunk_start: int, chunk_end: int) -> Tuple[int, List[Dict]]:
            for peer in peers:
                if self._peer_is_blocked(peer.node_id):
                    continue
                if peer.height < chunk_end:
                    continue
                try:
                    resp = self._do_send(
                        peer.ip, peer.port,
                        {"type": "get_blocks_range", "start": chunk_start, "end": chunk_end},
                        DEFAULT_TIMEOUT * 2,
                    )
                    if resp and "blocks" in resp:
                        return (chunk_start, resp["blocks"])
                except Exception:
                    continue
            return (chunk_start, [])

        result = {}
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
            futures = {pool.submit(fetch_chunk, s, e): (s, e) for s, e in chunks}
            for fut in as_completed(futures):
                try:
                    chunk_start, blocks = fut.result()
                    result[chunk_start] = blocks
                except Exception as e:
                    s, e = futures[fut]
                    log.warning(f"[SYNC-PARALELO] chunk {s}-{e} falhou: {e}")
                    result[s] = []

        all_blocks = []
        for start_key in sorted(result.keys()):
            all_blocks.extend(result[start_key])
        return all_blocks

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
                log.warning(f"[REORG] peer {peer_id[:12]} cooldown")

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
                f"h={best.height} work={best.cumulative_work}"
            )
        return best

    def _resolve_fork(self, peer: PeerStatus) -> bool:
        peer_id = peer.node_id
        if self._peer_is_blocked(peer_id):
            return False

        with self.sync_lock:
            local_h = self._safe_height()
            if peer.height <= local_h:
                return False

            log.warning(f"[REORG] resolvendo fork com {peer_id[:12]}")
            self.status.forking = True
            self._fork_signal.set()

            try:
                target_h = peer.height
                start = max(0, local_h - REORG_LOOKBACK)
                end = target_h + 1

                with self.peer_lock:
                    all_peers = [p for p in self.peers.values() if p.height >= start]
                all_peers.sort(key=lambda p: p.height, reverse=True)

                log.info(f"[REORG] baixando #{start}...#{target_h} (paralelo)")
                all_blocks = self._fetch_blocks_parallel(all_peers, start, end, chunk_size=20)

                if not all_blocks:
                    self._peer_record_fail(peer_id)
                    return False

                common_h = -1
                common_hash = ""
                sorted_by_h = sorted(all_blocks, key=lambda b: int(b.get("height", -1)), reverse=True)
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
                    self._peer_record_fail(peer_id)
                    return False

                new_blocks = sorted(
                    [b for b in all_blocks if common_h < int(b.get("height", -1)) <= target_h],
                    key=lambda b: int(b.get("height", -1)),
                )

                if not new_blocks:
                    self._peer_record_fail(peer_id)
                    return False

                try:
                    from checkpoints import load_checkpoints, verify_checkpoint
                    cps = load_checkpoints()
                    if cps:
                        cp_heights = [int(h) for h in cps.keys()]
                        highest_cp = max(cp_heights) if cp_heights else -1
                        if highest_cp >= 0 and common_h < highest_cp:
                            log.error(
                                f"[REORG] BLOQUEADO: ancestral #{common_h} < "
                                f"checkpoint #{highest_cp}"
                            )
                            self._peer_record_fail(peer_id)
                            return False
                except ImportError:
                    pass

                ok, motivo = self._validate_chain_segment(
                    new_blocks, start_height=common_h + 1, expected_prev_hash=common_hash,
                )
                if not ok:
                    log.warning(f"[REORG] cadeia nova INVÁLIDA: {motivo}")
                    self._peer_record_fail(peer_id)
                    return False

                if not hasattr(self.chain, "reorg_to"):
                    self._peer_record_fail(peer_id)
                    return False

                ok, msg = self.chain.reorg_to(new_blocks)
                if ok:
                    self.status.reorgs_total += 1
                    self.status.last_reorg_height = common_h
                    log.warning(f"[REORG] OK — fork resolvido em #{common_h}")
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

        with self.peer_lock:
            all_peers = [p for p in self.peers.values() if p.height > local_h]
        all_peers.sort(key=lambda p: p.height, reverse=True)

        if not all_peers:
            self.status.last_error = "nenhum peer com altura maior"
            return False

        log.info(
            f"[SYNC-PARALELO] {local_h} -> {peer.height} "
            f"usando {len(all_peers)} peer(s)"
        )

        cursor = local_h + 1
        total_applied = 0
        while cursor <= peer.height and not self._stop_sync.is_set():
            chunk_end = min(cursor + self.batch_size * 2, peer.height + 1)
            blocks = self._fetch_blocks_parallel(
                all_peers, cursor, chunk_end, chunk_size=self.batch_size,
            )

            if not blocks:
                self.status.last_error = f"falha no chunk {cursor}-{chunk_end}"
                if self._stop_sync.wait(timeout=2):
                    break
                continue

            prev = self.db.get_block(cursor - 1) if cursor > 0 else None
            expected_prev = prev["hash"] if prev else "0" * 64
            ok, motivo = self._validate_chain_segment(blocks, cursor, expected_prev)
            if not ok:
                self.status.last_error = f"segmento inválido: {motivo}"
                log.warning(f"[SYNC-PARALELO] {motivo}")
                break

            applied, failed = self.apply_block_batch(blocks)
            total_applied += applied
            cursor = self._safe_height() + 1

            self.status.local_height = self._safe_height()
            target = max(1, self.status.target_height)
            self.status.progress = min(
                100.0, self.status.local_height / target * 100.0,
            )

            log.info(
                f"[SYNC-PARALELO] {self.status.local_height}/{peer.height} "
                f"({self.status.progress:.1f}%) +{applied} ok"
            )

            if failed > 0:
                break
            if applied == 0:
                break

        if not self._stop_sync.is_set():
            self.status.synced = (self._safe_height() >= peer.height)
        return self.status.synced

    # =================================================================
    # LOOP PRINCIPAL (v9.4)
    # =================================================================
    def start_sync_loop(self) -> None:
        if self.sync_lock.locked():
            log.debug("sync loop já está rodando")
            return
        self._stop_sync.clear()
        self.status.syncing = True

        def _loop():
            try:
                is_origin = bool(getattr(self.chain, "is_origin", False))
                if is_origin:
                    log.info("[SYNC] este nó é Origin — sync loop desativado "
                             "(ele É a fonte)")
                    self.status.synced = True
                    self.status.local_height = self._safe_height()
                    self.status.target_height = self.status.local_height
                    return

                if self._origin_url:
                    log.info(
                        "[SYNC] aguardando Origin (%s) ficar online...",
                        self._origin_url,
                    )
                    online = self._wait_origin_online()
                    if not online and self._stop_sync.is_set():
                        return

                while not self._stop_sync.is_set():
                    try:
                        if self._origin_url and not self._check_origin_online():
                            self.status.origin_online = False
                            log.warning(
                                "[SYNC] Origin caiu — aguardando voltar..."
                            )
                            if not self._wait_origin_online():
                                if self._stop_sync.is_set():
                                    break
                            continue

                        self.status.origin_online = True
                        self.sync_trust_network()
                        self._refresh_peers_from_p2p()

                        fork_peer = self._detect_fork()
                        if fork_peer is not None:
                            self._resolve_fork(fork_peer)
                            continue

                        best = self._best_peer_by_work()
                        if best is not None:
                            self.status.active_peer = best.node_id
                            self.sync_with_peer(best)

                            if self.status.synced:
                                target_h = self._safe_height()
                                self._wait_convergence(target_h)
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
            target=_loop, daemon=True, name="ChainSync",
        )
        self.sync_thread.start()
        log.info("ChainSyncManager v9.4.1 (aguarda Origin + convergência "
                 "+ fast-sync aware) iniciado")

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

        candidates.sort(
            key=lambda p: (p.cumulative_work, p.is_origin, p.height),
            reverse=True,
        )
        best = candidates[0]

        if best.is_origin:
            log.debug(
                "[SYNC] escolhido Origin (%s) h=%d work=%d",
                best.addr, best.height, best.cumulative_work,
            )
        else:
            log.debug(
                "[SYNC] escolhido peer (%s) h=%d work=%d",
                best.addr, best.height, best.cumulative_work,
            )
        return best

    def stop(self) -> None:
        self._stop_sync.set()
        if self.sync_thread and self.sync_thread.is_alive():
            self.sync_thread.join(timeout=5)

    # =================================================================
    # [v9.4.1] get_status agora é HONESTO sobre fast-sync
    # =================================================================
    def get_status(self) -> Dict:
        local = self._safe_height()

        # [v9.4.1] Fast-sync awareness: se o nó ainda está abaixo de
        # BRN_FAST_SYNC_UNTIL, o "alvo real" para o operador é esse
        # valor (não o topo dos peers). Sem isso, num cliente que acabou
        # de bootar (DB vazio, 0 peers ainda), target=local → progress=100%
        # → synced=True, mas a mineração continua bloqueada pelo gate.
        # O painel /sync e /api/sync/status mostravam "sincronizado"
        # mentindo.
        fast_until = self._fast_sync_until
        # Se o env mudou depois do __init__, respeitamos
        try:
            runtime = int(os.environ.get("BRN_FAST_SYNC_UNTIL", "0") or "0")
            if runtime > 0:
                fast_until = runtime
        except (TypeError, ValueError):
            pass
        fast_active = (fast_until > 0 and local < fast_until)

        target = local
        active = None
        with self.peer_lock:
            peers = list(self.peers.values())
        for p in peers:
            if p.height > target:
                target = p.height
                active = p.addr

        if fast_active:
            target = max(target, fast_until)

        progress = 100.0 if target <= 0 else min(100.0, local / target * 100.0)
        synced = (local >= target and target >= 0 and not fast_active)

        fast_pct = (
            round((max(local, 0) / fast_until) * 100.0, 1)
            if fast_active else 100.0
        )

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
            "parallel_workers":    PARALLEL_WORKERS,
            "peers_in_cooldown":   blocked,
            "trust_require_signature": BRN_TRUST_REQUIRE_SIGNATURE,
            "origin_online":       self.status.origin_online,
            "waiting_origin":      self.status.waiting_origin,
            "convergence_cycles":  self.status.convergence_cycles,
            # [v9.4.1] Fast-sync (o painel /sync usa isso para exibir
            # banner "Fast-sync até #6000: 4520/6000 = 75.3%")
            "fast_sync_active":    fast_active,
            "fast_sync_until":     fast_until,
            "fast_sync_percent":   fast_pct,
        }

    def __enter__(self):
        self.start_sync_loop()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False
