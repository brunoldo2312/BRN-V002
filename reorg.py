"""reorg.py — Reorganização de cadeia (Chain Reorganization) BRN (v2.1)
================================================================
v2.1:
  [FIX] CRÍTICO: find_fork_point agora detecta corretamente o fork
        quando o lote remoto começa em local_h + 1 e o prev_hash
        do primeiro bloco bate com o tip local.
        Antes, o loop descendente não encontrava o ancestral e
        retornava -1, travando o sync com "sem ancestral comum".
v2.0:
  [FIX] Desempate de forks na MESMA altura por timestamp e hash.
================================================================
"""
from __future__ import annotations
import logging
import threading
import time

log = logging.getLogger("reorg")
_REORG_LOCK = threading.RLock()
DEFAULT_MAX_DEPTH = 200


class ReorgResult:
    __slots__ = ("ok", "msg", "fork_height", "depth", "blocks_removed",
                 "blocks_added", "work_local", "work_remote", "duration_s")
    def __init__(self):
        self.ok = False
        self.msg = ""
        self.fork_height = -1
        self.depth = 0
        self.blocks_removed = 0
        self.blocks_added = 0
        self.work_local = 0
        self.work_remote = 0
        self.duration_s = 0.0
    def to_tuple(self): return (self.ok, self.msg)
    def to_dict(self):
        return {"ok": self.ok, "msg": self.msg, "fork_height": self.fork_height,
                "depth": self.depth, "blocks_removed": self.blocks_removed,
                "blocks_added": self.blocks_added, "work_local": self.work_local,
                "work_remote": self.work_remote, "duration_s": round(self.duration_s, 3)}


def _work(d): return 16 ** int(d or 0)
def _cum_work(blocks): return sum(_work(b.get("difficulty", 0)) for b in blocks)
def _local_work_above(chain, fh):
    total = 0
    for h in range(fh + 1, chain.db.height() + 1):
        b = chain.db.get_block(h)
        if b: total += _work(b.get("difficulty", 0))
    return total

# ============================================================
# [FIX v2.1] find_fork_point — detecta fork em local_h + 1
# ============================================================
def find_fork_point(chain, remote_blocks, max_depth=200):
    if not remote_blocks: return -1
    try: local_h = chain.db.height()
    except Exception: return -1
    remote_by_h = {}
    for b in remote_blocks:
        try: remote_by_h[int(b["height"])] = b
        except Exception: continue
    if not remote_by_h: return -1

    # [FIX] Se o lote começa exatamente no próximo bloco local,
    # e o prev_hash do primeiro bloco remoto bate com o topo local,
    # o ponto de fork é o próprio topo local.
    min_remote_h = min(remote_by_h.keys())
    if min_remote_h == local_h + 1:
        lowest = remote_by_h[min_remote_h]
        local_tip = chain.db.tip_hash()
        if lowest.get("prev_hash") == local_tip:
            log.info(f"[REORG] ancestral #{local_h} (tip local = prev_hash do lote)")
            return local_h

    # Fallback: procura o hash em comum
    start_h = min(local_h, max(remote_by_h.keys()))
    end_h = max(-1, start_h - max(1, int(max_depth)))
    for h in range(start_h, end_h - 1, -1):
        rb = remote_by_h.get(h)
        if not rb: continue
        lb = chain.db.get_block(h)
        if lb and lb.get("hash") == rb.get("hash"):
            return h
    return -1


def _should_use_remote(chain, fork_h, blocks_to_add, local_h):
    work_remote = _cum_work(blocks_to_add)
    work_local = _local_work_above(chain, fork_h)
    if work_remote > work_local:
        log.warning(f"[REORG] work remoto {work_remote} > local {work_local}")
        return True, "work_maior"
    if work_remote < work_local:
        log.info(f"[REORG] work remoto {work_remote} < local {work_local}")
        return False, "work_menor"
    first_remote = blocks_to_add[0]
    first_local = chain.db.get_block(fork_h + 1)
    if not first_local: return True, "local_sem_bloco"
    ts_r = int(first_remote.get("timestamp", 0))
    ts_l = int(first_local.get("timestamp", 0))
    if ts_r < ts_l:
        log.warning(f"[REORG] timestamps: remoto #{ts_r} < local #{ts_l} → escolhe remoto")
        return True, "timestamp_mais_antigo"
    if ts_r > ts_l:
        return False, "timestamp_mais_novo"
    hr = str(first_remote.get("hash", ""))
    hl = str(first_local.get("hash", ""))
    if hr < hl:
        return True, "hash_menor"
    return False, "hash_maior"


def _pre_validate(chain, fork_h, new_blocks):
    if not new_blocks: return False, "lista vazia"
    try: from blockchain import block_hash, meets_difficulty
    except ImportError: return False, "blockchain.py sem helpers"
    try: ordered = sorted(new_blocks, key=lambda b: int(b["height"]))
    except Exception as e: return False, f"altura inválida: {e}"
    expected_h = fork_h + 1
    if fork_h >= 0:
        prev = chain.db.get_block(fork_h)
        if not prev: return False, f"bloco #{fork_h} ausente"
        prev_hash = prev["hash"]
    else:
        prev_hash = "0" * 64
    for blk in ordered:
        h = int(blk["height"])
        if h != expected_h: return False, f"esperado #{expected_h}, recebido #{h}"
        if blk.get("prev_hash") != prev_hash: return False, f"#{h} prev_hash não encadeia"
        calc = block_hash(blk["prev_hash"], blk["merkle"], blk["timestamp"], blk["nonce"], blk["difficulty"])
        if calc != blk.get("hash"): return False, f"#{h} hash inválido"
        if not meets_difficulty(calc, int(blk["difficulty"])): return False, f"#{h} PoW inválido"
        prev_hash = calc
        expected_h += 1
    return True, "ok"


