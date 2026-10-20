"""p2p_secure.py — Fachada que une p2p_auth + p2p_cipher
================================================================
Fornece handshake autenticado (Ed25519) + opcionalmente cifrado
(ChaCha20-Poly1305 sobre X25519) numa única API.

API:
    handshake_client(sock, my_priv, expected_pub_hex=None)
        → (SecureConn, peer_hex)

    handshake_server(sock, my_priv)
        → (SecureConn, peer_hex)

SecureConn é compatível com socket (sendall/recv/close/settimeout).

Fluxo cliente:
    1. (opcional) wrap_client(sock)   → cifra
    2. build_auth() + send hello
    3. recebe hello resposta
    4. verify_auth(resp)              → valida assinatura do servidor

Fluxo servidor:
    1. (opcional) wrap_server(sock)   → cifra
    2. recebe hello
    3. verify_auth(hello)             → valida assinatura do cliente
    4. build_auth() + send hello resposta

Env:
    BRN_P2P_AUTH        (off | optional | required)
    BRN_P2P_CIPHER      (0 | 1)
================================================================
"""
from __future__ import annotations

import json
import logging
import socket

from p2p_auth import (
    build_auth,
    verify_auth,
    set_node_id_priv,
    auth_enabled,
)
from p2p_cipher import (
    wrap_client,
    wrap_server,
    CipherSocket,
    cipher_enabled,
)


log = logging.getLogger("p2p_secure")

HELLO_TIMEOUT = float(__import__("os").environ.get(
    "BRN_SECURE_HELLO_TIMEOUT", "5.0"))
MAX_HELLO = 8192


# ============================================================
# SecureConn — interface compatível com socket
# ============================================================
class SecureConn:
    """
    Envelopa um socket (possivelmente cifrado) com metadados do peer.
    Todas as operações de socket delegam para o objeto subjacente.
    """

    def __init__(self, sock, peer_pub_hex: str = "",
                 peer_verified: bool = False,
                 cipher_on: bool = False):
        self.sock = sock
        self.peer_pub_hex = peer_pub_hex
        self.peer_verified = peer_verified
        self.cipher_on = cipher_on

    # ---- interface socket ----
    def settimeout(self, t):
        self.sock.settimeout(t)

    def gettimeout(self):
        return self.sock.gettimeout()

    def sendall(self, data: bytes) -> None:
        self.sock.sendall(data)

    def send(self, data: bytes):
        return self.sock.sendall(data)

    def recv(self, n: int) -> bytes:
        return self.sock.recv(n)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def __repr__(self):
        c = "cipher" if self.cipher_on else "plain"
        v = "verified" if self.peer_verified else "unverified"
        return f"<SecureConn {self.peer_pub_hex[:12]}... {c} {v}>"


