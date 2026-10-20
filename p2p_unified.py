"""
p2p_unified.py — BRN P2P Network v6.6.5-Bootstrap
================================================================
v6.6.5:
  [FIX] P2PClient.send_message e variantes aceitam timeout.
================================================================
"""
from __future__ import annotations

import os
import json
import time
import uuid
import base64
import socket
import hashlib
import logging
import threading
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Optional

log = logging.getLogger("p2p")

from p2p_auth import (
    auth_enabled, auth_required, build_auth, verify_auth,
    set_node_id_priv as _auth_set_priv,
)

try:
    from blockchain import block_hash, meets_difficulty, compute_merkle_root
    _HAS_BLOCKCHAIN_HELPERS = True
except ImportError:
    _HAS_BLOCKCHAIN_HELPERS = False
    block_hash = meets_difficulty = compute_merkle_root = None

try:
    from discovery_v2 import TOKEN as TOKEN_ESPERADO
except ImportError:
    _secret = os.environ.get("BRN_NETWORK_SECRET")
    if not _secret:
        raise RuntimeError("BRN_NETWORK_SECRET obrigatório")
    import hmac as _hmac
    TOKEN_ESPERADO = _hmac.new(_secret.encode(), b"brn-p2p-v1", hashlib.sha256).hexdigest()

MULTICAST_GROUP  = "239.255.42.99"
MULTICAST_PORT   = 50007
TCP_PORT_DEFAULT = 6001

DISCOVERY_INTERVAL_MIN = 2.0
DISCOVERY_INTERVAL_MAX = 30.0
PEER_TIMEOUT           = 60
PEER_CLEANUP_S         = 30
MAX_MSG_SIZE           = 8 * 1024 * 1024

PROTOCOL_VERSION = "BRN5/2.1-Bootstrap"
NETWORK_MAGIC    = b"BRN5"
CHAIN_ID         = os.environ.get("BRN_CHAIN_ID", "mainnet").strip()

PEER_SCORE_BAN_THRESHOLD = -100
PEER_SCORE_REWARD_GOOD   = 10
PEER_SCORE_PENALTY_BAD   = -50
RATE_LIMIT_MSGS_PER_SEC  = int(os.environ.get("BRN_RATE_LIMIT", "200"))

MAX_DISCOVERED_PEERS = 500
MAX_PEERS_PER_PEX    = 50
HEIGHT_CACHE_TTL     = 120

PEERS_FILE     = "peers_discovered.json"
BOOTSTRAP_FILE = "bootstrap_peers.json"
BOOTSTRAP_ENV  = os.environ.get("BRN_BOOTSTRAP_PEERS", "")

GH_USER     = os.environ.get("BRN_GH_USER", "").strip()
GH_REPO     = os.environ.get("BRN_GH_REPO", "brn-peers").strip()
GH_TOKEN    = os.environ.get("BRN_GH_TOKEN", "").strip()
GH_BRANCH   = os.environ.get("BRN_GH_BRANCH", "main").strip()
GH_FILE     = os.environ.get("BRN_GH_FILE", "peers.json").strip()
GH_INTERVAL = int(os.environ.get("BRN_GH_INTERVAL", "180"))
GH_TTL      = 600
GH_API      = "https://api.github.com"

TRACKER_URL             = os.environ.get("BRN_TRACKER", "").rstrip("/")
TRACKER_INTERVAL        = 120
BOOTSTRAP_PING_INTERVAL = 60

READ_ONLY              = os.environ.get("BRN_READ_ONLY", "0") == "1"
SYNC_BATCH_SIZE        = int(os.environ.get("BRN_SYNC_BATCH", "50"))
SYNC_RETRY_MAX         = int(os.environ.get("BRN_SYNC_RETRY_MAX", "5"))
SYNC_RETRY_BASE_S      = float(os.environ.get("BRN_SYNC_RETRY_BASE", "1.0"))
SYNC_RETRY_MAX_S       = float(os.environ.get("BRN_SYNC_RETRY_MAX_S", "300.0"))
TCP_TIMEOUT_DEFAULT    = float(os.environ.get("BRN_TCP_TIMEOUT", "10.0"))


def _obter_ou_criar_uuid() -> str:
    port = os.environ.get("BRN_P2P_PORT", str(TCP_PORT_DEFAULT))
    uuid_file = f"node_uuid_{port}.txt"
    if os.path.exists(uuid_file):
        try:
            with open(uuid_file) as f:
                val = f.read().strip()
                if val: return val
        except Exception: pass
    novo = str(uuid.uuid4())
    try:
        with open(uuid_file, "w") as f: f.write(novo)
    except Exception: pass
    return novo


def _get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception: return "127.0.0.1"
    finally: s.close()


def _get_public_ip() -> str:
    for url in ("https://ifconfig.me/ip", "https://api.ipify.org", "https://icanhazip.com"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=5) as r:
                ip = r.read().decode().strip()
                if ip and "." in ip: return ip
        except Exception: continue
    return ""


def _mesma_subnet(ip1: str, ip2: str) -> bool:
    try: return ip1.rsplit(".", 1)[0] == ip2.rsplit(".", 1)[0]
    except Exception: return False


def _carregar_bootstrap() -> set[str]:
    peers = set()
    if os.path.exists(BOOTSTRAP_FILE):
        try:
            with open(BOOTSTRAP_FILE) as f:
                data = json.load(f)
                if isinstance(data, list):
                    for p in data:
                        p = str(p).strip()
                        if p: peers.add(p)
        except Exception: pass
    for p in BOOTSTRAP_ENV.split(","):
        p = p.strip()
        if p: peers.add(p)
    return peers


