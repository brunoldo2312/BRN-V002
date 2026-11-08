"""
security.py — Camada de segurança HTTP do BRN (v6.2)
================================================================
v6.2 (2026-11-08):
  [NEW] Adicionado bypass total via BRN_DISABLE_RATE_LIMIT=1.
        Ideal para redes locais/Tailscale onde o 429 atrapalha o Pool.
================================================================
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import ipaddress
import json
import logging
import os
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Optional

from flask import request, jsonify

log = logging.getLogger("security")

# ============================================================
# CONFIG
# ============================================================
LOG_FILE          = os.environ.get("BRN_AUDIT_LOG", "audit.log")
AUDIT_ENABLED     = os.environ.get("BRN_AUDIT_ENABLED", "1") == "1"
try:
    AUDIT_MAX_BYTES = int(os.environ.get("BRN_AUDIT_MAX_BYTES",
                                         str(50 * 1024 * 1024)))
except (ValueError, TypeError):
    AUDIT_MAX_BYTES = 50 * 1024 * 1024
try:
    AUDIT_BACKUPS = int(os.environ.get("BRN_AUDIT_BACKUPS", "3"))
    if AUDIT_BACKUPS < 0:
        AUDIT_BACKUPS = 0
except (ValueError, TypeError):
    AUDIT_BACKUPS = 3

_TRUSTED_RAW  = os.environ.get("BRN_TRUSTED_PROXIES", "") or ""
_RATE_BACKEND = (os.environ.get("BRN_RATE_LIMIT_BACKEND", "auto") or "auto").strip().lower()

CLEANUP_INTERVAL_S = 300
BUCKET_MAX_IDLE_S  = 3600
ADMIN_TOKEN_MIN_LEN = 16

# ============================================================
# RATE LIMITS
# ============================================================
DEFAULT_RATE_LIMITS = {
    "mine":             (30,   60),
    "transfer":         (30,   60),
    "nova_carteira":    (10,   60),
    "portfolio":        (300,  60),
    "tx_status":        (300,  60),
    "minhas_txs":       (120,  60),
    "verificar":        (120,  60),
    "faucet":           (5,    60),
    "miner_start":      (10,   60),
    "miner_stop":       (30,   60),
    "miner_status":     (600,  60),
    "status":           (600,  60),
    "sync_info":        (600,  60),
    "list_blocks":      (600,  60),
    "get_block":        (600,  60),
    "fee_estimate":     (300,  60),
    "work":             (300,  60),
    "peers_discovered": (600,  60),
    "bootstrap_add":    (300,  60),
    "hd_create":        (5,    60),
    "hd_derive":        (10,   60),
    "contract_deploy":  (10,   60),
    "contract_call":    (30,   60),
    "l2_create":        (10,   60),
    "l2_cancel":        (10,   60),
    "l2_claim":         (10,   60),
    "l2_quote":         (60,   60),
    "l2_list":          (120,  60),
    "l2_order":         (60,   60),
    "l2_stats":         (120,  60),
    "pool_register":    (30,   60),
    "pool_work":        (300,  60),
    "pool_submit":      (1200, 60),
    "pool_heartbeat":   (600,  60),
    "pool_disconnect":  (30,   60),
    "pool_stats":       (1200, 60),
    "pool_miners":      (600,  60),
    "pool_local_status":(600,  60),
    "trust_stats":      (300,  60),
    "trust_nodes":      (300,  60),
    "experiment":       (60,   60),
}

_node_id_hash = "unknown"

def set_node_id_hash(node_id: str) -> None:
    global _node_id_hash
    if not node_id: return
    s = str(node_id).strip()
    if not s: return
    _node_id_hash = hashlib.sha256(s.encode("utf-8")).hexdigest()[:16] if len(s) > 32 else s

def _parse_trusted_proxies(raw: str) -> tuple[set, list]:
    ips: set = set()
    nets: list = []
    if not raw: return ips, nets
    for item in raw.split(","):
        s = item.strip()
        if not s: continue
        if "/" in s:
            try: nets.append(ipaddress.ip_network(s, strict=False))
            except ValueError: pass
        else:
            try:
                ipaddress.ip_address(s)
                ips.add(s)
            except ValueError: pass
    return ips, nets

_TRUSTED_IPS, _TRUSTED_NETS = _parse_trusted_proxies(_TRUSTED_RAW)

def _is_trusted_proxy(ip: str) -> bool:
    if not ip: return False
    if ip in _TRUSTED_IPS: return True
    if _TRUSTED_NETS:
        try:
            addr = ipaddress.ip_address(ip)
            for net in _TRUSTED_NETS:
                if addr in net: return True
        except ValueError: return False
    return False

def get_client_ip() -> str:
    try: direct = request.remote_addr or "unknown"
    except RuntimeError: return "unknown"
    if _TRUSTED_IPS or _TRUSTED_NETS:
        if _is_trusted_proxy(direct):
            xff = request.headers.get("X-Forwarded-For", "")
            if xff:
                candidate = xff.split(",")[0].strip()
                try:
                    ipaddress.ip_address(candidate)
                    return candidate
                except ValueError: pass
            real_ip = request.headers.get("X-Real-IP", "").strip()
            if real_ip:
                try:
                    ipaddress.ip_address(real_ip)
                    return real_ip
                except ValueError: pass
    return direct

class RateLimiter:
    backend = "memory"
    def __init__(self, cleanup_every: int = CLEANUP_INTERVAL_S):
        self._buckets: dict = defaultdict(deque)
        self._lock = threading.RLock()
        self._last_cleanup = time.time()
        self._cleanup_every = cleanup_every
        self._hits = 0
    def check(self, key: str, ip: str, limit: int, window: int) -> tuple[bool, int, int]:
        now = time.time()
        bucket_key = (key, ip)
        with self._lock:
            bucket = self._buckets[bucket_key]
            cutoff = now - window
            while bucket and bucket[0] < cutoff: bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + window - now))
                self._maybe_cleanup(now)
                return False, retry_after, 0
            bucket.append(now)
            self._hits += 1
            remaining = max(0, limit - len(bucket))
            self._maybe_cleanup(now)
            return True, 0, remaining
    def _maybe_cleanup(self, now: float) -> None:
        if now - self._last_cleanup < self._cleanup_every: return
        self.cleanup(now)
    def cleanup(self, now: Optional[float] = None) -> int:
        now = now or time.time()
        removidos = 0
        with self._lock:
            for k in list(self._buckets.keys()):
                b = self._buckets[k]
                if not b or (now - b[-1]) > BUCKET_MAX_IDLE_S:
                    del self._buckets[k]
                    removidos += 1
            self._last_cleanup = now
        return removidos
    def reset(self, key: Optional[str] = None, ip: Optional[str] = None) -> int:
        removidos = 0
        with self._lock:
            for k in list(self._buckets.keys()):
                k_key, k_ip = k
                if key and k_key != key: continue
                if ip and k_ip != ip: continue
                del self._buckets[k]
                removidos += 1
        return removidos
    def reset_all(self) -> int:
        with self._lock:
            n = len(self._buckets)
            self._buckets.clear()
            return n
    def stats(self) -> dict:
        with self._lock:
            n_buckets = len(self._buckets)
            n_entries = sum(len(b) for b in self._buckets.values())
        return {"backend": "memory", "buckets": n_buckets, "entries": n_entries, "hits_total": self._hits, "last_cleanup_ago_s": int(time.time() - self._last_cleanup)}

class RateLimiterMongo:
    backend = "mongo"
    def __init__(self):
        self._fallback = RateLimiter()
        self._indexes_ok = False
        self._hits_mongo = 0
        self._hits_fallback = 0
        self._lock = threading.RLock()
    def _get_collection(self):
        try:
            from mongo_client import mongo
            if not mongo.is_configured(): return None, False
            db = mongo.db()
            if db is None: return None, False
            coll = db.rate_limits
            if not self._indexes_ok:
                try:
                    coll.create_index("expires_at", expireAfterSeconds=0, name="ttl_expires_at", background=True)
                    self._indexes_ok = True
                except Exception: pass
            return coll, True
        except Exception: return None, False
    def check(self, key: str, ip: str, limit: int, window: int) -> tuple[bool, int, int]:
        coll, ok = self._get_collection()
        if not ok or coll is None:
            with self._lock: self._hits_fallback += 1
            return self._fallback.check(key, ip, limit, window)
        now = time.time()
        bucket = int(now // window)
        doc_id = f"{key}:{ip}:{bucket}"
        expires_at = (bucket + 2) * window
        try:
            result = coll.find_one_and_update({"_id": doc_id}, {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": expires_at, "created_at": now, "key": key, "ip": ip}}, upsert=True, return_document=True)
            count = int((result or {}).get("count", 1))
            if count > limit:
                retry_after = max(1, int((bucket + 1) * window - now))
                return False, retry_after, 0
            remaining = max(0, limit - count)
            with self._lock: self._hits_mongo += 1
            return True, 0, remaining
        except Exception:
            with self._lock: self._hits_fallback += 1
            return self._fallback.check(key, ip, limit, window)
    def cleanup(self, now: Optional[float] = None) -> int: return 0
    def reset(self, key: Optional[str] = None, ip: Optional[str] = None) -> int:
        coll, ok = self._get_collection()
        if not ok or coll is None: return self._fallback.reset(key, ip)
        try:
            q: dict = {}
            if key: q["key"] = key
            if ip: q["ip"] = ip
            return coll.delete_many(q).deleted_count
        except Exception: return 0
    def reset_all(self) -> int:
        coll, ok = self._get_collection()
        if not ok or coll is None: return self._fallback.reset_all()
        try: return coll.delete_many({}).deleted_count
        except Exception: return 0
    def stats(self) -> dict:
        coll, ok = self._get_collection()
        with self._lock:
            h_mongo = self._hits_mongo
            h_fallback = self._hits_fallback
        n_docs = -1
        if ok and coll is not None:
            try: n_docs = coll.estimated_document_count()
            except Exception: pass
        return {"backend": "mongo" if ok else "memory(fallback)", "docs_in_collection": n_docs, "hits_mongo": h_mongo, "hits_fallback": h_fallback, "fallback_stats": self._fallback.stats()}

_limiter: Optional[Any] = None
_limiter_lock = threading.Lock()

def get_rate_limiter():
    global _limiter
    if _limiter is not None: return _limiter
    with _limiter_lock:
        if _limiter is not None: return _limiter
        backend = _RATE_BACKEND
        if backend == "auto":
            try:
                from mongo_client import mongo
                backend = "mongo" if mongo.is_configured() else "memory"
            except Exception: backend = "memory"
        if backend == "mongo":
            try:
                _limiter = RateLimiterMongo()
                log.info("rate limiter: MongoDB (distribuído)")
            except Exception:
                _limiter = RateLimiter()
        else:
            _limiter = RateLimiter()
            log.info("rate limiter: memória (single-node)")
        return _limiter

_limiter = get_rate_limiter()

_cleanup_started = [False]
_cleanup_pid = [0]
_cleanup_lock = threading.Lock()

def _cleanup_loop() -> None:
    while True:
        time.sleep(CLEANUP_INTERVAL_S)
        try:
            n = _limiter.cleanup()
            if n: log.debug("rate-limiter cleanup: %d buckets removidos", n)
        except Exception: pass

def _ensure_cleanup_started() -> None:
    with _cleanup_lock:
        current_pid = os.getpid()
        if _cleanup_started[0] and _cleanup_pid[0] == current_pid: return
        _cleanup_started[0] = True
        _cleanup_pid[0] = current_pid
    threading.Thread(target=_cleanup_loop, daemon=True, name="SecurityCleanup").start()

_ensure_cleanup_started()

def require_rate_limit(endpoint_key: str):
    # [NOVO] Bypass total se a variável de ambiente estiver setada
    if os.environ.get("BRN_DISABLE_RATE_LIMIT", "0") == "1":
        def decorator(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                return fn(*args, **kwargs)
            return wrapper
        return decorator

    limit, window = DEFAULT_RATE_LIMITS.get(endpoint_key, (120, 60))
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if request.method == "OPTIONS": return ("", 200)
            ip = get_client_ip()
            ok, retry, remaining = _limiter.check(endpoint_key, ip, limit, window)
            backend = getattr(_limiter, "backend", "memory")
            if not ok:
                audit_log("rate_limit_hit", {"endpoint": endpoint_key, "ip": ip, "limit": limit, "window": window, "backend": backend})
                resp = jsonify({"ok": False, "msg": f"Muitas requisições. Tente em {retry}s."})
                resp.status_code = 429
                resp.headers["Retry-After"] = str(retry)
                return resp
            resp = fn(*args, **kwargs)
            return resp
        return wrapper
    return decorator

_audit_lock = threading.RLock()
_audit_fp = None

def _open_audit():
    global _audit_fp
    if not AUDIT_ENABLED: return None
    if _audit_fp is not None: return _audit_fp
    try:
        p = Path(LOG_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        _audit_fp = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        return _audit_fp
    except Exception: return None

def _rotate_audit_if_needed() -> None:
    pass

_SENSITIVE_KEYS = frozenset({"private_key", "priv", "sk", "secret", "password", "senha", "mnemonic", "seed", "x-brn-sk", "authorization", "token", "api_key", "brn_admin_token", "x-brn-token", "bearer", "session_key"})
_SENSITIVE_SUBSTRINGS = ("password", "passwd", "secret", "private", "mnemonic", "api_key", "apikey", "admin_token", "bearer_token")

def _is_sensitive_key(k: str) -> bool:
    lk = str(k).lower()
    if lk in _SENSITIVE_KEYS: return True
    for frag in _SENSITIVE_SUBSTRINGS:
        if frag in lk: return True
    return False

def _sanitize(value: Any, depth: int = 0) -> Any:
    if depth > 6: return "<max_depth>"
    if isinstance(value, str): return value[:32] + f"...({len(value)} chars)" if len(value) > 80 else value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _is_sensitive_key(k) and isinstance(v, str) and v: out[k] = "<redacted>"
            else: out[k] = _sanitize(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)): return [_sanitize(v, depth + 1) for v in value[:50]]
    return value

def hash_short(value: str, length: int = 12) -> str:
    if not value: return ""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]

def audit_log(event: str, data: Optional[dict] = None) -> None:
    if not AUDIT_ENABLED: return
    try:
        ip = get_client_ip()
        ua = (request.headers.get("User-Agent", "")[:120] if request else "")
    except Exception: ip, ua = "unknown", ""
    payload = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, "ip": ip, "ua": ua, "pid": os.getpid(), "node_id_hash": _node_id_hash}
    if data: payload.update(_sanitize(data))
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with _audit_lock:
        fp = _open_audit()
        if fp:
            try:
                fp.write(line + "\n")
                fp.flush()
            except Exception: pass

def close_audit() -> None:
    global _audit_fp
    with _audit_lock:
        if _audit_fp:
            try: _audit_fp.close()
            except Exception: pass
            _audit_fp = None

_atexit_registered = [False]
def _register_atexit() -> None:
    if _atexit_registered[0]: return
    _atexit_registered[0] = True
    try: atexit.register(close_audit)
    except Exception: pass

_register_atexit()

ADMIN_TOKEN = os.environ.get("BRN_ADMIN_TOKEN", "").strip()
if ADMIN_TOKEN and len(ADMIN_TOKEN) < ADMIN_TOKEN_MIN_LEN:
    ADMIN_TOKEN = ""

CERT_FILE = os.environ.get("BRN_TLS_CERT", "brn_cert.pem")
KEY_FILE  = os.environ.get("BRN_TLS_KEY",  "brn_key.pem")

def ensure_self_signed_cert(cert_file: str = CERT_FILE, key_file: str = KEY_FILE, hostname: str = "127.0.0.1", days: int = 3650) -> tuple[str, str]:
    if os.path.exists(cert_file) and os.path.exists(key_file): return cert_file, key_file
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime as _dt
    except ImportError: raise RuntimeError("Instale: pip install cryptography")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "BR"), x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BRN Local Node"), x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    try: san = [x509.IPAddress(ipaddress.ip_address(hostname))]
    except ValueError: san = [x509.DNSName(hostname)]
    san.append(x509.DNSName("localhost"))
    san.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    now = _dt.datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + _dt.timedelta(days=days)).add_extension(x509.SubjectAlternativeName(san), critical=False).add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True).sign(key, hashes.SHA256()))
    with open(key_file, "wb") as f: f.write(key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.TraditionalOpenSSL, encryption_algorithm=serialization.NoEncryption()))
    try: os.chmod(key_file, 0o600)
    except Exception: pass
    with open(cert_file, "wb") as f: f.write(cert.public_bytes(serialization.Encoding.PEM))
    return cert_file, key_file

def https_enabled() -> bool: return os.environ.get("BRN_HTTPS", "0") == "1"

def get_ssl_context() -> Optional[tuple[str, str]]:
    if not https_enabled(): return None
    try: return ensure_self_signed_cert()
    except Exception as e: return None
