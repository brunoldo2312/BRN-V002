import ctypes
import os

lib = ctypes.CDLL(os.path.join(os.path.dirname(__file__), "brn_merkle.so"))

def compute_merkle_root_fast(txids: list[str]) -> str:
    # Prepara os dados para enviar para o C
    # Chama lib.compute_merkle_root_c(...)
    # Retorna o resultado
    pass
