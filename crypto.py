# crypto.py — Primitivas criptográficas do BRN (v3.2)
# ================================================================
# v3.2 (2026-11-17):
#   [FIX CRÍTICO] sign_ecdsa agora é RFC6979 DE VERDADE (k determinístico
#         HMAC-SHA256). Antes dizia RFC6979 mas usava secrets.randbelow().
#   [FIX] Chamadas nativas com try/except -> se o C falhar, cai pro Python
#         em vez de matar o nó.
#   [FIX] print() no import trocado por logging (não polui o boot).
#   [KEEP] v3.1: aceleração C, fallback Python, Schnorr BIP-340, testes.
# ================================================================
from __future__ import annotations

__version__ = "3.2"

__all__ = [
    "Ed25519PrivateKey", "Ed25519PublicKey",
    "sha256", "double_sha256", "hash160",
    "generate_private_key", "pubkey_from_priv", "pubkey_to_address",
    "sign_ecdsa", "verify_ecdsa",
    "sign_schnorr", "verify_schnorr",
    "P", "N", "Gx", "Gy",
]

import hashlib
import hmac as _hmac
import logging
import secrets

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

log = logging.getLogger("crypto")

# ============================================================
# Aceleração C (libsecp256k1)
# ============================================================
try:
    import crypto_native
    _HAS_NATIVE = crypto_native.is_available()
except ImportError:
    crypto_native = None
    _HAS_NATIVE = False

if _HAS_NATIVE:
    log.info("[crypto] Aceleração C ATIVA (libsecp256k1)")
else:
    log.debug("[crypto] Sem aceleração C — usando Python puro")

# ============================================================
# HASHES
# ============================================================
def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()

def double_sha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()

def hash160(data: bytes) -> bytes:
    h = hashlib.sha256(data).digest()
    try:
        r = hashlib.new("ripemd160")
        r.update(h)
        return r.digest()
    except ValueError:
        return h[:20]

# ============================================================
# SECP256K1 — parâmetros
# ============================================================
P  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_HALF_N = N // 2

# ============================================================
# ARITMÉTICA DE CURVA (Fallback Python)
# ============================================================
def _inv_mod(a: int, m: int) -> int:
    return pow(a, m - 2, m)

def _point_on_curve(x: int, y: int) -> bool:
    if not (0 <= x < P) or not (0 <= y < P):
        return False
    return (y * y) % P == (pow(x, 3, P) + 7) % P

def _decompress_pubkey(prefix: int, x: int):
    if prefix not in (0x02, 0x03):
        return None
    if not (0 <= x < P):
        return None
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if (y * y) % P != y_sq:
        return None
    if (y % 2 == 0) != (prefix == 0x02):
        y = P - y
    return (x, y)

def _point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if p1 == p2:
        if y1 == 0:
            return None
        lam = (3 * x1 * x1) * _inv_mod(2 * y1, P) % P
    else:
        lam = (y2 - y1) * _inv_mod(x2 - x1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)

def _point_mul(k: int, point):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result

# ============================================================
# RFC6979 — k determinístico (o que faltava de verdade)
# RFC6979 §3.2: HMAC_DRBG com priv + bits2octets(msg)
# ============================================================
def _rfc6979_k(priv_bytes: bytes, msg_hash: bytes) -> int:
    x = int.from_bytes(priv_bytes, "big")
    h1 = int.from_bytes(msg_hash, "big")
    # bits2octets(h1) = h1 mod N
    h1_oct = (h1 % N).to_bytes(32, "big")
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = _hmac.new(k, v + b"\x00" + priv_bytes + h1_oct, hashlib.sha256).digest()
    v = _hmac.new(k, v, hashlib.sha256).digest()
    k = _hmac.new(k, v + b"\x01" + priv_bytes + h1_oct, hashlib.sha256).digest()
    v = _hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = _hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < N:
            return cand
        k = _hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = _hmac.new(k, v, hashlib.sha256).digest()

# ============================================================
# CHAVE PRIVADA / PUBKEY
# ============================================================
def generate_private_key() -> bytes:
    while True:
        k = secrets.token_bytes(32)
        val = int.from_bytes(k, "big")
        if 1 <= val < N:
            return k

def pubkey_from_priv(priv_bytes: bytes, compressed: bool = True) -> bytes:
    k = int.from_bytes(priv_bytes, "big")
    if k == 0 or k >= N:
        raise ValueError("chave privada fora do range")
    point = _point_mul(k, (Gx, Gy))
    if point is None:
        raise ValueError("ponto no infinito (privkey inválida?)")
    x, y = point
    if compressed:
        prefix = b"\x02" if y % 2 == 0 else b"\x03"
        return prefix + x.to_bytes(32, "big")
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")