def _registrar_peer_no_db(blockchain, ip: str, port: int, peer_id: Optional[str] = None) -> None:
    if blockchain is None: return
    try:
        addr = f"{ip}:{port}"
        genesis = blockchain.db.get_meta("genesis_hash") or ""
        node_id = peer_id or f"peer-{ip}:{port}"
        blockchain.db.upsert_peer(
            node_id=node_id, address=addr, genesis_hash=genesis,
            version=PROTOCOL_VERSION, height=0, is_miner=False,
            public_key=peer_id or "",
        )
    except Exception: pass


class ExponentialBackoff:
    def __init__(self, base: float = 1.0, max_s: float = 300.0, jitter: float = 0.1):
        self.base = base
        self.max_s = max_s
        self.jitter = jitter
        self.attempts = 0

    def next_sleep(self) -> float:
        self.attempts += 1
        exp = min(self.base * (2 ** (self.attempts - 1)), self.max_s)
        import random
        jit = exp * self.jitter * (random.random() * 2 - 1)
        return max(0.1, exp + jit)

    def reset(self) -> None:
        self.attempts = 0

    def give_up(self, max_attempts: int) -> bool:
        return self.attempts >= max_attempts


class SyncResult(Enum):
    OK         = "ok"
    UP_TO_DATE = "up_to_date"
    RETRYABLE  = "retryable"
    FATAL      = "fatal"


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.start_time = time.time()
        self.data = {
            "blocks_received": 0, "blocks_accepted": 0, "blocks_rejected": 0,
            "txs_received": 0, "txs_accepted": 0, "txs_rejected": 0,
            "bytes_in": 0, "bytes_out": 0,
            "messages_in": 0, "messages_out": 0,
            "sync_runs": 0, "sync_success": 0, "sync_failed": 0,
            "sync_blocks_applied": 0,
            "last_sync_duration_ms": 0.0, "last_sync_height": 0,
            "auth_ok": 0, "auth_failed": 0,
            "auth_replay_blocked": 0, "auth_payload_swap_blocked": 0,
            "forks_detected": 0,
        }
        self.peer_metrics = defaultdict(lambda: {
            "last_seen": 0, "latency_ms": 0.0, "blocks_contributed": 0,
            "tokens_sent": 0, "errors": 0,
        })

    def inc(self, key: str, n: int = 1) -> None:
        with self._lock:
            if key in self.data: self.data[key] += n

    def set(self, key: str, val) -> None:
        with self._lock: self.data[key] = val

    def peer_update(self, addr: str, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if k in self.peer_metrics[addr]:
                    self.peer_metrics[addr][k] = v
            self.peer_metrics[addr]["last_seen"] = time.time()

    def peer_add_tokens(self, addr: str, n: int) -> None:
        with self._lock: self.peer_metrics[addr]["tokens_sent"] += n

    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self.data)
            d["uptime_s"] = round(time.time() - self.start_time, 1)
            d["peers_tracked"] = len(self.peer_metrics)
            d["peers_active_5min"] = sum(1 for p in self.peer_metrics.values() if time.time() - p["last_seen"] < 300)
            top = sorted(self.peer_metrics.items(), key=lambda kv: kv[1]["blocks_contributed"], reverse=True)[:10]
            d["top_peers"] = [
                {"addr": a, "blocks": p["blocks_contributed"], "latency_ms": round(p["latency_ms"], 1), "last_seen_s": round(time.time() - p["last_seen"], 1)}
                for a, p in top
            ]
            return d


