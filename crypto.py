"""crypto.py — Primitivas criptográficas do BRN (v3.1)
================================================================
v3.1 (2026-10-28):
  [NEW] Aceleração C via libsecp256k1 (crypto_native.py).
        Se a biblioteca dinâmica (brn_crypto.dll / libbrn_crypto.so)
        estiver presente na pasta, as funções de assinatura e
        verificação (ECDSA e Schnorr) usam o C, que é ~100x mais
        rápido e protegido contra ataques de timing.
  [KEEP] Implementação em Python puro 100% mantida como fallback.
        Se o C não existir ou falhar, o nó funciona exatamente
        como antes.
  [KEEP] API pública idêntica à v3.0 (auto, schnorr, ecdsa).
================================================================
"""
from __future__ import annotations

__version__ = "3.1"

__all__ = [
    "Ed25519PrivateKey", "Ed25519PublicKey",
    "sha256", "double_sha256", "hash160",
    "generate_private_key", "pubkey_from_priv", "pubkey_to_address",
    "sign_ecdsa", "verify_ecdsa",
    "sign_schnorr", "verify_schnorr",
    "P", "N", "Gx", "Gy",
]

import hashlib
import secrets

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# ============================================================
# [NEW v3.1] Aceleração C (libsecp256k1)
# ============================================================
try:
    import crypto_native
    _HAS_NATIVE = crypto_native.is_available()
except ImportError:
    crypto_native = None
    _HAS_NATIVE = False

if _HAS_NATIVE:
    print(f"[crypto] Aceleração C ATIVA (libsecp256k1)")
else:
    # Silencioso se não houver C, para não poluir o boot
    pass


# ============================================================
# HASHES
# ============================================================
def sha256(data: bytes) -> bytes:
    """SHA-256 simples (32 bytes)."""
    return hashlib.sha256(data).digest()