def pubkey_to_address(pubkey_hex: str) -> str:
    from bech32 import address_from_pubkey
    return address_from_pubkey(bytes.fromhex(pubkey_hex))

# ============================================================
# ECDSA secp256k1 — RFC6979
# ============================================================
def _sign_ecdsa_py(priv_bytes: bytes, msg_hash: bytes) -> bytes:
    if len(msg_hash) != 32:
        raise ValueError("msg_hash deve ter 32 bytes (SHA-256)")
    z = int.from_bytes(msg_hash, "big")
    d = int.from_bytes(priv_bytes, "big")
    if d == 0 or d >= N:
        raise ValueError("chave privada fora do range")
    # RFC6979: k determinístico. Se r ou s == 0, avança (RFC6979 permite
    # re-gerar com extra entropy — aqui usamos contador simples).
    extra = 0
    while True:
        if extra == 0:
            k = _rfc6979_k(priv_bytes, msg_hash)
        else:
            # k' = RFC6979(priv || msg || extra) — determinístico também
            k = _rfc6979_k(priv_bytes, sha256(msg_hash + extra.to_bytes(4, "big")))
        point = _point_mul(k, (Gx, Gy))
        if point is None:
            extra += 1
            continue
        x, _y = point
        r = x % N
        if r == 0:
            extra += 1
            continue
        s = (_inv_mod(k, N) * (z + r * d)) % N
        if s == 0:
            extra += 1
            continue
        if s > _HALF_N:
            s = N - s
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

def sign_ecdsa(priv_bytes: bytes, msg_hash: bytes) -> bytes:
    """Assina usando ECDSA (RFC6979 determinístico)."""
    if _HAS_NATIVE:
        try:
            return crypto_native.sign_ecdsa(priv_bytes, msg_hash)
        except Exception as e:
            log.warning("[crypto] C sign_ecdsa falhou (%s) — usando Python", e)
    return _sign_ecdsa_py(priv_bytes, msg_hash)

def verify_ecdsa(pub_bytes: bytes, sig_bytes: bytes, msg_hash: bytes) -> bool:
    """Verifica assinatura ECDSA."""
    if _HAS_NATIVE:
        try:
            return bool(crypto_native.verify_ecdsa(pub_bytes, sig_bytes, msg_hash))
        except Exception as e:
            log.warning("[crypto] C verify_ecdsa falhou (%s) — usando Python", e)
    try:
        if not isinstance(pub_bytes, (bytes, bytearray)):
            return False
        if not isinstance(sig_bytes, (bytes, bytearray)):
            return False
        if len(msg_hash) != 32:
            return False
        if len(sig_bytes) != 64:
            return False
        r = int.from_bytes(sig_bytes[:32], "big")
        s = int.from_bytes(sig_bytes[32:], "big")
        if not (1 <= r < N and 1 <= s < N):
            return False
        if s > _HALF_N:
            return False
        if len(pub_bytes) == 65 and pub_bytes[0] == 0x04:
            x = int.from_bytes(pub_bytes[1:33], "big")
            y = int.from_bytes(pub_bytes[33:], "big")
            if not _point_on_curve(x, y):
                return False
            Q = (x, y)
        elif len(pub_bytes) == 33 and pub_bytes[0] in (0x02, 0x03):
            x = int.from_bytes(pub_bytes[1:], "big")
            Q = _decompress_pubkey(pub_bytes[0], x)
            if Q is None:
                return False
        else:
            return False
        z = int.from_bytes(msg_hash, "big")
        w = _inv_mod(s, N)
        u1 = (z * w) % N
        u2 = (r * w) % N
        P1 = _point_mul(u1, (Gx, Gy))
        P2 = _point_mul(u2, Q)
        R = _point_add(P1, P2)
        if R is None:
            return False
        return (R[0] % N) == r
    except Exception:
        return False

# ============================================================
# SCHNORR BIP-340
# ============================================================
def _tagged_hash(tag: str, msg: bytes) -> bytes:
    tag_hash = hashlib.sha256(tag.encode("ascii")).digest()
    return hashlib.sha256(tag_hash + tag_hash + msg).digest()

def _lift_x(x: int):
    if not (0 <= x < P):
        return None
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if (y * y) % P != y_sq:
        return None
    if y % 2 != 0:
        y = P - y
    return (x, y)

