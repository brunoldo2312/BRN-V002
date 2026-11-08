"""secure_store.py — Armazenamento cifrado local do BRN
================================================================
Formato do blob:
    MAGIC(4) || VERSION(1) || SALT(16) || NONCE(12) || CIPHERTEXT+TAG

KDF  : Argon2id
AEAD : ChaCha20-Poly1305
AAD  : varia por tipo de arquivo (wallet / node_identity / current_wallet)

Assinaturas públicas (ordem dos argumentos — LER COM ATENÇÃO):

    # Carteiras "avulsas" (data primeiro):
    save_wallet(data: dict, password: str, path: str = "wallet_encrypted.dat") -> bool
    load_wallet(password: str, path: str = "wallet_encrypted.dat") -> dict | None

    # Identidade do nó (data primeiro, mesma convenção):
    save_node_identity(data: dict, password: str, path: str = "node_identity.enc") -> bool
    load_node_identity(password: str, path: str = "node_identity.enc") -> dict | None

    # Carteira ATIVA (duas ordens aceitas — FIX 2026-11-08, ver abaixo):
    save_current_wallet(password: str, data: dict) -> dict
    save_current_wallet(data: dict, password: str) -> dict   # compat
    load_current_wallet(password: str) -> dict

[FIX 2026-11-06]:
  - A carteira ativa agora é salva na RAIZ e na pasta wallets/ simultaneamente.
  - Leitura tenta na raiz, com fallback para a pasta wallets/.

[FIX 2026-11-08] (Bug 4 — auditoria):
  - save_current_wallet tinha a assinatura INVERTIDA em relação a
    save_wallet/save_node_identity (password antes de data). Quem
    copiava o padrão do bloco "Carteiras" do docstring caía em
    `ValueError: senha deve ser string` em produção.
  - Agora aceita AMBAS as ordens por introspecção. Mantém 100% de
    compatibilidade com chamadas antigas (password, data) e passa a
    aceitar também (data, password), alinhando com save_wallet.
  - load_current_wallet não foi alterado (só um argumento: password).

Uso típico:

    from secure_store import (
        load_current_wallet, save_current_wallet,
    )

    # Ler (sempre password):
    w = load_current_wallet(BRN_WALLET_SESSION_PASS)
    if not w.get("ok"):
        raise RuntimeError(w["msg"])

    # Salvar — as duas formas abaixo são equivalentes:
    save_current_wallet(password, w)
    save_current_wallet(w, password)
================================================================
"""
from __future__ import annotations

import os
import json
import hmac
import time
import logging
import threading
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.exceptions import InvalidTag

try:
    from argon2.low_level import hash_secret_raw, Type
    _ARGON2_IMPL = "cffi"
except ImportError:
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
    _ARGON2_IMPL = "cryptography"


log = logging.getLogger("secure_store")

MAGIC           = b"BRNS"
FORMAT_VERSION  = 1
SALT_SIZE       = 16
NONCE_SIZE      = 12
TAG_SIZE        = 16
KEY_SIZE        = 32
MIN_PASSWORD_LEN = 8

ARGON2_TIME    = int(os.environ.get("BRN_ARGON2_TIME", "3"))
ARGON2_MEMORY  = int(os.environ.get("BRN_ARGON2_MEMORY", "65536"))
ARGON2_PAR     = int(os.environ.get("BRN_ARGON2_PARALLEL", "4"))

# ============================================================
# CAMINHOS DA CARTEIRA ATIVA (RAIZ + WALLETS)
# ============================================================
_BASE_DIR = Path(__file__).resolve().parent
_WALLET_SUBDIR = _BASE_DIR / "wallets"

# Cria a pasta wallets se não existir
_WALLET_SUBDIR.mkdir(parents=True, exist_ok=True)

# Caminhos absolutos para evitar problemas de CWD
_DEFAULT_WALLET_ROOT = _BASE_DIR / "current_wallet.enc"
_DEFAULT_WALLET_SUB  = _WALLET_SUBDIR / "current_wallet.enc"

# Se a env BRN_CURRENT_WALLET estiver setada, ela tem prioridade (compatibilidade)
# Mas na ausência dela, usamos a raiz.
_env_wallet = os.environ.get("BRN_CURRENT_WALLET")
CURRENT_WALLET_ENC = _env_wallet if _env_wallet else str(_DEFAULT_WALLET_ROOT)
CURRENT_WALLET_JSON = str(_BASE_DIR / "current_wallet.json")


