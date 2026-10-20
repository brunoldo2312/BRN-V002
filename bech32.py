"""bech32.py — Codificação Bech32 para endereços BRN (prefixo brn1)
================================================================
Implementa o esquema Bech32 (BIP173 simplificado).
Endereço BRN = bech32("brn", convertbits(hash160(pubkey), 8, 5))

API pública:
    address_from_pubkey(pubkey_bytes) -> str
    validate_address(addr) -> bool
    bech32_encode(hrp, data) -> str
    bech32_decode(bech) -> (hrp, data) | (None, None)
================================================================
"""
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
CHARSET_REV = {c: i for i, c in enumerate(CHARSET)}
BECH32_CONST = 1
HRP = "brn"


# ============================================================
# POLYNOMIAL / CHECKSUM
# ============================================================
def _polymod(values):
    generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _verify_checksum(hrp, data):
    return _polymod(_hrp_expand(hrp) + data) == BECH32_CONST


def _create_checksum(hrp, data):
    values = _hrp_expand(hrp) + data
    polymod = _polymod(values + [0, 0, 0, 0, 0, 0]) ^ BECH32_CONST
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


# ============================================================
# CONVERSÃO DE BITS
# ============================================================
def _convertbits(data, frombits, tobits, pad=True):
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
    combined = data + _create_checksum(hrp, data)
    return hrp + "1" + "".join([CHARSET[d] for d in combined])


def bech32_decode(bech: str):
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


# ============================================================
# API BRN
# ============================================================
def address_from_pubkey(pubkey: bytes) -> str:
    """Deriva endereço Bech32 (brn1...) a partir da pubkey (bytes)."""
    from crypto import hash160
    h = hash160(pubkey)
    data = _convertbits(list(h), 8, 5, True)
    return bech32_encode(HRP, data)


def validate_address(addr) -> bool:
    """True se for um endereço BRN válido (brn1..., 20 bytes)."""
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


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    from crypto import generate_private_key, pubkey_from_priv

    priv = generate_private_key()
    pub = pubkey_from_priv(priv)
    addr = address_from_pubkey(pub)
    print(f"pub    : {pub.hex()[:24]}...")
    print(f"address: {addr}")
    print(f"valid  : {validate_address(addr)}")

    # Testa roundtrip de encode/decode
    hrp, data = bech32_decode(addr)
    print(f"decode : hrp={hrp} bytes={len(data) if data else 0}")

    # Testa inválidos
    for bad in ["", "brn1", "xxx1yyy", addr[:-1] + "z", "brn1" + "q" * 60]:
        assert not validate_address(bad), f"deveria rejeitar: {bad!r}"
    print("rejeições OK")