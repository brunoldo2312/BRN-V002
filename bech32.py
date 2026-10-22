"""bech32.py — Codificação Bech32 para endereços BRN (prefixo brn1)
================================================================
v2 (2026-10-22):
  [NEW] __version__, __all__
  [NEW] Docstrings em todas as funções (públicas e internas)
  [NEW] bech32_decode_strict(bech) — rejeita case misto (BIP173)
        O bech32_decode() original continua aceitando qualquer case
        (lower() no início) — backward compatible.
  [NEW] decode_address(addr) -> bytes | None — retorna o hash160
        (20 bytes) por trás do endereço. Útil para testes.
  [NEW] addr_short(addr, n=12) — retorna "brn1xxxx...yyyy" para logs
  [NEW] Auto-test com endereços inválidos (BTC, BNB, checksum bad,
        mixed case, char inválido)
  [FIX] Auto-test não quebra se crypto não tiver generate_private_key
  [KEEP] Encoding/decoding idêntico ao v1 — endereços existentes
         continuam válidos (não muda NADA no output)
================================================================
"""
from __future__ import annotations

__version__ = "2.0"

__all__ = [
    "CHARSET", "CHARSET_REV", "HRP",
    "bech32_encode", "bech32_decode", "bech32_decode_strict",
    "address_from_pubkey", "decode_address", "validate_address",
    "addr_short",
]


# ============================================================
# CONSTANTES (BIP173)
# ============================================================
CHARSET     = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
CHARSET_REV = {c: i for i, c in enumerate(CHARSET)}
BECH32_CONST = 1
HRP = "brn"


# ============================================================
# POLYNOMIAL / CHECKSUM (BIP173 reference implementation)
# ============================================================
def _polymod(values):
    """BIP173 polymod — checksum do Bech32."""
    generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp: str):
    """Expansão do HRP para o checksum (BIP173)."""
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _verify_checksum(hrp: str, data) -> bool:
    return _polymod(_hrp_expand(hrp) + data) == BECH32_CONST


def _create_checksum(hrp: str, data):
    values = _hrp_expand(hrp) + data
    polymod = _polymod(values + [0, 0, 0, 0, 0, 0]) ^ BECH32_CONST
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


# ============================================================
# CONVERSÃO DE BITS
# ============================================================
def _convertbits(data, frombits: int, tobits: int, pad: bool = True):
    """Conversão de grupos de bits (BIP173 reference)."""
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


# ============================================================
# ENCODE / DECODE
# ============================================================
def bech32_encode(hrp: str, data) -> str:
    """
    Codifica data (lista de valores 5-bit) + checksum.
    Retorna lowercase (hrp é lowercased defensivamente).
    """
    hrp = hrp.lower()
    combined = data + _create_checksum(hrp, data)
    return hrp + "1" + "".join([CHARSET[d] for d in combined])


def bech32_decode(bech: str):
    """
    Decodifica Bech32. Retorna (hrp, data) ou (None, None).
    Compatível com v1: aceita qualquer case (faz lower antes).
    """
    if not isinstance(bech, str):
        return (None, None)
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        return (None, None)
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech):
        return (None, None)
    hrp = bech[:pos]
    data = []
    for c in bech[pos + 1:]:
        if c not in CHARSET_REV:
            return (None, None)
        data.append(CHARSET_REV[c])
    if not _verify_checksum(hrp, data):
        return (None, None)
    return (hrp, data[:-6])


def bech32_decode_strict(bech: str):
    """
    [NEW v2] Decodifica Bech32 rejeitando case misto (BIP173 compliance).
    Um endereço válido deve ser all-lowercase OU all-uppercase, nunca
    "bRn1qqq". Retorna (hrp, data) ou (None, None).
    """
    if not isinstance(bech, str):
        return (None, None)
    if bech.lower() != bech and bech.upper() != bech:
        return (None, None)
    return bech32_decode(bech)


# ============================================================
# API BRN
# ============================================================
def address_from_pubkey(pubkey: bytes) -> str:
    """
    Deriva endereço Bech32 (brn1...) a partir da pubkey (bytes).
    pubkey pode ser Ed25519 (32 bytes) ou qualquer outro tamanho —
    hash160 é aplicado antes da codificação.
    """
    from crypto import hash160
    h = hash160(pubkey)
    data = _convertbits(list(h), 8, 5, True)
    return bech32_encode(HRP, data)


