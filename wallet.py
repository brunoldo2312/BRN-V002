"""wallet.py — Carteira BRN com Schnorr BIP-340 + fallback ECDSA (v3.0)
================================================================
v3.0 (2026-10-28):
  [NEW] Migração para Schnorr BIP-340 (crypto.py v3.0):
        - Wallet.sign() usa sign_schnorr (novas assinaturas)
        - Wallet.verify() tenta Schnorr, cai para ECDSA (compat)
        - BRN_SIG_SCHEME=auto|schnorr|ecdsa controla o comportamento
        - Address NÃO muda (mesma chave → mesmo pubkey → mesmo endereço)
  [KEEP] API pública v2 intacta: create, from_private_key, to_dict,
         from_dict, priv_hex, pub_hex, address, sign, verify.
  [KEEP] WalletManager, HDWalletManager, sign_transaction,
         verify_transaction inalterados na assinatura.
  [KEEP] Compatível com chain antiga (ECDSA) via fallback.
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


# ============================================================
# CONFIG — esquema de assinatura
# ------------------------------------------------------------
# BRN_SIG_SCHEME=auto     (default) → assina Schnorr, verifica
#                                      Schnorr com fallback ECDSA
# BRN_SIG_SCHEME=schnorr  → assina Schnorr, verifica só Schnorr
#                                      (quebra a chain antiga; usar após
#                                      reset completo da rede)
# BRN_SIG_SCHEME=ecdsa    → assina ECDSA, verifica só ECDSA
#                                      (modo legado, NÃO recomendado)
# ============================================================
WALLETS_DIR = os.environ.get("BRN_WALLETS_DIR", "wallets")
CURRENT_WALLET_JSON = "current_wallet.json"
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

_VALID_SCHEMES = ("auto", "schnorr", "ecdsa")
_SIG_SCHEME = (os.environ.get("BRN_SIG_SCHEME", "auto").strip().lower() or "auto")
if _SIG_SCHEME not in _VALID_SCHEMES:
    log.warning(
        f"BRN_SIG_SCHEME={_SIG_SCHEME!r} inválido; usando 'auto'. "
        f"Válidos: {_VALID_SCHEMES}"
    )
    _SIG_SCHEME = "auto"


def get_sig_scheme() -> str:
    """Retorna o esquema de assinatura configurado."""
    return _SIG_SCHEME


def set_sig_scheme(scheme: str) -> None:
    """Muda o esquema em runtime (útil em testes)."""
    global _SIG_SCHEME
    s = (scheme or "").strip().lower()
    if s not in _VALID_SCHEMES:
        raise ValueError(f"BRN_SIG_SCHEME inválido: {scheme!r}")
    _SIG_SCHEME = s


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

    # ---- assinatura (v3.0 — Schnorr BIP-340 com fallback) ----
    def sign(self, msg_hash: bytes) -> str:
        """
        Assina o hash (32 bytes). Retorna assinatura em hex (128 chars).

        O esquema depende de BRN_SIG_SCHEME:
          - auto (default) → Schnorr BIP-340
          - schnorr        → Schnorr BIP-340
          - ecdsa          → ECDSA (modo legado)
        """
        if _SIG_SCHEME == "ecdsa":
            return sign_ecdsa(self.priv, msg_hash).hex()
        # auto / schnorr → Schnorr
        return sign_schnorr(self.priv, msg_hash).hex()

    @staticmethod
    def verify(sig_hash: bytes, sig_hex: str, pub_hex: str) -> bool:
        """
        Verifica assinatura de bloco/transação.

        Comportamento (v3.0):
          - auto (default) → tenta Schnorr, cai para ECDSA se falhar
          - schnorr        → aceita só Schnorr
          - ecdsa          → aceita só ECDSA

        Ambos os esquemas produzem 64 bytes, então o formato bruto é
        indistinguível. A verificação criptográfica (Schnorr BIP-340
        vs ECDSA secp256k1) descarta naturalmente a assinatura do
        esquema errado — não há falso positivo.
        """
        try:
            pub = bytes.fromhex(pub_hex)
            sig = bytes.fromhex(sig_hex)
        except Exception:
            return False

        if _SIG_SCHEME == "ecdsa":
            try:
                return verify_ecdsa(pub, sig, sig_hash)
            except Exception:
                return False

        if _SIG_SCHEME == "schnorr":
            try:
                return verify_schnorr(pub, sig, sig_hash)
            except Exception:
                return False

        # auto: Schnorr primeiro, ECDSA como fallback
        try:
            if verify_schnorr(pub, sig, sig_hash):
                return True
        except Exception:
            pass
        try:
            return verify_ecdsa(pub, sig, sig_hash)
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

    As assinaturas resultantes respeitam BRN_SIG_SCHEME (auto|schnorr|ecdsa).
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
            "sig_scheme":   _SIG_SCHEME,
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
    """Preenche pubkey + signature + txid de uma tx com inputs[].

    v3.0: assina UMA vez e reusa a mesma assinatura em todos os inputs
    (evita desperdício — o hash é único para a tx inteira).
    """
    from blockchain import signing_hash, txid

    tx = dict(tx)
    pub_hex = wallet.pub_hex

    # 1) injeta pubkey em todos os inputs
    for inp in tx.get("inputs", []):
        inp["pubkey"] = pub_hex
        inp.setdefault("signature", "")

    # 2) hash único (todos os inputs assinam o mesmo hash)
    h = signing_hash(tx)

    # 3) assinatura única, reusada em todos os inputs
    sig_hex = wallet.sign(h)
    for inp in tx.get("inputs", []):
        inp["signature"] = sig_hex

    # 4) txid
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

    # CLI: gen / from / mnemonic / scheme
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

    # Auto-teste
    print(f"wallet.py v3.0 — sig_scheme = {get_sig_scheme()}")
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

    # 1) Address é determinístico e independente do esquema
    def t1():
        priv = generate_private_key().hex()
        w_a = Wallet.from_private_key(priv)
        addr_auto = w_a.address
        set_sig_scheme("schnorr")
        w_b = Wallet.from_private_key(priv)
        set_sig_scheme("ecdsa")
        w_c = Wallet.from_private_key(priv)
        set_sig_scheme("auto")
        assert addr_auto == w_b.address == w_c.address, \
            "address deve ser igual em todos os esquemas"
    _check("address independente do esquema (auto/schnorr/ecdsa)", t1)

    # 2) sign/verify com auto (Schnorr produzida, Schnorr verificada)
    def t2():
        set_sig_scheme("auto")
        w = Wallet.create()
        msg = hashlib.sha256(b"hello auto").digest()
        sig = w.sign(msg)
        assert len(bytes.fromhex(sig)) == 64
        assert w.verify(msg, sig, w.pub_hex)
        assert not w.verify(hashlib.sha256(b"x").digest(), sig, w.pub_hex)
    _check("auto: roundtrip sign/verify", t2)

    # 3) sign com ecdsa, verify com auto → aceita (fallback)
    def t3():
        w = Wallet.create()
        msg = hashlib.sha256(b"hello ecdsa compat").digest()
        set_sig_scheme("ecdsa")
        sig_ecdsa = w.sign(msg)
        set_sig_scheme("auto")
        # auto deve aceitar assinatura ECDSA (chain antiga)
        assert w.verify(msg, sig_ecdsa, w.pub_hex), \
            "auto deve aceitar ECDSA (fallback)"
    _check("auto: aceita assinatura ECDSA (compat chain antiga)", t3)

    # 4) sign com schnorr, verify com ecdsa → REJEITA
    def t4():
        w = Wallet.create()
        msg = hashlib.sha256(b"hello mismatch").digest()
        set_sig_scheme("schnorr")
        sig_schnorr = w.sign(msg)
        set_sig_scheme("ecdsa")
        # ecdsa NÃO deve aceitar Schnorr (são incompatíveis)
        assert not w.verify(msg, sig_schnorr, w.pub_hex), \
            "ecdsa NÃO deve aceitar assinatura Schnorr"
    _check("ecdsa: rejeita assinatura Schnorr", t4)

    # 5) sign com ecdsa, verify com schnorr → REJEITA
    def t5():
        w = Wallet.create()
        msg = hashlib.sha256(b"hello reverse").digest()
        set_sig_scheme("ecdsa")
        sig_ecdsa = w.sign(msg)
        set_sig_scheme("schnorr")
        assert not w.verify(msg, sig_ecdsa, w.pub_hex), \
            "schnorr NÃO deve aceitar assinatura ECDSA"
    _check("schnorr: rejeita assinatura ECDSA", t5)

    # 6) sign_transaction + verify_transaction
    def t6():
        from blockchain import txid
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
        signed = sign_transaction(w, tx)
        ok, msg = verify_transaction(signed)
        assert ok, f"verify_transaction falhou: {msg}"
        # mesmo hash, mesma assinatura nos 2 inputs
        assert signed["inputs"][0]["signature"] == signed["inputs"][1]["signature"]
    _check("sign_transaction/verify_transaction (auto)", t6)

    # 7) to_dict/from_dict preserva address
    def t7():
        set_sig_scheme("auto")
        w = Wallet.create()
        d = w.to_dict()
        w2 = Wallet.from_dict(d)
        assert w2.address == w.address
        assert w2.priv_hex == w.priv_hex
    _check("to_dict/from_dict", t7)

    # 8) HDWallet determinístico (mesmo mnemônico → mesmo address)
    def t8():
        if not _HAS_MNEMONIC:
            return  # skip
        set_sig_scheme("auto")
        m1 = HDWalletManager.create()
        m2 = HDWalletManager.from_mnemonic(m1["mnemonic"], index=0)
        assert m1["address"] == m2["address"]
        # índice diferente → address diferente
        m3 = HDWalletManager.from_mnemonic(m1["mnemonic"], index=1)
        assert m3["address"] != m1["address"]
    _check("HDWallet determinístico (auto)", t8)

    # 9) Restaurar estado
    def t9():
        set_sig_scheme("auto")
        assert get_sig_scheme() == "auto"
    _check("set_sig_scheme / get_sig_scheme", t9)

    # Reset para o default
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
