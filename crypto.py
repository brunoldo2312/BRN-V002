"""crypto.py — Primitivas criptográficas do BRN (v2.0)
================================================================
v2.0 (2026-10-27):
  [FIX CRÍTICO] verify_ecdsa() agora VALIDA que a pubkey está na
                curva secp256k1:
                  - 65 bytes (não comprimida): confere y² ≡ x³+7 mod P
                  - 33 bytes (comprimida):     confere y² == x³+7 mod P
                    após recuperar y via sqrt (rejeita x³+7 que NÃO é
                    resíduo quadrático — antes era aceito silenciosamente)
                  - x, y em [0, P)
                  - ponto não é o infinito
  [FIX] _point_add: se p1 == p2 e y1 == 0, retorna None (infinito).
        Defesa em profundidade — em secp256k1 (ordem prima) isso não
        ocorre, mas evita `_inv_mod(0, P) == 0` gerar ponto inválido.
  [FIX] pubkey_from_priv: valida retorno de _point_mul.
  [NEW] _point_on_curve(x, y) — helper usado na validação de pubkey.
  [NEW] _decompress_pubkey() — recuperação de y com validação de QR.
  [NEW] __version__, __all__, docstrings.
  [KEEP] API 100% compatível com v1: assinaturas, retornos e nomes
         inalterados. Aliases sign_schnorr/verify_schnorr mantidos
         (NÃO são BIP340 — são ECDSA; ver comentário).
================================================================
"""
from __future__ import annotations

__version__ = "2.0"

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
# ARITMÉTICA DE CURVA
# ============================================================
def _inv_mod(a: int, m: int) -> int:
    """Inverso modular via Fermat (m deve ser primo)."""
    return pow(a, m - 2, m)


def _point_on_curve(x: int, y: int) -> bool:
    """
    [NEW v2.0] True se (x, y) satisfaz y² ≡ x³ + 7 (mod P).
    Também exige x, y em [0, P) e não é o ponto no infinito
    (representado fora desta função como None).
    """
    if not (0 <= x < P) or not (0 <= y < P):
        return False
    lhs = (y * y) % P
    rhs = (pow(x, 3, P) + 7) % P
    return lhs == rhs


def _decompress_pubkey(prefix: int, x: int):
    """
    [NEW v2.0] Recupera y de uma pubkey comprimida.
    Retorna (x, y) se válido, None caso contrário.

    Valida:
      - prefix ∈ {0x02, 0x03}
      - x ∈ [0, P)
      - y_sq = x³+7 mod P é resíduo quadrático (y² == y_sq após sqrt)
      - paridade de y bate com o prefixo
    """
    if prefix not in (0x02, 0x03):
        return None
    if not (0 <= x < P):
        return None
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)  # válido porque P ≡ 3 mod 4
    # [FIX v2.0] confirmar que y_sq é realmente QR
    if (y * y) % P != y_sq:
        return None
    if (y % 2 == 0) != (prefix == 0x02):
        y = P - y
    return (x, y)


def _point_add(p1, p2):
    """Soma de dois pontos. None = ponto no infinito."""
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if p1 == p2:
        # [FIX v2.0] defesa: y1 == 0 implicaria infinito (não ocorre em
        # secp256k1, mas evita _inv_mod(0, P) == 0 gerar ponto espúrio).
        if y1 == 0:
            return None
        lam = (3 * x1 * x1) * _inv_mod(2 * y1, P) % P
    else:
        lam = (y2 - y1) * _inv_mod(x2 - x1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)


def _point_mul(k: int, point):
    """Multiplicação escalar. Retorna None = infinito."""
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
    """Gera 32 bytes de chave privada válida para secp256k1."""
    while True:
        k = secrets.token_bytes(32)
        val = int.from_bytes(k, "big")
        if 1 <= val < N:
            return k