def decode_address(addr: str):
    """
    [NEW v2] Decodifica um endereço BRN e retorna o hash160 (20 bytes).
    Retorna None se inválido.
    """
    try:
        if not isinstance(addr, str) or not addr.startswith(HRP + "1"):
            return None
        hrp, data = bech32_decode(addr)
        if hrp != HRP or data is None:
            return None
        decoded = _convertbits(data, 5, 8, False)
        if decoded is None or len(decoded) != 20:
            return None
        return bytes(decoded)
    except Exception:
        return None


def validate_address(addr) -> bool:
    """True se for um endereço BRN válido (brn1..., hash160 20 bytes)."""
    try:
        if not isinstance(addr, str) or not addr.startswith(HRP + "1"):
            return False
        if len(addr) < 4 or len(addr) > 128:
            return False
        hrp, data = bech32_decode(addr)
        if hrp != HRP or data is None:
            return False
        decoded = _convertbits(data, 5, 8, False)
        if decoded is None or len(decoded) != 20:
            return False
        return True
    except Exception:
        return False


def addr_short(addr: str, n: int = 12) -> str:
    """
    [NEW v2] Versão encurtada para log/UI: "brn1xxxx...yyyy"
    Se o endereço for menor que n*2+3, retorna sem truncar.
    """
    if not addr or not isinstance(addr, str):
        return ""
    if len(addr) <= n * 2 + 3:
        return addr
    return f"{addr[:n]}...{addr[-4:]}"


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import sys

    print(f"bech32.py v{__version__}")
    print("=" * 60)

    # 1. Roundtrip com hash160 sintético
    test_hash = bytes(range(20))  # 0x00..0x13
    data = _convertbits(list(test_hash), 8, 5, True)
    addr = bech32_encode(HRP, data)
    print(f"[1] Test addr : {addr}")
    assert validate_address(addr), "deveria ser válido"
    assert decode_address(addr) == test_hash, "roundtrip falhou"
    print(f"    [ok] roundtrip encode/decode")

    # 2. Uppercase aceito (BIP173: mesma semântica)
    addr_upper = addr.upper()
    assert validate_address(addr_upper), "uppercase deveria ser válido"
    print(f"    [ok] uppercase aceito")

    # 3. Mixed case: validate_address aceita (compat v1)
    mixed = addr[:10] + addr[10:].upper()
    assert validate_address(mixed), "mixed aceito em validate (compat v1)"

    # 4. bech32_decode_strict rejeita mixed case
    hrp_s, data_s = bech32_decode_strict(mixed)
    assert hrp_s is None and data_s is None, "strict deveria rejeitar"
    print(f"    [ok] bech32_decode_strict rejeita mixed case")

    # 5. bech32_decode_strict aceita all-lowercase
    hrp_s, data_s = bech32_decode_strict(addr)
    assert hrp_s == HRP and data_s is not None
    print(f"    [ok] bech32_decode_strict aceita all-lowercase")

    # 6. address_from_pubkey com pubkey real (se crypto disponível)
    try:
        from crypto import generate_private_key, pubkey_from_priv
        priv = generate_private_key()
        pub = pubkey_from_priv(priv)
    except (ImportError, AttributeError):
        try:
            from crypto import Ed25519PrivateKey
            sk = Ed25519PrivateKey.generate()
            pub = sk.public_key().public_bytes_raw()
        except Exception as e:
            pub = None
            print(f"    [skip] crypto indisponível ({e})")

    if pub:
        addr_real = address_from_pubkey(pub)
        assert validate_address(addr_real), "addr real inválido"
        assert decode_address(addr_real) is not None
        print(f"    [ok] address_from_pubkey: {addr_short(addr_real, 16)}")

    # 7. Endereços inválidos
    invalid_addrs = [
        "",
        "brn1",
        "xxx1yyy",
        addr[:-1] + "z",
        "brn1" + "q" * 60,
        "brn1" + "!" * 50,
        "bnb1grpf0955h0ykzq3ar5nmum7y6gdfl6lxfn46h2",
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
    ]
    for bad in invalid_addrs:
        assert not validate_address(bad), f"deveria rejeitar: {bad!r}"
    print(f"    [ok] {len(invalid_addrs)} endereços inválidos rejeitados")

    # 8. addr_short
    assert addr_short("") == ""
    assert addr_short("short") == "short"
    assert "..." in addr_short(addr, 8)
    print(f"    [ok] addr_short")

    print()
    print("=" * 60)
    print(" TODOS OS TESTES PASSARAM")
    print("=" * 60)
