"""chain_validator.py — Auditoria completa da cadeia BRN
================================================================
Audita, bloco a bloco:
  1. Encadeamento prev_hash
  2. PoW (via meets_difficulty() do blockchain.py)
  3. Merkle root
  4. Coinbase (formato + reward não excedido)
  5. Assinaturas de tx (exceto coinbase)
  6. Auth tag HMAC (a partir de AUTH_TAG_START_HEIGHT)
  7. Checkpoints assinados (Ed25519)

API esperada por main.py v12.5:
    r = verify_chain(chain)
    r.valid                 → bool
    r.summary()             → str
    r.blocks_checked        → int
    r.checkpoints_checked   → int
    r.warnings              → list[str]
    r.errors                → list[str]

Também exporta:
    verify_chain_dict(chain) → dict
    verify_range(chain, start, end) → dict
    find_fork_point(chain_a, chain_b, max_depth) → int

[FIX v2]
  - _verify_pow usa meets_difficulty() do blockchain.py
    (comparação numérica correta com target)
  - Fallback numérico se blockchain.py não disponível
  - verify_range agora também verifica merkle

Requer:
    blockchain.py (v9.1.7+)
================================================================
"""
from __future__ import annotations

import hashlib
import logging
import time

from crypto import verify_ecdsa, sha256, double_sha256


log = logging.getLogger("chain_validator")


# ============================================================
# RESULTADO
# ============================================================
class VerifyResult:
    def __init__(self):
        self.valid = True
        self.blocks_checked = 0
        self.checkpoints_checked = 0
        self.warnings = []
        self.errors = []
        self.height = -1
        self.duration = 0.0

    def summary(self) -> str:
        if self.valid:
            return (f"cadeia OK · {self.blocks_checked} blocos · "
                    f"{self.checkpoints_checked} checkpoints · "
                    f"{self.duration:.2f}s")
        return (f"cadeia INVALIDA · {len(self.errors)} erro(s) · "
                f"{self.blocks_checked} blocos verificados")

    def __bool__(self):
        return self.valid

    def to_dict(self) -> dict:
        return {
            "success":              self.valid,
            "valid":                self.valid,
            "height":               self.height,
            "blocks_checked":       self.blocks_checked,
            "verified":             self.blocks_checked,
            "checkpoints_checked":  self.checkpoints_checked,
            "warnings":             list(self.warnings),
            "errors":               list(self.errors),
            "duration_s":           round(self.duration, 3),
        }


# ============================================================
# HELPERS
# ============================================================
def _is_coinbase(tx: dict) -> bool:
    if tx.get("type") == "coinbase" or tx.get("is_coinbase"):
        return True
    ins = tx.get("inputs") or []
    return bool(ins and ins[0].get("txid") == "0" * 64)


# ============================================================
# [FIX] PoW — usa meets_difficulty() numericamente correto
# ============================================================
def _target_from_difficulty(difficulty: int) -> int:
    """
    Replica de blockchain.target_from_difficulty().
    Target = '0'*diff + 'f'*(64-diff) interpretado em hex.
    """
    return int("0" * int(difficulty) + "f" * (64 - int(difficulty)), 16)


def _meets_difficulty(h_hex: str, difficulty: int) -> bool:
    """
    Fallback numérico caso blockchain.py não esteja importável.
    Mesma lógica de blockchain.meets_difficulty().
    """
    try:
        return int(h_hex, 16) <= _target_from_difficulty(difficulty)
    except Exception:
        return False


def _verify_pow(block: dict) -> bool:
    """
    Confere se hash do header bate com o hash declarado e satisfaz o PoW.

    [FIX v2] Usa meets_difficulty() do blockchain.py (comparação
    numérica com o target real), em vez de startswith('0'*difficulty).
    Isto evita falsos negativos em dificuldades >= 6.
    """
    try:
        # 1) Recalcula o hash
        try:
            from blockchain import block_hash as _bh
            h = _bh(block["prev_hash"], block["merkle"],
                    block["timestamp"], block["nonce"], block["difficulty"])
        except ImportError:
            hdr = (f'{block["prev_hash"]}{block["merkle"]}'
                   f'{block["timestamp"]}{block["nonce"]}{block["difficulty"]}')
            h = double_sha256(hdr.encode()).hex()

        if h != block["hash"]:
            return False

        # 2) Valida PoW via função oficial (numericamente correta)
        try:
            from blockchain import meets_difficulty as _md
            return bool(_md(h, int(block["difficulty"])))
        except ImportError:
            return _meets_difficulty(h, int(block["difficulty"]))
    except Exception:
        return False


# ============================================================
# Merkle
# ============================================================
def _verify_merkle(block: dict) -> bool:
    try:
        txids = [t.get("txid", "") for t in block.get("transactions", [])]
        if not txids:
            return False
        try:
            from blockchain import compute_merkle_root as _mr
            calc = _mr(txids)
        except ImportError:
            calc = _merkle_root_fallback(txids)
        return calc == block.get("merkle", "")
    except Exception:
        return False


