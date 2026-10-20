"""p2p_cipher.py — Camada de cifra opcional para P2P BRN (v2)
================================================================
Envolve um socket TCP e adiciona ChaCha20-Poly1305 sobre os bytes
já assinados pelo p2p_auth.py. NÃO substitui o p2p_auth — soma.

Arquitetura:
    socket TCP cru
        ↓
    wrap_client(sock) / wrap_server(sock)   ← este arquivo
        ↓
    CipherSocket (interface compatível com socket)

Handshake:
    Cliente → Servidor:  MAGIC_HELLO(8) || eph_pub(32)
    Servidor → Cliente:  MAGIC_OK(8) || eph_pub(32) || flag(1)

    shared = X25519(eph_cli, eph_srv)
    salt   = sha256(eph_cli || eph_srv)
    k_c2s  = HKDF(shared, salt, info="BRN-P2P-CIPHER-v1", 32B)
    k_s2c  = HKDF(shared, salt, info="BRN-P2P-CIPHER-v1", 32B)
    (ordem de eph troca para gerar chaves diferentes por direção)

Framing:
    [len(4 BE)][ChaCha20-Poly1305(key, nonce=contador, data)]

Fallback gracioso:
    Se o outro lado não responde MAGIC_HELLO, o socket continua em claro.
    Isso permite interoperar com nós antigos.

Env:
    BRN_P2P_CIPHER                  (default "1" = ativado)
    BRN_CIPHER_HANDSHAKE_TIMEOUT    (default 1.5s)
    BRN_CIPHER_RECV_TIMEOUT         (default 30s)
================================================================
"""
from __future__ import annotations

import os
import struct
import socket
import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)


# ============================================================
# CONSTANTES
# ============================================================
MAGIC_HELLO = b"BRNCH-HE"
MAGIC_OK    = b"BRNCH-OK"
HKDF_INFO   = b"BRN-P2P-CIPHER-v1"
MAX_FRAME   = 16 * 1024 * 1024

HANDSHAKE_TIMEOUT = float(os.environ.get("BRN_CIPHER_HANDSHAKE_TIMEOUT", "1.5"))
RECV_TIMEOUT      = float(os.environ.get("BRN_CIPHER_RECV_TIMEOUT", "30.0"))

EPH_PUB_SIZE = 32
NONCE_SIZE   = 12
TAG_SIZE     = 16
FRAME_HDR    = 4


# ============================================================
# HELPERS
# ============================================================
def cipher_enabled() -> bool:
    return os.environ.get("BRN_P2P_CIPHER", "1") == "1"


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("conexao fechada")
        buf.extend(chunk)
    return bytes(buf)


def _derive(shared: bytes, eph_a: bytes, eph_b: bytes) -> bytes:
    salt = hashlib.sha256(eph_a + eph_b).digest()
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=HKDF_INFO,
    ).derive(shared)


