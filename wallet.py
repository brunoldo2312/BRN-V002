# wallet.py — Carteira BRN com Schnorr BIP-340 + fallback ECDSA (v3.1)
# ================================================================
# v3.1 (2026-11-17):
#   [FIX] Wallet.__init__ agora SEMPRE deriva address/pubkey da priv.
#         Se from_dict() vier com address/pubkey divergente, loga
#         warning e usa o derivado (antes confiava no JSON e quebrava
#         validate_tx com address/pubkey mismatch silencioso).
#   [FIX] from_private_key / __init__ validam 1 <= priv < N e rejeitam 0.
#   [FIX] sign() valida len(msg_hash)==32 com ValueError claro.
#   [FIX] sign_transaction() faz deepcopy (não muta a tx do chamador).
#   [KEEP] v3.0: Schnorr BIP-340, fallback ECDSA, BRN_SIG_SCHEME,
#         HDWallet, WalletManager, sign/verify_transaction.
# ================================================================
from __future__ import annotations

import copy
import os
import json
import time
import hashlib
import hmac as hmac_lib
import logging

from crypto import (
    generate_private_key,
    pubkey_from_priv,
    pubkey_to_address,
    sign_schnorr,
    verify_schnorr,
    sign_ecdsa,
    verify_ecdsa,
)
from bech32 import validate_address as _bech32_validate

try:
    from mnemonic import Mnemonic
    _HAS_MNEMONIC = True
except ImportError:
    Mnemonic = None
    _HAS_MNEMONIC = False

log = logging.getLogger("wallet")

WALLETS_DIR = os.environ.get("BRN_WALLETS_DIR", "wallets")
CURRENT_WALLET_JSON = "current_wallet.json"
SECP256K1_N = (1 << 256) - 432420386565659656852420866394968145599

_VALID_SCHEMES = ("auto", "schnorr", "ecdsa")
_SIG_SCHEME = (os.environ.get("BRN_SIG_SCHEME", "auto").strip().lower() or "auto")
if _SIG_SCHEME not in _VALID_SCHEMES:
    log.warning(
        f"BRN_SIG_SCHEME={_SIG_SCHEME!r} inválido; usando 'auto'. "
        f"Válidos: {_VALID_SCHEMES}"
    )
    _SIG_SCHEME = "auto"

def get_sig_scheme() -> str:
    return _SIG_SCHEME

def set_sig_scheme(scheme: str) -> None:
    global _SIG_SCHEME
    s = (scheme or "").strip().lower()
    if s not in _VALID_SCHEMES:
        raise ValueError(f"BRN_SIG_SCHEME inválido: {scheme!r}")
    _SIG_SCHEME = s

def _validate_priv_bytes(priv: bytes) -> bytes:
    if not isinstance(priv, (bytes, bytearray)) or len(priv) != 32:
        raise ValueError("private_key deve ter 32 bytes (64 hex)")
    v = int.from_bytes(priv, "big")
    if not (1 <= v < SECP256K1_N):
        raise ValueError("private_key fora do range válido (1 <= priv < N)")
    return bytes(priv)