def _check_checkpoints(fork_h, new_blocks):
    try: from checkpoints import load_checkpoints
    except ImportError: return True, ""
    try: cps = load_checkpoints() or {}
    except Exception: return True, ""
    if not cps: return True, ""
    remote_by_h = {}
    for b in new_blocks:
        try: remote_by_h[int(b["height"])] = b.get("hash", "")
        except Exception: continue
    for h_str, cp in cps.items():
        try: h = int(h_str)
        except Exception: continue
        if h <= fork_h: continue
        expected = cp.get("hash", "")
        if remote_by_h.get(h) != expected:
            return False, f"checkpoint #{h} violado"
    return True, ""


def _rollback_to(chain, fork_h, current_tip):
    removed = 0
    if hasattr(chain.db, "rollback_block"):
        for h in range(current_tip, fork_h, -1):
            chain.db.rollback_block(h)
            removed += 1
    elif hasattr(chain.db, "delete_blocks_above"):
        removed = int(chain.db.delete_blocks_above(fork_h) or 0)
    else:
        raise RuntimeError("ChainDB sem rollback_block/delete_blocks_above")
    return removed


def _set_fork_signal(chain, active):
    sig = getattr(chain, "_fork_signal", None)
    if sig is None:
        try:
            sig = threading.Event()
            chain._fork_signal = sig
        except Exception: return
    try:
        if active: sig.set()
        else: sig.clear()
    except Exception: pass


def reorg_to(chain, new_blocks, max_depth=DEFAULT_MAX_DEPTH, allow_genesis_reorg=False):
    t0 = time.time()
    res = ReorgResult()
    with _REORG_LOCK:
        if not new_blocks:
            res.msg = "lista vazia"; res.duration_s = time.time() - t0; return res
        local_h = chain.db.height()
        if local_h < 0 and not allow_genesis_reorg:
            res.msg = "cadeia vazia"; res.duration_s = time.time() - t0; return res
        fork_h = find_fork_point(chain, new_blocks, max_depth=max_depth * 2)
        if fork_h < 0 and not allow_genesis_reorg:
            res.msg = "sem ancestral comum"; res.duration_s = time.time() - t0; return res
        res.fork_height = fork_h
        blocks_to_add = []
        for b in new_blocks:
            try:
                if int(b["height"]) > fork_h: blocks_to_add.append(b)
            except Exception: continue
        if not blocks_to_add:
            res.msg = "nada novo"; res.duration_s = time.time() - t0; return res
        blocks_to_add.sort(key=lambda b: int(b["height"]))
        depth = local_h - fork_h
        res.depth = depth
        if depth > max_depth:
            res.msg = f"reorg profundo ({depth} > {max_depth})"
            res.duration_s = time.time() - t0; return res
        ok_cp, msg_cp = _check_checkpoints(fork_h, blocks_to_add)
        if not ok_cp:
            res.msg = msg_cp; res.duration_s = time.time() - t0; return res
        ok_pre, msg_pre = _pre_validate(chain, fork_h, blocks_to_add)
        if not ok_pre:
            res.msg = f"pré-validação: {msg_pre}"; res.duration_s = time.time() - t0; return res
        use_remote, motivo = _should_use_remote(chain, fork_h, blocks_to_add, local_h)
        res.work_remote = _cum_work(blocks_to_add)
        res.work_local = _local_work_above(chain, fork_h)
        if not use_remote:
            res.msg = f"mantém cadeia local ({motivo})"
            res.duration_s = time.time() - t0; return res
        log.warning(f"[REORG] Iniciando reorg #{fork_h} motivo={motivo} "
                    f"depth={depth} add={len(blocks_to_add)}")
        _set_fork_signal(chain, True)
        try:
            backups = []
            for h in range(fork_h + 1, local_h + 1):
                b = chain.db.get_block(h)
                if b: backups.append(b)
            try:
                res.blocks_removed = _rollback_to(chain, fork_h, local_h)
            except Exception as e:
                res.msg = f"rollback falhou: {e}"; res.duration_s = time.time() - t0; return res
            added = 0
            for b in blocks_to_add:
                try: ok_acc, msg_acc = chain.accept_block(b)
                except Exception as e: ok_acc, msg_acc = False, f"exceção: {e}"
                if not ok_acc:
                    log.error(f"[REORG] falhou #{b.get('height')}: {msg_acc} — restaurando")
                    try:
                        cur = chain.db.height()
                        if cur > fork_h: _rollback_to(chain, fork_h, cur)
                    except Exception: pass
                    for ob in backups:
                        try: chain.accept_block(ob)
                        except Exception: pass
                    res.blocks_added = added
                    res.msg = f"falha #{b.get('height')}: {msg_acc}"
                    res.duration_s = time.time() - t0; return res
                added += 1
            res.blocks_added = added
            res.ok = True
            res.msg = f"reorg OK (fork #{fork_h}, -{res.blocks_removed} +{added})"
            log.warning(f"[REORG] {res.msg} — altura final: {chain.db.height()}")
        finally:
            _set_fork_signal(chain, False)
        res.duration_s = time.time() - t0
        return res
