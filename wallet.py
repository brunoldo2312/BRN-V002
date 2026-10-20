"""wallet.py — Carteira BRN (chave única + HD BIP39/BIP44 simplificada)
================================================================
API esperada pelos módulos de produção:

  Wallet:
    - Wallet.create()                          (classmethod)
    - Wallet.from_private_key(hex)             (classmethod)
    - Wallet.from_dict(d) / .to_dict()
    - w.address, w.priv_hex, w.pub_hex
    - w.sign(msg_hash_bytes) -> hex
    - Wallet.verify(sig_hash, sig_hex, pub_hex) (staticmethod)
    - w.public_key_hex(), w.private_key_hex()

  WalletManager:
    - generate_keypair() -> dict
    - validate_address(addr) -> bool
    - save_current(addr, sk, pk, password) -> dict
    - load_current(password) -> dict
    - lock_session()
    - save_encrypted_wallet / load_encrypted_wallet / list_wallets
    - migrate_legacy_current(password)

  HDWalletManager:
    - create(strength=128) -> dict
    - from_mnemonic(mn, index=0) -> dict
    - validate_mnemonic(mn) -> bool

Requer:
    pip install cryptography mnemonic
================================================================
"""
from __future__ import annotations

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


# ============================================================
# CONSTANTES
# ============================================================
WALLETS_DIR = os.environ.get("BRN_WALLETS_DIR", "wallets")
CURRENT_WALLET_JSON = "current_wallet.json"
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


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
            self.priv = bytes.fromhex(private_key_hex)
        else:
            self.priv = generate_private_key()
        self.pub = pubkey_from_priv(self.priv, compressed=True)
        self.address = address or pubkey_to_address(self.pub.hex())
        self.public_key = public_key or self.pub.hex()
        self.created_at = created_at or int(time.time())
        self.label = label

    # ---- properties ----
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

    # ---- criação ----
    @classmethod
    def create(cls) -> "Wallet":
        return cls()

    @classmethod
    def generate(cls) -> "Wallet":
        return cls()

    @classmethod
    def from_private_key(cls, priv_hex: str) -> "Wallet":
        priv = bytes.fromhex(priv_hex)
        if len(priv) != 32:
            raise ValueError("private_key deve ter 32 bytes (64 hex)")
        return cls(private_key_hex=priv_hex)

    # ---- assinatura ----
    def sign(self, msg_hash: bytes) -> str:
        """Assina um hash. Retorna assinatura em hex (128 chars)."""
        return sign_ecdsa(self.priv, msg_hash).hex()

    @staticmethod
    def verify(sig_hash: bytes, sig_hex: str, pub_hex: str) -> bool:
        try:
            return verify_ecdsa(bytes.fromhex(pub_hex),
                                bytes.fromhex(sig_hex),
                                sig_hash)
        except Exception:
            return False

    # ---- serialização ----
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

    # ---------- geração ----------
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

    # ---------- validação ----------
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
            return len(bytes.fromhex(priv_hex)) == 32
        except Exception:
            return False

    # ---------- carteira ativa (current_wallet) ----------
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
        """Limpa a chave privada em memória. Usado no shutdown."""
        WalletManager._session_priv = None

    # ---------- carteiras exportadas (.wallet) ----------
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

    # ---------- legado ----------
    @staticmethod
    def load_legacy_json() -> dict:
        """Lê current_wallet.json (formato antigo em plaintext)."""
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
    """
    Deriva chaves no caminho m/44'/0'/0'/0/{index}.
    Não é BIP32 completo, mas é determinístico e reproduzível
    a partir do mesmo mnemônico.
    """
    PURPOSE = 44
    COIN_TYPE = 0
    ACCOUNT = 0
    CHANGE = 0
    _N = SECP256K1_N

    # ---------- helpers ----------
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

    # ---------- criação ----------
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
            HDWalletManager.PURPOSE  + 0x80000000,
            HDWalletManager.COIN_TYPE + 0x80000000,
            HDWalletManager.ACCOUNT  + 0x80000000,
            HDWalletManager.CHANGE,
            index,
        ]
        for child in path:
            if child >= 0x80000000:
                data = b"\x00" + key.to_bytes(32, "big") \
                       + child.to_bytes(4, "big")
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
            "mnemonic":     mn,
            "address":      w.address,
            "private_key":  w.priv_hex,
            "public_key":   w.pub_hex,
            "index":        index,
            "path":         f"m/44'/{HDWalletManager.COIN_TYPE}'/0'/0/{index}",
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
    """Preenche pubkey + signature + txid de uma tx com inputs[]."""
    from blockchain import signing_hash, txid

    tx = dict(tx)
    pub_hex = wallet.pub_hex

    # 1) injeta pubkey em todos os inputs
    for inp in tx.get("inputs", []):
        inp["pubkey"] = pub_hex
        inp.setdefault("signature", "")

    # 2) hash único (todos os inputs assinam o mesmo hash)
    h = signing_hash(tx)

    # 3) assinatura
    sig_hex = wallet.sign(h)
    for inp in tx.get("inputs", []):
        inp["signature"] = sig_hex

    # 4) txid
    tx["txid"] = txid(tx)
    return tx


def verify_transaction(tx: dict) -> tuple:
    """Retorna (ok, msg)."""
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
# CLI RÁPIDO
# ============================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "gen":
        w = Wallet.create()
        print(json.dumps(w.to_dict(), indent=2))

    elif len(sys.argv) > 2 and sys.argv[1] == "from":
        w = Wallet.from_private_key(sys.argv[2])
        print(json.dumps(w.to_dict(), indent=2))

    elif len(sys.argv) > 1 and sys.argv[1] == "mnemonic":
        try:
            m = HDWalletManager.create()
            print("mnemonico:", m["mnemonic"])
            print("endereco :", m["address"])
        except Exception as e:
            print("erro:", e)

    else:
        # auto-teste básico
        print("Rodando auto-teste...")
        w = Wallet.create()
        print("  address:", w.address)
        msg = hashlib.sha256(b"hello").digest()
        sig = w.sign(msg)
        assert w.verify(msg, sig, w.pub_hex), "verify falhou"
        assert not w.verify(hashlib.sha256(b"x").digest(), sig, w.pub_hex)
        print("  sign/verify: OK")

        d = w.to_dict()
        w2 = Wallet.from_dict(d)
        assert w2.address == w.address
        print("  to_dict/from_dict: OK")

        if _HAS_MNEMONIC:
            m = HDWalletManager.create()
            print("  HD mnemonic:", m["mnemonic"][:40], "...")
            w3 = HDWalletManager.from_mnemonic(m["mnemonic"], index=0)
            assert w3["address"] == m["address"]
            print("  HD determinístico: OK")
        else:
            print("  HD pulado (mnemonic nao instalado)")

        print("OK")