# ============================================================
# SERVIDOR TCP
# ============================================================
class P2PServer(threading.Thread):
    def __init__(self, blockchain, port: int, metrics: Metrics, on_new_block=None, on_new_tx=None, on_peer_bad=None, on_peer_good=None, get_peers_callback=None):
        super().__init__(daemon=True, name="P2P-Server")
        self.bc = blockchain
        self.port = port
        self.metrics = metrics
        self.on_new_block = on_new_block
        self.on_new_tx = on_new_tx
        self.on_peer_bad = on_peer_bad
        self.on_peer_good = on_peer_good
        self.get_peers_callback = get_peers_callback

        self.on_relay_caps = None
        self.on_relay_request = None
        self.on_relay_incoming = None
        self.on_relay_accepted = None
        self.on_relay_data = None

        self.node_uuid = ""
        self.running = False
        self.sock = None
        self._rate_counters = {}
        self._rate_lock = threading.Lock()

    def stop(self) -> None:
        self.running = False
        if self.sock:
            try: self.sock.close()
            except Exception: pass

    def _check_rate(self, addr: str) -> bool:
        agora = time.time()
        with self._rate_lock:
            janela = self._rate_counters.setdefault(addr, [])
            janela[:] = [t for t in janela if agora - t < 1.0]
            if len(janela) >= RATE_LIMIT_MSGS_PER_SEC: return False
            janela.append(agora)
            return True

    def _recv_message(self, conn: socket.socket, timeout: float = 30.0):
        conn.settimeout(timeout)
        raw = b""
        first = True
        while True:
            try: chunk = conn.recv(65536)
            except socket.timeout:
                if first: return None
                first = False
                continue
            first = False
            if not chunk: break
            raw += chunk
            if len(raw) > MAX_MSG_SIZE: return None
            if raw.startswith(NETWORK_MAGIC):
                try:
                    json.loads(raw[len(NETWORK_MAGIC):].decode())
                    break
                except (json.JSONDecodeError, UnicodeDecodeError): continue
        return raw

    def run(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(("0.0.0.0", self.port))
            self.sock.listen(20)
            log.info(f"Servidor TCP escutando na porta {self.port}")
            self.running = True
        except Exception as e:
            log.error(f"Erro abrindo porta {self.port}: {e}")
            return
        while self.running:
            try:
                self.sock.settimeout(1.0)
                conn, addr = self.sock.accept()
                threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True).start()
            except socket.timeout: continue
            except Exception as e:
                if self.running: log.error(f"accept: {e}")

    def _handle_conn(self, conn: socket.socket, addr) -> None:
        peer_ip = addr[0]
        peer_id = None
        try:
            if not self._check_rate(peer_ip): return
            score = self.bc.db.get_peer_score(peer_ip)
            if score <= PEER_SCORE_BAN_THRESHOLD: return

            raw = self._recv_message(conn, timeout=30.0)
            if not raw or not raw.startswith(NETWORK_MAGIC): return

            self.metrics.inc("messages_in")
            self.metrics.inc("bytes_in", len(raw))
            msg = json.loads(raw[len(NETWORK_MAGIC):].decode())

            if msg.get("_chain") != CHAIN_ID: return

            _auth = msg.pop("_auth", None)
            if auth_enabled():
                if _auth:
                    ok, err = verify_auth(_auth, msg=msg)
                    if not ok:
                        log.warning(f"[Auth] {peer_ip}: REJEITADO — {err}")
                        self.metrics.inc("auth_failed")
                        return
                    peer_id = _auth.get("pub", "")[:64] or None
                    self.metrics.inc("auth_ok")
                elif auth_required():
                    log.warning(f"[Auth] {peer_ip}: sem auth (required)")
                    self.metrics.inc("auth_failed")
                    return

            _registrar_peer_no_db(self.bc, peer_ip, int(msg.get("_port", 6001)), peer_id=peer_id)

            response = self._process_message(msg, addr, peer_id)
            if response:
                response["_chain"] = CHAIN_ID
                response["_uuid"] = self.node_uuid
                if auth_enabled():
                    try:
                        resp_sem = {k: v for k, v in response.items() if k != "_auth"}
                        response["_auth"] = build_auth(msg=resp_sem)
                    except Exception: pass
                payload = NETWORK_MAGIC + json.dumps(response).encode()
                conn.sendall(payload)
                self.metrics.inc("messages_out")
                self.metrics.inc("bytes_out", len(payload))
        except Exception as e:
            log.warning(f"erro com {addr}: {e}")
        finally:
            try: conn.close()
            except Exception: pass

    def _process_message(self, msg: dict, addr, peer_id: Optional[str]) -> Optional[dict]:
        mtype = msg.get("type")
        peer_ip = addr[0]
        try:
            if mtype == "ping":
                return {"type": "pong", "version": PROTOCOL_VERSION, "height": self.bc.db.height(), "work": self.bc.cumulative_work(), "decentralized": self.bc.rede_descentralizada, "queue_size": len(self.bc.fila_mineracao)}

            if mtype == "get_chain_height":
                return {"type": "chain_height", "height": self.bc.db.height(), "hash": self.bc.db.tip_hash(), "work": self.bc.cumulative_work(), "decentralized": self.bc.rede_descentralizada, "queue_size": len(self.bc.fila_mineracao)}

            if mtype == "join_mining_queue":
                req_addr = msg.get("address", "").strip()
                req_pub = msg.get("pubkey", "").strip()
                if req_addr and req_pub:
                    self.bc.adicionar_na_fila(req_addr, req_pub)
                    self.metrics.inc("queue_joined")
                return {
                    "type": "queue_ack",
                    "ok": True,
                    "queue_size": len(self.bc.fila_mineracao),
                    "decentralized": self.bc.rede_descentralizada
                }

            if mtype == "get_block":
                return {"type": "block", "block": self.bc.db.get_block(int(msg.get("height", 0)))}

            if mtype == "get_blocks_range":
                start = int(msg.get("start", 0))
                end = int(msg.get("end", start + 50))
                end = min(end, start + SYNC_BATCH_SIZE)
                blocks = self.bc.db.get_blocks_range(start, end)
                self.metrics.peer_add_tokens(peer_ip, len(blocks))
                return {"type": "blocks_range", "blocks": blocks}

            if mtype == "new_block":
                if READ_ONLY: return {"type": "ack", "ok": False, "reason": "read_only"}
                block_dict = msg.get("block")
                if block_dict and self.on_new_block:
                    ok = self.on_new_block(block_dict)
                    if ok and self.on_peer_good: self.on_peer_good(peer_ip, peer_id)
                    elif not ok and self.on_peer_bad: self.on_peer_bad(peer_ip, peer_id)
                    return {"type": "ack", "ok": bool(ok)}
                return {"type": "ack", "ok": False}

            if mtype == "new_tx":
                if READ_ONLY: return {"type": "ack", "ok": False, "reason": "read_only"}
                tx_dict = msg.get("tx")
                if tx_dict and self.on_new_tx:
                    ok = self.on_new_tx(tx_dict)
                    if ok and self.on_peer_good: self.on_peer_good(peer_ip, peer_id)
                    return {"type": "ack", "ok": bool(ok)}
                return {"type": "ack", "ok": False}

            if mtype == "get_mempool":
                return {"type": "mempool", "txs": self.bc.db.all_mempool(limit=200)}

            if mtype == "get_peers":
                peers = []
                if self.get_peers_callback:
                    try: peers = self.get_peers_callback()
                    except Exception: pass
                return {"type": "peers", "peers": peers[:MAX_PEERS_PER_PEX]}

            if mtype == "relay_capabilities":
                if self.on_relay_caps and peer_id:
                    try: self.on_relay_caps(peer_ip, peer_id, msg)
                    except Exception: pass
                return {"ok": True, "type": "ack"}

            if mtype == "relay_connect_request":
                if self.on_relay_request:
                    try: return self.on_relay_request(peer_ip, peer_id, msg)
                    except Exception: pass
                return {"ok": False, "error": "relay_disabled"}

            if mtype == "relay_connect_incoming":
                if self.on_relay_incoming:
                    try: return self.on_relay_incoming(peer_ip, peer_id, msg)
                    except Exception: pass
                return {"ok": False, "error": "relay_disabled"}

            if mtype == "relay_connect_accepted":
                if self.on_relay_accepted:
                    try: self.on_relay_accepted(peer_ip, peer_id, msg)
                    except Exception: pass
                return {"ok": True, "type": "ack"}

            if mtype == "relay_data":
                if self.on_relay_data:
                    try: return self.on_relay_data(peer_ip, peer_id, msg)
                    except Exception: pass
                return {"ok": False, "error": "relay_disabled"}

        except Exception as e:
            return {"type": "error", "message": str(e)}
        return {"type": "error", "message": "Tipo desconhecido"}


