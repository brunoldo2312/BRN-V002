"""
tracker.py — Tracker HTTP para descoberta de peers BRN (v3)
================================================================
v3 (2026-10-20):
  [SECURITY] Rate limit proxy-aware (X-Forwarded-For).
  [FIX] _validar_endereco rejeita loopback/link-local/unspecified.
  [FIX] Validação real de IPv4/IPv6 via ipaddress.
  [FIX] _rate_buckets limpos no cleanup (memory leak).
  [FIX] /peers com rate limit.
  [NEW] /health retorna peers_ativos e uptime_s.
================================================================
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
import ipaddress
import os
import re
import time
import threading
import logging
from collections import defaultdict

PORT = int(os.environ.get("PORT", "9000"))
TTL = int(os.environ.get("BRN_TRACKER_TTL", "600"))
CLEANUP_INTERVAL = int(os.environ.get("BRN_TRACKER_CLEANUP", "60"))
MAX_PEERS = int(os.environ.get("BRN_TRACKER_MAX_PEERS", "5000"))
RATE_LIMIT = int(os.environ.get("BRN_TRACKER_RATE", "30"))
RATE_WINDOW = 60
RATE_BUCKET_IDLE = 600

ALLOW_PRIVATE = os.environ.get("BRN_TRACKER_ALLOW_PRIVATE", "1") == "1"

ADDR_RE = re.compile(
    r"^("
    r"(?:\d{1,3}\.){3}\d{1,3}"
    r"|"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9\-\.]*[a-zA-Z0-9])?"
    r"|"
    r"\[[0-9a-fA-F:]+\]"
    r")"
    r":(\d{1,5})$"
)

app = Flask(__name__)
CORS(app)

PEERS = {}
PEERS_LOCK = threading.Lock()

_rate_buckets = defaultdict(list)
_rate_lock = threading.Lock()

_START_TS = time.time()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tracker")


def _parse_trusted_proxies(raw: str) -> tuple:
    ips: set = set()
    nets: list = []
    if not raw:
        return ips, nets
    for item in raw.split(","):
        s = item.strip()
        if not s:
            continue
        if "/" in s:
            try:
                nets.append(ipaddress.ip_network(s, strict=False))
            except ValueError:
                log.warning("BRN_TRUSTED_PROXIES: CIDR inválido '%s'", s)
        else:
            try:
                ipaddress.ip_address(s)
                ips.add(s)
            except ValueError:
                log.warning("BRN_TRUSTED_PROXIES: IP inválido '%s'", s)
    return ips, nets


_TRUSTED_IPS, _TRUSTED_NETS = _parse_trusted_proxies(
    os.environ.get("BRN_TRUSTED_PROXIES", "")
)


def _is_trusted_proxy(ip: str) -> bool:
    if not ip:
        return False
    if ip in _TRUSTED_IPS:
        return True
    if _TRUSTED_NETS:
        try:
            addr = ipaddress.ip_address(ip)
            for net in _TRUSTED_NETS:
                if addr in net:
                    return True
        except ValueError:
            return False
    return False


def _get_client_ip() -> str:
    try:
        direct = request.remote_addr or "unknown"
    except RuntimeError:
        return "unknown"
    if _TRUSTED_IPS or _TRUSTED_NETS:
        if _is_trusted_proxy(direct):
            xff = request.headers.get("X-Forwarded-For", "")
            if xff:
                candidate = xff.split(",")[0].strip()
                try:
                    ipaddress.ip_address(candidate)
                    return candidate
                except ValueError:
                    pass
            real_ip = request.headers.get("X-Real-IP", "").strip()
            if real_ip:
                try:
                    ipaddress.ip_address(real_ip)
                    return real_ip
                except ValueError:
                    pass
    return direct


def _validar_endereco(addr: str) -> bool:
    if not addr or len(addr) > 255:
        return False
    m = ADDR_RE.match(addr)
    if not m:
        return False
    host = m.group(1)
    try:
        porta = int(m.group(2))
        if porta < 1 or porta > 65535:
            return False
    except (ValueError, IndexError):
        return False
    host_clean = host.strip("[]")
    if host_clean.lower() in ("localhost", "0.0.0.0", ""):
        return False
    try:
        ip = ipaddress.ip_address(host_clean)
        if ip.is_loopback:
            return False
        if ip.is_link_local:
            return False
        if ip.is_unspecified:
            return False
        if ip.is_reserved:
            return False
        if ip.is_private and not ALLOW_PRIVATE:
            return False
        return True
    except ValueError:
        return True


def _rate_ok(ip: str) -> bool:
    agora = time.time()
    with _rate_lock:
        bucket = _rate_buckets[ip]
        cutoff = agora - RATE_WINDOW
        bucket[:] = [t for t in bucket if t >= cutoff]
        if len(bucket) >= RATE_LIMIT:
            return False
        bucket.append(agora)
        return True


def _cleanup_loop():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            agora = time.time()
            with PEERS_LOCK:
                expirados = [
                    k for k, v in PEERS.items()
                    if agora - v["last_seen"] > TTL
                ]
                for k in expirados:
                    del PEERS[k]
                total_peers = len(PEERS)
            with _rate_lock:
                buckets_removidos = 0
                for ip in list(_rate_buckets.keys()):
                    b = _rate_buckets[ip]
                    b[:] = [t for t in b if agora - t < RATE_WINDOW]
                    if not b:
                        del _rate_buckets[ip]
                        buckets_removidos += 1
                    elif agora - b[-1] > RATE_BUCKET_IDLE:
                        del _rate_buckets[ip]
                        buckets_removidos += 1
                total_buckets = len(_rate_buckets)
            if expirados or buckets_removidos:
                log.info(
                    "cleanup: peers=-%d (restam %d) | buckets=-%d (restam %d)",
                    len(expirados), total_peers,
                    buckets_removidos, total_buckets,
                )
        except Exception as e:
            log.error(f"cleanup erro: {e}")


@app.route("/anunciar", methods=["POST"])
def anunciar():
    ip = _get_client_ip()
    if not _rate_ok(ip):
        return jsonify({"ok": False, "msg": "rate limit"}), 429
    data = request.get_json(force=True, silent=True) or {}
    addr = (data.get("address") or "").strip()
    if not _validar_endereco(addr):
        return jsonify({
            "ok": False,
            "msg": "address invalido (formato host:port, sem loopback/link-local)",
        }), 400
    agora = time.time()
    with PEERS_LOCK:
        if len(PEERS) >= MAX_PEERS and addr not in PEERS:
            return jsonify({"ok": False, "msg": "tracker cheio"}), 503
        if addr in PEERS:
            PEERS[addr]["last_seen"] = agora
            PEERS[addr]["hits"] = PEERS[addr].get("hits", 0) + 1
        else:
            PEERS[addr] = {
                "last_seen": agora,
                "first_seen": agora,
                "hits": 1,
            }
        total = len(PEERS)
    log.info(f"anunciar: {addr} (de {ip}) — total={total}")
    return jsonify({"ok": True, "total": total})


@app.route("/peers", methods=["GET"])
def peers():
    ip = _get_client_ip()
    if not _rate_ok(ip):
        return jsonify({"ok": False, "msg": "rate limit"}), 429
    agora = time.time()
    with PEERS_LOCK:
        ativos = {
            k: v for k, v in PEERS.items()
            if agora - v["last_seen"] <= TTL
        }
    peers_sorted = sorted(
        ativos.items(), key=lambda kv: kv[1]["last_seen"], reverse=True
    )
    return jsonify({
        "peers": [k for k, _ in peers_sorted],
        "count": len(peers_sorted),
        "ttl": TTL,
    })


@app.route("/stats", methods=["GET"])
def stats():
    ip = _get_client_ip()
    if not _rate_ok(ip):
        return jsonify({"ok": False, "msg": "rate limit"}), 429
    agora = time.time()
    with PEERS_LOCK:
        total = len(PEERS)
        ativos = sum(
            1 for v in PEERS.values()
            if agora - v["last_seen"] <= TTL
        )
    with _rate_lock:
        n_buckets = len(_rate_buckets)
    return jsonify({
        "total_registrados": total,
        "ativos": ativos,
        "ttl": TTL,
        "max_peers": MAX_PEERS,
        "rate_buckets": n_buckets,
        "trusted_proxies_ips": sorted(_TRUSTED_IPS),
        "trusted_proxies_cidr": [str(n) for n in _TRUSTED_NETS],
        "allow_private": ALLOW_PRIVATE,
        "uptime_s": int(agora - _START_TS),
    })


@app.route("/health", methods=["GET"])
def health():
    agora = time.time()
    with PEERS_LOCK:
        ativos = sum(
            1 for v in PEERS.values()
            if agora - v["last_seen"] <= TTL
        )
    return jsonify({
        "ok": True,
        "ts": agora,
        "peers_ativos": ativos,
        "uptime_s": int(agora - _START_TS),
    })


@app.route("/", methods=["GET"])
def raiz():
    agora = time.time()
    with PEERS_LOCK:
        ativos = sum(
            1 for v in PEERS.values()
            if agora - v["last_seen"] <= TTL
        )
    return (
        f"BRN Tracker — {ativos} peers ativos "
        f"(TTL={TTL}s)\n"
        f"Endpoints:\n"
        f"  POST /anunciar   {{\"address\": \"ip:porta\"}}\n"
        f"  GET  /peers      lista de peers\n"
        f"  GET  /stats      estatisticas\n"
        f"  GET  /health     healthcheck\n"
    ), 200, {"Content-Type": "text/plain; charset=utf-8"}


if __name__ == "__main__":
    t = threading.Thread(target=_cleanup_loop, daemon=True, name="Cleanup")
    t.start()
    log.info(f"BRN Tracker v3 — http://0.0.0.0:{PORT}")
    log.info(
        f"   TTL={TTL}s | Cleanup={CLEANUP_INTERVAL}s | "
        f"Max peers={MAX_PEERS} | Rate={RATE_LIMIT}/min"
    )
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)