"""chain_validator.py — Auditoria completa da cadeia BRN (v2.2)
================================================================
v2.2 (2026-10-27):
  [NEW] Verificação de validators_sig:
        - Modo default (strict_validators=False): cada assinatura
          em block["validators_sig"] é verificada criptograficamente
          (Ed25519 sobre BLOCK_SIG_DOMAIN || hash). Não exige count
          específico, porque trust_net é dinâmico e não há snapshot
          histórico do set de validadores.
        - Modo strict_validators=True: exige count >= required do
          set atual E cada pubkey no set atual. Use quando souber
          que o conjunto não mudou durante a janela auditada.
  [NEW] verify_chain(chain, strict_validators=False) e
        verify_range(chain, start, end, strict_validators=False)
        aceitam o parâmetro.
  [NEW] VerifyResult expõe validators_checked e strict_validators.
  [NEW] Aviso quando validators_sig vazio mas trust_net tem
        validadores (potencial fork/regressão, não erro estrutural).
  [KEEP] v2.1: DB vazio não é cadeia inválida (warning).
  [KEEP] _verify_pow usa meets_difficulty() do blockchain.py.
  [KEEP] verify_range verifica merkle.
  [KEEP] Compatível com Blockchain.verify_chain / verify_chain_dict
         já plugados no blockchain.py.
================================================================
"""
from __future__ import annotations

import hashlib
import logging
import time

from crypto import verify_ecdsa, sha256, double_sha256

log = logging.getLogger("chain_validator")

__version__ = "2.2"


# ============================================================
# RESULTADO
# ============================================================
class VerifyResult:
    def __init__(self):
        self.valid = True
        self.blocks_checked = 0
        self.checkpoints_checked = 0
        self.validators_checked = 0          # [NEW v2.2]
        self.strict_validators = False       # [NEW v2.2]
        self.warnings = []
        self.errors = []
        self.height = -1
        self.duration = 0.0

    def summary(self) -> str:
        if self.valid:
            if self.height < 0:
                return "cadeia vazia (aguardando genesis)"
            extra = f" · {self.validators_checked} bloco(s) com sigs" if self.validators_checked else ""
            return (f"cadeia OK · {self.blocks_checked} blocos · "
                    f"{self.checkpoints_checked} checkpoints · "
                    f"{self.duration:.2f}s{extra}")
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
            "validators_checked":   self.validators_checked,
            "strict_validators":    self.strict_validators,
            "warnings":             list(self.warnings),
            "errors":               list(self.errors),
            "duration_s":           round(self.duration, 3),
        }


# ============================================================
# HELPERS BÁSICOS
# ============================================================
def _is_coinbase(tx: dict) -> bool:
    if tx.get("type") == "coinbase" or tx.get("is_coinbase"):
        return True
    ins = tx.get("inputs") or []
    return bool(ins and ins[0].get("txid") == "0" * 64)


def _target_from_difficulty(difficulty: int) -> int:
    return int("0" * int(difficulty) + "f" * (64 - int(difficulty)), 16)


def _meets_difficulty(h_hex: str, difficulty: int) -> bool:
    try:
        return int(h_hex, 16) <= _target_from_difficulty(difficulty)
    except Exception:
        return False


def _verify_pow(block: dict) -> bool:
    try:
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
    try:
        from blockchain import (AUTH_TAG_START_HEIGHT, verify_auth_tag)
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
# [NEW v2.2] Assinaturas de validador do bloco
# ============================================================
def _get_chain_validators(chain) -> list:
    """
    Obtém o set de validadores ativos da chain, se disponível.
    Retorna [] se trust_net ausente ou vazio → modo bootstrap.
    """
    fn = getattr(chain, "_get_active_validators", None)
    if callable(fn):
        try:
            return list(fn() or [])
        except Exception as e:
            log.debug(f"_get_active_validators falhou: {e}")
            return []
    return []


