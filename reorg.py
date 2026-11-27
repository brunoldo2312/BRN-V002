# reorg.py — Reorganização de cadeia (Chain Reorganization) BRN (v2.5)
# ================================================================
# v2.5 (2026-11-27) — REESCRITA DE ROBUSTEZ (compatível db.py 9.7 → 9.9):
#   [FIX] _rollback_to: contagem autoritativa (altura antes/depois) e
#         verificação de que db.height() == fork_h. O bool retornado pelo
#         método do banco NÃO é tratado como contagem de blocos removidos.
#         db.truncate_to (≥9.7) já remove blocks/utxos/tx_index/nonces
#         acima do alvo — a reversão do conjunto UTXO acontece dentro do
#         banco; aqui só conferimos que a altura parou no fork.
#   [FIX] restauração pós-falha: _restore_backups_direct continua sem
#         chamar accept_block (usa add_block + apply_tx direto) e sem
#         tocar a mempool; falha parcial agora marca consistent=False e
#         é reportada no resultado (restored / restore_failed).
#   [NEW] _revalidate_mempool: após reorg adotada, descarta da mempool as
#         txs que chain.validate_tx repassar inválidas (UTXO gasto ou
#         removido no ramo local descartado); guarded por hasattr,
#         best-effort, não bloqueia a reorg.
#   [NEW] _pre_validate: rejeita alturas duplicadas no lote; verifica o
#         merkle root (compute_merkle_root); rejeita timestamp futuro
#         além de MAX_FUTURE_DRIFT_S; agora retorna a lista ordenada
#         (elimina segundo sort). Mantém só checagem de CABEÇALHO —
#         assinaturas de validadores, auth_tag e consumo de UTXO seguem
#         com accept_block/validate_block.
#   [MELH] _should_use_remote: work nunca None/negativo; empate e
#         zero-zero mantêm local (first-seen); motivo com código estável.
#   [MELH] ReorgResult: novos campos consistent / restored /
#         restore_failed / mempool_dropped; to_dict() expõe;
#         to_tuple() mantido (compatível com Blockchain.reorg_to, que
#         faz `return res.to_tuple()`).
#   [MELH] guarda de profundidade do LOTE REMOTO: ramo com mais de
#         max_depth blocos é recusado antes de qualquer rollback.
#   [MELH] auto-teste comportamental com fakes instrumentados (não lê
#         mais o fonte com inspect.getsource); cobre empate, rollback,
#         merkle, checkpoint, restauração e pré-validação.
#
# v2.4 (2026-11-17) — COMPATIBILIDADE db.py 9.7.0:
#   [FIX CRÍTICO] _rollback_to usava rollback_block / delete_blocks_above,
#         que não existiam no ChainDB 9.7.0. Passa a usar truncate_to() /
#         remove_blocks_above() / prune_above() / reset_to_height().
#   [KEEP] v2.3: _restore_backups_direct NÃO usa accept_block; empate de
#         work mantém local; find_fork_point detecta fork em local_h+1.
# ================================================================
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("reorg")

_REORG_LOCK = threading.RLock()

DEFAULT_MAX_DEPTH = 200          # guarda conservador quando max_depth não vem do caller
MAX_FUTURE_DRIFT_S = 120         # até 4 blocos (BLOCK_TIME default 30s) no futuro
GENESIS_PREV_HASH = "0" * 64

REQUIRED_FIELDS = ("height", "prev_hash", "merkle", "timestamp",
                   "nonce", "difficulty", "hash")


class ReorgResult:
    __slots__ = ("ok", "msg", "fork_height", "depth", "blocks_removed",
                 "blocks_added", "work_local", "work_remote", "duration_s",
                 "consistent", "restored", "restore_failed", "mempool_dropped")

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
        self.consistent = True
        self.restored = 0
        self.restore_failed = 0
        self.mempool_dropped = 0

    def to_tuple(self):
        """Compatibilidade: Blockchain.reorg_to faz `return res.to_tuple()`."""
        return (self.ok, self.msg)

    def to_dict(self):
        return {
            "ok": self.ok,
            "msg": self.msg,
            "fork_height": self.fork_height,
            "depth": self.depth,
            "blocks_removed": self.blocks_removed,
            "blocks_added": self.blocks_added,
            "work_local": self.work_local,
            "work_remote": self.work_remote,
            "duration_s": round(self.duration_s, 3),
            "consistent": self.consistent,
            "restored": self.restored,
            "restore_failed": self.restore_failed,
            "mempool_dropped": self.mempool_dropped,
        }


