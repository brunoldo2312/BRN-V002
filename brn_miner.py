"""brn_miner.py — Wrapper ctypes para o PoW em C
================================================================
Carrega brn_miner.so (Linux) ou brn_miner.dll (Windows) e expõe:

    is_available() -> bool
    pow_search(prefix, diff_str, difficulty,
               nonce_start=0, max_iters=0) -> (nonce, hash_hex, hashes)
                                             | (None, None, hashes)
                                             | None (biblioteca ausente)
    benchmark(iters=100_000) -> float (hashes/s)
    info() -> dict

Se a biblioteca não estiver presente, is_available() retorna False
e o blockchain.py cai automaticamente para Python puro.
================================================================
"""
from __future__ import annotations

import ctypes
import os
import platform
import sys
from pathlib import Path


_LIB = None
_LIB_PATH = None
_LOAD_ATTEMPTED = False


def _library_candidates():
    base = Path(__file__).resolve().parent
    sysname = platform.system().lower()

    if sysname == "linux":
        names = ["brn_miner.so"]
        extra = [
            Path("/usr/local/lib/brn_miner.so"),
            Path("/usr/lib/brn_miner.so"),
        ]
    elif sysname == "darwin":
        names = ["brn_miner.dylib", "brn_miner.so"]
        extra = [Path("/usr/local/lib/brn_miner.dylib")]
    elif sysname == "windows":
        names = ["brn_miner.dll"]
        extra = [Path("C:/brn/brn_miner.dll")]
    else:
        names = ["brn_miner.so"]
        extra = []

    return [base / n for n in names] + extra


def _load():
    global _LIB, _LIB_PATH, _LOAD_ATTEMPTED

    if _LIB is not None:
        return _LIB
    if _LOAD_ATTEMPTED:
        return None
    _LOAD_ATTEMPTED = True

    for path in _library_candidates():
        if not path.exists():
            continue
        try:
            lib = ctypes.CDLL(str(path))
        except Exception as e:
            print(f"[brn_miner] falha carregando {path}: {e}",
                  file=sys.stderr)
            continue

        lib.brn_pow_search.restype  = ctypes.c_int
        lib.brn_pow_search.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        ]
        lib.brn_benchmark.restype  = ctypes.c_double
        lib.brn_benchmark.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_int,
            ctypes.c_uint64,
        ]

        _LIB = lib
        _LIB_PATH = path
        print(f"[brn_miner] carregada: {path}")
        return lib

    return None


def is_available() -> bool:
    return _load() is not None


def library_path():
    _load()
    return _LIB_PATH


def pow_search(prefix: str, diff_str: str, difficulty: int,
               nonce_start: int = 0, max_iters: int = 0):
    """
    Retorna:
        (nonce, hash_hex, hashes) — encontrou
        (None,  None,     hashes) — esgotou max_iters
        None                      — biblioteca indisponível
    """
    lib = _load()
    if lib is None:
        return None

    pfx = prefix.encode() if isinstance(prefix, str) else prefix
    dif = diff_str.encode() if isinstance(diff_str, str) else diff_str

    out_hex = ctypes.create_string_buffer(65)
    out_nonce = ctypes.c_uint64(0)
    out_hashes = ctypes.c_uint64(0)

    rc = lib.brn_pow_search(
        pfx, len(pfx),
        dif, len(dif),
        ctypes.c_uint64(nonce_start),
        ctypes.c_uint64(max_iters),
        ctypes.c_int(difficulty),
        out_hex,
        ctypes.byref(out_nonce),
        ctypes.byref(out_hashes),
    )

    if rc == 0:
        return (out_nonce.value, out_hex.value.decode("ascii"),
                out_hashes.value)
    if rc == 1:
        return (None, None, out_hashes.value)
    return None


def benchmark(iters: int = 100_000) -> float:
    lib = _load()
    if lib is None:
        return 0.0
    pfx = b"benchmark_prefix_hash_merkle_timestamp"
    dif = b"5"
    return float(lib.brn_benchmark(pfx, len(pfx), dif, len(dif), 5, iters))


def info() -> dict:
    lib = _load()
    return {
        "available":  lib is not None,
        "path":       str(_LIB_PATH) if _LIB_PATH else None,
        "platform":   platform.system(),
        "arch":       platform.machine(),
    }


if __name__ == "__main__":
    print("=" * 60)
    print(" brn_miner.py — status")
    print("=" * 60)
    for k, v in info().items():
        print(f"  {k}: {v}")

    if not is_available():
        print()
        print("[X] Biblioteca nao encontrada.")
        print("    Compile com:")
        print("      gcc -O3 -march=native -shared -fPIC \\")
        print("          -o brn_miner.so brn_miner.c -lcrypto")
        raise SystemExit(1)

    print()
    print("Benchmarkando (100k hashes)...")
    hr = benchmark(100_000)
    print(f"  C: {hr:,.0f} hashes/s")
    print()

    import hashlib
    import time
    prefix = "benchmark_prefix_hash_merkle_timestamp"
    diff_str = "5"
    N = 100_000
    t0 = time.time()
    sha = hashlib.sha256
    for i in range(N):
        data = (prefix + str(i) + diff_str).encode()
        sha(sha(data).digest()).digest()
    dt = time.time() - t0
    hr_py = N / dt
    print(f"  Python: {hr_py:,.0f} hashes/s")
    print()
    print(f"  Speedup C/Python: {hr/hr_py:.1f}x")