# ============================================================
# KDF / AEAD
# ============================================================
def _derive_key(password: str, salt: bytes) -> bytes:
    if _ARGON2_IMPL == "cffi":
        return hash_secret_raw(
            secret=password.encode("utf-8"), salt=salt,
            time_cost=ARGON2_TIME, memory_cost=ARGON2_MEMORY,
            parallelism=ARGON2_PAR, hash_len=KEY_SIZE, type=Type.ID,
        )
    kdf = Argon2id(salt=salt, length=KEY_SIZE, iterations=ARGON2_TIME,
                   lanes=ARGON2_PAR, memory_cost=ARGON2_MEMORY)
    return kdf.derive(password.encode("utf-8"))


def _validate_password(p) -> None:
    if not isinstance(p, str):
        raise ValueError("senha deve ser string")
    if len(p) < MIN_PASSWORD_LEN:
        raise ValueError(f"senha muito curta (min {MIN_PASSWORD_LEN})")


def _encrypt(data: bytes, key: bytes, aad: bytes = b"") -> bytes:
    nonce = os.urandom(NONCE_SIZE)
    ct = ChaCha20Poly1305(key).encrypt(nonce, data, aad)
    return nonce + ct


def _decrypt(packed: bytes, key: bytes, aad: bytes = b"") -> bytes:
    if len(packed) < NONCE_SIZE + TAG_SIZE:
        raise ValueError("blob curto demais")
    nonce, ct = packed[:NONCE_SIZE], packed[NONCE_SIZE:]
    return ChaCha20Poly1305(key).decrypt(nonce, ct, aad)


# ============================================================
# I/O ATÔMICO
# ============================================================
def _atomic_write(path, data: bytes, mode: int = 0o600) -> None:
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


# ============================================================
# API "BLOB" (bytes → blob, para uso genérico)
# ============================================================
def encrypt_blob(data: bytes, password: str) -> bytes:
    _validate_password(password)
    salt = os.urandom(SALT_SIZE)
    key = _derive_key(password, salt)
    return MAGIC + bytes([FORMAT_VERSION]) + salt + _encrypt(data, key, b"blob")


def decrypt_blob(blob: bytes, password: str) -> bytes:
    if len(blob) < len(MAGIC) + 1 + SALT_SIZE + NONCE_SIZE + TAG_SIZE:
        raise ValueError("blob truncado")
    if blob[:4] != MAGIC:
        raise ValueError("magic invalido")
    ver = blob[4]
    if ver == 49:
        ver = 1
    if ver != FORMAT_VERSION:
        raise ValueError(f"versao nao suportada: {ver}")
    off = 5
    salt = blob[off:off + SALT_SIZE]; off += SALT_SIZE
    key = _derive_key(password, salt)
    return _decrypt(blob[off:], key, b"blob")


# ============================================================
# HELPERS INTERNOS DE ARQUIVO
# ============================================================
def _save_blob(path: str, data: dict, password: str, aad: bytes) -> bool:
    _validate_password(password)
    salt = os.urandom(SALT_SIZE)
    key = _derive_key(password, salt)
    pt = json.dumps(data, ensure_ascii=False,
                    separators=(",", ":")).encode("utf-8")
    blob = MAGIC + bytes([FORMAT_VERSION]) + salt + _encrypt(pt, key, aad)
    _atomic_write(path, blob, mode=0o600)
    return True