# ----------------------------------------------------------------
# Work (heurística por dificuldade — mesmo eixo do PoW do BRN)
# ----------------------------------------------------------------
_WORK_FN = None


def _resolve_work_fn():
    global _WORK_FN
    if _WORK_FN is not None:
        return _WORK_FN
    try:
        from blockchain import work_from_difficulty as fn
        _WORK_FN = fn
    except Exception:
        def fn(d):
            try:
                return 16 ** max(0, int(d or 0))
            except Exception:
                return 0
        _WORK_FN = fn
    return _WORK_FN


def _work(d):
    try:
        return max(0, int(_resolve_work_fn()(d) or 0))
    except Exception:
        return 0


def _cum_work(blocks):
    total = 0
    for b in blocks or []:
        if isinstance(b, dict):
            total += _work(b.get("difficulty", 0))
    return total


def _safe_height(db):
    try:
        return int(db.height())
    except Exception:
        return -1


def _local_work_above(chain, fork_h):
    total = 0
    db = chain.db
    tip = _safe_height(db)
    for h in range(fork_h + 1, tip + 1):
        b = db.get_block(h)
        if b:
            total += _work(b.get("difficulty", 0))
    return total


# ----------------------------------------------------------------
# Detecção de ponto de fork
# ----------------------------------------------------------------
def find_fork_point(chain, remote_blocks, max_depth=DEFAULT_MAX_DEPTH):
    """Altura do último bloco idêntico entre local e remoto.

    Retorna -1 quando não há ancestral comum dentro da janela de busca.
    """
    if not remote_blocks:
        return -1
    db = chain.db
    local_h = _safe_height(db)
    if local_h < 0:
        return -1

    remote_by_h = {}
    for b in remote_blocks:
        if not isinstance(b, dict):
            continue
        try:
            h = int(b.get("height", -1))
        except Exception:
            continue
        if h >= 0 and h not in remote_by_h:
            remote_by_h[h] = b
    if not remote_by_h:
        return -1

    # Extensão pura: primeiro bloco remoto é local_h+1 e encadeia no tip.
    min_remote_h = min(remote_by_h)
    if min_remote_h == local_h + 1:
        lowest = remote_by_h[min_remote_h]
        try:
            local_tip = db.tip_hash()
        except Exception:
            local_tip = ""
        if lowest.get("prev_hash") == local_tip:
            log.info("[REORG] ancestral #%s (tip local = prev_hash do lote)", local_h)
            return local_h

    start_h = min(local_h, max(remote_by_h))
    end_h = max(-1, start_h - max(1, int(max_depth)))
    for h in range(start_h, end_h - 1, -1):
        rb = remote_by_h.get(h)
        if not rb:
            continue
        lb = db.get_block(h)
        if lb and lb.get("hash") is not None and lb.get("hash") == rb.get("hash"):
            return h
    return -1


# ----------------------------------------------------------------
# Decisão: adota remoto só se o work for ESTRITAMENTE maior.
# Empate (ou zero-zero) mantém local — regra first-seen.
# ----------------------------------------------------------------
def _should_use_remote(chain, fork_h, blocks_to_add, local_h=None):
    work_remote = _cum_work(blocks_to_add)
    work_local = _local_work_above(chain, fork_h)
    if work_remote > work_local:
        log.warning("[REORG] work remoto %d > local %d — adota remoto",
                    work_remote, work_local)
        return True, "work_maior"
    if work_remote == work_local:
        log.info("[REORG] work remoto == local (%d) — mantém local (first seen)",
                 work_remote)
        return False, "work_empate_first_seen"
    log.info("[REORG] work remoto %d < local %d — mantém local",
             work_remote, work_local)
    return False, "work_menor"