# ============================================================
# WALLET — chave única
# ============================================================
class Wallet:
    def __init__(self, private_key_hex: str | None = None,
                 address: str = "",
                 public_key: str = "",
                 created_at: int | None = None,
                 label: str = ""):
        if private_key_hex:
            try:
                raw = bytes.fromhex(private_key_hex)
            except Exception as e:
                raise ValueError(f"private_key_hex inválido: {e}") from e
            self.priv = _validate_priv_bytes(raw)
        else:
            self.priv = generate_private_key()
        self.pub = pubkey_from_priv(self.priv, compressed=True)
        # [FIX v3.1] Address/pubkey SEMPRE derivados da priv. Nunca confia no JSON.
        derived_addr = pubkey_to_address(self.pub.hex())
        derived_pk = self.pub.hex()
        if address and address != derived_addr:
            log.warning(
                "[wallet] address no JSON (%s...) diverge do derivado (%s...) — usando derivado",
                address[:16], derived_addr[:16],
            )
        if public_key and public_key.lower() != derived_pk.lower():
            log.warning(
                "[wallet] public_key no JSON diverge da derivada — usando derivada"
            )
        self.address = derived_addr
        self.public_key = derived_pk
        self.created_at = created_at or int(time.time())
        self.label = label

    @property
    def priv_hex(self) -> str:
        return self.priv.hex()

    @property
    def pub_hex(self) -> str:
        return self.pub.hex()

    def private_key_hex(self) -> str:
        return self.priv_hex

    def public_key_hex(self) -> str:
        return self.pub_hex

    @classmethod
    def create(cls) -> "Wallet":
        return cls()

    @classmethod
    def generate(cls) -> "Wallet":
        return cls()

    @classmethod
    def from_private_key(cls, priv_hex: str) -> "Wallet":
        if not isinstance(priv_hex, str):
            raise ValueError("private_key deve ser string hex")
        priv_hex = priv_hex.strip()
        try:
            raw = bytes.fromhex(priv_hex)
        except Exception as e:
            raise ValueError(f"private_key hex inválido: {e}") from e
        _validate_priv_bytes(raw)  # rejeita 0 e >= N antes de derivar
        return cls(private_key_hex=priv_hex)

    def sign(self, msg_hash: bytes) -> str:
        if not isinstance(msg_hash, (bytes, bytearray)) or len(msg_hash) != 32:
            raise ValueError("sign: msg_hash deve ter 32 bytes")
        if _SIG_SCHEME == "ecdsa":
            return sign_ecdsa(self.priv, bytes(msg_hash)).hex()
        return sign_schnorr(self.priv, bytes(msg_hash)).hex()

    @staticmethod
    def verify(sig_hash: bytes, sig_hex: str, pub_hex: str) -> bool:
        try:
            pub = bytes.fromhex(pub_hex)
            sig = bytes.fromhex(sig_hex)
        except Exception:
            return False
        if not isinstance(sig_hash, (bytes, bytearray)) or len(sig_hash) != 32:
            return False
        if len(sig) != 64:
            return False
        if _SIG_SCHEME == "ecdsa":
            try:
                return verify_ecdsa(pub, sig, bytes(sig_hash))
            except Exception:
                return False
        if _SIG_SCHEME == "schnorr":
            try:
                return verify_schnorr(pub, sig, bytes(sig_hash))
            except Exception:
                return False
        # auto: Schnorr primeiro, ECDSA como fallback
        try:
            if verify_schnorr(pub, sig, bytes(sig_hash)):
                return True
        except Exception:
            pass
        try:
            return verify_ecdsa(pub, sig, bytes(sig_hash))
        except Exception:
            return False

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "private_key": self.priv_hex,
            "public_key": self.pub_hex,
            "created_at": self.created_at,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Wallet":
        if not isinstance(d, dict) or "private_key" not in d:
            raise ValueError("from_dict: dict sem 'private_key'")
        return cls(
            private_key_hex=d["private_key"],
            address=d.get("address", ""),
            public_key=d.get("public_key") or d.get("pubkey", ""),
            created_at=d.get("created_at"),
            label=d.get("label", ""),
        )

    def __repr__(self):
        return f"<Wallet {self.address[:16]}...>"

