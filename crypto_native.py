import ctypes
import os
import sys
import platform
from pathlib import Path

_LIB = None
_LIB_PATH = None

def _get_lib_name():
    system = platform.system().lower()
    if "windows" in system:
        return "brn_crypto.dll"
    elif "darwin" in system:
        return "libbrn_crypto.dylib"
    else:
        return "libbrn_crypto.so"

def _load_lib():
    global _LIB, _LIB_PATH
    if _LIB is not None:
        return _LIB
    
    base = Path(__file__).parent.resolve()
    name = _get_lib_name()
    paths = [base / name, base / "lib" / name]
    
    for p in paths:
        if p.exists():
            try:
                lib = ctypes.CDLL(str(p))
                lib.brn_verify_ecdsa.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p]
                lib.brn_verify_ecdsa.restype = ctypes.c_int
                lib.brn_sign_ecdsa.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
                lib.brn_sign_ecdsa.restype = ctypes.c_int
                lib.brn_verify_schnorr.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p]
                lib.brn_verify_schnorr.restype = ctypes.c_int
                lib.brn_sign_schnorr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
                lib.brn_sign_schnorr.restype = ctypes.c_int
                _LIB = lib
                _LIB_PATH = str(p)
                return lib
            except Exception as e:
                print(f"[crypto_native] Erro ao carregar {p}: {e}", file=sys.stderr)
    return None

def is_available() -> bool:
    return _load_lib() is not None

def verify_ecdsa(pub_bytes: bytes, sig_bytes: bytes, msg32: bytes) -> bool:
    lib = _load_lib()
    if not lib: return False
    return lib.brn_verify_ecdsa(pub_bytes, len(pub_bytes), sig_bytes, msg32) == 1

def sign_ecdsa(priv_bytes: bytes, msg32: bytes) -> bytes:
    lib = _load_lib()
    if not lib: raise RuntimeError("C library not loaded")
    out = ctypes.create_string_buffer(64)
    if lib.brn_sign_ecdsa(priv_bytes, msg32, out) == 1:
        return out.raw
    raise ValueError("Falha ao assinar ECDSA")

def verify_schnorr(pub_bytes: bytes, sig_bytes: bytes, msg32: bytes) -> bool:
    lib = _load_lib()
    if not lib: return False
    return lib.brn_verify_schnorr(pub_bytes, len(pub_bytes), sig_bytes, msg32) == 1

def sign_schnorr(priv_bytes: bytes, msg32: bytes, aux_rand: bytes = b"\x00"*32) -> bytes:
    lib = _load_lib()
    if not lib: raise RuntimeError("C library not loaded")
    out = ctypes.create_string_buffer(64)
    if lib.brn_sign_schnorr(priv_bytes, msg32, aux_rand, out) == 1:
        return out.raw
    raise ValueError("Falha ao assinar Schnorr")