def _sign_schnorr_py(priv_bytes: bytes, msg_hash: bytes, aux_rand: bytes | None = None) -> bytes:
    if not isinstance(priv_bytes, (bytes, bytearray)) or len(priv_bytes) != 32:
        raise ValueError("priv_bytes deve ter 32 bytes")
    if not isinstance(msg_hash, (bytes, bytearray)) or len(msg_hash) != 32:
        raise ValueError("msg_hash deve ter 32 bytes")
    if aux_rand is None:
        aux_rand = secrets.token_bytes(32)
    if not isinstance(aux_rand, (bytes, bytearray)) or len(aux_rand) != 32:
        raise ValueError("aux_rand deve ter 32 bytes")
    d_prime = int.from_bytes(priv_bytes, "big")
    if not (1 <= d_prime < N):
        raise ValueError("chave privada fora do range secp256k1")
    P_point = _point_mul(d_prime, (Gx, Gy))
    if P_point is None:
        raise ValueError("ponto no infinito (privkey inválida)")
    px, py = P_point
    d = d_prime if py % 2 == 0 else (N - d_prime)
    aux_hash = _tagged_hash("BIP0340/aux", bytes(aux_rand))
    t = bytes(a ^ b for a, b in zip(d.to_bytes(32, "big"), aux_hash))
    rand = _tagged_hash("BIP0340/nonce", t + px.to_bytes(32, "big") + bytes(msg_hash))
    k_prime = int.from_bytes(rand, "big") % N
    if k_prime == 0:
        raise ValueError("nonce k' = 0 (extremamente improvável); tente novamente")
    R_point = _point_mul(k_prime, (Gx, Gy))
    if R_point is None:
        raise ValueError("R no infinito (nonce inválido)")
    rx, ry = R_point
    k = k_prime if ry % 2 == 0 else (N - k_prime)
    e = int.from_bytes(
        _tagged_hash("BIP0340/challenge", rx.to_bytes(32, "big") + px.to_bytes(32, "big") + bytes(msg_hash)),
        "big",
    ) % N
    s = (k + e * d) % N
    return rx.to_bytes(32, "big") + s.to_bytes(32, "big")

def sign_schnorr(priv_bytes: bytes, msg_hash: bytes, aux_rand: bytes | None = None) -> bytes:
    """Assina usando Schnorr (BIP-340)."""
    if _HAS_NATIVE:
        try:
            if aux_rand is None:
                aux_rand = secrets.token_bytes(32)
            return crypto_native.sign_schnorr(priv_bytes, msg_hash, aux_rand)
        except Exception as e:
            log.warning("[crypto] C sign_schnorr falhou (%s) — usando Python", e)
    return _sign_schnorr_py(priv_bytes, msg_hash, aux_rand)

def verify_schnorr(pub_bytes: bytes, sig_bytes: bytes, msg_hash: bytes) -> bool:
    """Verifica assinatura Schnorr (BIP-340)."""
    if _HAS_NATIVE:
        try:
            return bool(crypto_native.verify_schnorr(pub_bytes, sig_bytes, msg_hash))
        except Exception as e:
            log.warning("[crypto] C verify_schnorr falhou (%s) — usando Python", e)
    try:
        if not isinstance(pub_bytes, (bytes, bytearray)):
            return False
        if not isinstance(sig_bytes, (bytes, bytearray)):
            return False
        if not isinstance(msg_hash, (bytes, bytearray)):
            return False
        if len(msg_hash) != 32 or len(sig_bytes) != 64:
            return False
        if len(pub_bytes) == 32:
            px = int.from_bytes(pub_bytes, "big")
        elif len(pub_bytes) == 33 and pub_bytes[0] in (0x02, 0x03):
            px = int.from_bytes(pub_bytes[1:], "big")
        else:
            return False
        P_point = _lift_x(px)
        if P_point is None:
            return False
        r = int.from_bytes(sig_bytes[:32], "big")
        s = int.from_bytes(sig_bytes[32:], "big")
        if r >= P or s >= N:
            return False
        e = int.from_bytes(
            _tagged_hash("BIP0340/challenge", sig_bytes[:32] + px.to_bytes(32, "big") + bytes(msg_hash)),
            "big",
        ) % N
        R1 = _point_mul(s, (Gx, Gy))
        R2 = _point_mul((N - e) % N, P_point)
        R_point = _point_add(R1, R2)
        if R_point is None:
            return False
        rx, ry = R_point
        if ry % 2 != 0:
            return False
        return rx == r
    except Exception:
        return False

# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    print(f"crypto.py v{__version__}")
    print("Modo:", "Aceleração C ATIVA" if _HAS_NATIVE else "Python puro (fallback)")
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
        priv = generate_private_key()
        msg = sha256(b"hello brn")
        sig = sign_ecdsa(priv, msg)
        assert len(sig) == 64
        pk_c = pubkey_from_priv(priv, compressed=True)
        pk_u = pubkey_from_priv(priv, compressed=False)
        assert verify_ecdsa(pk_c, sig, msg)
        assert verify_ecdsa(pk_u, sig, msg)
    _check("[ECDSA] roundtrip 33/65 bytes", t1)

    def t2():
        priv = generate_private_key()
        sig = sign_ecdsa(priv, sha256(b"a"))
        pk = pubkey_from_priv(priv)
        assert not verify_ecdsa(pk, sig, sha256(b"b"))
    _check("[ECDSA] rejeita msg diferente", t2)

    def t2b():
        # RFC6979: mesma priv + mesma msg = mesma assinatura
        priv = bytes.fromhex("11" * 32)
        msg = sha256(b"rfc6979")
        assert sign_ecdsa(priv, msg) == sign_ecdsa(priv, msg)
    _check("[ECDSA] RFC6979 determinístico", t2b)

    def t3():
        priv = generate_private_key()
        msg = sha256(b"hello schnorr")
        sig = sign_schnorr(priv, msg)
        assert len(sig) == 64
        pk_c = pubkey_from_priv(priv, compressed=True)
        pk_x = pk_c[1:]
        assert verify_schnorr(pk_c, sig, msg)
        assert verify_schnorr(pk_x, sig, msg)
    _check(" roundtrip (33B e 32B x-only)", t3)

    def t4():
        priv = generate_private_key()
        sig = sign_schnorr(priv, sha256(b"a"))
        pk = pubkey_from_priv(priv)
        assert not verify_schnorr(pk, sig, sha256(b"b"))
    _check(" rejeita msg diferente", t4)

    def t5():
        priv = generate_private_key()
        msg = sha256(b"x")
        sig = bytearray(sign_schnorr(priv, msg))
        sig[0] ^= 0x01
        pk = pubkey_from_priv(priv)
        assert not verify_schnorr(pk, bytes(sig), msg)
    _check(" rejeita sig adulterada", t5)

    def t6():
        priv = generate_private_key()
        msg = sha256(b"det")
        aux = b"\x00" * 32
        sig1 = sign_schnorr(priv, msg, aux_rand=aux)
        sig2 = sign_schnorr(priv, msg, aux_rand=aux)
        assert sig1 == sig2
    _check(" aux_rand determinístico", t6)

    def t7():
        seckey = bytes.fromhex("0000000000000000000000000000000000000000000000000000000000000003")
        expected_pub = "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9"
        aux = bytes(32)
        msg = bytes(32)
        expected_sig = (
            "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
            "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0"
        )
        pub_c = pubkey_from_priv(seckey, compressed=True)
        pub_x = pub_c[1:]
        assert pub_x.hex().upper() == expected_pub
        sig = sign_schnorr(seckey, msg, aux_rand=aux)
        assert sig.hex().upper() == expected_sig, f"\n  obtido={sig.hex().upper()}\n  esper={expected_sig}"
        assert verify_schnorr(pub_x, sig, msg)
    _check(" test vector index 0 (oficial)", t7)

    def t8():
        seckey = bytes.fromhex("B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF")
        expected_pub = "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659"
        aux = bytes.fromhex("0000000000000000000000000000000000000000000000000000000000000001")
        msg = bytes.fromhex("243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89")
        expected_sig = (
            "6896BD60EEAE296DB48A229FF71DFE071BDE413E6D43F917DC8DCF8C78DE3341"
            "8906D11AC976ABCCB20B091292BFF4EA897EFCB639EA871CFA95F6DE339E4B0A"
        )
        pub_c = pubkey_from_priv(seckey, compressed=True)
        pub_x = pub_c[1:]
        assert pub_x.hex().upper() == expected_pub
        sig = sign_schnorr(seckey, msg, aux_rand=aux)
        assert sig.hex().upper() == expected_sig
        assert verify_schnorr(pub_x, sig, msg)
    _check(" test vector index 1 (oficial)", t8)

    print()
    print("=" * 60)
    if failures:
        print(f" FALHAS: {len(failures)} teste(s)")
        for f in failures:
            print(f"   - {f}")
        sys.exit(1)
    print(" TODOS OS TESTES PASSARAM")
    print("=" * 60)