def _merkle_root_fallback(txids: list) -> str:
    if not txids:
        return "0" * 64
    layer = [bytes.fromhex(t) for t in txids]
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [sha256(layer[i] + layer[i + 1])
                 for i in range(0, len(layer), 2)]
    return layer[0].hex()


# ============================================================
# Coinbase
# ============================================================
def _verify_coinbase(cb: dict, height: int, chain) -> tuple:
    """
    Confere:
      - Formato de coinbase (input = 0*64)
      - Outputs não vazios
      - Soma dos outputs <= reward_at(height)
    """
    ins = cb.get("inputs") or []
    if not ins or ins[0].get("txid") != "0" * 64:
        return False, "input nao e 0*64"

    outs = cb.get("outputs") or []
    if not outs:
        return False, "coinbase sem outputs"

    total = sum(int(o.get("amount", 0)) for o in outs)
    try:
        reward = chain.current_reward(height)
        if total > reward:
            return False, f"coinbase ({total}) > reward ({reward})"
    except Exception:
        pass

    if height > 0:
        for o in outs:
            if not o.get("pubkey"):
                return False, "output de coinbase sem pubkey"

    return True, "ok"


# ============================================================
# Assinatura de tx
# ============================================================
def _verify_tx_signature(tx: dict, chain=None) -> bool:
    """Verifica assinatura(s) de uma tx não-coinbase."""
    if _is_coinbase(tx):
        return True

    inputs = tx.get("inputs") or []
    if not inputs:
        return False

    try:
        from blockchain import signing_hash as _sh
        h = _sh(tx)
    except Exception:
        return False

    for inp in inputs:
        sig_hex = inp.get("signature", "")
        pub_hex = inp.get("pubkey", "")
        if not sig_hex or not pub_hex:
            return False
        try:
            if not verify_ecdsa(bytes.fromhex(pub_hex),
                                bytes.fromhex(sig_hex), h):
                return False
        except Exception:
            return False
    return True


# ============================================================
# Auth tag HMAC
# ============================================================
def _verify_auth_tag(block: dict, chain) -> tuple:
    """
    Valida auth_tag se o nó exige.
    Só vale a partir de AUTH_TAG_START_HEIGHT (6000 no v9.1.7).
    """
    try:
        from blockchain import (AUTH_TAG_START_HEIGHT,
                                 verify_auth_tag)
    except ImportError:
        return True, ""

    require = getattr(chain, "require_auth_tag", False)
    secret = getattr(chain, "network_secret", "") or ""

    if not require or not secret:
        return True, ""

    h = int(block["height"])
    tag = block.get("auth_tag")

    if h > AUTH_TAG_START_HEIGHT:
        if not tag:
            return False, "sem auth_tag"
        if not verify_auth_tag(h, block["hash"], tag, secret):
            return False, "auth_tag invalido"
    else:
        if tag and not verify_auth_tag(h, block["hash"], tag, secret):
            return False, "auth_tag invalido em bloco legado"

    return True, ""


