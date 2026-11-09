"""
p2p_unified.py — BRN P2P Network v6.7.10
================================================================
v6.7.10 (2026-11-08) — PATCH DE ROBUSTEZ:
  [FIX] P2PClient.send_message: retry em ConnectionResetError e
        ConnectionRefusedError. Antes, um reset transitório (comum
        em Tailscale/WAN) descartava a mensagem. broadcast_block
        não tem retry — bloco ficava preso no minerador até o
        próximo sync (30s+).
  [FIX] _handle_conn: valida peer_id contra _auth.pub. Antes, um
        atacante enviava _auth com pubkey alheia e ganhava reputação
        no peer errado. Combinado com PEER_SCORE_BAN_THRESHOLD,
        podia derrubar peers honestos enviando blocos válidos com
        a identidade deles.
  [FIX] _sync_incremental: pré-valida validators_sig antes de
        accept_block. Antes, aceitava o lote, accept_block rejeitava
        por sig, e o loop continuava tentando o peer — log cheio
        de "prev_hash diverge" mesmo quando o problema real era
        multi-sig.
  [FIX] broadcast_block e broadcast_tx: fallback síncrono quando o
        pool está cheio. Antes, ThreadPoolExecutor.submit() levantava
        RuntimeError e o bloco/tx não era enviado — silenciosamente.
  [KEEP] v6.7.9: suporte a PBFT.
  [KEEP] v6.7.8: race recovery reversa.
  [KEEP] v6.7.7: race recovery.

v6.7.9 (2026-11-05):
  [FIX CRÍTICO] Adicionado suporte ao PBFT.
v6.7.8 (2026-11-04):
  [FIX] _handle_sign_block aceita cenário de race recovery reversa.
v6.7.7 (2026-11-02):
  [FIX] _handle_sign_block aceita race recovery.
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

PROTOCOL_VERSION = "BRN5/2.4-MultiSig"
NETWORK_MAGIC    = b"BRN5"
CHAIN_ID         = os.environ.get("BRN_CHAIN_ID", "mainnet").strip()

PEER_SCORE_BAN_THRESHOLD = -100
PEER_SCORE_REWARD_GOOD   = 10
PEER_SCORE_PENALTY_BAD   = -50
RATE_LIMIT_MSGS_PER_SEC  = int(os.environ.get("BRN_RATE_LIMIT", "200"))

MAX_CONNECTIONS_PER_IP   = int(os.environ.get("BRN_MAX_CONNS_PER_IP", "5"))

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
BLOCK_SIG_TIMEOUT_S    = float(os.environ.get("BRN_BLOCK_SIG_TIMEOUT", "5.0"))

# [v6.7.10] Retry de conexão TCP
SEND_RETRY_MAX         = int(os.environ.get("BRN_SEND_RETRY_MAX", "2"))
SEND_RETRY_BASE_S      = float(os.environ.get("BRN_SEND_RETRY_BASE", "0.3"))


def _obter_ou_criar_uuid() -> str:
    port = os.environ.get("BRN_P2P_PORT", str(TCP_PORT_DEFAULT))
    uuid_file = f"node_uuid_{port}.txt"
    if os.path.exists(uuid_file):
        try:
            with open(uuid_file) as f:
                val = f.read().strip()
                if val:
                    return val
        except Exception:
            pass
    novo = str(uuid.uuid4())
    try:
        with open(uuid_file, "w") as f:
            f.write(novo)
    except Exception:
        pass
    return novo


def _get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _get_public_ip() -> str:
    for url in ("https://ifconfig.me/ip", "https://api.ipify.org", "https://icanhazip.com"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=5) as r:
                ip = r.read().decode().strip()
                if ip and "." in ip:
                    return ip
        except Exception:
            continue
    return ""


def _mesma_subnet(ip1: str, ip2: str) -> bool:
    try:
        return ip1.rsplit(".", 1)[0] == ip2.rsplit(".", 1)[0]
    except Exception:
        return False


def _carregar_bootstrap() -> set[str]:
    peers = set()
    if os.path.exists(BOOTSTRAP_FILE):
        try:
            with open(BOOTSTRAP_FILE) as f:
                data = json.load(f)
                if isinstance(data, list):
                    for p in data:
                        p = str(p).strip()
                        if p:
                            peers.add(p)
        except Exception:
            pass
    for p in BOOTSTRAP_ENV.split(","):
        p = p.strip()
        if p:
            peers.add(p)
    return peers


def _registrar_peer_no_db(blockchain, ip: str, port: int, peer_id: Optional[str] = None) -> None:
    if blockchain is None:
        return
    try:
        addr = f"{ip}:{port}"
        genesis = blockchain.db.get_meta("genesis_hash") or ""
        node_id = peer_id or f"peer-{ip}:{port}"
        blockchain.db.upsert_peer(
            node_id=node_id, address=addr, genesis_hash=genesis,
            version=PROTOCOL_VERSION, height=0, is_miner=False,
            public_key=peer_id or "",
        )
    except Exception:
        pass


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
            "block_sigs_requested": 0,
            "block_sigs_provided": 0,
            "block_sigs_invalid": 0,
        }
        self.peer_metrics = defaultdict(lambda: {
            "last_seen": 0, "latency_ms": 0.0, "blocks_contributed": 0,
            "tokens_sent": 0, "errors": 0,
        })

    def inc(self, key: str, n: int = 1) -> None:
        with self._lock:
            if key in self.data:
                self.data[key] += n

    def set(self, key: str, val) -> None:
        with self._lock:
            self.data[key] = val

    def peer_update(self, addr: str, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if k in self.peer_metrics[addr]:
                    self.peer_metrics[addr][k] = v
            self.peer_metrics[addr]["last_seen"] = time.time()

    def peer_add_tokens(self, addr: str, n: int) -> None:
        with self._lock:
            self.peer_metrics[addr]["tokens_sent"] += n

    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self.data)
            d["uptime_s"] = round(time.time() - self.start_time, 1)
            d["peers_tracked"] = len(self.peer_metrics)
            d["peers_active_5min"] = sum(
                1 for p in self.peer_metrics.values()
                if time.time() - p["last_seen"] < 300
            )
            top = sorted(
                self.peer_metrics.items(),
                key=lambda kv: kv[1]["blocks_contributed"],
                reverse=True,
            )[:10]
            d["top_peers"] = [
                {
                    "addr": a,
                    "blocks": p["blocks_contributed"],
                    "latency_ms": round(p["latency_ms"], 1),
                    "last_seen_s": round(time.time() - p["last_seen"], 1),
                }
                for a, p in top
            ]
            return d


class P2PServer(threading.Thread):
    def __init__(self, blockchain, port: int, metrics: Metrics,
                 on_new_block=None, on_new_tx=None, on_peer_bad=None,
                 on_peer_good=None, get_peers_callback=None):
        super().__init__(daemon=True, name="P2P-Server")
        self.bc = blockchain
        self.port = port
        self.metrics = metrics
        self.on_new_block = on_new_block
        self.on_new_tx = on_new_tx
        self.pbft = None
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

        self._active_conns_by_ip = defaultdict(int)

    def stop(self) -> None:
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

    def set_pbft(self, pbft) -> None:
        self.pbft = pbft
        if pbft is not None:
            log.info("[P2P] PBFT injetado no P2PServer")

    def _check_rate(self, addr: str) -> bool:
        agora = time.time()
        with self._rate_lock:
            janela = self._rate_counters.setdefault(addr, [])
            janela[:] = [t for t in janela if agora - t < 1.0]
            if len(janela) >= RATE_LIMIT_MSGS_PER_SEC:
                return False
            janela.append(agora)
            return True

    def _recv_message(self, conn: socket.socket, timeout: float = 30.0):
        conn.settimeout(timeout)
        raw = b""
        first = True
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                if first:
                    return None
                first = False
                continue
            first = False
            if not chunk:
                break
            raw += chunk
            if len(raw) > MAX_MSG_SIZE:
                return None
            if raw.startswith(NETWORK_MAGIC):
                try:
                    json.loads(raw[len(NETWORK_MAGIC):].decode())
                    break
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
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
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    log.error(f"accept: {e}")

    def _handle_conn(self, conn: socket.socket, addr) -> None:
        peer_ip = addr[0]
        peer_id = None

        with self._rate_lock:
            self._active_conns_by_ip[peer_ip] += 1
            if self._active_conns_by_ip[peer_ip] > MAX_CONNECTIONS_PER_IP:
                self._active_conns_by_ip[peer_ip] -= 1
                log.warning(f"[SECURITY] Limite de conexões excedido para {peer_ip}. Derrubando.")
                try: conn.close()
                except: pass
                return

        try:
            if not self._check_rate(peer_ip):
                return
            score = self.bc.db.get_peer_score(peer_ip)
            if score <= PEER_SCORE_BAN_THRESHOLD:
                return

            raw = self._recv_message(conn, timeout=30.0)
            if not raw or not raw.startswith(NETWORK_MAGIC):
                return

            self.metrics.inc("messages_in")
            self.metrics.inc("bytes_in", len(raw))
            msg = json.loads(raw[len(NETWORK_MAGIC):].decode())

            if msg.get("_chain") != CHAIN_ID:
                return

            _auth = msg.pop("_auth", None)
            if auth_enabled():
                if _auth:
                    ok, err = verify_auth(_auth, msg=msg)
                    if not ok:
                        log.warning(f"[Auth] {peer_ip}: REJEITADO — {err}")
                        self.metrics.inc("auth_failed")
                        return
                    # [FIX v6.7.10] Valida que `pub` do _auth é o peer_id
                    # e é consistente com o que o peer enviou. Antes, qualquer
                    # _auth com pub válido virava peer_id, permitindo spoofing
                    # de reputação (_on_peer_good creditava pubkey alheia).
                    auth_pub = (_auth.get("pub") or "").strip().lower()
                    if auth_pub and not _is_plausible_pubkey(auth_pub):
                        log.warning(
                            f"[Auth] {peer_ip}: pub inválida no _auth "
                            f"(len={len(auth_pub)})"
                        )
                        self.metrics.inc("auth_failed")
                        return
                    peer_id = auth_pub or None
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
                    except Exception:
                        pass
                payload = NETWORK_MAGIC + json.dumps(response).encode()
                conn.sendall(payload)
                self.metrics.inc("messages_out")
                self.metrics.inc("bytes_out", len(payload))
        except Exception as e:
            log.warning(f"erro com {addr}: {e}")
        finally:
            with self._rate_lock:
                self._active_conns_by_ip[peer_ip] -= 1
            try:
                conn.close()
            except Exception:
                pass

    def _handle_sign_block(self, msg: dict, addr, peer_id) -> dict:
        """
        Valida o bloco antes de assinar. Aceita 3 cenários:
          a) block_h == local_h + 1 (normal)
          b) block_h == local_h     (RACE RECOVERY — bloco já é o tip)
          c) block_h < local_h      (RACE RECOVERY REVERSA)
        """
        block = msg.get("block")
        if not block or not isinstance(block, dict):
            return {"type": "sign_block_response", "pubkey": "", "signature": "",
                    "error": "block_invalido"}

        trust = getattr(self.bc, "trust_net", None)
        local_sk = getattr(self.bc, "_local_validator_sk", None)

        if trust is None or local_sk is None:
            return {"type": "sign_block_response", "pubkey": "", "signature": "",
                    "error": "sem_config trust=%s sk=%s" % (
                        "ok" if trust else "None", "ok" if local_sk else "None")}

        try:
            from wallet import Wallet
            from blockchain import block_hash as _bh, sign_block_as_validator

            if not isinstance(local_sk, Wallet):
                local_sk = Wallet(private_key_hex=str(local_sk))
            my_pub = local_sk.pub_hex

            validators = trust.get_top_validators(count=5)
            if my_pub not in validators:
                log.info(
                    "[SIG] peer %s: pubkey %s não está em get_top_validators() "
                    "— não assina",
                    addr[0], my_pub[:16],
                )
                return {"type": "sign_block_response", "pubkey": "", "signature": "",
                        "error": "nao_no_top5 my_pub=%s top5=%d" % (my_pub[:16], len(validators))}

            try:
                h_calc = _bh(
                    block["prev_hash"], block["merkle"], block["timestamp"],
                    block["nonce"], block["difficulty"],
                )
                if h_calc != block["hash"]:
                    self.metrics.inc("block_sigs_invalid")
                    log.warning(
                        "[SIG] rejeitando assinatura de %s: header hash diverge",
                        addr[0],
                    )
                    return {"type": "sign_block_response", "pubkey": "", "signature": "",
                            "error": "header_hash_diverge calc=%s recv=%s" % (
                                h_calc[:16], block["hash"][:16])}
            except KeyError as e:
                log.warning("[SIG] bloco malformado (campo ausente: %s)", e)
                return {"type": "sign_block_response", "pubkey": "", "signature": "",
                        "error": "bloco_malformado campo=%s" % e}

            try:
                if not meets_difficulty(block["hash"], int(block["difficulty"])):
                    self.metrics.inc("block_sigs_invalid")
                    log.warning(
                        "[SIG] rejeitando assinatura de %s: PoW insuficiente",
                        addr[0],
                    )
                    return {"type": "sign_block_response", "pubkey": "", "signature": "",
                            "error": "pow_insuf diff=%s" % block.get("difficulty")}
            except Exception as e:
                log.warning("[SIG] falha checando PoW: %s", e)
                return {"type": "sign_block_response", "pubkey": "", "signature": "",
                        "error": "excecao_pow %s" % e}

            try:
                local_h = int(self.bc.db.height())
                local_tip = self.bc.db.tip_hash()
                block_h = int(block.get("height", -1))

                if block_h == local_h + 1:
                    if block.get("prev_hash") != local_tip:
                        log.info(
                            "[SIG] rejeitando: prev_hash em fork "
                            "(local_tip=%s... block=%s...)",
                            (local_tip or "")[:16],
                            (block.get("prev_hash") or "")[:16],
                        )
                        return {"type": "sign_block_response", "pubkey": "", "signature": "",
                                "error": "prev_hash_fork local_tip=%s block_prev=%s" % (
                                    (local_tip or "")[:16],
                                    (block.get("prev_hash") or "")[:16])}
                    prev_block = self.bc.db.get_block(local_h)
                elif block_h == local_h:
                    if block.get("hash") != local_tip:
                        log.info(
                            "[SIG] altura %d == local mas hash %s != tip %s",
                            block_h,
                            (block.get("hash") or "")[:16],
                            (local_tip or "")[:16],
                        )
                        return {"type": "sign_block_response", "pubkey": "", "signature": "",
                                "error": "hash_fork local_tip=%s block_hash=%s" % (
                                    (local_tip or "")[:16],
                                    (block.get("hash") or "")[:16])}
                    prev_block = self.bc.db.get_block(block_h - 1) if block_h > 0 else None
                    log.info("[SIG] assinando bloco #%d em RACE RECOVERY (já é o tip local)", block_h)
                elif block_h < local_h:
                    try:
                        _existing = self.bc.db.get_block(block_h)
                    except Exception:
                        _existing = None
                    if not _existing:
                        log.info(
                            "[SIG] bloco #%d nao existe localmente "
                            "(peer em #%d) - recusando",
                            block_h, local_h,
                        )
                        return {"type": "sign_block_response", "pubkey": "", "signature": "",
                                "error": "altura_fora local_h=%d block_h=%d (sem bloco local)" % (local_h, block_h)}
                    if _existing.get("hash") != block.get("hash"):
                        log.info(
                            "[SIG] bloco #%d existe mas hash diverge "
                            "(peer em #%d) - recusando",
                            block_h, local_h,
                        )
                        return {"type": "sign_block_response", "pubkey": "", "signature": "",
                                "error": "altura_fora local_h=%d block_h=%d (hash divergente)" % (local_h, block_h)}
                    prev_block = self.bc.db.get_block(block_h - 1) if block_h > 0 else None
                    log.info(
                        "[SIG] assinando bloco #%d em RACE RECOVERY REVERSA "
                        "(peer em #%d, hash bate)",
                        block_h, local_h,
                    )
                else:
                    log.info(
                        "[SIG] altura %d fora da janela [%d, %d]",
                        block_h, local_h, local_h + 1,
                    )
                    return {"type": "sign_block_response", "pubkey": "", "signature": "",
                            "error": "altura_fora local_h=%d block_h=%d" % (local_h, block_h)}
            except Exception as e:
                log.warning("[SIG] falha checando altura/prev_hash: %s", e)
                return {"type": "sign_block_response", "pubkey": "", "signature": "",
                        "error": "excecao_altura %s" % e}

            try:
                ok_val, msg_val = self.bc.validate_block(
                    block, prev_block=prev_block, validators=[],
                )
                if not ok_val:
                    self.metrics.inc("block_sigs_invalid")
                    log.warning(
                        "[SIG] rejeitando assinatura de %s: validate_block: %s",
                        addr[0], msg_val,
                    )
                    return {"type": "sign_block_response", "pubkey": "", "signature": "",
                            "error": "validate_block %s" % msg_val[:80]}
            except Exception as e:
                log.warning("[SIG] validate_block exceção: %s", e)
                return {"type": "sign_block_response", "pubkey": "", "signature": "",
                        "error": "excecao_validate %s" % e}

            entry = sign_block_as_validator(block, local_sk)
            if not entry:
                return {"type": "sign_block_response", "pubkey": "", "signature": "",
                        "error": "sign_falhou (wallet retornou vazio)"}

            self.metrics.inc("block_sigs_provided")
            log.info(
                "[SIG] assinando bloco #%d para %s (validação completa OK)",
                int(block.get("height", -1)), addr[0],
            )
            return {
                "type": "sign_block_response",
                "pubkey": entry["pubkey"],
                "signature": entry["signature"],
            }
        except Exception as e:
            log.warning("[SIG] _handle_sign_block exceção: %s", e)
            return {"type": "sign_block_response", "pubkey": "", "signature": "",
                    "error": "excecao %s" % e}

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
                return {"type": "queue_ack", "ok": True, "queue_size": len(self.bc.fila_mineracao), "decentralized": self.bc.rede_descentralizada}

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
                    if ok and self.on_peer_good: self.on_peer_good(