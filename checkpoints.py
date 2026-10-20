"""checkpoints.py — Checkpoints assinados do BRN
================================================================
A cada CHECKPOINT_INTERVAL blocos, o nó ORIGEM assina o hash do bloco
com sua chave Ed25519. Todos os nós verificam a assinatura antes de
aceitar blocos naquela altura.

Efeito: um reorg não pode violar um checkpoint. Um atacante com
100x mais hashrate não consegue reescrever a cadeia — os checkpoints
são imutáveis.

Formato de checkpoints.json:
{
  "1000": {"height":1000, "hash":"...", "sig":"...", "pub":"...", "ts":123},
  "2000": {...}
}

API usada por blockchain.py v9.1.7:
    load_checkpoints() -> dict
    sign_checkpoint(height, block_hash, priv) -> dict
    verify_checkpoint(cp) -> bool
    save_checkpoints(d) -> bool

Requer:
    pip install cryptography
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
    """Lê checkpoints.json. Retorna {} se não existir ou estiver inválido."""
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
    """Grava atomicamente (tmp + rename). Retorna True se sucesso."""
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
# ASSINATURA
# ============================================================
def sign_checkpoint(height: int, block_hash: str, priv) -> dict:
    """
    Assina um checkpoint. 'priv' é Ed25519PrivateKey.
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

    try:
        sig_bytes = priv.sign(_payload(h, block_hash))
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
    """
    Verifica a assinatura Ed25519 do checkpoint.
    Retorna True se válido.
    """
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
    """
    Procura um checkpoint para `height` e verifica se bate com o bloco
    atual da cadeia. Retorna (ok, msg).
    """
    cps = load_checkpoints()
    cp = cps.get(str(height))
    if not cp:
        return True, ""  # sem checkpoint = sem restrição

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
    """
    Une dois dicts. Em conflito de altura, mantém o LOCAL.
    Útil para sincronizar checkpoints entre peers sem perder os locais.
    """
    merged = dict(remote or {})
    merged.update(local or {})
    return merged


def merge_from_file(path: str) -> int:
    """Funde um checkpoints.json externo no local. Retorna quantos adicionou."""
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
# API ALTO NÍVEL — assinatura automática em intervalos
# ============================================================
class CheckpointSigner:
    """
    Helper que assina um checkpoint a cada CHECKPOINT_INTERVAL blocos.
    Chame .maybe_sign(block, priv) a cada bloco aceito.
    """

    def __init__(self, priv=None, interval: int = CHECKPOINT_INTERVAL):
        self.priv = priv
        self.interval = interval
        self._cache: dict = load_checkpoints()

    def set_priv(self, priv) -> None:
        self.priv = priv

    def maybe_sign(self, block: dict) -> bool:
        """Assina se a altura for múltipla do intervalo. Retorna True se assinou."""
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
            # já assinado
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
        print("Testando checkpoints...")
        with tempfile.TemporaryDirectory() as td:
            # aponta arquivo temporário
            globals()["CHECKPOINT_FILE"] = os.path.join(td, "cps.json")

            # 1. Cria chave e assina
            priv = Ed25519PrivateKey.generate()
            cp = sign_checkpoint(1000, "abc123" * 8, priv)
            assert cp, "sign_checkpoint falhou"
            assert cp["height"] == 1000
            assert cp["hash"] == "abc123" * 8
            assert "sig" in cp and "pub" in cp
            print("  [ok] sign_checkpoint")

            # 2. Verifica
            assert verify_checkpoint(cp), "verify_checkpoint deveria passar"
            print("  [ok] verify_checkpoint (valido)")

            # 3. Adulterar hash
            bad = dict(cp)
            bad["hash"] = "deadbeef" * 8
            assert not verify_checkpoint(bad), "checkpoint adulterado deveria falhar"
            print("  [ok] verify_checkpoint rejeita adulteracao")

            # 4. Save/load
            d = {"1000": cp}
            assert save_checkpoints(d), "save_checkpoints falhou"
            loaded = load_checkpoints()
            assert loaded == d, f"load_checkpoints retornou {loaded}"
            print("  [ok] save/load")

            # 5. Merge
            cp2 = sign_checkpoint(2000, "abc999" * 8, priv)
            merged = merge_checkpoints({"1000": cp}, {"2000": cp2, "1000": {"x": 1}})
            assert "2000" in merged
            assert merged["1000"] == cp  # local venceu
            print("  [ok] merge_checkpoints")

            # 6. CheckpointSigner
            signer = CheckpointSigner(priv, interval=100)
            assert signer.maybe_sign({"height": 100, "hash": "h1"})
            assert not signer.maybe_sign({"height": 100, "hash": "h1"})  # já assinado
            assert not signer.maybe_sign({"height": 50, "hash": "h2"})   # não múltiplo
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
        print(f"Uso: python checkpoints.py [test|show]")