# ============================================================
# CLIENTE TCP [FIX v6.6.5 - timeout em todos os métodos]
# ============================================================
class P2PClient:
    @staticmethod
    def send_message(ip: str, port: int, message: dict, timeout: Optional[float] = None):
        if timeout is None: timeout = TCP_TIMEOUT_DEFAULT
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            t0 = time.time()
            s.connect((ip, port))

            message = dict(message)
            message["_chain"] = CHAIN_ID
            message["_port"] = int(os.environ.get("BRN_P2P_PORT", "6001"))

            if auth_enabled():
                try:
                    msg_sem = {k: v for k, v in message.items() if k != "_auth"}
                    message["_auth"] = build_auth(msg=msg_sem)
                except Exception: pass

            payload = NETWORK_MAGIC + json.dumps(message).encode()
            s.sendall(payload)

            raw = b""
            while True:
                try: chunk = s.recv(65536)
                except socket.timeout: break
                if not chunk: break
                raw += chunk
                if len(raw) > MAX_MSG_SIZE: break
                if raw.startswith(NETWORK_MAGIC):
                    try:
                        json.loads(raw[len(NETWORK_MAGIC):].decode())
                        break
                    except (json.JSONDecodeError, UnicodeDecodeError): continue

            latency_ms = (time.time() - t0) * 1000
            s.close()

            if raw.startswith(NETWORK_MAGIC):
                resp = json.loads(raw[len(NETWORK_MAGIC):].decode())
                if resp.get("_chain") != CHAIN_ID: return None, latency_ms
                if auth_enabled():
                    _rauth = resp.pop("_auth", None)
                    if _rauth:
                        ok, err = verify_auth(_rauth, msg=resp)
                        if not ok: return None, latency_ms
                    elif auth_required(): return None, latency_ms
                return resp, latency_ms
            return None, latency_ms
        except Exception as e:
            return None, 0.0

    @staticmethod
    def ping(ip, port, timeout=None): return P2PClient.send_message(ip, port, {"type": "ping"}, timeout=timeout)
    @staticmethod
    def get_chain_height(ip, port, timeout=None): return P2PClient.send_message(ip, port, {"type": "get_chain_height"}, timeout=timeout)
    @staticmethod
    def get_blocks_range(ip, port, s, e, timeout=None): return P2PClient.send_message(ip, port, {"type": "get_blocks_range", "start": s, "end": e}, timeout=timeout)
    @staticmethod
    def send_block(ip, port, block, timeout=None): return P2PClient.send_message(ip, port, {"type": "new_block", "block": block}, timeout=timeout)
    @staticmethod
    def send_tx(ip, port, tx, timeout=None): return P2PClient.send_message(ip, port, {"type": "new_tx", "tx": tx}, timeout=timeout)
    @staticmethod
    def get_mempool(ip, port, timeout=None): return P2PClient.send_message(ip, port, {"type": "get_mempool"}, timeout=timeout)
    @staticmethod
    def get_peers(ip, port, timeout=None): return P2PClient.send_message(ip, port, {"type": "get_peers"}, timeout=timeout)