def _required_sigs_for(chain, validators: list) -> int:
    """Quanto o bloco precisa ter, segundo a config da chain."""
    if not validators:
        return 0
    fn = getattr(chain, "_required_signatures", None)
    if callable(fn):
        try:
            return int(fn(validators))
        except Exception:
            pass
    # Fallback: espelha o default do blockchain.py
    import os
    if os.environ.get("BRN_REQUIRE_ALL_SIGS", "1") == "1":
        return len(validators)
    try:
        q = float(os.environ.get("BRN_VALIDATOR_QUORUM", "0.67"))
    except ValueError:
        q = 0.67
    import math
    return max(1, min(len(validators), math.ceil(len(validators) * q)))


def _verify_block_sig(pubkey_hex: str, block: dict, sig_hex: str) -> bool:
    """
    Verifica Ed25519 sobre BLOCK_SIG_DOMAIN || hash.
    Usa a mesma função do blockchain.py (compatibilidade total).
    """
    try:
        from blockchain import verify_block_signature
        return bool(verify_block_signature(pubkey_hex, block, sig_hex))
    except ImportError:
        # Fallback mínimo: se blockchain.py não importar, falha fechado
        return False


def _verify_validator_sigs(
    block: dict, chain, strict: bool = False
) -> tuple:
    """
    Verifica block["validators_sig"].

    strict=False (default):
        - Cada assinatura é Ed25519 válida sobre o hash do bloco.
        - Não exige count mínimo (trust_net é dinâmico).
        - Não exige que pubkey esteja no top-5 atual (pode ter mudado).

    strict=True:
        - Exige count >= _required_signatures(current_validators).
        - Exige cada pubkey no set atual.

    Retorna (ok, msg, count_validas).
    Blocos antigos (sem campo) → OK com count=0.
    """
    sigs = block.get("validators_sig")
    if not isinstance(sigs, list):
        return False, "validators_sig nao e lista", 0

    if not sigs:
        # Sem assinaturas: aceitar estruturalmente.
        # Aviso (não erro) é emitido pelo chamador se trust_net tem validadores.
        return True, "", 0

    # Deduplica por pubkey
    seen: set = set()
    valid_count = 0
    for entry in sigs:
        if not isinstance(entry, dict):
            return False, "entrada de validators_sig nao e dict", valid_count
        pubkey = (entry.get("pubkey") or "").strip()
        signature = (entry.get("signature") or "").strip()
        if not pubkey or not signature:
            return False, "entry sem pubkey/signature", valid_count
        if pubkey in seen:
            continue
        seen.add(pubkey)
        if _verify_block_sig(pubkey, block, signature):
            valid_count += 1
        else:
            # Com strict=True: erro. Com strict=False: erro também —
            # assinatura inválida é sempre corrupção/ataque.
            return (
                False,
                f"assinatura invalida de {pubkey[:16]} em #{block.get('height')}",
                valid_count,
            )

    if strict:
        validators = _get_chain_validators(chain)
        if validators:
            vset = set(validators)
            not_in_set = [p for p in seen if p not in vset]
            if not_in_set:
                return (
                    False,
                    f"pubkey fora do set atual em #{block.get('height')}: "
                    f"{', '.join(p[:12] for p in not_in_set[:3])}",
                    valid_count,
                )
            required = _required_sigs_for(chain, validators)
            if valid_count < required:
                return (
                    False,
                    f"#{block.get('height')}: {valid_count}/{required} "
                    f"assinaturas (strict)",
                    valid_count,
                )

    return True, "", valid_count