# ----------------------------------------------------------------
# Checkpoints assinados (não podem ser violados pelo ramo remoto)
# ----------------------------------------------------------------
def _check_checkpoints(fork_h, new_blocks):
    try:
        from checkpoints import load_checkpoints
    except ImportError:
        return True, ""
    try:
        cps = load_checkpoints() or {}
    except Exception:
        return True, ""
    if not cps:
        return True, ""

    remote_by_h = {}
    for b in new_blocks:
        try:
            remote_by_h[int(b["height"])] = b.get("hash", "")
        except Exception:
            continue

    for h_str, cp in cps.items():
        try:
            h = int(h_str)
        except Exception:
            continue
        if h <= fork_h:
            continue
        expected = (cp or {}).get("hash", "") if isinstance(cp, dict) else ""
        if remote_by_h.get(h) != expected:
            return False, f"checkpoint #{h} violado"
    return True, ""


# ----------------------------------------------------------------
# Pré-validação de cabeçalho (barata; roda ANTES de qualquer rollback)
# Assinaturas / auth_tag / UTXO ficam para accept_block.
# ----------------------------------------------------------------
def _pre_validate(chain, fork_h, new_blocks):
    if not new_blocks:
        return False, "lista vazia", []

    try:
        from blockchain import block_hash, meets_difficulty, compute_merkle_root
    except ImportError:
        return False, "blockchain.py sem helpers", []

    try:
        ordered = sorted(new_blocks, key=lambda b: int(b.get("height", -1)))
    except Exception as e:
        return False, f"altura inválida: {e}", []

    seen = set()
    for b in ordered:
        try:
            h = int(b.get("height", -1))
        except Exception:
            return False, "bloco com altura não inteira", []
        if h in seen:
            return False, f"altura #{h} duplicada no lote", []
        seen.add(h)
        for campo in REQUIRED_FIELDS:
            if campo not in b:
                return False, f"campo ausente em #{h}: {campo}", []
        if "transactions" not in b:
            return False, f"#{h} sem lista de transactions", []

    expected_h = fork_h + 1
    if fork_h >= 0:
        prev = chain.db.get_block(fork_h)
        if not prev:
            return False, f"bloco #{fork_h} ausente", []
        prev_hash = prev.get("hash", "")
    else:
        prev_hash = GENESIS_PREV_HASH

    now = int(time.time())
    for blk in ordered:
        h = int(blk["height"])
        if h != expected_h:
            return False, f"esperado #{expected_h}, recebido #{h}", []
        if blk.get("prev_hash") != prev_hash:
            return False, f"#{h} prev_hash não encadeia", []

        try:
            txids = [(t or {}).get("txid", "")
                     for t in (blk.get("transactions") or [])]
            calc_merkle = compute_merkle_root(txids)
        except Exception as e:
            return False, f"#{h} erro de merkle: {e}", []
        if calc_merkle != blk.get("merkle"):
            return False, f"#{h} merkle incorreto", []

        try:
            calc = block_hash(blk["prev_hash"], blk["merkle"], blk["timestamp"],
                              blk["nonce"], blk["difficulty"])
        except Exception as e:
            return False, f"#{h} erro de hash: {e}", []
        if calc != blk.get("hash"):
            return False, f"#{h} hash inválido", []

        try:
            diff = int(blk["difficulty"])
        except Exception:
            return False, f"#{h} dificuldade inválida", []
        if not meets_difficulty(calc, diff):
            return False, f"#{h} PoW inválido", []

        try:
            ts = int(float(blk["timestamp"]))
            if ts > now + MAX_FUTURE_DRIFT_S:
                return False, (f"#{h} timestamp futuro "
                               f"({ts} > {now + MAX_FUTURE_DRIFT_S})"), []
        except Exception:
            pass

        prev_hash = calc
        expected_h += 1

    return True, "ok", ordered