# ============================================================
# DESCOBERTA (UDP multicast)
# ============================================================
class PeerDiscovery:
    def __init__(self, tcp_port: int, node_uuid: str, on_peer_found=None, blockchain=None):
        self.tcp_port = tcp_port
        self.node_uuid = node_uuid
        self.on_peer_found = on_peer_found
        self.bc = blockchain
        self.discovered_peers = {}
        self.peers_lock = threading.Lock()
        self.peers_respondidos = set()
        self.running = False
        self.server_socket = None
        self.client_socket = None
        self._threads = []
        self.uuid_to_addr = {}
        self.uuid_lock = threading.Lock()
        self._height_cache = {}
        self._height_cache_lock = threading.Lock()
        self._carregar_peers()

    def _salvar_peers(self) -> None:
        try:
            with self.peers_lock:
                lista = list(self.discovered_peers.keys())
            with open(PEERS_FILE, "w") as f: json.dump(lista, f)
        except Exception: pass

    def _carregar_peers(self) -> None:
        try:
            if os.path.exists(PEERS_FILE):
                with open(PEERS_FILE) as f:
                    for addr in json.load(f):
                        self.discovered_peers[addr] = 0
        except Exception: pass
        for p in _carregar_bootstrap():
            self.discovered_peers.setdefault(p, 0)

    def _add_peer(self, addr: str, ts: Optional[float] = None) -> bool:
        with self.peers_lock:
            is_new = addr not in self.discovered_peers
            if not is_new:
                if ts is not None: self.discovered_peers[addr] = ts
                return False
            if len(self.discovered_peers) >= MAX_DISCOVERED_PEERS:
                try:
                    oldest = min(self.discovered_peers.items(), key=lambda kv: kv[1])
                    del self.discovered_peers[oldest[0]]
                except ValueError: pass
            self.discovered_peers[addr] = ts if ts is not None else time.time()
            return True

    def register_uuid(self, uuid_hex: str, ip: str, port: int) -> None:
        if not uuid_hex or not ip: return
        with self.uuid_lock:
            self.uuid_to_addr[uuid_hex] = (ip, int(port), time.time())

    def get_addr_by_uuid(self, uuid_hex: str) -> Optional[tuple[str, int]]:
        with self.uuid_lock:
            v = self.uuid_to_addr.get(uuid_hex)
            if not v: return None
            if time.time() - v[2] > 600: return None
            return (v[0], v[1])

    def _start_server(self) -> None:
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try: self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError): pass
            self.server_socket.bind(("", MULTICAST_PORT))
            mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton("0.0.0.0")
            self.server_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            local_ip = _get_local_ip()
            log.info(f"Discovery multicast ativo — local={local_ip}")
            while self.running:
                try:
                    self.server_socket.settimeout(2.0)
                    data, addr = self.server_socket.recvfrom(2048)
                    remote_ip = addr[0]
                    if not _mesma_subnet(remote_ip, local_ip): continue
                    msg = data.decode("utf-8", errors="ignore")
                    if not self._token_valido(msg): continue
                    if msg.startswith("BRN_NODE_PING:"): self._processar_ping(msg, remote_ip, addr)
                    elif msg.startswith("BRN_NODE_PONG:"): self._processar_pong(msg, remote_ip)
                    elif msg.startswith("BRN_NODE_BYE:"): self._processar_bye(msg, remote_ip)
                except socket.timeout: continue
                except OSError: break
                except Exception: pass
        except Exception as e:
            log.error(f"falha no discovery multicast: {e}")

    def _token_valido(self, msg: str) -> bool:
        try: return msg.split(":")[-1] == TOKEN_ESPERADO
        except Exception: return False

    def _processar_ping(self, msg: str, remote_ip: str, addr) -> None:
        try:
            partes = msg.split(":")
            if len(partes) < 4: return
            remote_port = int(partes[1])
            remote_uuid = partes[2]
            if remote_uuid == self.node_uuid: return
            peer_address = f"{remote_ip}:{remote_port}"
            self.register_uuid(remote_uuid, remote_ip, remote_port)
            novo = self._add_peer(peer_address)
            if novo:
                log.info(f"Novo peer LAN: {peer_address}")
                self._salvar_peers()
                if self.on_peer_found:
                    try: self.on_peer_found(remote_ip, remote_port)
                    except Exception: pass
            if remote_ip not in self.peers_respondidos:
                self.peers_respondidos.add(remote_ip)
                response = (f"BRN_NODE_PONG:{self.tcp_port}:{self.node_uuid}:{TOKEN_ESPERADO}")
                try: self.server_socket.sendto(response.encode("utf-8"), addr)
                except Exception: pass
        except Exception: pass

    def _processar_pong(self, msg: str, remote_ip: str) -> None:
        try:
            partes = msg.split(":")
            if len(partes) < 4: return
            remote_port = int(partes[1])
            remote_uuid = partes[2]
            if remote_uuid == self.node_uuid: return
            peer_address = f"{remote_ip}:{remote_port}"
            self.register_uuid(remote_uuid, remote_ip, remote_port)
            novo = self._add_peer(peer_address)
            if novo:
                log.info(f"Conexão mútua: {peer_address}")
                self._salvar_peers()
                if self.on_peer_found:
                    try: self.on_peer_found(remote_ip, remote_port)
                    except Exception: pass
        except Exception: pass

    def _processar_bye(self, msg: str, remote_ip: str) -> None:
        try:
            partes = msg.split(":")
            if len(partes) < 4: return
            remote_port = int(partes[1])
            with self.peers_lock: self.discovered_peers.pop(f"{remote_ip}:{remote_port}", None)
        except Exception: pass

    def _start_client(self) -> None:
        try:
            self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self.client_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            self.client_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            log.info("Discovery multicast client ativo")
            while self.running:
                try:
                    msg = (f"BRN_NODE_PING:{self.tcp_port}:{self.node_uuid}:{TOKEN_ESPERADO}")
                    self.client_socket.sendto(msg.encode("utf-8"), (MULTICAST_GROUP, MULTICAST_PORT))
                    with self.peers_lock: n = len(self.discovered_peers)
                    if n >= 5: intervalo = DISCOVERY_INTERVAL_MAX
                    elif n >= 2: intervalo = 10.0
                    else: intervalo = DISCOVERY_INTERVAL_MIN
                    time.sleep(intervalo)
                except OSError: break
                except Exception: time.sleep(5)
        except Exception as e:
            log.error(f"falha no discovery client: {e}")

    def _ping_bootstrap_loop(self) -> None:
        log.info(f"Bootstrap loop iniciado ({BOOTSTRAP_PING_INTERVAL}s)")
        while self.running:
            try:
                peers = _carregar_bootstrap()
                for peer in peers:
                    if not self.running: break
                    try:
                        ip, port = peer.split(":")
                        port = int(port)
                        resp, _ = P2PClient.ping(ip, port)
                        if resp:
                            novo = self._add_peer(peer)
                            if resp.get("_uuid"): self.register_uuid(resp["_uuid"], ip, port)
                            _registrar_peer_no_db(self.bc, ip, port, resp.get("_uuid"))
                            if novo:
                                log.info(f"Bootstrap conectado: {peer}")
                                self._salvar_peers()
                            if self.on_peer_found:
                                try: self.on_peer_found(ip, port)
                                except Exception: pass
                    except Exception: pass
            except Exception: pass
            time.sleep(BOOTSTRAP_PING_INTERVAL)

    def _limpar_peers_mortos(self) -> None:
        while self.running:
            try:
                time.sleep(PEER_CLEANUP_S)
                agora = time.time()
                with self.peers_lock:
                    for addr, ts in list(self.discovered_peers.items()):
                        if ts > 0 and (agora - ts) > PEER_TIMEOUT: del self.discovered_peers[addr]
                self._salvar_peers()
            except Exception: pass

    def _refresh_heights_loop(self) -> None:
        while self.running:
            try:
                peers = self.listar_peers()
                for p in peers[:50]:
                    if not self.running: break
                    try:
                        ip, port = p.split(":")
                        resp, lat = P2PClient.get_chain_height(ip, int(port))
                        if not resp: continue
                        try: _registrar_peer_no_db(self.bc, ip, int(port), resp.get("_uuid"))
                        except Exception: pass
                        with self._height_cache_lock:
                            self._height_cache[p] = (time.time(), resp.get("height", 0), resp.get("work", 0), lat)
                        if resp.get("_uuid"): self.register_uuid(resp["_uuid"], ip, int(port))
                    except Exception: continue
            except Exception: pass
            time.sleep(60)

    def run(self) -> None:
        if self.running: return
        self.running = True
        threads = [
            threading.Thread(target=self._start_server, daemon=True, name="Disc-UDP"),
            threading.Thread(target=self._start_client, daemon=True, name="Disc-UDP-Client"),
            threading.Thread(target=self._limpar_peers_mortos, daemon=True, name="Disc-Cleanup"),
            threading.Thread(target=self._ping_bootstrap_loop, daemon=True, name="Disc-Bootstrap"),
            threading.Thread(target=self._refresh_heights_loop, daemon=True, name="Disc-Heights"),
        ]
        for t in threads:
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        if not self.running: return
        self.running = False
        try:
            if self.client_socket:
                bye = (f"BRN_NODE_BYE:{self.tcp_port}:{self.node_uuid}:{TOKEN_ESPERADO}")
                self.client_socket.sendto(bye.encode("utf-8"), (MULTICAST_GROUP, MULTICAST_PORT))
        except Exception: pass
        for sock in (self.server_socket, self.client_socket):
            if sock:
                try: sock.close()
                except Exception: pass
        self._salvar_peers()

    def listar_peers(self) -> list[str]:
        with self.peers_lock: return list(self.discovered_peers.keys())

    def listar_peers_com_altura(self) -> list[dict]:
        with self._height_cache_lock:
            now = time.time()
            return [
                {"address": a, "height": h, "work": w, "latency_ms": round(lat, 1), "age_s": round(now - ts, 1)}
                for a, (ts, h, w, lat) in self._height_cache.items() if now - ts < HEIGHT_CACHE_TTL
            ]