# ============================================================
# WALLET MANAGER
# ============================================================
class WalletManager:
    WALLETS_DIR = WALLETS_DIR
    _session_priv = None

    @staticmethod
    def generate_keypair() -> dict:
        w = Wallet.create()
        return {
            "address": w.address,
            "private_key": w.priv_hex,
            "public_key": w.pub_hex,
            "created_at": w.created_at,
        }

    @staticmethod
    def from_private_key(priv_hex: str) -> dict:
        w = Wallet.from_private_key(priv_hex)
        return w.to_dict()

    @staticmethod
    def validate_address(addr) -> bool:
        try:
            return bool(_bech32_validate(addr))
        except Exception:
            return (isinstance(addr, str)
                    and addr.startswith("brn1")
                    and 20 < len(addr) < 128)

    @staticmethod
    def validate_private_key(priv_hex: str) -> bool:
        try:
            raw = bytes.fromhex(priv_hex)
            _validate_priv_bytes(raw)
            return True
        except Exception:
            return False

    @staticmethod
    def save_current(address: str, private_key: str, public_key: str,
                     password: str) -> dict:
        from secure_store import save_current_wallet
        return save_current_wallet(password, {
            "address": address,
            "private_key": private_key,
            "public_key": public_key,
            "saved_at": int(time.time()),
        })

    @staticmethod
    def load_current(password: str) -> dict:
        from secure_store import load_current_wallet
        return load_current_wallet(password)

    @staticmethod
    def migrate_legacy_current(password: str) -> dict:
        from secure_store import migrate_current_wallet
        return migrate_current_wallet(password)

    @staticmethod
    def lock_session() -> None:
        WalletManager._session_priv = None

    @staticmethod
    def _garantir_dir():
        os.makedirs(WalletManager.WALLETS_DIR, exist_ok=True)

    @staticmethod
    def save_encrypted_wallet(filename: str, password: str,
                              address: str, sk: str, pk: str) -> dict:
        from secure_store import save_wallet
        try:
            WalletManager._garantir_dir()
            if not filename.endswith(".wallet"):
                filename += ".wallet"
            path = os.path.join(WalletManager.WALLETS_DIR, filename)
            save_wallet({
                "address": address,
                "private_key": sk,
                "public_key": pk,
                "saved_at": int(time.time()),
            }, password, path)
            return {"ok": True, "path": path,
                    "msg": f"Carteira salva em {path}"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    @staticmethod
    def load_encrypted_wallet(filename: str, password: str) -> dict:
        from secure_store import load_wallet
        try:
            if not filename.endswith(".wallet"):
                filename += ".wallet"
            path = os.path.join(WalletManager.WALLETS_DIR, filename)
            data = load_wallet(password, path)
            if data is None:
                return {"ok": False, "msg": "arquivo vazio"}
            return {"ok": True, **data}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    @staticmethod
    def list_wallets() -> list:
        try:
            WalletManager._garantir_dir()
            out = []
            for fname in sorted(os.listdir(WalletManager.WALLETS_DIR)):
                if not fname.endswith(".wallet"):
                    continue
                path = os.path.join(WalletManager.WALLETS_DIR, fname)
                out.append({
                    "filename": fname,
                    "size": os.path.getsize(path),
                    "modified": os.path.getmtime(path),
                })
            return out
        except Exception:
            return []

    @staticmethod
    def delete_wallet(filename: str) -> bool:
        try:
            if not filename.endswith(".wallet"):
                filename += ".wallet"
            path = os.path.join(WalletManager.WALLETS_DIR, filename)
            if os.path.exists(path):
                os.remove(path)
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def load_legacy_json() -> dict:
        if not os.path.exists(CURRENT_WALLET_JSON):
            return {}
        try:
            with open(CURRENT_WALLET_JSON, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

# ============================================================
# HD WALLET (BIP39 + BIP44 simplificado)
# ============================================================
class HDWalletManager:
    PURPOSE = 44
    COIN_TYPE = 0
    ACCOUNT = 0
    CHANGE = 0
    _N = SECP256K1_N

    @staticmethod
    def _m():
        if not _HAS_MNEMONIC:
            raise ImportError("pip install mnemonic")
        return Mnemonic("english")

    @staticmethod
    def validate_mnemonic(mn: str) -> bool:
        try:
            return HDWalletManager._m().check(mn)
        except Exception:
            return False

    @staticmethod
    def create(strength: int = 128) -> dict:
        m = HDWalletManager._m()
        phrase = m.generate(strength=strength)
        return HDWalletManager.from_mnemonic(phrase, index=0)

    @staticmethod
    def from_mnemonic(mn: str, index: int = 0, passphrase: str = "") -> dict:
        m = HDWalletManager._m()
        if not m.check(mn):
            raise ValueError("mnemonico invalido")
        seed = m.to_seed(mn, passphrase=passphrase)
        I = hmac_lib.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
        key = int.from_bytes(I[:32], "big")
        chain = I[32:]
        path = [
            HDWalletManager.PURPOSE + 0x80000000,
            HDWalletManager.COIN_TYPE + 0x80000000,
            HDWalletManager.ACCOUNT + 0x80000000,
            HDWalletManager.CHANGE,
            index,
        ]
        for child in path:
            if child >= 0x80000000:
                data = b"\x00" + key.to_bytes(32, "big") + child.to_bytes(4, "big")
            else:
                pub = pubkey_from_priv(key.to_bytes(32, "big"))
                data = pub + child.to_bytes(4, "big")
            I = hmac_lib.new(chain, data, hashlib.sha512).digest()
            key = (int.from_bytes(I[:32], "big") + key) % HDWalletManager._N
            if key == 0:
                raise ValueError("derivacao BIP32 produziu chave invalida")
            chain = I[32:]
        w = Wallet(private_key_hex=key.to_bytes(32, "big").hex())
        return {
            "mnemonic": mn,
            "address": w.address,
            "private_key": w.priv_hex,
            "public_key": w.pub_hex,
            "index": index,
            "path": f"m/44'/{HDWalletManager.COIN_TYPE}'/0'/0/{index}",
            "sig_scheme": _SIG_SCHEME,
        }

    @staticmethod
    def derive_many(mn: str, count: int = 5) -> list:
        if count < 1 or count > 100:
            raise ValueError("count deve estar entre 1 e 100")
        return [HDWalletManager.from_mnemonic(mn, index=i)
                for i in range(count)]

# ============================================================
# HELPERS DE TX (usados por server.py e testes)
# ============================================================
def sign_transaction(wallet: Wallet, tx: dict) -> dict:
    """Preenche pubkey + signature + txid. NÃO muta a tx original (deepcopy)."""
    from blockchain import signing_hash, txid

    tx = copy.deepcopy(tx)  # [FIX v3.1] antes era dict(tx) shallow — mutava o chamador
    pub_hex = wallet.pub_hex
    for inp in tx.get("inputs", []):
        inp["pubkey"] = pub_hex
        inp.setdefault("signature", "")
    h = signing_hash(tx)
    sig_hex = wallet.sign(h)
    for inp in tx.get("inputs", []):
        inp["signature"] = sig_hex
    tx["txid"] = txid(tx)
    return tx

def verify_transaction(tx: dict) -> tuple:
    """Retorna (ok, msg). Aceita Schnorr e ECDSA (auto)."""
    try:
        from blockchain import signing_hash
        h = signing_hash(tx)
        for i, inp in enumerate(tx.get("inputs", [])):
            sig = inp.get("signature", "")
            pk = inp.get("pubkey", "")
            if not sig or not pk:
                return False, f"input[{i}] sem assinatura/pubkey"
            if not Wallet.verify(h, sig, pk):
                return False, f"input[{i}] assinatura invalida"
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "gen":
        w = Wallet.create()
        d = w.to_dict()
        d["sig_scheme"] = get_sig_scheme()
        print(json.dumps(d, indent=2))
        sys.exit(0)
    if len(sys.argv) > 2 and sys.argv[1] == "from":
        w = Wallet.from_private_key(sys.argv[2])
        d = w.to_dict()
        d["sig_scheme"] = get_sig_scheme()
        print(json.dumps(d, indent=2))
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "mnemonic":
        try:
            m = HDWalletManager.create()
            print("mnemonico  :", m["mnemonic"])
            print("endereco   :", m["address"])
            print("sig_scheme :", m["sig_scheme"])
        except Exception as e:
            print("erro:", e)
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "scheme":
        print(f"BRN_SIG_SCHEME = {get_sig_scheme()}")
        sys.exit(0)

    print(f"wallet.py v3.1 — sig_scheme = {get_sig_scheme()}")
    print("=" * 60)
    failures = []

    def _check(name, fn):
        try:
            fn()
            print(f"  [ok]   {name}")
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failures.append(name)
        except Exception as e:
            print(f"  [ERR]  {name}: {e}")
            failures.append(name)

    def t1():
        priv = generate_private_key().hex()
        w_a = Wallet.from_private_key(priv)
        addr_auto = w_a.address
        set_sig_scheme("schnorr")
        w_b = Wallet.from_private_key(priv)
        set_sig_scheme("ecdsa")
        w_c = Wallet.from_private_key(priv)
        set_sig_scheme("auto")
        assert addr_auto == w_b.address == w_c.address
    _check("address independente do esquema", t1)

    def t2():
        set_sig_scheme("auto")
        w = Wallet.create()
        msg = hashlib.sha256(b"hello auto").digest()
        sig = w.sign(msg)
        assert len(bytes.fromhex(sig)) == 64
        assert w.verify(msg, sig, w.pub_hex)
        assert not w.verify(hashlib.sha256(b"x").digest(), sig, w.pub_hex)
    _check("auto: roundtrip sign/verify", t2)

    def t3():
        w = Wallet.create()
        msg = hashlib.sha256(b"hello ecdsa compat").digest()
        set_sig_scheme("ecdsa")
        sig_ecdsa = w.sign(msg)
        set_sig_scheme("auto")
        assert w.verify(msg, sig_ecdsa, w.pub_hex)
    _check("auto: aceita assinatura ECDSA (compat)", t3)

    def t4():
        w = Wallet.create()
        msg = hashlib.sha256(b"hello mismatch").digest()
        set_sig_scheme("schnorr")
        sig_schnorr = w.sign(msg)
        set_sig_scheme("ecdsa")
        assert not w.verify(msg, sig_schnorr, w.pub_hex)
    _check("ecdsa: rejeita assinatura Schnorr", t4)

    def t5():
        w = Wallet.create()
        msg = hashlib.sha256(b"hello reverse").digest()
        set_sig_scheme("ecdsa")
        sig_ecdsa = w.sign(msg)
        set_sig_scheme("schnorr")
        assert not w.verify(msg, sig_ecdsa, w.pub_hex)
    _check("schnorr: rejeita assinatura ECDSA", t5)

    def t6():
        set_sig_scheme("auto")
        w = Wallet.create()
        tx = {
            "txid": "",
            "inputs": [
                {"txid": "a" * 64, "vout": 0, "pubkey": "", "signature": ""},
                {"txid": "a" * 64, "vout": 1, "pubkey": "", "signature": ""},
            ],
            "outputs": [{"address": w.address, "amount": 1000, "pubkey": ""}],
            "timestamp": 1234567890,
            "locktime": 0,
            "nonce": 0,
        }
        orig_sig = tx["inputs"][0]["signature"]
        signed = sign_transaction(w, tx)
        ok, msg = verify_transaction(signed)
        assert ok, msg
        assert signed["inputs"][0]["signature"] == signed["inputs"][1]["signature"]
        # [FIX v3.1] original NÃO pode ter sido mutada
        assert tx["inputs"][0]["signature"] == orig_sig == ""
        assert tx["txid"] == ""
    _check("sign_transaction não muta original + verify", t6)

    def t7():
        set_sig_scheme("auto")
        w = Wallet.create()
        d = w.to_dict()
        w2 = Wallet.from_dict(d)
        assert w2.address == w.address
        assert w2.priv_hex == w.priv_hex
    _check("to_dict/from_dict", t7)

    def t7b():
        # [FIX v3.1] JSON com address errado -> usa o derivado, não o JSON
        w = Wallet.create()
        d = w.to_dict()
        d["address"] = "brn1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
        d["public_key"] = "02" + "00" * 32
        w2 = Wallet.from_dict(d)
        assert w2.address == w.address
        assert w2.pub_hex == w.pub_hex
    _check("from_dict ignora address/pubkey divergentes", t7b)

    def t7c():
        for bad in ("00" * 32, "ff" * 32):
            try:
                Wallet.from_private_key(bad)
                raise AssertionError(f"deveria rejeitar priv {bad[:8]}...")
            except ValueError:
                pass
    _check("from_private_key rejeita 0 e >= N", t7c)

    def t8():
        if not _HAS_MNEMONIC:
            return
        set_sig_scheme("auto")
        m1 = HDWalletManager.create()
        m2 = HDWalletManager.from_mnemonic(m1["mnemonic"], index=0)
        assert m1["address"] == m2["address"]
        m3 = HDWalletManager.from_mnemonic(m1["mnemonic"], index=1)
        assert m3["address"] != m1["address"]
    _check("HDWallet determinístico", t8)

    def t9():
        set_sig_scheme("auto")
        assert get_sig_scheme() == "auto"
    _check("set_sig_scheme / get_sig_scheme", t9)

    set_sig_scheme("auto")
    print()
    print("=" * 60)
    if failures:
        print(f" FALHAS: {len(failures)} teste(s)")
        for f in failures:
            print(f"   - {f}")
        sys.exit(1)
    print(" TODOS OS TESTES PASSARAM")
    print("=" * 60)