# ----------------------------------------------------------------
# Rollback: banco é responsável por remover UTXO/tx_index/nonce acima
# do alvo (db.truncate_to faz DELETE FROM utxos WHERE height > ?).
# Aqui a garantia é: a altura FINAL bate exatamente no fork_h.
# ----------------------------------------------------------------
def _rollback_to(chain, fork_h):
    db = chain.db
    before = _safe_height(db)
    if before <= fork_h:
        return 0

    for meth in ("truncate_to", "remove_blocks_above", "prune_above",
                 "reset_to_height"):
        fn = getattr(db, meth, None)
        if not callable(fn):
            continue
        ok = fn(fork_h)
        after = _safe_height(db)
        if ok and after == fork_h:
            return max(0, before - after)
        if ok:
            raise RuntimeError(
                f"db.{meth}({fork_h}) ok mas altura ficou {after} "
                f"(esperado {fork_h})")
        raise RuntimeError(f"db.{meth}({fork_h}) retornou False")

    # Legado: rollback bloco a bloco (db.py anterior à 9.7)
    if hasattr(db, "rollback_block"):
        for h in range(before, fork_h, -1):
            db.rollback_block(h)
        after = _safe_height(db)
        return max(0, before - after)
    if hasattr(db, "delete_blocks_above"):
        db.delete_blocks_above(fork_h)
        after = _safe_height(db)
        return max(0, before - after)

    raise RuntimeError(
        "ChainDB sem método de rollback "
        "(truncate_to/remove_blocks_above/prune_above/reset_to_height)")


# ----------------------------------------------------------------
# Restauração dos blocos locais que estavam acima do fork.
# NÃO usa accept_block: esses blocos já haviam sido aceitos (PoW,
# merkle, auth_tag e multi-sig verificados na época). Re-validar
# multi-sig aqui quebraria o restore quando o validator set mudou.
# NÃO remove nada da mempool: são blocos históricos; as txs deles já
# haviam sido retiradas da mempool na aceitação original.
# ----------------------------------------------------------------
def _restore_backups_direct(chain, backups):
    restored = 0
    failed = 0
    db = chain.db
    for ob in backups or []:
        try:
            h = int(ob["height"])
            if db.get_block(h):
                continue
            db.add_block(ob)
            for i, t in enumerate(ob.get("transactions") or []):
                db.apply_tx(t, h, coinbase=(i == 0))
            restored += 1
        except Exception as e:
            log.error("[REORG] restore #%s falhou: %s", ob.get("height"), e)
            failed += 1
    return restored, failed


# ----------------------------------------------------------------
# Pós-reorg: limpa da mempool as txs que deixaram de ser válidas
# (ex.: gastavam UTXO criado só no ramo local descartado).
# Best-effort: sem validate_tx no nó, não faz nada.
# ----------------------------------------------------------------
def _revalidate_mempool(chain):
    db = getattr(chain, "db", None)
    if db is None:
        return 0
    if not hasattr(db, "all_mempool") or not hasattr(db, "remove_mempool"):
        return 0
    if not hasattr(chain, "validate_tx"):
        return 0

    try:
        txs = db.all_mempool(limit=1000)
    except Exception as e:
        log.warning("[REORG] leitura da mempool após reorg falhou: %s", e)
        return 0

    dropped = 0
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        txid = tx.get("txid", "")
        if not txid:
            continue
        try:
            ok, _msg = chain.validate_tx(tx, from_mempool=True)
            if ok:
                continue
            db.remove_mempool(txid)
            dropped += 1
        except Exception:
            continue
    if dropped:
        log.info("[REORG] %d tx(s) removidas da mempool após reorg", dropped)
    return dropped


def _notify_peer_hook(chain, fork_h, removed, added, peer_id):
    hook = getattr(chain, "note_reorg", None)
    if not callable(hook):
        return
    try:
        hook(fork_h, removed, added, peer_id)
    except Exception:
        pass


