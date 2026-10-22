"""p2p_relay.py — Relay TCP com mensagens JSON enquadradas para o BRN.

Formato no fio:
    4 bytes: comprimento da mensagem, big-endian
    N bytes: objeto JSON codificado em UTF-8

A lista bootstrap_peers.json é usada como filtro de IP, não como
mecanismo de autenticação criptográfica.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

BASE_DIR = Path(__file__).resolve().parent
BOOTSTRAP_FILE = BASE_DIR / "bootstrap_peers.json"

DEFAULT_PORT = 6001
MAX_PAYLOAD_SIZE = 1 * 1024 * 1024
SOCKET_TIMEOUT = 5.0
MAX_WORKERS = 32

log = logging.getLogger("p2p_relay")


def _canonical_ip(value: str) -> str | None:
    """Normaliza um endereço IP; retorna None se não for um IP válido."""
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _peer_ip(peer: Any) -> str | None:
    """Extrai o IP de formatos como '1.2.3.4:6001' ou '[::1]:6001'."""
    if not isinstance(peer, str):
        return None

    peer = peer.strip()
    if not peer:
        return None

    # Formato IPv6 entre colchetes, opcionalmente seguido de uma porta.
    if peer.startswith("["):
        end = peer.find("]")
        if end < 0:
            return None
        host = peer[1:end]
    else:
        # Se for un IPv6 literal, ip_address o reconhecerá diretamente.
        if _canonical_ip(peer) is not None:
            return _canonical_ip(peer)
        host = peer.rsplit(":", 1)[0] if ":" in peer else peer

    return _canonical_ip(host)


def _recv_exact(conn: socket.socket, count: int) -> bytes:
    """Lê exatamente count bytes ou levanta ConnectionError."""
    chunks = bytearray()
    while len(chunks) < count:
        chunk = conn.recv(count - len(chunks))
        if not chunk:
            raise ConnectionError("Conexão encerrada durante a leitura")
        chunks.extend(chunk)
    return bytes(chunks)


def _encode_message(data: dict[str, Any]) -> bytes:
    if not isinstance(data, dict):
        raise TypeError("A mensagem precisa ser um objeto JSON (dict)")

    payload = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    if not payload or len(payload) > MAX_PAYLOAD_SIZE:
        raise ValueError("Tamanho da mensagem fora do limite permitido")

    return struct.pack("!I", len(payload)) + payload


class P2PRelay:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        bootstrap_file: Path = BOOTSTRAP_FILE,
        *,
        max_payload_size: int = MAX_PAYLOAD_SIZE,
        socket_timeout: float = SOCKET_TIMEOUT,
        max_workers: int = MAX_WORKERS,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("Porta inválida")
        if max_payload_size <= 0:
            raise ValueError("max_payload_size precisa ser positivo")
        if max_workers <= 0:
            raise ValueError("max_workers precisa ser positivo")

        self.host = host
        self.port = port
        self.bootstrap_file = Path(bootstrap_file)
        self.max_payload_size = max_payload_size
        self.socket_timeout = socket_timeout
        self.max_workers = max_workers

        self._stop_event = threading.Event()
        self._server_socket: socket.socket | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._start_lock = threading.Lock()

    def _get_allowed_peers(self) -> set[str]:
        """Carrega os IPs autorizados do JSON; formatos aceitos: lista ou dict."""
        try:
            with self.bootstrap_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            log.warning("Arquivo de peers não encontrado: %s", self.bootstrap_file)
            return set()
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Não foi possível ler a lista de peers: %s", exc)
            return set()

        if isinstance(data, dict):
            data = data.get("peers", [])

        if not isinstance(data, list):
            log.error("O arquivo de peers deve conter uma lista ou um objeto com 'peers'")
            return set()

        return {
            ip
            for item in data
            if (ip := _peer_ip(item)) is not None
        }

    def send_to_peer(
        self,
        peer: str,
        data: dict[str, Any],
        *,
        port: int | None = None,
    ) -> bool:
        """Envia uma mensagem para um peer. O peer pode incluir a porta."""
        ip = _peer_ip(peer)
        if ip is None:
            log.warning("Endereço de peer inválido: %r", peer)
            return False

        target_port = port or self.port
        if not 1 <= target_port <= 65535:
            log.warning("Porta de destino inválida: %r", target_port)
            return False

        try:
            frame = _encode_message(data)
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.socket_timeout)
                sock.connect((ip, target_port))
                sock.sendall(frame)
            return True
        except (OSError, TypeError, ValueError) as exc:
            log.warning("Falha ao enviar para %s:%s: %s", ip, target_port, exc)
            return False

    def broadcast(self, data: dict[str, Any]) -> dict[str, bool]:
        """Envia para os peers listados e retorna o resultado por endereço IP."""
        peers = sorted(self._get_allowed_peers())
        results: dict[str, bool] = {}
        for peer_ip in peers:
            results[peer_ip] = self.send_to_peer(peer_ip, data)
        return results

    def _handle_client(
        self,
        conn: socket.socket,
        addr: tuple[Any, ...],
        callback: Callable[[dict[str, Any], str], None],
    ) -> None:
        peer_ip = _canonical_ip(str(addr[0]))
        try:
            conn.settimeout(self.socket_timeout)

            if peer_ip is None or peer_ip not in self._get_allowed_peers():
                log.warning("Conexão recusada de peer não listado: %s", addr[0])
                return

            header = _recv_exact(conn, 4)
            (length,) = struct.unpack("!I", header)

            if length == 0 or length > self.max_payload_size:
                log.warning("Payload de tamanho inválido de %s: %d", peer_ip, length)
                return

            raw = _recv_exact(conn, length)
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                log.warning("JSON inválido recebido de %s: %s", peer_ip, exc)
                return

            if not isinstance(message, dict):
                log.warning("Mensagem não é um objeto JSON: %s", peer_ip)
                return

            callback(message, peer_ip)

        except (OSError, ConnectionError) as exc:
            if not self._stop_event.is_set():
                log.info("Conexão encerrada com %s: %s", addr[0], exc)
        except Exception:
            log.exception("Erro ao processar mensagem de %s", addr[0])
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def start_server(
        self,
        callback: Callable[[dict[str, Any], str], None],
    ) -> None:
        """Inicia o servidor; este método bloqueia até stop() ser chamado."""
        if not callable(callback):
            raise TypeError("callback precisa ser chamável")

        with self._start_lock:
            if self._server_socket is not None:
                raise RuntimeError("O servidor já foi iniciado")
            self._stop_event.clear()

            family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
            server = socket.socket(family, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            try:
                server.bind((self.host, self.port))
                server.listen(self.max_workers)
                server.settimeout(1.0)
            except Exception:
                server.close()
                raise

            self._server_socket = server
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="brn-relay",
            )

        log.info("Relay escutando em %s:%s", self.host, self.port)

        try:
            while not self._stop_event.is_set():
                try:
                    conn, addr = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop_event.is_set():
                        break
                    raise

                executor = self._executor
                if executor is None:
                    conn.close()
                    break

                try:
                    executor.submit(self._handle_client, conn, addr, callback)
                except RuntimeError:
                    conn.close()
                    break
        finally:
            self._close_server()

    def _close_server(self) -> None:
        with self._start_lock:
            server, self._server_socket = self._server_socket, None
            executor, self._executor = self._executor, None

        if server is not None:
            try:
                server.close()
            except OSError:
                pass

        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def stop(self) -> None:
        """Solicita o encerramento do loop de escuta."""
        self._stop_event.set()
        server = self._server_socket
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