# ============================================================
# AUDITORIA COMPLETA
# ============================================================
def verify_chain(chain) -> VerifyResult:
    """Percorre a cadeia inteira e retorna um VerifyResult."""
    t0 = time.time()
    r = VerifyResult()

    # ---------- altura ----------
    try:
        h = chain.db.height()
    except Exception as e:
        r.valid = False
        r.errors.append(f"nao consegui ler altura: {e}")
        r.duration = time.time() - t0
        return r

    r.height = h
    if h < 0:
        r.valid = False
        r.errors.append("cadeia vazia (sem genesis)")
        r.duration = time.time() - t0
        return r

    # ---------- checkpoints ----------
    checkpoints = {}
    try:
        from checkpoints import load_checkpoints, verify_checkpoint
        checkpoints = load_checkpoints()
        r.checkpoints_checked = len(checkpoints)
    except ImportError:
        r.warnings.append("checkpoints.py nao disponivel")
        verify_checkpoint = None
    except Exception as e:
        r.warnings.append(f"erro carregando checkpoints: {e}")
        verify_checkpoint = None

    # ---------- varredura ----------
    prev = None
    for i in range(0, h + 1):
        try:
            block = chain.db.get_block(i)
        except Exception as e:
            r.valid = False
            r.errors.append(f"bloco {i}: erro lendo do DB: {e}")
            break

        if not block:
            r.valid = False
            r.errors.append(f"bloco {i} ausente no DB")
            prev = None
            continue

        r.blocks_checked += 1

        # ---------- encadeamento ----------
        if prev is not None:
            if block["prev_hash"] != prev["hash"]:
                r.valid = False
                r.errors.append(f"bloco {i}: prev_hash nao bate")
            if int(block["timestamp"]) < int(prev["timestamp"]):
                r.warnings.append(f"bloco {i}: timestamp regressivo")

        # ---------- PoW (genesis isento) ----------
        if i > 0:
            if not _verify_pow(block):
                r.valid = False
                r.errors.append(f"bloco {i}: PoW invalido")

        # ---------- merkle ----------
        if not _verify_merkle(block):
            r.valid = False
            r.errors.append(f"bloco {i}: merkle root nao bate")

        # ---------- checkpoint ----------
        cp = checkpoints.get(str(i))
        if cp and verify_checkpoint is not None:
            try:
                if not verify_checkpoint(cp):
                    r.valid = False
                    r.errors.append(f"bloco {i}: checkpoint invalido")
                elif cp.get("hash") != block.get("hash"):
                    r.valid = False
                    r.errors.append(f"bloco {i}: checkpoint hash != bloco")
            except Exception as e:
                r.warnings.append(f"bloco {i}: checkpoint erro {e}")

        # ---------- auth tag ----------
        ok_at, msg_at = _verify_auth_tag(block, chain)
        if not ok_at:
            r.valid = False
            r.errors.append(f"bloco {i}: {msg_at}")

        # ---------- transações ----------
        txs = block.get("transactions") or []
        if not txs:
            r.valid = False
            r.errors.append(f"bloco {i}: sem transacoes")
        else:
            cb = txs[0]
            if not _is_coinbase(cb):
                r.valid = False
                r.errors.append(f"bloco {i}: primeira tx nao e coinbase")
            else:
                ok_cb, msg_cb = _verify_coinbase(cb, i, chain)
                if not ok_cb:
                    r.valid = False
                    r.errors.append(f"bloco {i}: coinbase invalida — {msg_cb}")

            for tx in txs[1:]:
                if _is_coinbase(tx):
                    r.valid = False
                    r.errors.append(
                        f"bloco {i}: coinbase em posicao invalida")
                    continue
                if not _verify_tx_signature(tx, chain):
                    r.valid = False
                    tid = tx.get("txid", "?")[:16]
                    r.errors.append(
                        f"bloco {i}: assinatura invalida em tx {tid}")

        prev = block

        if len(r.errors) >= 200:
            r.errors.append("... (truncado em 200 erros)")
            break

    r.duration = time.time() - t0
    if not r.valid:
        log.warning("verify_chain: cadeia INVALIDA — %s", r.summary())
    return r


# ============================================================
# API DICT / RANGE / FORK
# ============================================================
def verify_chain_dict(chain) -> dict:
    return verify_chain(chain).to_dict()


def verify_range(chain, start: int, end: int) -> dict:
    """
    Verifica apenas blocos [start, end]. Útil para debug / sync rápido.

    [FIX v2] Agora também verifica merkle root.
    """
    r = VerifyResult()
    prev = chain.db.get_block(start - 1) if start > 0 else None
    for i in range(start, end + 1):
        b = chain.db.get_block(i)
        if not b:
            r.valid = False
            r.errors.append(f"bloco {i} ausente")
            prev = None
            continue
        r.blocks_checked += 1

        # Encadeamento
        if prev is not None and b["prev_hash"] != prev["hash"]:
            r.valid = False
            r.errors.append(f"bloco {i}: prev_hash nao bate")

        # PoW (genesis isento)
        if i > 0 and not _verify_pow(b):
            r.valid = False
            r.errors.append(f"bloco {i}: PoW invalido")

        # [FIX v2] Merkle
        if not _verify_merkle(b):
            r.valid = False
            r.errors.append(f"bloco {i}: merkle root nao bate")

        prev = b
    return r.to_dict()


def find_fork_point(chain_a, chain_b, max_depth: int = 1000) -> int:
    """
    Encontra a altura do último bloco em comum entre duas cadeias.
    Retorna -1 se não houver nenhum.
    """
    h = min(chain_a.db.height(), chain_b.db.height())
    for i in range(h, max(-1, h - max_depth), -1):
        ba = chain_a.db.get_block(i)
        bb = chain_b.db.get_block(i)
        if ba and bb and ba["hash"] == bb["hash"]:
            return i
    return -1


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "brn_v2_chain.db"
    print(f"Auditando {path}...")

    try:
        from blockchain import Blockchain
        bc = Blockchain(path, auto_genesis=False)
    except Exception as e:
        print(f"Erro ao abrir blockchain: {e}")
        sys.exit(1)

    r = verify_chain(bc)
    print(f"  height              : {r.height}")
    print(f"  blocks_checked      : {r.blocks_checked}")
    print(f"  checkpoints_checked : {r.checkpoints_checked}")
    print(f"  duration            : {r.duration:.2f}s")
    print(f"  valid               : {r.valid}")
    print(f"  summary             : {r.summary()}")
    if r.warnings:
        print(f"  warnings ({len(r.warnings)}):")
        for w in r.warnings[:5]:
            print(f"    - {w}")
    if r.errors:
        print(f"  errors ({len(r.errors)}):")
        for e in r.errors[:10]:
            print(f"    - {e}")

    sys.exit(0 if r.valid else 2)