def _set_fork_signal(chain, active):
    sig = getattr(chain, "_fork_signal", None)
    if sig is None:
        try:
            sig = threading.Event()
            chain._fork_signal = sig
        except Exception:
            return
    try:
        if active:
            sig.set()
        else:
            sig.clear()
    except Exception:
        pass


def _finish(res, msg, t0):
    res.msg = msg
    res.duration_s = time.time() - t0
    return res


# ----------------------------------------------------------------
# Entrada principal
# ----------------------------------------------------------------
def reorg_to(chain, new_blocks, max_depth=DEFAULT_MAX_DEPTH,
             allow_genesis_reorg=False, remote_peer_id=None):
    t0 = time.time()
    res = ReorgResult()

    with _REORG_LOCK:
        if not new_blocks:
            return _finish(res, "lista vazia", t0)

        db = chain.db
        local_h = _safe_height(db)
        if local_h < 0 and not allow_genesis_reorg:
            return _finish(res, "cadeia vazia", t0)

        fork_h = find_fork_point(chain, new_blocks, max_depth=max_depth * 2)
        if fork_h < 0 and not allow_genesis_reorg:
            return _finish(res, "sem ancestral comum", t0)
        res.fork_height = fork_h

        blocks_to_add = []
        seen_h = set()
        for b in new_blocks:
            try:
                h = int(b.get("height", -1))
            except Exception:
                continue
            if h > fork_h and h not in seen_h:
                seen_h.add(h)
                blocks_to_add.append(b)
        if not blocks_to_add:
            return _finish(res, "nada novo", t0)
        blocks_to_add.sort(key=lambda b: int(b["height"]))

        depth = max(0, local_h - fork_h)
        res.depth = depth
        if depth > max_depth:
            return _finish(res, f"reorg profunda ({depth} > {max_depth})", t0)
        if len(blocks_to_add) > max_depth:
            return _finish(
                res,
                f"ramo remoto longo ({len(blocks_to_add)} > {max_depth})",
                t0)

        ok_cp, msg_cp = _check_checkpoints(fork_h, blocks_to_add)
        if not ok_cp:
            return _finish(res, msg_cp, t0)

        ok_pre, msg_pre, ordered = _pre_validate(chain, fork_h, blocks_to_add)
        if not ok_pre:
            return _finish(res, f"pré-validação: {msg_pre}", t0)

        use_remote, motivo = _should_use_remote(chain, fork_h, ordered, local_h)
        res.work_remote = _cum_work(ordered)
        res.work_local = _local_work_above(chain, fork_h)
        if not use_remote:
            res.msg = f"mantém cadeia local ({motivo})"
            res.duration_s = time.time() - t0
            return res

        log.warning("[REORG] Iniciando reorg #%s motivo=%s depth=%d add=%d",
                    fork_h, motivo, depth, len(ordered))

        # Snapshot dos blocos locais que serão removidos (para restore).
        backups = []
        for h in range(fork_h + 1, local_h + 1):
            b = db.get_block(h)
            if b:
                backups.append(b)

        _set_fork_signal(chain, True)
        try:
            try:
                res.blocks_removed = _rollback_to(chain, fork_h)
            except Exception as e:
                res.msg = f"rollback falhou: {e}"
                res.duration_s = time.time() - t0
                return res

            added = 0
            for b in ordered:
                try:
                    ok_acc, msg_acc = chain.accept_block(b)
                except Exception as e:
                    ok_acc, msg_acc = False, f"exceção: {e}"
                if not ok_acc:
                    log.error("[REORG] falhou #%s: %s — restaurando backups",
                              b.get("height"), msg_acc)
                    _restore_chain(chain, backups, fork_h, res)
                    res.blocks_added = added
                    res.msg = f"falha #{b.get('height')}: {msg_acc}"
                    res.duration_s = time.time() - t0
                    return res
                added += 1

            res.blocks_added = added
            res.mempool_dropped = _revalidate_mempool(chain)
            _notify_peer_hook(chain, fork_h, res.blocks_removed, added,
                              remote_peer_id)
            res.ok = True
            res.msg = (f"reorg OK (fork #{fork_h}, -{res.blocks_removed} "
                       f"+{added}, mempool-{res.mempool_dropped})")
            log.warning("[REORG] %s — altura final: %s",
                        res.msg, _safe_height(db))
        finally:
            _set_fork_signal(chain, False)

        res.duration_s = time.time() - t0
        return res