# ============================================================
# CipherSocket — compatível com socket
# ============================================================
class CipherSocket:
    """
    Envelopa um socket adicionando cifra simétrica.
    Se send_key/recv_key forem None, opera em modo claro.
    """

    def __init__(self, sock, send_key=None, recv_key=None,
                 prebuf: bytes = b""):
        self.sock = sock
        self.send_key = send_key
        self.recv_key = recv_key
        self.tx_nonce = 0
        self.rx_nonce = 0
        self.cipher_on = (send_key is not None and recv_key is not None)
        self._buf: bytes = bytes(prebuf)
        self._closed = False

    # ---------- interface socket ----------
    def settimeout(self, t):
        self.sock.settimeout(t)

    def gettimeout(self):
        return self.sock.gettimeout()

    def sendall(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("socket fechado")
        if not self.cipher_on:
            self.sock.sendall(data)
            return
        n = self.tx_nonce.to_bytes(NONCE_SIZE, "big")
        self.tx_nonce += 1
        ct = ChaCha20Poly1305(self.send_key).encrypt(n, data, None)
        self.sock.sendall(struct.pack(">I", len(ct)) + ct)

    def send(self, data: bytes):
        """Alias para sendall (compat com algumas APIs)."""
        return self.sendall(data)

    def recv(self, bufsize: int) -> bytes:
        if self._closed:
            return b""
        if not self.cipher_on:
            if self._buf:
                chunk = self._buf[:bufsize]
                self._buf = self._buf[bufsize:]
                return chunk
            return self.sock.recv(bufsize)

        if self._buf:
            chunk = self._buf[:bufsize]
            self._buf = self._buf[bufsize:]
            return chunk

        try:
            hdr = _recv_exact(self.sock, FRAME_HDR)
        except ConnectionError:
            return b""

        (ln,) = struct.unpack(">I", hdr)
        if ln > MAX_FRAME + TAG_SIZE:
            raise ConnectionError(f"frame grande: {ln}")

        ct = _recv_exact(self.sock, ln)
        n = self.rx_nonce.to_bytes(NONCE_SIZE, "big")
        self.rx_nonce += 1
        try:
            pt = ChaCha20Poly1305(self.recv_key).decrypt(n, ct, None)
        except Exception:
            raise ConnectionError("falha ao decifrar (tag invalida)")

        self._buf = pt
        chunk = self._buf[:bufsize]
        self._buf = self._buf[bufsize:]
        return chunk

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.close()
        except Exception:
            pass

    # ---------- context manager ----------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


# ============================================================
# WRAP CLIENT
# ============================================================
def wrap_client(sock: socket.socket,
                timeout: float = HANDSHAKE_TIMEOUT) -> CipherSocket:
    """
    Executa o lado cliente do handshake.
    Se o servidor não responder com MAGIC_OK, cai para socket claro.
    """
    if not cipher_enabled():
        return CipherSocket(sock)

    try:
        sock.settimeout(timeout)

        eph = X25519PrivateKey.generate()
        eph_pub = eph.public_key().public_bytes_raw()

        sock.sendall(MAGIC_HELLO + eph_pub)

        try:
            resp_magic = _recv_exact(sock, 8)
        except (ConnectionError, socket.timeout, OSError):
            return CipherSocket(sock)

        if resp_magic != MAGIC_OK:
            # Servidor não suporta cifra — devolve os bytes lidos
            return CipherSocket(sock, prebuf=resp_magic)

        s_eph_pub = _recv_exact(sock, EPH_PUB_SIZE)
        flag = _recv_exact(sock, 1)
        if flag != b"\x01":
            return CipherSocket(sock, prebuf=s_eph_pub + flag)

        shared = eph.exchange(X25519PublicKey.from_public_bytes(s_eph_pub))
        k_c2s = _derive(shared, eph_pub, s_eph_pub)
        k_s2c = _derive(shared, s_eph_pub, eph_pub)

        sock.settimeout(RECV_TIMEOUT)
        return CipherSocket(sock, send_key=k_c2s, recv_key=k_s2c)

    except Exception:
        try:
            sock.settimeout(RECV_TIMEOUT)
        except Exception:
            pass
        return CipherSocket(sock)


# ============================================================
# WRAP SERVER
# ============================================================
def wrap_server(conn: socket.socket,
                timeout: float = HANDSHAKE_TIMEOUT) -> CipherSocket:
    """
    Executa o lado servidor do handshake.
    Se o cliente não mandar MAGIC_HELLO, cai para socket claro.
    """
    if not cipher_enabled():
        return CipherSocket(conn)

    try:
        conn.settimeout(timeout)

        try:
            magic = _recv_exact(conn, 8)
        except (ConnectionError, socket.timeout, OSError):
            try:
                conn.settimeout(RECV_TIMEOUT)
            except Exception:
                pass
            return CipherSocket(conn)

        if magic != MAGIC_HELLO:
            try:
                conn.settimeout(RECV_TIMEOUT)
            except Exception:
                pass
            return CipherSocket(conn, prebuf=magic)

        c_eph_pub = _recv_exact(conn, EPH_PUB_SIZE)

        eph = X25519PrivateKey.generate()
        eph_pub = eph.public_key().public_bytes_raw()

        conn.sendall(MAGIC_OK + eph_pub + b"\x01")

        shared = eph.exchange(X25519PublicKey.from_public_bytes(c_eph_pub))
        k_c2s = _derive(shared, c_eph_pub, eph_pub)
        k_s2c = _derive(shared, eph_pub, c_eph_pub)

        conn.settimeout(RECV_TIMEOUT)
        return CipherSocket(conn, send_key=k_s2c, recv_key=k_c2s)

    except Exception:
        try:
            conn.settimeout(RECV_TIMEOUT)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        return CipherSocket(conn)


# ============================================================
# INFO
# ============================================================
def info() -> dict:
    return {
        "enabled":            cipher_enabled(),
        "magic_hello":        MAGIC_HELLO.decode("ascii", "replace"),
        "magic_ok":           MAGIC_OK.decode("ascii", "replace"),
        "hkdf_info":          HKDF_INFO.decode("ascii", "replace"),
        "max_frame":          MAX_FRAME,
        "handshake_timeout":  HANDSHAKE_TIMEOUT,
        "recv_timeout":       RECV_TIMEOUT,
    }


# ============================================================
# AUTO-TESTE (loopback in-process)
# ============================================================
if __name__ == "__main__":
    import threading
    import time

    print("Testando p2p_cipher...")

    # ---------- 1. Round-trip cliente ↔ servidor ----------
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]

    result = {}

    def run_server():
        try:
            conn, _ = server_sock.accept()
            cs = wrap_server(conn)
            result["server_cipher"] = cs.cipher_on

            msg = cs.recv(1024)
            result["server_received"] = msg
            cs.sendall(b"pong:" + msg)
            cs.close()
        except Exception as e:
            result["server_error"] = repr(e)

    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        cs = wrap_client(s)
        result["client_cipher"] = cs.cipher_on

        cs.sendall(b"ping")
        reply = cs.recv(1024)
        result["client_received"] = reply
        cs.close()
    except Exception as e:
        result["client_error"] = repr(e)

    t.join(timeout=5)

    print(f"  cliente cifrado?  {result.get('client_cipher')}")
    print(f"  servidor cifrado? {result.get('server_cipher')}")
    print(f"  servidor recebeu: {result.get('server_received')!r}")
    print(f"  cliente recebeu:  {result.get('client_received')!r}")
    assert result.get("client_cipher") is True
    assert result.get("server_cipher") is True
    assert result.get("server_received") == b"ping"
    assert result.get("client_received") == b"pong:ping"
    print("  [ok] round-trip cifrado")

    # ---------- 2. Fallback quando servidor não suporta cifra ----------
    server_sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock2.bind(("127.0.0.1", 0))
    server_sock2.listen(1)
    port2 = server_sock2.getsockname()[1]

    result2 = {}

    def run_server_plain():
        try:
            conn, _ = server_sock2.accept()
            # servidor "legado": lê o que vier, responde em claro
            data = conn.recv(1024)
            result2["server_received"] = data
            conn.sendall(b"plain-response")
            conn.close()
        except Exception as e:
            result2["server_error"] = repr(e)

    t2 = threading.Thread(target=run_server_plain, daemon=True)
    t2.start()

    try:
        s2 = socket.create_connection(("127.0.0.1", port2), timeout=5)
        cs2 = wrap_client(s2, timeout=0.5)
        # servidor não respondeu MAGIC_OK, então caiu para claro
        result2["client_cipher"] = cs2.cipher_on
        cs2.sendall(b"hello-legacy")
        # o servidor legado recebeu o MAGIC_HELLO+eph_pub. Vai responder em claro.
        reply = cs2.recv(1024)
        result2["client_received"] = reply
        cs2.close()
    except Exception as e:
        result2["client_error"] = repr(e)

    t2.join(timeout=5)

    print(f"  [legacy] cliente cifrado? {result2.get('client_cipher')}")
    print(f"  [legacy] cliente recebeu:  {result2.get('client_received')!r}")
    # aceita qualquer coisa razoável
    assert result2.get("client_cipher") is False
    print("  [ok] fallback para claro quando peer não suporta")

    # ---------- 3. Config desabilitada ----------
    os.environ["BRN_P2P_CIPHER"] = "0"
    s3 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cs3 = wrap_client(s3)
    assert cs3.cipher_on is False
    os.environ["BRN_P2P_CIPHER"] = "1"
    s3.close()
    print("  [ok] cipher_enabled() respeitado")

    # ---------- 4. Info ----------
    i = info()
    print(f"  [info] enabled={i['enabled']} magic_hello={i['magic_hello']}")

    print("p2p_cipher OK.")