def pubkey_from_priv(priv_bytes: bytes, compressed: bool = True) -> bytes:
    """
    Deriva a pubkey secp256k1 a partir da privkey.
    compressed=True → 33 bytes (0x02/0x03 || x)
    compressed=False → 65 bytes (0x04 || x || y)
    """
    k = int.from_bytes(priv_bytes, "big")
    if k == 0 or k >= N:
        raise ValueError("chave privada fora do range")
    point = _point_mul(k, (Gx, Gy))
    # [FIX v2.0] _point_mul nunca deveria retornar None para k válido,
    # mas defendemos o caso.
    if point is None:
        raise ValueError("ponto no infinito (privkey inválida?)")
    x, y = point
    if compressed:
        prefix = b"\x02" if y % 2 == 0 else b"\x03"
        return prefix + x.to_bytes(32, "big")
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def pubkey_to_address(pubkey_hex: str) -> str:
    """Deriva endereço Bech32 (brn1...) a partir da pubkey em hex."""
    from bech32 import address_from_pubkey
    return address_from_pubkey(bytes.fromhex(pubkey_hex))


# ============================================================
# ECDSA secp256k1
# ============================================================
def sign_ecdsa(priv_bytes: bytes, msg_hash: bytes) -> bytes:
    """
    Assina o hash (32 bytes). Retorna r||s (64 bytes) com low-S.
    Levanta ValueError para chave inválida.
    """
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
            continue  # probabilidade ~0, mas não trava
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
    """
    Verifica assinatura ECDSA (r||s, low-S).

    [FIX v2.0] Valida a pubkey:
      - comprimento 33 ou 65
      - prefixo correto
      - ponto realmente na curva secp256k1
      - x, y em [0, P)
    Rejeita: sig != 64 bytes, r/s fora de [1, N-1], s > N/2, ponto no
    infinito, R == None, mismatch de r, msg_hash != 32 bytes.
    """
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

        # ---------- Recuperar e VALIDAR Q ----------
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

        # ---------- Verificação ECDSA ----------
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
# ALIASES (compatibilidade — NÃO são BIP340 real)
# ------------------------------------------------------------
# Estes aliases existem para compatibilidade com chamadas antigas.
# Eles usam ECDSA secp256k1, NÃO Schnorr/BIP340. Não use para
# interoperabilidade com Bitcoin Taproot ou qualquer coisa que
# exija BIP340 de verdade.
# ============================================================
sign_schnorr = sign_ecdsa
verify_schnorr = verify_ecdsa


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import sys

    print(f"crypto.py v{__version__}")
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

    # 1) Roundtrip sign/verify (comprimida e não comprimida)
    def t1():
        priv = generate_private_key()
        msg = sha256(b"hello brn")
        sig = sign_ecdsa(priv, msg)
        assert len(sig) == 64
        pk_c = pubkey_from_priv(priv, compressed=True)
        pk_u = pubkey_from_priv(priv, compressed=False)
        assert len(pk_c) == 33
        assert len(pk_u) == 65
        assert verify_ecdsa(pk_c, sig, msg), "comprimida deveria verificar"
        assert verify_ecdsa(pk_u, sig, msg), "não comprimida deveria verificar"
    _check("roundtrip sign/verify (33 e 65 bytes)", t1)

    # 2) Mensagem errada
    def t2():
        priv = generate_private_key()
        sig = sign_ecdsa(priv, sha256(b"a"))
        pk = pubkey_from_priv(priv)
        assert not verify_ecdsa(pk, sig, sha256(b"b"))
    _check("rejeita mensagem diferente", t2)

    # 3) Assinatura adulterada
    def t3():
        priv = generate_private_key()
        msg = sha256(b"x")
        sig = bytearray(sign_ecdsa(priv, msg))
        sig[0] ^= 0xFF
        pk = pubkey_from_priv(priv)
        assert not verify_ecdsa(pk, bytes(sig), msg)
    _check("rejeita assinatura adulterada", t3)

    # 4) [FIX v2.0] pubkey não comprimida fora da curva
    def t4():
        priv = generate_private_key()
        msg = sha256(b"y")
        sig = sign_ecdsa(priv, msg)
        pk = bytearray(pubkey_from_priv(priv, compressed=False))
        # corromper y
        pk[64] ^= 0x01
        assert not verify_ecdsa(bytes(pk), sig, msg), \
            "pubkey fora da curva NÃO pode passar"
    _check("[FIX] rejeita pubkey 65B fora da curva", t4)

    # 5) [FIX v2.0] pubkey comprimida cujo x³+7 NÃO é QR
    def t5():
        # escolhe x arbitrário e testa se a recuperação falha
        # varre até achar um x onde x³+7 não é QR
        found = None
        for x in range(1, 50):
            y_sq = (pow(x, 3, P) + 7) % P
            y = pow(y_sq, (P + 1) // 4, P)
            if (y * y) % P != y_sq:
                found = x
                break
        assert found is not None, "nenhum x encontrado (improvável)"
        fake_pk = b"\x02" + found.to_bytes(32, "big")
        msg = sha256(b"z")
        # qualquer assinatura deve ser rejeitada
        fake_sig = b"\x01" + b"\x00" * 31 + b"\x01" + b"\x00" * 31
        assert not verify_ecdsa(fake_pk, fake_sig, msg), \
            "pubkey comprimida não-QR NÃO pode passar"
    _check("[FIX] rejeita pubkey 33B não-QR", t5)

    # 6) Tamanhos inválidos
    def t6():
        priv = generate_private_key()
        msg = sha256(b"w")
        sig = sign_ecdsa(priv, msg)
        pk = pubkey_from_priv(priv)
        assert not verify_ecdsa(b"", sig, msg)
        assert not verify_ecdsa(pk, b"", msg)
        assert not verify_ecdsa(pk, sig + b"\x00", msg)
        assert not verify_ecdsa(b"\x04" + b"\x00" * 64, sig, msg)
        assert not verify_ecdsa(pk, sig, b"")
        assert not verify_ecdsa(pk, sig, b"short")
    _check("rejeita tamanhos inválidos", t6)

    # 7) r=0, s=0, high-S
    def t7():
        priv = generate_private_key()
        msg = sha256(b"v")
        pk = pubkey_from_priv(priv)
        assert not verify_ecdsa(pk, b"\x00" * 64, msg)  # r=s=0
        sig = sign_ecdsa(priv, msg)
        r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
        # high-S: s' = N - s (não é o mesmo sig válido)
        high_s = r.to_bytes(32, "big") + (N - s).to_bytes(32, "big")
        assert not verify_ecdsa(pk, high_s, msg), "high-S deve ser rejeitado"
    _check("rejeita r=0, s=0, high-S", t7)

    # 8) low-S está sendo produzido
    def t8():
        for _ in range(5):
            priv = generate_private_key()
            msg = sha256(secrets.token_bytes(16))
            sig = sign_ecdsa(priv, msg)
            s = int.from_bytes(sig[32:], "big")
            assert s <= _HALF_N, "sign_ecdsa deve produzir low-S"
    _check("sign_ecdsa produz low-S", t8)

    # 9) hash160 e pubkey_to_address
    def t9():
        assert len(hash160(b"test")) == 20
        priv = generate_private_key()
        pk = pubkey_from_priv(priv, compressed=False)
        addr = pubkey_to_address(pk.hex())
        assert addr.startswith("brn1")
        from bech32 import validate_address
        assert validate_address(addr)
    _check("hash160 e pubkey_to_address", t9)

    # 10) Aliases (compat)
    def t10():
        priv = generate_private_key()
        msg = sha256(b"alias")
        sig = sign_schnorr(priv, msg)
        pk = pubkey_from_priv(priv)
        assert verify_schnorr(pk, sig, msg)
    _check("aliases sign_schnorr/verify_schnorr", t10)

    # 11) _point_on_curve direto
    def t11():
        assert _point_on_curve(Gx, Gy)
        assert not _point_on_curve(Gx, Gy + 1)
        assert not _point_on_curve(-1, 0)
        assert not _point_on_curve(P, 0)
    _check("_point_on_curve", t11)

    # 12) pubkey_from_priv rejeita chave inválida
    def t12():
        for bad in (b"\x00" * 32, (N).to_bytes(32, "big")):
            try:
                pubkey_from_priv(bad)
                raise AssertionError(f"deveria rejeitar {bad.hex()[:8]}")
            except ValueError:
                pass
    _check("pubkey_from_priv rejeita privkey inválida", t12)

    print()
    print("=" * 60)
    if failures:
        print(f" FALHAS: {len(failures)} teste(s)")
        for f in failures:
            print(f"   - {f}")
        sys.exit(1)
    print(" TODOS OS TESTES PASSARAM")
    print("=" * 60)