def _restore_chain(chain, backups, fork_h, res):
    """Rolo de volta o que foi adicionado e restaura os backups locais."""
    db = chain.db
    try:
        cur = _safe_height(db)
        if cur > fork_h:
            _rollback_to(chain, fork_h)
    except Exception as e:
        log.error("[REORG] rollback de recuperação falhou: %s", e)

    restored, failed = _restore_backups_direct(chain, backups)
    res.restored = restored
    res.restore_failed = failed
    res.consistent = (failed == 0)
    if failed:
        log.error(
            "[REORG] ATENÇÃO: restore incompleto (%d/%d, %d falhas). "
            "Chain pode estar inconsistente; rode sync_all_peers.py.",
            restored, len(backups), failed)
    try:
        res.mempool_dropped = _revalidate_mempool(chain)
    except Exception:
        pass


# ----------------------------------------------------------------
# Auto-teste comportamental: python reorg.py
# ----------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import types

    print("=" * 60)
    print(" reorg.py v2.5 — auto-teste comportamental")
    print("=" * 60)
    falhas = 0

    def check(cond, ok_msg, fail_msg):
        global falhas
        if cond:
            print(f" [ok] {ok_msg}")
        else:
            print(f" [X] {fail_msg}")
            falhas += 1

    # Stub do módulo blockchain (não depende do nó rodando).
    bc = types.ModuleType("blockchain")
    bc.block_hash = lambda prev, merkle, ts, nonce, diff: "H" * 64
    bc.meets_difficulty = lambda h, d: True
    bc.compute_merkle_root = lambda txids: "M" * 64
    sys.modules["blockchain"] = bc

    # T1: restauração NÃO chama accept_block; usa add_block + apply_tx.
    class _DB1:
        def __init__(self):
            self.blocks = {}
            self.added = []
            self.applied = []
        def get_block(self, h):
            return self.blocks.get(h)
        def add_block(self, b):
            self.blocks[b["height"]] = b
            self.added.append(b["height"])
        def apply_tx(self, t, h, coinbase=False):
            self.applied.append((h, coinbase))

    class _Chain1:
        def __init__(self):
            self.db = _DB1()
            self.accept_calls = 0
        def accept_block(self, b):
            self.accept_calls += 1
            return True, ""

    c1 = _Chain1()
    backups = [{"height": 1, "transactions": [
        {"txid": "c", "inputs": [], "outputs": []},
        {"txid": "t", "inputs": [], "outputs": []}]}]
    restored, failed = _restore_backups_direct(c1, backups)
    check(restored == 1 and failed == 0 and c1.accept_calls == 0,
          "restore usa add_block/apply_tx e não chama accept_block",
          f"restore inesperado: restored={restored} failed={failed} "
          f"accept_calls={c1.accept_calls}")
    check(c1.db.applied == [(1, True), (1, False)],
          "coinbase é i==0; demais txs não-coinbase",
          f"aplicações: {c1.db.applied}")

    # T2: empate de work mantém local (first-seen).
    class _DB2:
        def height(self):
            return 1
        def get_block(self, h):
            return {"difficulty": 1} if h == 1 else None

    class _Chain2:
        db = _DB2()

    ok, why = _should_use_remote(_Chain2(), 0,
                                  [{"height": 1, "difficulty": 1}], 1)
    check(ok is False and "empate" in why,
          f"empate mantém local ({why})",
          f"empate adotou remoto: ok={ok} why={why}")

    # T3: rollback reporta contagem autoritativa e confere altura final.
    class _DB3:
        def __init__(self):
            self.h = 8
            self.calls = []
        def height(self):
            return self.h
        def truncate_to(self, t):
            self.calls.append(t)
            self.h = t
            return True

    class _Chain3:
        def __init__(self):
            self.db = _DB3()

    c3 = _Chain3()
    r3 = _rollback_to(c3, 5)
    check(r3 == 3 and c3.db.calls == [5] and c3.db.h == 5,
          f"rollback autoritativo: {r3} removidos, calls={c3.db.calls}",
          f"rollback inválido: r={r3} calls={c3.db.calls} h={c3.db.h}")

    # T4: pré-validação — gênesis válido; rejeita quebra de ligação,
    # duplicidade de altura e merkle errado.
    class _DB4:
        def get_block(self, h):
            return {"hash": "X" * 64} if h == 0 else None

    class _Chain4:
        db = _DB4()

    genesis = {"height": 0, "prev_hash": GENESIS_PREV_HASH,
               "merkle": "M" * 64, "timestamp": int(time.time()),
               "nonce": 0, "difficulty": 1, "hash": "H" * 64,
               "transactions": []}
    ok, msg, _ = _pre_validate(_Chain4(), -1, [genesis])
    check(ok, f"gênesis válido ({msg})", f"gênesis rejeitado: {msg}")

    bad_link = dict(genesis, height=1, prev_hash="Z" * 64)
    ok, msg, _ = _pre_validate(_Chain4(), 0, [bad_link])
    check(not ok and "encadeia" in msg,
          "prev_hash quebrado rejeitado",
          f"quebra de ligação não detectada: {msg}")

    dup = [dict(genesis, height=1, prev_hash="X" * 64),
           dict(genesis, height=1, prev_hash="X" * 64)]
    ok, msg, _ = _pre_validate(_Chain4(), 0, dup)
    check(not ok and "duplicada" in msg,
          "altura duplicada rejeitada",
          f"duplicidade não detectada: {msg}")

    bad_merkle = dict(genesis, height=1, prev_hash="X" * 64,
                      merkle="Z" * 64)
    ok, msg, _ = _pre_validate(_Chain4(), 0, [bad_merkle])
    check(not ok and "merkle" in msg,
          "merkle incorreto rejeitado",
          f"merkle não verificado: {msg}")

    # T5: checkpoint assinado não pode ser violado pelo ramo remoto.
    cp_mod = types.ModuleType("checkpoints")
    cp_mod.load_checkpoints = lambda: {"1": {"hash": "C" * 64}}
    sys.modules["checkpoints"] = cp_mod
    ok, msg = _check_checkpoints(0, [{"height": 1, "hash": "Z" * 64}])
    check(not ok and "checkpoint" in msg,
          f"checkpoint violado rejeitado ({msg})",
          f"checkpoint não checado: {msg}")

    # T6: find_fork_point no meio da cadeia.
    class _DB6:
        def __init__(self):
            self.h = 3
            self.blocks = {1: {"hash": "a"}, 2: {"hash": "b"},
                           3: {"hash": "c"}}
        def height(self):
            return self.h
        def get_block(self, h):
            return self.blocks.get(h)
        def tip_hash(self):
            return "c"

    class _Chain6:
        db = _DB6()

    fh = find_fork_point(_Chain6(),
                         [{"height": 2, "hash": "b"},
                          {"height": 4, "hash": "d"}])
    check(fh == 2,
          f"fork point detectado em #{fh}",
          f"fork point errado: {fh}")

    # T7: compatibilidade do resultado com callers existentes.
    r7 = ReorgResult()
    r7.ok = True
    r7.msg = "ok"
    check(r7.to_tuple() == (True, "ok"),
          "to_tuple mantido (compatível com Blockchain.reorg_to)",
          "to_tuple quebrado")
    d7 = r7.to_dict()
    check(all(k in d7 for k in ("consistent", "restored",
                                "restore_failed", "mempool_dropped")),
          "to_dict expõe os novos campos",
          "to_dict sem campos novos")

    print("=" * 60)
    if falhas == 0:
        print(" RESULTADO: OK — reorg.py v2.5 passou em todos os testes")
        sys.exit(0)
    print(f" RESULTADO: {falhas} falha(s) — reorg.py v2.5 NÃO está correto")
    sys.exit(1)