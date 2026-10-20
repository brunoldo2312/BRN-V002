content = r'''"""secure_store.py — BRN Chain v9.0"""
from __future__ import annotations
import os, json, hmac, time, logging, threading
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
SALT_SIZE = 16
KEY_SIZE = 32
NONCE_SIZE = 12
TAG_SIZE = 16
MAGIC = b"BRNS"
FORMAT_VERSION = 1
ARGON2_TIME_COST = int(os.environ.get("BRN_ARGON2_TIME", "3"))
ARGON2_MEMORY_COST = int(os.environ.get("BRN_ARGON2_MEMORY", "65536"))
ARGON2_PARALLELISM = int(os.environ.get("BRN_ARGON2_PARALLEL", "4"))
MIN_PASSWORD_LEN = 8

def _derive_key(password, salt):
    if _ARGON2_IMPL == "cffi":
        return hash_secret_raw(secret=password.encode("utf-8"), salt=salt,
            time_cost=ARGON2_TIME_COST, memory_cost=ARGON2_MEMORY_COST,
            parallelism=ARGON2_PARALLELISM, hash_len=KEY_SIZE, type=Type.ID)
    kdf = Argon2id(salt=salt, length=KEY_SIZE, iterations=ARGON2_TIME_COST,
                   lanes=ARGON2_PARALLELISM, memory_cost=ARGON2_MEMORY_COST)
    return kdf.derive(password.encode("utf-8"))

def _validate_password(password):
    if not isinstance(password, str):
        raise ValueError("senha deve ser string")
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError("senha muito curta")

def _encrypt(data, key, aad=b""):
    nonce = os.urandom(NONCE_SIZE)
    return nonce + ChaCha20Poly1305(key).encrypt(nonce, data, aad)

def _decrypt(packed, key, aad=b""):
    if len(packed) < NONCE_SIZE + TAG_SIZE:
        raise ValueError("blob curto")
    nonce, ct = packed[:NONCE_SIZE], packed[NONCE_SIZE:]
    return ChaCha20Poly1305(key).decrypt(nonce, ct, aad)

def _atomic_write(path, data, mode=0o600):
    p = Path(path)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except Exception:
        try: tmp.unlink()
        except FileNotFoundError: pass
        raise

def encrypt_blob(data, password):
    _validate_password(password)
    salt = os.urandom(SALT_SIZE)
    key = _derive_key(password, salt)
    return MAGIC + bytes([FORMAT_VERSION]) + salt + _encrypt(data, key, b"blob")

def decrypt_blob(blob, password):
    if len(blob) < len(MAGIC) + 1 + SALT_SIZE + NONCE_SIZE + TAG_SIZE:
        raise ValueError("blob truncado")
    if blob[:4] != MAGIC:
        raise ValueError("magic invalido")
    ver = blob[4]
    if ver == 49: ver = 1
    if ver != FORMAT_VERSION:
        raise ValueError(f"versao nao suportada: {ver}")
    off = 5
    salt = blob[off:off + SALT_SIZE]; off += SALT_SIZE
    key = _derive_key(password, salt)
    return _decrypt(blob[off:], key, b"blob")

def constant_time_eq(a, b):
    if isinstance(a, str): a = a.encode()
    if isinstance(b, str): b = b.encode()
    return hmac.compare_digest(a, b)

def _save_blob(path, data, password, aad):
    _validate_password(password)
    salt = os.urandom(SALT_SIZE)
    key = _derive_key(password, salt)
    pt = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    blob = MAGIC + bytes([FORMAT_VERSION]) + salt + _encrypt(pt, key, aad)
    _atomic_write(path, blob, mode=0o600)
    return True

def _load_blob(path, password, aad):
    p = Path(path)
    if not p.exists(): return None
    raw = p.read_bytes()
    min_size = len(MAGIC) + 1 + SALT_SIZE + NONCE_SIZE + TAG_SIZE
    if len(raw) < min_size:
        raise ValueError(f"arquivo {path} truncado")
    if raw[:4] != MAGIC:
        raise ValueError(f"arquivo {path} nao e um blob BRN")
    version = raw[4]
    if version == 49: version = 1
    if version != FORMAT_VERSION:
        raise ValueError(f"versao nao suportada: {version}")
    off = 5
    salt = raw[off:off + SALT_SIZE]; off += SALT_SIZE
    encrypted = raw[off:]
    key = _derive_key(password, salt)
    try:
        decrypted = _decrypt(encrypted, key, aad)
    except InvalidTag:
        raise ValueError("Senha incorreta ou arquivo corrompido")
    except Exception as e:
        raise ValueError(f"Falha ao decifrar: {e}")
    try:
        return json.loads(decrypted.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Conteudo invalido: {e}")

def save_wallet(wallet_data, password, path="wallet_encrypted.dat"):
    return _save_blob(path, wallet_data, password, aad=b"wallet")

def load_wallet(password, path="wallet_encrypted.dat"):
    return _load_blob(path, password, aad=b"wallet")

def save_node_identity(identity_data, password, path="node_identity.enc"):
    return _save_blob(path, identity_data, password, aad=b"node_identity")

def load_node_identity(password, path="node_identity.enc"):
    return _load_blob(path, password, aad=b"node_identity")

def delete_identity(path="node_identity.enc", secure=True):
    p = Path(path)
    if not p.exists(): return False
    if secure:
        try:
            size = p.stat().st_size
            with p.open("r+b") as f:
                f.write(os.urandom(size)); f.flush(); os.fsync(f.fileno())
        except Exception as e:
            log.warning(f"overwrite de {path} falhou: {e}")
    p.unlink()
    return True

CURRENT_WALLET_ENC  = os.environ.get("BRN_CURRENT_WALLET", "current_wallet.enc")
CURRENT_WALLET_JSON = "current_wallet.json"

def save_current_wallet(password, data):
    try:
        _save_blob(CURRENT_WALLET_ENC, data, password, b"current_wallet")
        return {"ok": True, "path": CURRENT_WALLET_ENC}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

def load_current_wallet(password):
    try:
        data = _load_blob(CURRENT_WALLET_ENC, password, b"current_wallet")
        if data is None:
            return {"ok": False, "msg": "current_wallet.enc nao existe"}
        return {"ok": True, **data}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

def migrate_current_wallet(password):
    if not os.path.exists(CURRENT_WALLET_JSON):
        return {"ok": False, "msg": "current_wallet.json nao existe"}
    try:
        with open(CURRENT_WALLET_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        r = save_current_wallet(password, data)
        if not r.get("ok"): return r
        os.rename(CURRENT_WALLET_JSON, CURRENT_WALLET_JSON + ".migrated")
        return {"ok": True, "msg": "migrado", "path": CURRENT_WALLET_ENC}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

class WalletSession:
    def __init__(self, ttl=15 * 60):
        self.ttl = ttl
        self._data = None
        self._unlocked_at = 0.0
        self._lock = threading.RLock()
    def unlock(self, data):
        with self._lock:
            self._data = dict(data)
            self._unlocked_at = time.time()
    def lock(self):
        with self._lock:
            if self._data:
                for k in list(self._data.keys()):
                    try: del self._data[k]
                    except Exception: pass
            self._data = None
            self._unlocked_at = 0.0
    def get(self):
        with self._lock:
            if self._data is None: return None
            if time.time() - self._unlocked_at > self.ttl:
                self.lock(); return None
            return dict(self._data)
    def seconds_until_lock(self):
        with self._lock:
            if self._data is None: return 0
            elapsed = time.time() - self._unlocked_at
            return max(0, int(self.ttl - elapsed))
    def is_unlocked(self):
        return self.get() is not None
'''

open("secure_store.py", "w", encoding="utf-8", newline="\n").write(content)
print(f"OK - secure_store.py v9.0 escrito ({len(content)} bytes)")