import ctypes
import os
import platform
import hashlib

_lib_name = "brn_merkle.dll" if platform.system() == "Windows" else "brn_merkle.so"
_lib_path = os.path.join(os.path.dirname(__file__), _lib_name)

try:
    lib = ctypes.CDLL(_lib_path)
    lib.compute_merkle_root_c.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p]
    lib.compute_merkle_root_c.restype = None
    HAS_C_MERKLE = True
except Exception as e:
    print(f"[brn_merkle] C indisponivel. Usando Python puro. ({e})")
    HAS_C_MERKLE = False

def compute_merkle_root_fast(txids: list[str]) -> str:
    if not HAS_C_MERKLE:
        # Fallback Python
        if not txids: return "0" * 64
        layer = [bytes.fromhex(t) for t in txids]
        while len(layer) > 1:
            if len(layer) % 2: layer.append(layer[-1])
            layer = [hashlib.sha256(hashlib.sha256(layer[i] + layer[i+1]).digest()).digest() for i in range(0, len(layer), 2)]
        return layer[0].hex()

    if not txids:
        return "0" * 64
    
    raw_bytes = b"".join(bytes.fromhex(t) for t in txids)
    out_buf = ctypes.create_string_buffer(32)
    lib.compute_merkle_root_c(raw_bytes, len(txids), out_buf)
    return out_buf.raw.hex()