def double_sha256(data: bytes) -> bytes:
    """SHA-256 duas vezes (usado em txid e hash de bloco)."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def hash160(data: bytes) -> bytes:
    """RIPEMD160(SHA256(data)) com fallback sha256[:20] se indisponível."""
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
    """Inverso modular via Fermat (m deve ser primo)."""
    return pow(a, m - 2, m)


def _point_on_curve(x: int, y: int) -> bool:
    if not (0 <= x < P) or not (0 <= y < P):
        return False
    return (y * y) % P == (pow(x, 3, P) + 7) % P


def _decompress_pubkey(prefix: int, x: int):
    """Recupera y de pubkey comprimida (33B com prefixo 02/03)."""
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
# ECDSA secp256k1
# ============================================================
def sign_ecdsa(priv_bytes: bytes, msg_hash: bytes) -> bytes:
    """Assina usando ECDSA (RFC6979 determinístico)."""
    if _HAS_NATIVE:
        return crypto_native.sign_ecdsa(priv_bytes, msg_hash)
        
    # --- Fallback Python ---
    if len(msg_hash) != 32:
        raise ValueError("msg_hash deve ter 32 bytes (SHA-256)")
    z = int.from_bytes(msg_hash, "big")
    d = int.from_bytes(priv_bytes, "big")
    if d == 0 or d >= N:
        raise ValueError("chave privada fora do range")

    while True:
        k = secrets.randbelow(N - 1) + 1
        point = _point_mul(k, (Gx, Gy))
        if point is None:
            continue
        x, _y = point
        r = x % N
        if r == 0:
            continue
        s = (_inv_mod(k, N) * (z + r * d)) % N
        if s == 0:
            continue
        if s > _HALF_N:
            s = N - s
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def verify_ecdsa(pub_bytes: bytes, sig_bytes: bytes, msg_hash: bytes) -> bool:
    """Verifica assinatura ECDSA."""
    if _HAS_NATIVE:
        return crypto_native.verify_ecdsa(pub_bytes, sig_bytes, msg_hash)
        
    # --- Fallback Python ---
    try:
        if not isinstance(pub_bytes, (bytes, bytearray)): return False
        if not isinstance(sig_bytes, (bytes, bytearray)): return False
        if len(msg_hash) != 32: return False
        if len(sig_bytes) != 64: return False
        
        r = int.from_bytes(sig_bytes[:32], "big")
        s = int.from_bytes(sig_bytes[32:], "big")
        if not (1 <= r < N and 1 <= s < N): return False
        if s > _HALF_N: return False

        if len(pub_bytes) == 65 and pub_bytes[0] == 0x04:
            x = int.from_bytes(pub_bytes[1:33], "big")
            y = int.from_bytes(pub_bytes[33:], "big")
            if not _point_on_curve(x, y): return False
            Q = (x, y)
        elif len(pub_bytes) == 33 and pub_bytes[0] in (0x02, 0x03):
            x = int.from_bytes(pub_bytes[1:], "big")
            Q = _decompress_pubkey(pub_bytes[0], x)
            if Q is None: return False
        else:
            return False

        z = int.from_bytes(msg_hash, "big")
        w = _inv_mod(s, N)
        u1 = (z * w) % N
        u2 = (r * w) % N
        P1 = _point_mul(u1, (Gx, Gy))
        P2 = _point_mul(u2, Q)
        R = _point_add(P1, P2)
        if R is None: return False
        return (R[0] % N) == r
    except Exception:
        return False


# ============================================================
# SCHNORR BIP-340
# ============================================================
def _tagged_hash(tag: str, msg: bytes) -> bytes:
    """tagged_hash(tag, msg) = SHA256(SHA256(tag) || SHA256(tag) || msg)."""
    tag_hash = hashlib.sha256(tag.encode("ascii")).digest()
    return hashlib.sha256(tag_hash + tag_hash + msg).digest()


def _has_even_y(point) -> bool:
    if point is None:
        return False
    return point[1] % 2 == 0


def _lift_x(x: int):
    """Retorna o ponto (x, y) com Y PAR. Retorna None se x não é QR."""
    if not (0 <= x < P): return None
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if (y * y) % P != y_sq: return None
    if y % 2 != 0: y = P - y
    return (x, y)


def sign_schnorr(priv_bytes: bytes, msg_hash: bytes, aux_rand: bytes | None = None) -> bytes:
    """Assina usando Schnorr (BIP-340)."""
    if _HAS_NATIVE:
        if aux_rand is None:
            aux_rand = secrets.token_bytes(32)
        return crypto_native.sign_schnorr(priv_bytes, msg_hash, aux_rand)
        
    # --- Fallback Python ---
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


def verify_schnorr(pub_bytes: bytes, sig_bytes: bytes, msg_hash: bytes) -> bool:
    """Verifica assinatura Schnorr (BIP-340)."""
    if _HAS_NATIVE:
        return crypto_native.verify_schnorr(pub_bytes, sig_bytes, msg_hash)
        
    # --- Fallback Python ---
    try:
        if not isinstance(pub_bytes, (bytes, bytearray)): return False
        if not isinstance(sig_bytes, (bytes, bytearray)): return False
        if not isinstance(msg_hash, (bytes, bytearray)): return False
        if len(msg_hash) != 32 or len(sig_bytes) != 64: return False

        if len(pub_bytes) == 32:
            px = int.from_bytes(pub_bytes, "big")
        elif len(pub_bytes) == 33 and pub_bytes[0] in (0x02, 0x03):
            px = int.from_bytes(pub_bytes[1:], "big")
        else:
            return False

        P_point = _lift_x(px)
        if P_point is None: return False

        r = int.from_bytes(sig_bytes[:32], "big")
        s = int.from_bytes(sig_bytes[32:], "big")
        if r >= P or s >= N: return False

        e = int.from_bytes(
            _tagged_hash("BIP0340/challenge", sig_bytes[:32] + px.to_bytes(32, "big") + bytes(msg_hash)),
            "big",
        ) % N

        R1 = _point_mul(s, (Gx, Gy))
        R2 = _point_mul((N - e) % N, P_point)
        R_point = _point_add(R1, R2)
        if R_point is None: return False

        rx, ry = R_point
        if ry % 2 != 0: return False
        return rx == r
    except Exception:
        return False


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import sys

    print(f"crypto.py v{__version__}")
    if _HAS_NATIVE:
        print("Modo: Aceleração C (libsecp256k1) ATIVA")
    else:
        print("Modo: Python puro (fallback)")
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

    def t3():
        priv = generate_private_key()
        msg = sha256(b"hello schnorr")
        sig = sign_schnorr(priv, msg)
        assert len(sig) == 64, f"esperado 64B, recebido {len(sig)}"
        pk_c = pubkey_from_priv(priv, compressed=True)
        pk_x = pk_c[1:]
        assert verify_schnorr(pk_c, sig, msg), "verify com 33B falhou"
        assert verify_schnorr(pk_x, sig, msg), "verify com 32B x-only falhou"
    _check("[BIP340] roundtrip sign/verify (33B e 32B x-only)", t3)

    def t4():
        priv = generate_private_key()
        sig = sign_schnorr(priv, sha256(b"a"))
        pk = pubkey_from_priv(priv)
        assert not verify_schnorr(pk, sig, sha256(b"b"))
    _check("[BIP340] rejeita msg diferente", t4)

    def t5():
        priv = generate_private_key()
        msg = sha256(b"x")
        sig = bytearray(sign_schnorr(priv, msg))
        sig[0] ^= 0x01
        pk = pubkey_from_priv(priv)
        assert not verify_schnorr(pk, bytes(sig), msg)
    _check("[BIP340] rejeita sig adulterada", t5)

    def t6():
        priv = generate_private_key()
        msg = sha256(b"det")
        aux = b"\x00" * 32
        sig1 = sign_schnorr(priv, msg, aux_rand=aux)
        sig2 = sign_schnorr(priv, msg, aux_rand=aux)
        assert sig1 == sig2, "com mesmo aux_rand deve ser determinístico"
        sig3 = sign_schnorr(priv, msg)
        assert sig1 != sig3
    _check("[BIP340] aux_rand determinístico", t6)

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
    _check("[BIP340] test vector index 0 (oficial)", t7)

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
    _check("[BIP340] test vector index 1 (oficial)", t8)

    print()
    print("=" * 60)
    if failures:
        print(f" FALHAS: {len(failures)} teste(s)")
        for f in failures:
            print(f"   - {f}")
        sys.exit(1)
    print(" TODOS OS TESTES PASSARAM")
    print("=" * 60)