def _load_blob(path: str, password: str, aad: bytes):
    p = Path(path)
    if not p.exists():
        return None
    raw = p.read_bytes()
    min_size = len(MAGIC) + 1 + SALT_SIZE + NONCE_SIZE + TAG_SIZE
    if len(raw) < min_size:
        raise ValueError(f"arquivo {path} truncado")
    if raw[:4] != MAGIC:
        raise ValueError(f"arquivo {path} nao e um blob BRN")
    version = raw[4]
    if version == 49:
        version = 1
    if version != FORMAT_VERSION:
        raise ValueError(f"versao nao suportada: {version}")
    off = 5
    salt = raw[off:off + SALT_SIZE]; off += SALT_SIZE
    key = _derive_key(password, salt)
    try:
        decrypted = _decrypt(raw[off:], key, aad)
    except InvalidTag:
        raise ValueError("Senha incorreta ou arquivo corrompido")
    except Exception as e:
        raise ValueError(f"Falha ao decifrar: {e}")
    try:
        return json.loads(decrypted.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Conteudo invalido: {e}")


# ============================================================
# CARTEIRA AVULSA  (data, password)  — ordem ORIGINAL
# ============================================================
def save_wallet(data: dict, password: str,
                path: str = "wallet_encrypted.dat") -> bool:
    return _save_blob(path, data, password, aad=b"wallet")


def load_wallet(password: str, path: str = "wallet_encrypted.dat"):
    return _load_blob(path, password, aad=b"wallet")


# ============================================================
# IDENTIDADE DO NÓ  (data, password)  — ordem ORIGINAL
# ============================================================
def save_node_identity(data: dict, password: str,
                       path: str = "node_identity.enc") -> bool:
    return _save_blob(path, data, password, aad=b"node_identity")


def load_node_identity(password: str, path: str = "node_identity.enc"):
    return _load_blob(path, password, aad=b"node_identity")


def delete_identity(path: str = "node_identity.enc", secure: bool = True) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    if secure:
        try:
            size = p.stat().st_size
            with p.open("r+b") as f:
                f.write(os.urandom(size)); f.flush(); os.fsync(f.fileno())
        except Exception as e:
            log.warning(f"overwrite de {path} falhou: {e}")
    p.unlink()
    return True


# ============================================================
# CARTEIRA ATIVA  (duas ordens aceitas — FIX v2026-11-08)
# ============================================================
def save_current_wallet(arg1, arg2) -> dict:
    """
    Salva a carteira ativa em DOIS locais (raiz + wallets/).

    Aceita AMBAS as ordens por compatibilidade:

        save_current_wallet(password, data)   # ordem antiga
        save_current_wallet(data, password)   # ordem nova (= save_wallet)

    Retorna dict:
        {"ok": True,  "path": "...", "backup": "..."}
        {"ok": False, "msg": "<motivo>"}
    """
    # [FIX Bug 4] Introspecção leve — não custa nada em runtime.
    if isinstance(arg1, dict) and isinstance(arg2, str):
        data, password = arg1, arg2
    elif isinstance(arg1, str) and isinstance(arg2, dict):
        password, data = arg1, arg2
    else:
        return {
            "ok": False,
            "msg": (
                "argumentos inválidos: esperado (password, data) ou "
                f"(data, password); recebido ({type(arg1).__name__}, "
                f"{type(arg2).__name__})"
            ),
        }

    try:
        # 1. Salva na raiz (onde o main.py procura)
        _save_blob(CURRENT_WALLET_ENC, data, password, b"current_wallet")

        # 2. Salva na pasta wallets/ (backup automático)
        _save_blob(str(_DEFAULT_WALLET_SUB), data, password, b"current_wallet")

        return {
            "ok": True,
            "path": CURRENT_WALLET_ENC,
            "backup": str(_DEFAULT_WALLET_SUB),
        }
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def load_current_wallet(password: str) -> dict:
    """
    Lê a carteira ativa.

    Ordem de tentativa:
        1. CURRENT_WALLET_ENC (raiz, ou BRN_CURRENT_WALLET se setado)
        2. wallets/current_wallet.enc (backup)

    Retorna dict sempre no formato {"ok": bool, ...}:
        {"ok": True,  **data}
        {"ok": False, "msg": "<motivo>"}
    """
    try:
        # 1. Tenta ler da raiz
        data = _load_blob(CURRENT_WALLET_ENC, password, b"current_wallet")

        # 2. Se não achou na raiz, tenta na pasta wallets/
        if data is None:
            data = _load_blob(str(_DEFAULT_WALLET_SUB), password, b"current_wallet")

        if data is None:
            return {
                "ok": False,
                "msg": "current_wallet.enc nao existe (nem na raiz nem em wallets/)",
            }
        return {"ok": True, **data}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def migrate_current_wallet(password: str) -> dict:
    """Migra `current_wallet.json` (plaintext legado) → `current_wallet.enc`."""
    if not os.path.exists(CURRENT_WALLET_JSON):
        return {"ok": False, "msg": "current_wallet.json nao existe"}
    try:
        with open(CURRENT_WALLET_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        r = save_current_wallet(password, data)
        if not r.get("ok"):
            return r
        os.rename(CURRENT_WALLET_JSON, CURRENT_WALLET_JSON + ".migrated")
        return {"ok": True, "msg": "migrado", "path": CURRENT_WALLET_ENC}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


# ============================================================
# UTILITÁRIOS
# ============================================================
def constant_time_eq(a, b) -> bool:
    if isinstance(a, str): a = a.encode()
    if isinstance(b, str): b = b.encode()
    return hmac.compare_digest(a, b)


def is_encrypted_file(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except Exception:
        return False


def reencrypt_file(path: str, old_password: str, new_password: str) -> bool:
    data = _load_blob(path, old_password, aad=b"wallet")
    if data is None:
        raise FileNotFoundError(path)
    _save_blob(path, data, new_password, aad=b"wallet")
    return True


# ============================================================
# SESSÃO DE CARTEIRA EM MEMÓRIA
# ============================================================
class WalletSession:
    def __init__(self, ttl: int = 15 * 60):
        self.ttl = ttl
        self._data = None
        self._unlocked_at = 0.0
        self._lock = threading.RLock()

    def unlock(self, data: dict) -> None:
        with self._lock:
            self._data = dict(data)
            self._unlocked_at = time.time()

    def lock(self) -> None:
        with self._lock:
            if self._data:
                for k in list(self._data.keys()):
                    try:
                        del self._data[k]
                    except Exception:
                        pass
            self._data = None
            self._unlocked_at = 0.0

    def get(self):
        with self._lock:
            if self._data is None:
                return None
            if time.time() - self._unlocked_at > self.ttl:
                self.lock()
                return None
            return dict(self._data)

    def seconds_until_lock(self) -> int:
        with self._lock:
            if self._data is None:
                return 0
            elapsed = time.time() - self._unlocked_at
            return max(0, int(self.ttl - elapsed))

    def is_unlocked(self) -> bool:
        return self.get() is not None


# ============================================================
# SELF-TEST  (roda com: python secure_store.py)
# ============================================================
def _self_test() -> int:
    """Valida o fix do Bug 4 (ambas as ordens) + round-trip completo."""
    import tempfile, shutil

    print("=" * 64)
    print(" secure_store.py — self-test (inclui regressão do Bug 4)")
    print("=" * 64)

    # 0. Round-trip do blob genérico
    blob = encrypt_blob(b"hello", "senha-de-teste")
    assert decrypt_blob(blob, "senha-de-teste") == b"hello"
    print("  [ok] encrypt_blob / decrypt_blob")

    # 1. save_wallet / load_wallet (ordem original)
    d = tempfile.mkdtemp(prefix="brn_ss_test_")
    try:
        p = os.path.join(d, "w.dat")
        assert save_wallet({"k": "v"}, "senha-de-teste", p) is True
        assert load_wallet("senha-de-teste", p) == {"k": "v"}
        print("  [ok] save_wallet(data, password) / load_wallet(password)")

        # 2. save_current_wallet — ORDEM ANTIGA (password, data)
        global CURRENT_WALLET_ENC
        orig = CURRENT_WALLET_ENC
        CURRENT_WALLET_ENC = os.path.join(d, "current_wallet.enc")
        try:
            r = save_current_wallet("senha-de-teste", {"pub": "abc"})
            assert r.get("ok") is True, f"ordem antiga falhou: {r}"
            print("  [ok] save_current_wallet(password, data)  ← ordem antiga")

            # 3. save_current_wallet — ORDEM NOVA (data, password)
            r = save_current_wallet({"pub": "xyz"}, "senha-de-teste")
            assert r.get("ok") is True, f"ordem nova falhou: {r}"
            print("  [ok] save_current_wallet(data, password)  ← ordem nova")

            # 4. load_current_wallet vê o último save
            w = load_current_wallet("senha-de-teste")
            assert w.get("ok") is True
            assert w.get("pub") == "xyz", f"got {w}"
            print("  [ok] load_current_wallet reflete o último save")

            # 5. Argumentos inválidos → {"ok": False, "msg": ...}
            r = save_current_wallet(123, 456)
            assert r.get("ok") is False and "msg" in r
            print("  [ok] argumentos inválidos são rejeitados com mensagem")

            # 6. Senha errada → {"ok": False, "msg": ...}
            w = load_current_wallet("senha-errada")
            assert w.get("ok") is False
            print("  [ok] senha errada é rejeitada em load_current_wallet")
        finally:
            CURRENT_WALLET_ENC = orig
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print("=" * 64)
    print(" TODOS OS TESTES PASSARAM (incluindo Bug 4)")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