# ============================================================
# AUDITORIA COMPLETA
# ============================================================
def verify_chain(chain, strict_validators: bool = False) -> VerifyResult:
    t0 = time.time()
    r = VerifyResult()
    r.strict_validators = strict_validators

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
        # DB vazio não é CADEIA INVÁLIDA. É cliente aguardando genesis.
        r.warnings.append("Banco não contém blocos (aguardando genesis)")
        r.duration = time.time() - t0
        return r

    # ---------- checkpoints ----------
    checkpoints = {}
    verify_checkpoint = None
    try:
        from checkpoints import load_checkpoints, verify_checkpoint as _vc
        checkpoints = load_checkpoints()
        verify_checkpoint = _vc
        r.checkpoints_checked = len(checkpoints)
    except ImportError:
        r.warnings.append("checkpoints.py nao disponivel")
    except Exception as e:
        r.warnings.append(f"erro carregando checkpoints: {e}")

    # ---------- validadores ativos (para warning) ----------
    active_validators = _get_chain_validators(chain)

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

        # ---------- validators_sig [NEW v2.2] ----------
        ok_vs, msg_vs, count_vs = _verify_validator_sigs(
            block, chain, strict=strict_validators
        )
        if count_vs > 0:
            r.validators_checked += 1
        if not ok_vs:
            r.valid = False
            r.errors.append(f"bloco {i}: {msg_vs}")
        elif count_vs == 0 and active_validators and i > 0:
            # Bloco sem sigs mas a chain TEM validadores ativos.
            # Não é erro estrutural (pode ser bloco legado pré-v9.4.0,
            # ou bloco bootstrap), mas é sinal de alerta.
            r.warnings.append(
                f"bloco {i}: validators_sig vazio, mas trust_net tem "
                f"{len(active_validators)} validador(es) ativo(s)"
            )

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
                    r.errors.append(f"bloco {i}: coinbase em posicao invalida")
                    continue
                if not _verify_tx_signature(tx, chain):
                    r.valid = False
                    tid = tx.get("txid", "?")[:16]
                    r.errors.append(f"bloco {i}: assinatura invalida em tx {tid}")

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
def verify_chain_dict(chain, strict_validators: bool = False) -> dict:
    return verify_chain(chain, strict_validators=strict_validators).to_dict()


def verify_range(
    chain, start: int, end: int, strict_validators: bool = False
) -> dict:
    r = VerifyResult()
    r.strict_validators = strict_validators
    prev = chain.db.get_block(start - 1) if start > 0 else None
    for i in range(start, end + 1):
        b = chain.db.get_block(i)
        if not b:
            r.valid = False
            r.errors.append(f"bloco {i} ausente")
            prev = None
            continue
        r.blocks_checked += 1

        if prev is not None and b["prev_hash"] != prev["hash"]:
            r.valid = False
            r.errors.append(f"bloco {i}: prev_hash nao bate")

        if i > 0 and not _verify_pow(b):
            r.valid = False
            r.errors.append(f"bloco {i}: PoW invalido")

        if not _verify_merkle(b):
            r.valid = False
            r.errors.append(f"bloco {i}: merkle root nao bate")

        # [NEW v2.2] validators_sig
        ok_vs, msg_vs, count_vs = _verify_validator_sigs(
            b, chain, strict=strict_validators
        )
        if count_vs > 0:
            r.validators_checked += 1
        if not ok_vs:
            r.valid = False
            r.errors.append(f"bloco {i}: {msg_vs}")

        prev = b
    return r.to_dict()


def find_fork_point(chain_a, chain_b, max_depth: int = 1000) -> int:
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
    strict = "--strict" in sys.argv

    print(f"Auditando {path}... (strict_validators={strict})")

    try:
        from blockchain import Blockchain
        bc = Blockchain(path, auto_genesis=False)
    except Exception as e:
        print(f"Erro ao abrir blockchain: {e}")
        sys.exit(1)

    r = verify_chain(bc, strict_validators=strict)
    print(f"  height              : {r.height}")
    print(f"  blocks_checked      : {r.blocks_checked}")
    print(f"  checkpoints_checked : {r.checkpoints_checked}")
    print(f"  validators_checked  : {r.validators_checked}")
    print(f"  strict_validators   : {r.strict_validators}")
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
