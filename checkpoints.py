"""checkpoints.py — Checkpoints assinados do BRN (v1.1)
================================================================
v1.1 (2026-11-04):
  [FIX CRÍTICO] sign_checkpoint() agora detecta se 'priv' é Wallet
                (hex strings) ou Ed25519PrivateKey (bytes) e usa a
                interface correta. Antes, main.py passava uma Wallet
                (validator_sk) e a chamada priv.public_key() falhava
                com "'str' object is not callable" — porque Wallet
                expõe pub_hex como ATRIBUTO string, não como método.
                Resultado: checkpoint #4000 e #4001 nunca eram
                assinados ("sign_checkpoint retornou vazio").
v1.0:
  Versão inicial.
================================================================
"""
from __future__ import annotations

import os
import json
import time
import logging
import threading

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


log = logging.getLogger("checkpoints")

# ============================================================
# CONSTANTES
# ============================================================
CHECKPOINT_FILE     = os.environ.get("BRN_CHECKPOINT_FILE", "checkpoints.json")
CHECKPOINT_DOMAIN   = b"BRN-CHECKPOINT-v1|"
CHECKPOINT_INTERVAL = int(os.environ.get("BRN_CHECKPOINT_INTERVAL", "1000"))

_lock = threading.RLock()


# ============================================================
# PAYLOAD CANÔNICO
# ============================================================
def _payload(height: int, block_hash: str) -> bytes:
    """Mensagem assinada: domínio || height || ':' || hash."""
    return CHECKPOINT_DOMAIN + f"{int(height)}:{block_hash}".encode("utf-8")


# ============================================================
# LEITURA / ESCRITA
# ============================================================
def load_checkpoints() -> dict:
    if not os.path.exists(CHECKPOINT_FILE):
        return {}
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.warning(f"{CHECKPOINT_FILE} nao contem dict — ignorando")
            return {}
        return data
    except Exception as e:
        log.warning(f"erro lendo {CHECKPOINT_FILE}: {e}")
        return {}


def save_checkpoints(d: dict) -> bool:
    if not isinstance(d, dict):
        return False
    with _lock:
        try:
            tmp = CHECKPOINT_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CHECKPOINT_FILE)
            return True
        except Exception as e:
            log.error(f"erro ao salvar {CHECKPOINT_FILE}: {e}")
            return False


# ============================================================
# ASSINATURA  ← [FIX v1.1] detecta interface
# ============================================================
def sign_checkpoint(height: int, block_hash: str, priv) -> dict:
    """Assina um checkpoint. Aceita duas interfaces:

      - Wallet (BRN): priv.sign(msg) → str hex;
                       priv.pub_hex → str hex (atributo)
      - Ed25519PrivateKey (cryptography): priv.sign(msg) → bytes;
                       priv.public_key().public_bytes_raw() → bytes

    Retorna dict vazio se priv for None ou assinatura falhar.
    """
    if priv is None:
        return {}
    if not isinstance(block_hash, str) or not block_hash:
        return {}
    try:
        h = int(height)
    except (ValueError, TypeError):
        return {}

    payload = _payload(h, block_hash)

    try:
        # ─── [v1.1] Detecta interface pela PRESENÇA de pub_hex ───
        # Wallet tem pub_hex como atributo string; Ed25519PrivateKey não.
        pub_hex_attr = getattr(priv, "pub_hex", None)
        if isinstance(pub_hex_attr, str) and pub_hex_attr:
            # ─── Interface Wallet ───
            sig = priv.sign(payload)
            if not isinstance(sig, str):
                # Se retornou bytes por engano, converte
                try:
                    sig = sig.hex()
                except Exception:
                    return {}
            return {
                "height": h,
                "hash":   block_hash,
                "sig":    sig,
                "pub":    pub_hex_attr.lower(),
                "ts":     int(time.time()),
            }

        # ─── Interface Ed25519PrivateKey (cryptography) ───
        sig_bytes = priv.sign(payload)
        pub_bytes = priv.public_key().public_bytes_raw()
        return {
            "height": h,
            "hash":   block_hash,
            "sig":    sig_bytes.hex(),
            "pub":    pub_bytes.hex(),
            "ts":     int(time.time()),
        }

    except Exception as e:
        log.error(f"erro ao assinar checkpoint #{h}: {e}")
        return {}


# ============================================================
# VERIFICAÇÃO
# ============================================================
def verify_checkpoint(cp: dict) -> bool:
    if not isinstance(cp, dict):
        return False
    try:
        height = int(cp["height"])
        block_hash = str(cp["hash"])
        sig = bytes.fromhex(cp["sig"])
        pub = bytes.fromhex(cp["pub"])
    except Exception:
        return False

    if len(pub) != 32 or len(sig) != 64:
        return False

    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(
            sig, _payload(height, block_hash)
        )
        return True
    except Exception:
        return False