# ============================================================
# Handshake — mensagens
# ============================================================
def _send_hello(conn) -> None:
    """Monta o payload de hello com auth e envia com header de tamanho."""
    auth = build_auth(msg={"t": "hello"})
    payload_obj = {"t": "hello"}
    if auth:
        payload_obj["auth"] = auth
    raw = json.dumps(payload_obj, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_HELLO:
        raise ValueError(f"hello grande demais: {len(raw)}B")
    frame = len(raw).to_bytes(4, "big") + raw
    conn.sendall(frame)


def _recv_hello(conn) -> dict:
    """Lê um hello do socket, com header de tamanho (4B BE)."""
    hdr = _recv_n(conn, 4)
    if not hdr or len(hdr) < 4:
        raise ConnectionError("sem header hello")
    ln = int.from_bytes(hdr, "big")
    if ln == 0 or ln > MAX_HELLO:
        raise ConnectionError(f"hello com tamanho invalido ({ln})")
    raw = _recv_n(conn, ln)
    if not raw or len(raw) < ln:
        raise ConnectionError("hello truncado")
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ConnectionError(f"hello nao e JSON valido: {e}")


def _recv_n(conn, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
    return bytes(buf)


# ============================================================
# Handshake — CLIENTE
# ============================================================
def handshake_client(sock: socket.socket,
                     my_priv,
                     expected_pub_hex: str | None = None):
    """
    Executa o handshake do lado cliente.

    Retorna (SecureConn, peer_pub_hex).

    Raises:
        ConnectionError se o servidor não validar ou a pubkey não bater.
    """
    set_node_id_priv(my_priv)
    sock.settimeout(HELLO_TIMEOUT)

    # ---------- cifra (opcional) ----------
    cipher_on = False
    if cipher_enabled():
        try:
            wrapped = wrap_client(sock)
            if isinstance(wrapped, CipherSocket):
                sock = wrapped
                cipher_on = wrapped.cipher_on
        except Exception as e:
            log.debug(f"cipher handshake cliente falhou: {e}")

    # ---------- auth hello ----------
    try:
        _send_hello(sock)
        resp = _recv_hello(sock)
    except Exception as e:
        raise ConnectionError(f"falha no hello: {e}")

    peer_hex = ""
    verified = False

    if auth_enabled():
        auth = resp.get("auth") or {}
        if not auth:
            raise ConnectionError("servidor nao enviou auth")
        ok, err = verify_auth(auth, msg={"t": "hello"})
        if not ok:
            raise ConnectionError(f"auth do servidor invalida: {err}")
        peer_hex = auth.get("pub", "")
        verified = True

        if expected_pub_hex:
            if peer_hex != expected_pub_hex:
                raise ConnectionError(
                    f"identidade do servidor mudou: "
                    f"esperado={expected_pub_hex[:16]}... "
                    f"recebido={peer_hex[:16]}..."
                )
    else:
        peer_hex = resp.get("pub", "")

    return SecureConn(sock, peer_hex, verified, cipher_on), peer_hex


# ============================================================
# Handshake — SERVIDOR
# ============================================================
def handshake_server(sock: socket.socket, my_priv):
    """
    Executa o handshake do lado servidor.

    Retorna (SecureConn, peer_pub_hex).

    Raises:
        ConnectionError se o cliente não validar.
    """
    set_node_id_priv(my_priv)
    sock.settimeout(HELLO_TIMEOUT)

    # ---------- cifra (opcional) ----------
    cipher_on = False
    if cipher_enabled():
        try:
            wrapped = wrap_server(sock)
            if isinstance(wrapped, CipherSocket):
                sock = wrapped
                cipher_on = wrapped.cipher_on
        except Exception as e:
            log.debug(f"cipher handshake servidor falhou: {e}")

    # ---------- lê hello do cliente ----------
    try:
        hello = _recv_hello(sock)
    except Exception as e:
        raise ConnectionError(f"falha lendo hello: {e}")

    peer_hex = ""
    verified = False

    if auth_enabled():
        auth = hello.get("auth") or {}
        if not auth:
            raise ConnectionError("cliente nao enviou auth")
        ok, err = verify_auth(auth, msg={"t": "hello"})
        if not ok:
            raise ConnectionError(f"auth do cliente invalida: {err}")
        peer_hex = auth.get("pub", "")
        verified = True

    # ---------- responde com nosso hello ----------
    try:
        _send_hello(sock)
    except Exception as e:
        raise ConnectionError(f"falha enviando hello resposta: {e}")

    return SecureConn(sock, peer_hex, verified, cipher_on), peer_hex


# ============================================================
# INFO
# ============================================================
def info() -> dict:
    return {
        "auth_enabled":     auth_enabled(),
        "cipher_enabled":   cipher_enabled(),
        "hello_timeout":    HELLO_TIMEOUT,
        "max_hello_bytes":  MAX_HELLO,
    }


# ============================================================
# AUTO-TESTE (loopback in-process)
# ============================================================
if __name__ == "__main__":
    import os
    import threading
    import time

    # Garante config de teste
    os.environ.setdefault("BRN_P2P_AUTH", "optional")
    os.environ.setdefault("BRN_P2P_CIPHER", "1")
    os.environ.setdefault("BRN_NETWORK_SECRET",
                          "segredo-de-teste-para-p2p-secure-1234567")

    # Importa Ed25519 para gerar identidades
    try:
        from crypto import Ed25519PrivateKey
    except Exception:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

    print("Testando p2p_secure...")
    print(f"  auth={auth_enabled()} cipher={cipher_enabled()}")

    # ---------- 1. Handshake completo ----------
    srv_priv = Ed25519PrivateKey.generate()
    cli_priv = Ed25519PrivateKey.generate()

    srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv_sock.bind(("127.0.0.1", 0))
    srv_sock.listen(1)
    port = srv_sock.getsockname()[1]

    result = {}

    def run_server():
        try:
            conn, _ = srv_sock.accept()
            sc, peer_hex = handshake_server(conn, srv_priv)
            result["server_peer_hex"]  = peer_hex
            result["server_cipher_on"] = sc.cipher_on
            result["server_verified"]  = sc.peer_verified

            # echo
            data = sc.recv(1024)
            result["server_received"] = data
            sc.sendall(b"echo:" + data)
            sc.close()
        except Exception as e:
            result["server_error"] = repr(e)

    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    try:
        cli_sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sc, peer_hex = handshake_client(cli_sock, cli_priv)
        result["client_peer_hex"]  = peer_hex
        result["client_cipher_on"] = sc.cipher_on
        result["client_verified"]  = sc.peer_verified

        # confere que o servidor apresentou a pubkey correta
        srv_pub_expected = srv_priv.public_key().public_bytes_raw().hex()
        assert peer_hex == srv_pub_expected, \
            f"peer_hex esperado {srv_pub_expected[:16]}, recebido {peer_hex[:16]}"

        sc.sendall(b"ping")
        reply = sc.recv(1024)
        result["client_received"] = reply
        sc.close()
    except Exception as e:
        result["client_error"] = repr(e)

    t.join(timeout=5)

    print(f"  servidor recebeu: {result.get('server_received')!r}")
    print(f"  cliente recebeu:  {result.get('client_received')!r}")
    print(f"  server cipher:    {result.get('server_cipher_on')}")
    print(f"  client cipher:    {result.get('client_cipher_on')}")
    print(f"  server verified:  {result.get('server_verified')}")
    print(f"  client verified:  {result.get('client_verified')}")

    assert "server_error" not in result, result.get("server_error")
    assert "client_error" not in result, result.get("client_error")
    assert result.get("server_received") == b"ping"
    assert result.get("client_received") == b"echo:ping"
    assert result.get("server_verified") is True
    assert result.get("client_verified") is True
    print("  [ok] handshake + echo")

    # ---------- 2. expected_pub_hex errado ----------
    srv_sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv_sock2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv_sock2.bind(("127.0.0.1", 0))
    srv_sock2.listen(1)
    port2 = srv_sock2.getsockname()[1]

    def run_server2():
        try:
            conn, _ = srv_sock2.accept()
            try:
                handshake_server(conn, srv_priv)
            except Exception:
                pass
            conn.close()
        except Exception:
            pass

    t2 = threading.Thread(target=run_server2, daemon=True)
    t2.start()

    try:
        s2 = socket.create_connection(("127.0.0.1", port2), timeout=5)
        try:
            handshake_client(s2, cli_priv, expected_pub_hex="ff" * 32)
            print("  [X] identidade errada foi aceita")
            raise SystemExit(1)
        except ConnectionError:
            print("  [ok] identidade errada rejeitada")
        s2.close()
    except Exception as e:
        print(f"  [X] teste falhou: {e!r}")
        raise SystemExit(1)

    t2.join(timeout=3)

    # ---------- 3. Info ----------
    print(f"  [info] {info()}")

    print("p2p_secure OK.")