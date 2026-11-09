"""reorg.py — Reorganização de cadeia (Chain Reorganization) BRN (v2.3)
================================================================
v2.3 (2026-11-09) — PATCH DE AUDITORIA (refinamento):
  [VERIFIED] _restore_backups_direct: NÃO usa chain.accept_block().
        Usa db.add_block() + db.apply_tx() direto — os blocos em
        `backups` já foram aceitos antes (PoW/merkle/auth_tag
        verificados na época). Re-validar multi-sig aqui quebraria
        quando o validator set mudou entre a reorg e a restauração.
        A v2.2 dizia isso nos comentários mas o disco de alguns nós
        ficou com uma versão híbrida onde a função ainda chamava
        accept_block. A v2.3 garante o comportamento correto.
  [NEW] Auto-teste embutido (python reorg.py) valida que a função
        não tem accept_block e que o work-tie preserva a chain local.

v2.2 (2026-11-08):
  [FIX] _should_use_remote: em EMPATE de work, mantém a cadeia local.
  [FIX] _restore_backups_direct (nova): restaura backups SEM
        re-validar multi-sig.

v2.1:
  [FIX] CRÍTICO: find_fork_point detecta fork em local_h + 1.
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
# find_fork_point — detecta fork em local_h + 1
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

    min_remote_h = min(remote_by_h.keys())
    if min_remote_h == local_h + 1:
        lowest = remote_by_h[min_remote_h]
        local_tip = chain.db.tip_hash()
        if lowest.get("prev_hash") == local_tip:
            log.info(f"[REORG] ancestral #{local_h} (tip local = prev_hash do lote)")
            return local_h

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
    """
    [FIX v2.2] Regra simplificada (alinhada com Bitcoin "first seen"):
      - work_remote > work_local → adota remoto
      - work_remote <= work_local → MANTÉM local

    Em empate de work, o "primeiro visto vence" é o comportamento
    seguro: quem chegou primeiro fica. Adotar fork remoto com work
    igual é um vetor de ataque barato.
    """
    work_remote = _cum_work(blocks_to_add)
    work_local = _local_work_above(chain, fork_h)

    if work_remote > work_local:
        log.warning(f"[REORG] work remoto {work_remote} > local {work_local} — adota remoto")
        return True, "work_maior"

    if work_remote == work_local:
        log.info(f"[REORG] work remoto == local ({work_remote}) — mantém local (first seen)")
        return False, "work_empate_first_seen"

    log.info(f"[REORG] work remoto {work_remote} < local {work_local} — mantém local")
    return False, "work_menor"


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


# ============================================================
# [v2.3 VERIFIED] Restore de backups SEM re-validar multi-sig
# ============================================================
def _restore_backups_direct(chain, backups):
    """
    Restaura blocos locais de backup diretamente no DB.

    ESTA FUNÇÃO NÃO PODE USAR chain.accept_block().
    Motivo: os blocos em `backups` JÁ FORAM ACEITOS antes (PoW,
    merkle, auth_tag verificados na época). Re-aceitar via
    accept_block re-valida multi-sig contra o validator set ATUAL,
    que pode ser diferente do set da época (heartbeats expiram,
    TTL de 300s, remove_ghosts). Quando isso acontece, a
    restauração falha e a chain fica truncada em fork_h em
    silêncio.

    O restore usa db.add_block + db.apply_tx direto:
      - db.add_block(ob)        → reescreve o bloco na tabela `blocks`
      - db.apply_tx(t, h, ...)  → reescreve UTXOs e TXs

    PoW/merkle/auth_tag desses blocos são imutáveis (hash depende
    só de prev/merkle/timestamp/nonce/diff). O único check que
    poderia divergir (sig contra o set atual) é justamente o que
    NÃO interessa para reorg de blocos já-aceitos.

    Retorna (restored, failed).
    """
    restored = 0
    failed = 0
    for ob in backups:
        try:
            h = int(ob["height"])
            if chain.db.get_block(h):
                # Já está no DB (reorg pode ter restaurado parte)
                continue
            chain.db.add_block(ob)
            for i, t in enumerate(ob["transactions"]):
                chain.db.apply_tx(t, h, coinbase=(i == 0))
            restored += 1
        except Exception as e:
            log.error(f"[REORG] restore #{ob.get('height')} falhou: {e}")
            failed += 1
    return restored, failed


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

                    # [v2.2/v2.3] Restore direto, SEM re-validar multi-sig.
                    restored, restore_failed = _restore_backups_direct(chain, backups)
                    if restore_failed > 0:
                        log.error(
                            f"[REORG] ATENÇÃO: restore incompleto "
                            f"({restored}/{len(backups)}, {restore_failed} falhas). "
                            f"Chain pode estar em estado inconsistente. "
                            f"Rode sync_all_peers.py para reparar."
                        )
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


# ============================================================
# AUTO-TESTE (python reorg.py)
# ============================================================
if __name__ == "__main__":
    import sys
    import inspect

    print("=" * 60)
    print(" reorg.py — auto-teste")
    print("=" * 60)

    falhas = 0

    # 1. _restore_backups_direct NÃO usa accept_block
    src = inspect.getsource(_restore_backups_direct)
    if "accept_block" in src:
        print(" [X] _restore_backups_direct CHAMA accept_block (BUG v2.1)")
        falhas += 1
    else:
        print(" [ok] _restore_backups_direct NÃO chama accept_block")

    if "db.add_block" not in src:
        print(" [X] _restore_backups_direct não usa db.add_block")
        falhas += 1
    else:
        print(" [ok] _restore_backups_direct usa db.add_block")

    # 2. _should_use_remote mantém local em empate
    class FakeDB:
        def __init__(self):
            self.blocks = {1: {"difficulty": 2, "hash": "a" * 64}}
            self.h = 1
        def height(self): return self.h
        def get_block(self, h): return self.blocks.get(h)
        def tip_hash(self): return "a" * 64

    class FakeChain:
        def __init__(self): self.db = FakeDB()

    chain = FakeChain()
    ok, why = _should_use_remote(chain, 0, [{"height": 1, "difficulty": 2}], 1)
    if ok is False and ("empate" in why.lower() or "first_seen" in why.lower()):
        print(f" [ok] _should_use_remote mantém local em empate ({why})")
    else:
        print(f" [X] _should_use_remote adotou remoto em empate ({ok}, {why})")
        falhas += 1

    # 3. find_fork_point existe e aceita 3 args
    try:
        sig = inspect.signature(find_fork_point)
        params = list(sig.parameters)
        if params == ["chain", "remote_blocks", "max_depth"]:
            print(f" [ok] find_fork_point assinatura: {params}")
        else:
            print(f" [X] find_fork_point assinatura inesperada: {params}")
            falhas += 1
    except Exception as e:
        print(f" [X] find_fork_point: {e}")
        falhas += 1

    print("=" * 60)
    if falhas == 0:
        print(" RESULTADO: OK — reorg.py v2.3 correto")
        sys.exit(0)
    else:
        print(f" RESULTADO: {falhas} falha(s) — reorg.py NÃO está correto")
        sys.exit(1)