def verify_checkpoint_for(chain, height: int) -> tuple:
    cps = load_checkpoints()
    cp = cps.get(str(height))
    if not cp:
        return True, ""

    if not verify_checkpoint(cp):
        return False, f"checkpoint #{height} assinatura invalida"

    try:
        blk = chain.db.get_block(height)
    except Exception as e:
        return False, f"nao consegui ler bloco #{height}: {e}"

    if not blk:
        return False, f"bloco #{height} ausente"

    if blk["hash"] != cp["hash"]:
        return False, (f"checkpoint #{height} hash diverge "
                       f"(esperado {cp['hash'][:16]}..., "
                       f"bloco {blk['hash'][:16]}...)")

    return True, ""


# ============================================================
# MESCLAGEM
# ============================================================
def merge_checkpoints(local: dict, remote: dict) -> dict:
    merged = dict(remote or {})
    merged.update(local or {})
    return merged


def merge_from_file(path: str) -> int:
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            remote = json.load(f)
    except Exception as e:
        log.warning(f"erro lendo {path}: {e}")
        return 0
    if not isinstance(remote, dict):
        return 0

    local = load_checkpoints()
    before = set(local.keys())
    merged = merge_checkpoints(local, remote)
    after = set(merged.keys())
    added = len(after - before)

    if added > 0:
        save_checkpoints(merged)
        log.info(f"merge: {added} checkpoint(s) adicionados de {path}")
    return added


# ============================================================
# API ALTO NÍVEL
# ============================================================
class CheckpointSigner:
    def __init__(self, priv=None, interval: int = CHECKPOINT_INTERVAL):
        self.priv = priv
        self.interval = interval
        self._cache: dict = load_checkpoints()

    def set_priv(self, priv) -> None:
        self.priv = priv

    def maybe_sign(self, block: dict) -> bool:
        if self.priv is None:
            return False
        try:
            h = int(block["height"])
        except Exception:
            return False
        if h == 0 or h % self.interval != 0:
            return False

        h_str = str(h)
        if h_str in self._cache:
            return False

        cp = sign_checkpoint(h, block["hash"], self.priv)
        if not cp:
            return False

        self._cache[h_str] = cp
        if save_checkpoints(self._cache):
            log.info(f"[CHECKPOINT] #{h} assinado e salvo")
            return True
        return False

    def get_all(self) -> dict:
        return dict(self._cache)

    def reload(self) -> None:
        self._cache = load_checkpoints()


# ============================================================
# CLI / AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import sys
    import tempfile

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"

    if cmd == "test":
        print("Testando checkpoints (v1.1)...")
        with tempfile.TemporaryDirectory() as td:
            globals()["CHECKPOINT_FILE"] = os.path.join(td, "cps.json")

            # 1. Ed25519PrivateKey (interface cryptography)
            priv = Ed25519PrivateKey.generate()
            cp = sign_checkpoint(1000, "abc123" * 8, priv)
            assert cp, "sign_checkpoint falhou com Ed25519PrivateKey"
            assert verify_checkpoint(cp), "verify falhou com Ed25519PrivateKey"
            print("  [ok] Ed25519PrivateKey (bytes)")

            # 2. [v1.1] Mock Wallet (interface hex)
            class MockWallet:
                def __init__(self):
                    self._priv = Ed25519PrivateKey.generate()
                    self.pub_hex = self._priv.public_key().public_bytes_raw().hex()
                def sign(self, msg):
                    return self._priv.sign(msg).hex()

            wallet = MockWallet()
            cp2 = sign_checkpoint(2000, "def456" * 8, wallet)
            assert cp2, "sign_checkpoint falhou com Wallet"
            assert cp2["sig"] == wallet.sign(_payload(2000, "def456" * 8))
            assert cp2["pub"] == wallet.pub_hex
            assert verify_checkpoint(cp2), "verify falhou com Wallet"
            print("  [ok] Wallet (hex)")

            # 3. Adulterar
            bad = dict(cp2)
            bad["hash"] = "deadbeef" * 8
            assert not verify_checkpoint(bad), "aceitou checkpoint adulterado"
            print("  [ok] rejeita adulteracao")

            # 4. Save/load
            d = {"1000": cp, "2000": cp2}
            assert save_checkpoints(d), "save falhou"
            loaded = load_checkpoints()
            assert loaded == d, "load retornou diferente"
            print("  [ok] save/load")

            # 5. CheckpointSigner
            signer = CheckpointSigner(wallet, interval=100)
            assert signer.maybe_sign({"height": 100, "hash": "h1"})
            assert not signer.maybe_sign({"height": 100, "hash": "h1"})
            assert not signer.maybe_sign({"height": 50, "hash": "h2"})
            assert signer.maybe_sign({"height": 200, "hash": "h2"})
            print("  [ok] CheckpointSigner")

        print("Todos os testes passaram.")

    elif cmd == "show":
        cps = load_checkpoints()
        print(f"Checkpoints em {CHECKPOINT_FILE}: {len(cps)}")
        for h in sorted(cps.keys(), key=lambda x: int(x))[:20]:
            cp = cps[h]
            ok = verify_checkpoint(cp)
            marker = "OK" if ok else "X "
            print(f"  [{marker}] #{h}  {cp.get('hash','?')[:24]}...")

    else:
        print("Uso: python checkpoints.py [test|show]")