# ============================================================
# MANAGER
# ============================================================
class P2PManager:
    def __init__(self, blockchain, node_id_priv=None, tcp_port: int = TCP_PORT_DEFAULT, enable_upnp: bool = True):
        self.bc = blockchain
        self.node_id_priv = node_id_priv
        self.tcp_port = tcp_port
        self.enable_upnp = enable_upnp
        self.read_only = READ_ONLY
        self.node_uuid = _obter_ou_criar_uuid()
        self.metrics = Metrics()
        self.external_ip = None
        self.upnp = None

        if node_id_priv is not None:
            _auth_set_priv(node_id_priv)
            pub_hex = node_id_priv.public_key().public_bytes_raw().hex()
            log.info(f"[Auth] node_id_priv (pub={pub_hex[:16]}...)")

        self._backoffs = defaultdict(lambda: ExponentialBackoff(base=SYNC_RETRY_BASE_S, max_s=SYNC_RETRY_MAX_S))
        self._backoff_lock = threading.Lock()
        self._broadcast_pool = ThreadPoolExecutor(max_workers=20, thread_name_prefix="p2p-bcast")

        self.server = P2PServer(
            blockchain, tcp_port, self.metrics,
            on_new_block=self._on_new_block,
            on_new_tx=self._on_new_tx,
            on_peer_bad=self._on_peer_bad,
            on_peer_good=self._on_peer_good,
            get_peers_callback=self._get_peers_for_pex,
        )
        self.server.node_uuid = self.node_uuid

        self.discovery = PeerDiscovery(tcp_port, self.node_uuid, on_peer_found=self._on_peer_found, blockchain=blockchain)

        if enable_upnp and not READ_ONLY:
            threading.Thread(target=self._setup_upnp, daemon=True, name="UPnP").start()

    def start(self) -> None:
        mode = "READ-ONLY" if self.read_only else "FULL"
        log.info(f"[P2P] iniciado node_id={self.node_uuid[:8]} modo={mode} chain={CHAIN_ID}")
        self.server.start()
        self.discovery.run()

    def stop(self) -> None:
        self.server.stop()
        self.discovery.stop()
        try: self._broadcast_pool.shutdown(wait=False, cancel_futures=True)
        except Exception: pass
        if self.upnp and self.external_ip:
            try: self.upnp.delete_port_mapping(self.tcp_port, "TCP")
            except Exception: pass

    def add_peer(self, ip: str, port: int) -> None:
        try: self.discovery._add_peer(f"{ip}:{port}", ts=time.time())
        except Exception: pass
        self._on_peer_found(ip, port)

    def get_status(self) -> dict:
        peers = self.discovery.listar_peers()
        return {
            "node_id": self.node_uuid, "port": self.tcp_port, "external_ip": self.external_ip,
            "peer_count": len(peers), "peers": peers, "height": self.bc.db.height(), "work": self.bc.cumulative_work(),
            "bootstrap": list(_carregar_bootstrap()), "read_only": self.read_only, "protocol": PROTOCOL_VERSION,
            "chain": CHAIN_ID, "decentralized": self.bc.rede_descentralizada, "queue_size": len(self.bc.fila_mineracao)
        }

    def get_metrics(self) -> dict: return self.metrics.snapshot()
    def get_peer_stats(self) -> list: return self.discovery.listar_peers_com_altura()
    def is_read_only(self) -> bool: return self.read_only

    def _setup_upnp(self) -> None:
        try:
            self.upnp = UPnPClient()
            if not self.upnp.control_url: return
            local_ip = _get_local_ip()
            ok = self.upnp.add_port_mapping(self.tcp_port, self.tcp_port, local_ip, description="BRN Node")
            if ok:
                self.external_ip = self.upnp.get_external_ip()
                log.info(f"[UPnP] porta {self.tcp_port} mapeada — {self.external_ip}")
        except Exception: pass

    def _get_peers_for_pex(self) -> list:
        try: return self.discovery.listar_peers_com_altura()[:MAX_PEERS_PER_PEX]
        except Exception: return []

    def _on_peer_found(self, ip: str, port: int) -> None:
        _registrar_peer_no_db(self.bc, ip, port)
        try:
            resp, latency = P2PClient.get_chain_height(ip, port)
            if not resp: return
            self.metrics.peer_update(f"{ip}:{port}", latency_ms=latency)
            if resp.get("_uuid"): self.discovery.register_uuid(resp["_uuid"], ip, port)

            if resp.get("decentralized") and not self.bc.rede_descentralizada:
                log.warning("[P2P] Rede descentralizada detectada via peer! Iniciando mineracao.")
                self.bc.rede_descentralizada = True

            remote_work = resp.get("work", 0)
            local_work = self.bc.cumulative_work()
            if remote_work > local_work:
                threading.Thread(target=self._sync_with_retry, args=(ip, port), daemon=True, name=f"Sync-{ip}-{port}").start()

            peers_resp, _ = P2PClient.get_peers(ip, port)
            if peers_resp and "peers" in peers_resp:
                novos = 0
                for p in peers_resp["peers"][:MAX_PEERS_PER_PEX]:
                    addr = p.get("address", "") if isinstance(p, dict) else str(p)
                    if not addr or ":" not in addr: continue
                    if self.discovery._add_peer(addr, ts=time.time()): novos += 1
                if novos > 0:
                    log.info(f"[PEX] +{novos} peer(s) via {ip}")
                    self.discovery._salvar_peers()
        except Exception: pass

    def _sync_with_retry(self, ip: str, port: int) -> None:
        addr = f"{ip}:{port}"
        backoff = self._backoffs[addr]
        self.metrics.inc("sync_runs")
        while True:
            try:
                result, msg = self._sync_incremental(ip, port)
                if result in (SyncResult.OK, SyncResult.UP_TO_DATE):
                    backoff.reset()
                    self.metrics.inc("sync_success")
                    return
                self.metrics.inc("sync_failed")
                if result == SyncResult.FATAL:
                    log.warning(f"[Sync] {addr}: fatal — {msg}")
                    return
                if backoff.give_up(SYNC_RETRY_MAX):
                    log.warning(f"[Sync] desistindo de {addr} — {msg}")
                    return
                wait = backoff.next_sleep()
                log.info(f"[Sync] {addr}: retry em {wait:.1f}s — {msg}")
                time.sleep(wait)
            except Exception as e:
                self.metrics.inc("sync_failed")
                if backoff.give_up(SYNC_RETRY_MAX): return
                wait = backoff.next_sleep()
                time.sleep(wait)

    def _sync_incremental(self, ip: str, port: int):
        t0 = time.time()
        addr = f"{ip}:{port}"
        resp, latency = P2PClient.get_chain_height(ip, port)
        if not resp: return SyncResult.RETRYABLE, "peer não respondeu"
        self.metrics.peer_update(addr, latency_ms=latency)

        remote_height = resp.get("height", -1)
        remote_work = resp.get("work", 0)
        local_height = self.bc.db.height()
        local_work = self.bc.cumulative_work()

        if remote_work <= local_work and remote_height <= local_height:
            return SyncResult.UP_TO_DATE, "sem mais trabalho"

        start = local_height + 1
        if start > remote_height: return SyncResult.UP_TO_DATE, "nada novo"

        log.info(f"[Sync] {addr}: {local_height} -> {remote_height}")
        blocks_to_apply = []
        cursor = start
        while cursor <= remote_height:
            end = min(cursor + SYNC_BATCH_SIZE, remote_height + 1)
            resp, lat = P2PClient.get_blocks_range(ip, port, cursor, end)
            if not resp or "blocks" not in resp: return SyncResult.RETRYABLE, f"falha no lote {cursor}-{end}"
            lote = resp["blocks"]
            if not lote: break
            self.metrics.peer_update(addr, latency_ms=lat)
            blocks_to_apply.extend(lote)
            cursor = end

        if not blocks_to_apply: return SyncResult.RETRYABLE, "nenhum bloco recebido"
        if not _HAS_BLOCKCHAIN_HELPERS: return SyncResult.FATAL, "blockchain helpers ausentes"

        prev = self.bc.db.get_block(local_height)
        prev_hash = prev["hash"] if prev else "0" * 64
        expected_height = local_height + 1
        for blk in blocks_to_apply:
            try:
                if blk.get("height") != expected_height: return SyncResult.FATAL, f"altura fora de ordem: {blk.get('height')}"
                if blk.get("prev_hash") != prev_hash: return SyncResult.FATAL, f"prev_hash diverge em #{blk.get('height')}"
                h_recalc = block_hash(blk["prev_hash"], blk["merkle"], blk["timestamp"], blk["nonce"], blk["difficulty"])
                if h_recalc != blk["hash"]: return SyncResult.FATAL, f"hash inválido em #{blk['height']}"
                if not meets_difficulty(blk["hash"], blk["difficulty"]): return SyncResult.FATAL, f"PoW insuf. em #{blk['height']}"
                prev_hash = blk["hash"]
                expected_height += 1
            except Exception as e:
                return SyncResult.FATAL, f"pré-validação falhou: {e}"

        self.metrics.inc("blocks_received", len(blocks_to_apply))
        applied = 0
        for blk in blocks_to_apply:
            expected_h = self.bc.db.height() + 1
            expected_prev = self.bc.db.tip_hash()
            if blk["height"] != expected_h: return SyncResult.FATAL, f"bloco #{blk['height']} fora de sequência"
            if blk["prev_hash"] != expected_prev: return SyncResult.FATAL, f"prev_hash diverge em #{blk['height']}"
            ok_blk, msg_blk = self.bc.accept_block(blk)
            if not ok_blk: return SyncResult.FATAL, f"bloco #{blk['height']}: {msg_blk}"
            applied += 1

        dt_ms = (time.time() - t0) * 1000
        self.metrics.set("last_sync_duration_ms", round(dt_ms, 1))
        self.metrics.set("last_sync_height", self.bc.db.height())
        self.metrics.inc("sync_blocks_applied", applied)
        self.metrics.inc("blocks_accepted", applied)
        self.metrics.peer_update(addr, blocks_contributed=applied)

        log.info(f"[Sync] {addr}: OK — +{applied} blocos em {dt_ms:.0f}ms")
        return SyncResult.OK, f"+{applied} blocos"

    def sync_with_peer(self, ip: str, port: int):
        return self._sync_incremental(ip, port)

    def broadcast_block(self, block_dict: dict) -> None:
        if self.read_only: return
        for peer in self.discovery.listar_peers():
            try:
                ip, port = peer.split(":")
                self._broadcast_pool.submit(P2PClient.send_block, ip, int(port), block_dict)
            except Exception: pass

    def broadcast_tx(self, tx_dict: dict) -> None:
        if self.read_only: return
        for peer in self.discovery.listar_peers():
            try:
                ip, port = peer.split(":")
                self._broadcast_pool.submit(P2PClient.send_tx, ip, int(port), tx_dict)
            except Exception: pass

    def _on_new_block(self, block_dict: dict) -> bool:
        try:
            h = int(block_dict.get("height", -1))
            local_h = self.bc.db.height()
            if h == local_h + 1:
                ok, msg = self.bc.accept_block(block_dict)
                if ok:
                    self.metrics.inc("blocks_accepted")
                    log.info(f"novo bloco aceito: #{h}")
                    try: self._broadcast_pool.submit(self.broadcast_block, block_dict)
                    except Exception: pass
                else:
                    self.metrics.inc("blocks_rejected")
                    log.warning(f"bloco #{h} rejeitado: {msg}")
                return ok
            if h <= local_h:
                try: local_blk = self.bc.db.get_block(h)
                except Exception: local_blk = None
                if local_blk and local_blk.get("hash") != block_dict.get("hash"):
                    self.metrics.inc("forks_detected")
                    log.warning(f"[P2P] FORK detectado em #{h}")
                return False
            return False
        except Exception: return False

    def _on_new_tx(self, tx_dict: dict) -> bool:
        try:
            ok, msg = self.bc.submit_tx(tx_dict)
            if ok:
                self.metrics.inc("txs_accepted")
                log.info(f"nova tx: {str(tx_dict.get('txid', '?'))[:16]}")
                try: self._broadcast_pool.submit(self.broadcast_tx, tx_dict)
                except Exception: pass
            else: self.metrics.inc("txs_rejected")
            return ok
        except Exception: return False

    def _on_peer_bad(self, peer_ip: str, peer_id: Optional[str] = None) -> None:
        try:
            novo = self.bc.db.add_peer_score(peer_ip, PEER_SCORE_PENALTY_BAD)
            if novo <= PEER_SCORE_BAN_THRESHOLD:
                log.warning(f"BAN: {peer_ip}")
                if peer_id:
                    try: self.bc.db.remover_peer_por_pubkey(peer_id)
                    except Exception: pass
                try: self.bc.db.remover_peer_por_endereco(peer_ip)
                except Exception: pass
        except Exception: pass

    def _on_peer_good(self, peer_ip: str, peer_id: Optional[str] = None) -> None:
        try: self.bc.db.add_peer_score(peer_ip, PEER_SCORE_REWARD_GOOD)
        except Exception: pass

if __name__ == "__main__":
    print("Rode via main.py")
