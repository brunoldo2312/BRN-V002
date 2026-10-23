"""
tracker.py — Tracker HTTP para descoberta de peers BRN (v3)
============================================================
v3 (2026-10-20):
  [SECURITY] Rate limit proxy-aware — respeita X-Forwarded-For apenas
             quando o request vem de um proxy em BRN_TRUSTED_PROXIES
             (mesma lógica do security.py). Antes, request.remote_addr
             era sempre o IP do proxy → todos os usuários compartilhavam
             o mesmo bucket e o primeiro a fazer 30 req/min derrubava
             o tracker para todos.
  [FIX] _validar_endereco rejeita loopback/link-local/unspecified.
        Antes, "127.0.0.1:6001" era aceito e peers tentavam conectar
        em si mesmos.
  [FIX] Validação real de IPv4/IPv6 via ipaddress. O regex antigo
        aceitava "999.999.999.999:6001".
  [FIX] _rate_buckets nunca eram removidos — memory leak infinito.
        Cleanup agora limpa peers expirados E buckets ociosos.
  [FIX] /peers sem rate limit — JSON grande (5000 peers) sem limite
        de leitura era DoS trivial.
  [NEW] /health retorna peers_ativos e uptime_s.
v2:
  - Thread-safe: Lock em todas as operações sobre PEERS
  - Cleanup em background (não depende de /peers ser chamado)
  - Rate limit por IP em /anunciar
  - Validação de endereço (IP/host + porta)
  - CORS habilitado (para clientes web)
  - Logging estruturado
  - Endpoints úteis: /peers, /stats, /health
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

# ============================================================
# CONFIG
# ============================================================
# Carrega a porta do Tracker do arquivo local sem sobrescrever o ambiente do processo.
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brn_network.env")
try:
    with open(_env_path, "r", encoding="utf-8-sig") as _env_stream:
        for _raw_line in _env_stream:
            _line = _raw_line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _value = _line.split("=", 1)
            _candidate_port = _value.strip().strip('"').strip("'")
            if _key.strip() == "BRN_TRACKER_PORT" and _candidate_port and "BRN_TRACKER_PORT" not in os.environ:
                os.environ["BRN_TRACKER_PORT"] = _candidate_port
                break
except OSError:
    pass

PORT = int(os.environ.get("BRN_TRACKER_PORT", os.environ.get("PORT", "9000")))
TTL = int(os.environ.get("BRN_TRACKER_TTL", "600"))                # 10 min
CLEANUP_INTERVAL = int(os.environ.get("BRN_TRACKER_CLEANUP", "60"))  # 1 min
MAX_PEERS = int(os.environ.get("BRN_TRACKER_MAX_PEERS", "5000"))
RATE_LIMIT = int(os.environ.get("BRN_TRACKER_RATE", "30"))         # req/min por IP
RATE_WINDOW = 60
RATE_BUCKET_IDLE = 600    # [FIX v3] limpa buckets sem uso após 10 min

# [FIX v3] Por padrão aceita IPs privados (LAN use case) mas rejeita
# loopback/link-local/unspecified. Para tracker público, defina =0.
ALLOW_PRIVATE = os.environ.get("BRN_TRACKER_ALLOW_PRIVATE", "1") == "1"

# Regex para validar formato "host:port"
ADDR_RE = re.compile(
    r"^("
    r"(?:\d{1,3}\.){3}\d{1,3}"                    # IPv4 (raw)
    r"|"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9\-\.]*[a-zA-Z0-9])?"  # hostname
    r"|"
    r"\[[0-9a-fA-F:]+\]"                           # IPv6 em brackets
    r")"
    r":(\d{1,5})$"                                  # :porta
)


# ============================================================
# ESTADO
# ============================================================
app = Flask(__name__)
CORS(app)

PEERS = {}                  # addr -> {"last_seen": ts, "first_seen": ts, "hits": n}
PEERS_LOCK = threading.Lock()

_rate_buckets = defaultdict(list)   # ip -> [timestamps]
_rate_lock = threading.Lock()

_START_TS = time.time()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tracker")


# ============================================================
# [SECURITY v3] TRUSTED PROXIES
# ============================================================
def _parse_trusted_proxies(raw: str) -> tuple[set, list]:
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
    """
    [FIX v3] Retorna o IP real do cliente, respeitando proxies confiáveis.

    Só consulta X-Forwarded-For / X-Real-IP se o request.remote_addr
    estiver em BRN_TRUSTED_PROXIES. Sem isso, um atacante pode forjar
    X-Forwarded-For e bypassar o rate limit.
    """
    try:
        direct = request.remote_addr or "unknown"
    except RuntimeError:
        return "unknown"

    if _TRUSTED_IPS or _TRUSTED_NETS:
        if _is_trusted_proxy(direct):
            # X-Forwarded-For: "client, proxy1, proxy2" → primeiro é o original
            xff = request.headers.get("X-Forwarded-For", "")
            if xff:
                candidate = xff.split(",")[0].strip()
                try:
                    ipaddress.ip_address(candidate)
                    return candidate
                except ValueError:
                    pass
            # Fallback: X-Real-IP (nginx)
            real_ip = request.headers.get("X-Real-IP", "").strip()
            if real_ip:
                try:
                    ipaddress.ip_address(real_ip)
                    return real_ip
                except ValueError:
                    pass
    return direct


# ============================================================
# HELPERS
# ============================================================
def _validar_endereco(addr: str) -> bool:
    """
    Valida 'host:port'.

    [FIX v3] Além do regex, valida IPv4/IPv6 via ipaddress.
    Rejeita loopback/link-local/unspecified (poluição do tracker).
    Aceita IPs privados por default (LAN); use BRN_TRACKER_ALLOW_PRIVATE=0
    para tracker público.
    """
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

    # Remove brackets de IPv6 em brackets: "[::1]" → "::1"
    host_clean = host.strip("[]")

    # Rejeita hostnames perigosos
    if host_clean.lower() in ("localhost", "0.0.0.0", ""):
        return False

    # Tenta interpretar como IP
    try:
        ip = ipaddress.ip_address(host_clean)
        # Sempre rejeita esses (não fazem sentido como peer)
        if ip.is_loopback:
            return False
        if ip.is_link_local:
            return False
        if ip.is_unspecified:
            return False
        if ip.is_reserved:
            return False
        # Privado: opcional
        if ip.is_private and not ALLOW_PRIVATE:
            return False
        return True
    except ValueError:
        # Não é IP — deve ser hostname válido (regex já validou formato)
        return True


def _rate_ok(ip: str) -> bool:
    """[FIX v3] Rate limit com limpeza preguiçosa do bucket."""
    agora = time.time()
    with _rate_lock:
        bucket = _rate_buckets[ip]
        cutoff = agora - RATE_WINDOW
        # Remove entradas antigas da janela
        bucket[:] = [t for t in bucket if t >= cutoff]

        if len(bucket) >= RATE_LIMIT:
            return False

        bucket.append(agora)
        return True


def _cleanup_loop():
    """
    [FIX v3] Limpa peers expirados E buckets de rate limit ociosos.
    Antes, _rate_buckets crescia infinitamente (memory leak).
    """
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            agora = time.time()

            # 1) Peers expirados
            with PEERS_LOCK:
                expirados = [
                    k for k, v in PEERS.items()
                    if agora - v["last_seen"] > TTL
                ]
                for k in expirados:
                    del PEERS[k]
                total_peers = len(PEERS)

            # 2) [FIX v3] Buckets de rate limit sem uso
            with _rate_lock:
                buckets_removidos = 0
                for ip in list(_rate_buckets.keys()):
                    b = _rate_buckets[ip]
                    # Limpa entradas antigas primeiro
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


# ============================================================
# ROTAS
# ============================================================
@app.route("/anunciar", methods=["POST"])
def anunciar():
    ip = _get_client_ip()   # [FIX v3]
    if not _rate_ok(ip):
        return jsonify({"ok": False, "msg": "rate limit"}), 429

    data = request.get_json(force=True, silent=True) or {}
    addr = (data.get("address") or "").strip()

    if not _validar_endereco(addr):   # [FIX v3] mais rigoroso
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
    # [FIX v3] Rate limit também em /peers (JSON grande era DoS)
    ip = _get_client_ip()
    if not _rate_ok(ip):
        return jsonify({"ok": False, "msg": "rate limit"}), 429

    agora = time.time()
    with PEERS_LOCK:
        ativos = {
            k: v for k, v in PEERS.items()
            if agora - v["last_seen"] <= TTL
        }

    # Ordena por last_seen desc (mais fresco primeiro) — útil para clientes
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
    # [FIX v3] Rate limit também em /stats
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
    """[NEW v3] Health check mais útil para load balancer / Kubernetes."""
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


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    t = threading.Thread(target=_cleanup_loop, daemon=True, name="Cleanup")
    t.start()

    log.info(f"🚀 BRN Tracker v3 — http://0.0.0.0:{PORT}")
    log.info(
        f"   TTL={TTL}s | Cleanup={CLEANUP_INTERVAL}s | "
        f"Max peers={MAX_PEERS} | Rate={RATE_LIMIT}/min"
    )
    log.info(
        f"   Trusted proxies: "
        f"{sorted(_TRUSTED_IPS) or '(nenhum)'} "
        f"{[str(n) for n in _TRUSTED_NETS] or ''}"
    )
    log.info(f"   Allow private IPs: {ALLOW_PRIVATE}")

    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
