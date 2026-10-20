"""discovery_v2.py — Descoberta de peers BRN na LAN
================================================================
Camadas de descoberta:
  1. UDP multicast em 239.255.42.99:50007
  2. mDNS (via zeroconf) — opcional
  3. PeerLiveness — remove peers mortos

Também exporta:
    TOKEN            — HMAC(NETWORK_SECRET, "brn-disc-v1")
                       (herdado pelo p2p_unified.py para o handshake)
    get_all_local_ips(), get_primary_ip()
    same_subnet(ip1, ip2)

Env:
    BRN_NETWORK_SECRET         (obrigatório)
    BRN_MC_TTL                 (default 2)
    BRN_ACCEPT_CROSS_SUBNET    (default 0)
    BRN_SUBNET_PREFIX          (default 24)
    BRN_EXTRA_PEERS            (CSV de "ip:porta" adicionais)
================================================================
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path


log = logging.getLogger("discovery")


# ============================================================
# CONFIG
# ============================================================
MULTICAST_GROUP = "239.255.42.99"
MULTICAST_PORT  = 50007
MDNS_SERVICE    = "_brn._udp.local."
PEERS_MANUAL_FILE = "peers_manual.json"

NETWORK_SECRET = os.environ.get("BRN_NETWORK_SECRET", "")
if not NETWORK_SECRET:
    raise RuntimeError(
        "BRN_NETWORK_SECRET é obrigatório. "
        "Defina no ambiente ou no brn_network.env antes de rodar o nó."
    )

TOKEN = hmac.new(
    NETWORK_SECRET.encode("utf-8"), b"brn-disc-v1", hashlib.sha256
).hexdigest()

MULTICAST_TTL       = int(os.environ.get("BRN_MC_TTL", "2"))
ACCEPT_CROSS_SUBNET = os.environ.get("BRN_ACCEPT_CROSS_SUBNET", "0") == "1"
SUBNET_PREFIX       = int(os.environ.get("BRN_SUBNET_PREFIX", "24"))
MSG_MAX_AGE         = 60


# ============================================================
# HELPERS DE REDE
# ============================================================
def get_all_local_ips() -> list:
    """Retorna lista de IPs IPv4 locais (excluindo loopback)."""
    ips: set = set()
    for probe in ("10.255.255.255", "192.168.255.255", "172.16.255.255"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.2)
            s.connect((probe, 1))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                        socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


def get_primary_ip() -> str:
    ips = get_all_local_ips()
    return ips[0] if ips else "127.0.0.1"


def same_subnet(ip1: str, ip2: str) -> bool:
    if ACCEPT_CROSS_SUBNET:
        return True
    try:
        p = SUBNET_PREFIX // 8
        r = SUBNET_PREFIX % 8
        a = ip1.split(".")
        b = ip2.split(".")
        if a[:p] != b[:p]:
            return False
        if r == 0:
            return True
        mask = (0xFF << (8 - r)) & 0xFF
        return (int(a[p]) & mask) == (int(b[p]) & mask)
    except Exception:
        return False


def _load_manual_peers() -> set:
    """Lê peers_manual.json + BRN_EXTRA_PEERS."""
    peers: set = set()
    p = Path(__file__).resolve().parent / PEERS_MANUAL_FILE
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(data, list):
                peers.update(str(x).strip() for x in data if str(x).strip())
            elif isinstance(data, dict):
                peers.update(
                    str(x).strip() for x in data.get("peers", [])
                    if str(x).strip()
                )
        except Exception as e:
            log.warning(f"peers_manual.json inválido: {e}")

    for x in os.environ.get("BRN_EXTRA_PEERS", "").split(","):
        x = x.strip()
        if x:
            peers.add(x)
    return peers


# ============================================================
# UDP MULTICAST DISCOVERY
# ============================================================
class UDPDiscovery(threading.Thread):
    """
    Descoberta de peers via UDP multicast.
    Uso:
        d = UDPDiscovery(tcp_port=6001, node_uuid="abc...", 
                         on_peer=callback, running_flag=lambda: True)
        d.start()
        ...
        d.stop()
    """

    def __init__(self, tcp_port: int, node_uuid: str,
                 on_peer, running_flag):
        super().__init__(daemon=True, name="UDP-Discovery")
        self.tcp_port = int(tcp_port)
        self.node_uuid = node_uuid
        self.on_peer = on_peer
        self.running_flag = running_flag

        self.sock: socket.socket | None = None
        self.local_ips = get_all_local_ips()
        self.primary_ip = get_primary_ip()

        self._seen: dict = {}
        self._seen_lock = threading.Lock()
        self._stop = threading.Event()

    # ---------- encoding ----------
    def _encode(self, kind: str) -> bytes:
        return json.dumps({
            "t": kind,
            "port": self.tcp_port,
            "uuid": self.node_uuid,
            "ts": int(time.time()),
            "tok": TOKEN,
        }).encode("utf-8")

    def _decode(self, data: bytes):
        try:
            m = json.loads(data)
        except (ValueError, TypeError):
            return None
        if not isinstance(m, dict):
            return None
        if m.get("tok") != TOKEN:
            return None
        if abs(time.time() - m.get("ts", 0)) > MSG_MAX_AGE:
            return None
        return m

    # ---------- socket ----------
    def _open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        s.bind(("", MULTICAST_PORT))

        # join em cada interface local
        joined = False
        for ip in self.local_ips:
            try:
                mreq = (socket.inet_aton(MULTICAST_GROUP)
                        + socket.inet_aton(ip))
                s.setsockopt(socket.IPPROTO_IP,
                             socket.IP_ADD_MEMBERSHIP, mreq)
                joined = True
            except OSError:
                continue

        # fallback: join em 0.0.0.0
        if not joined:
            try:
                mreq = (socket.inet_aton(MULTICAST_GROUP)
                        + socket.inet_aton("0.0.0.0"))
                s.setsockopt(socket.IPPROTO_IP,
                             socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError:
                pass

        try:
            s.setsockopt(socket.IPPROTO_IP,
                         socket.IP_MULTICAST_TTL, MULTICAST_TTL)
        except OSError:
            pass
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except OSError:
            pass
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass

        s.settimeout(2.0)
        self.sock = s

    # ---------- loops ----------
    def _recv_loop(self):
        while self.running_flag() and not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                continue

            remote_ip = addr[0]
            if not same_subnet(remote_ip, self.primary_ip):
                continue

            m = self._decode(data)
            if m is None or m.get("uuid") == self.node_uuid:
                continue

            try:
                r_port = int(m["port"])
            except (KeyError, ValueError, TypeError):
                continue

            self._register(f"{remote_ip}:{r_port}", remote_ip, r_port)

            # responde PING com PONG
            if m.get("t") == "PING":
                try:
                    self.sock.sendto(self._encode("PONG"), addr)
                except Exception:
                    pass

    def _send_loop(self):
        while self.running_flag() and not self._stop.is_set():
            ping = self._encode("PING")

            # multicast
            for target in [(MULTICAST_GROUP, MULTICAST_PORT),
                           ("255.255.255.255", MULTICAST_PORT)]:
                try:
                    self.sock.sendto(ping, target)
                except Exception:
                    pass

            # broadcast por interface
            for ip in self.local_ips:
                try:
                    bcast = ip.rsplit(".", 1)[0] + ".255"
                    self.sock.sendto(ping, (bcast, MULTICAST_PORT))
                except Exception:
                    pass

            # backoff adaptativo
            with self._seen_lock:
                n = len(self._seen)
            if n < 2:
                interval = 2.0
            elif n < 5:
                interval = 10.0
            else:
                interval = 30.0
            if self._stop.wait(interval):
                break

    # ---------- registro ----------
    def _register(self, peer: str, ip: str, port: int):
        with self._seen_lock:
            now = time.time()
            last = self._seen.get(peer, 0)
            fresh = (now - last) > 30
            self._seen[peer] = now

        if fresh:
            try:
                self.on_peer(ip, port)
            except Exception as e:
                log.debug(f"on_peer({peer}) falhou: {e}")

    # ---------- ciclo ----------
    def run(self):
        try:
            self._open()
        except Exception as e:
            log.error(f"[UDP] falha ao abrir socket: {e}")
            return
        log.info(f"[UDP] ativo | IPs={self.local_ips} | ttl={MULTICAST_TTL}")

        threading.Thread(target=self._send_loop, daemon=True,
                         name="UDP-Send").start()
        self._recv_loop()

    def stop(self):
        self._stop.set()
        s, self.sock = self.sock, None
        if s:
            try:
                s.close()
            except OSError:
                pass

    def listar_peers(self) -> list:
        with self._seen_lock:
            return list(self._seen.keys())


# ============================================================
# mDNS (opcional — requer zeroconf)
# ============================================================
class MDNSDiscovery(threading.Thread):
    """
    Descoberta via mDNS (Bonjour/Avahi).
    Se zeroconf não estiver instalado, apenas loga e sai.
    """

    def __init__(self, tcp_port: int, on_peer, running_flag,
                 node_uuid: str = ""):
        super().__init__(daemon=True, name="mDNS-Discovery")
        self.tcp_port = int(tcp_port)
        self.on_peer = on_peer
        self.running_flag = running_flag
        self.node_uuid = node_uuid
        self.zeroconf = None
        self.info = None
        self._stop = threading.Event()

    def run(self):
        try:
            from zeroconf import (
                ServiceInfo, Zeroconf, ServiceBrowser, ServiceListener,
            )
        except ImportError:
            log.info("[mDNS] zeroconf nao instalado — pulando")
            return

        from socket import inet_aton
        ip = get_primary_ip()

        class _Listener(ServiceListener):
            def __init__(self, on_peer, primary_ip):
                self.on_peer = on_peer
                self.primary_ip = primary_ip

            def _handle(self, zc, type_, name):
                info = zc.get_service_info(type_, name)
                if not info:
                    return
                for a in info.parsed_addresses():
                    if a.startswith("127."):
                        continue
                    if not same_subnet(a, self.primary_ip):
                        continue
                    try:
                        self.on_peer(a, info.port)
                    except Exception:
                        pass

            def add_service(self, zc, t, n):
                self._handle(zc, t, n)

            def update_service(self, zc, t, n):
                self._handle(zc, t, n)

            def remove_service(self, zc, t, n):
                pass

        try:
            self.zeroconf = Zeroconf()
            self.info = ServiceInfo(
                MDNS_SERVICE,
                f"BRN-{socket.gethostname()}.{MDNS_SERVICE}",
                addresses=[inet_aton(ip)],
                port=self.tcp_port,
                properties={"uuid": self.node_uuid.encode()},
                server=f"{socket.gethostname()}.local.",
            )
            self.zeroconf.register_service(self.info)
            ServiceBrowser(self.zeroconf, MDNS_SERVICE,
                           _Listener(self.on_peer, ip))
            log.info(f"[mDNS] registrado em {ip}:{self.tcp_port}")

            while self.running_flag() and not self._stop.wait(1):
                pass
        except Exception as e:
            log.warning(f"[mDNS] erro: {e}")
        finally:
            try:
                if self.zeroconf and self.info:
                    self.zeroconf.unregister_service(self.info)
                if self.zeroconf:
                    self.zeroconf.close()
            except Exception:
                pass

    def stop(self):
        self._stop.set()


# ============================================================
# PEER LIVENESS — ping periódico + remoção de mortos
# ============================================================
class PeerLiveness(threading.Thread):
    """
    Thread que periodicamente pinga cada peer conhecido.
    Após 3 falhas, remove o peer.
    """

    def __init__(self, get_peers, ping_peer, remove_peer, running_flag,
                 interval: float = 30.0, max_misses: int = 3):
        super().__init__(daemon=True, name="PeerLiveness")
        self.get_peers = get_peers
        self.ping_peer = ping_peer
        self.remove_peer = remove_peer
        self.running_flag = running_flag
        self.interval = interval
        self.max_misses = max_misses
        self._misses: dict = {}
        self._stop = threading.Event()

    def run(self):
        while self.running_flag() and not self._stop.wait(self.interval):
            try:
                peers = list(self.get_peers())
            except Exception:
                continue

            # remove entradas de peers que sumiram
            for k in [k for k in self._misses if k not in peers]:
                self._misses.pop(k, None)

            for p in peers:
                try:
                    ip, port = p.rsplit(":", 1)
                    ok = self.ping_peer(ip, int(port))
                except Exception:
                    ok = False

                if ok:
                    self._misses[p] = 0
                else:
                    self._misses[p] = self._misses.get(p, 0) + 1
                    if self._misses[p] >= self.max_misses:
                        try:
                            self.remove_peer(p)
                        except Exception:
                            pass
                        self._misses.pop(p, None)

    def stop(self):
        self._stop.set()


# ============================================================
# INFO / AUTO-TESTE
# ============================================================
def info() -> dict:
    return {
        "multicast_group":  MULTICAST_GROUP,
        "multicast_port":   MULTICAST_PORT,
        "mdns_service":     MDNS_SERVICE,
        "ttl":              MULTICAST_TTL,
        "accept_cross_subnet": ACCEPT_CROSS_SUBNET,
        "subnet_prefix":    SUBNET_PREFIX,
        "local_ips":        get_all_local_ips(),
        "primary_ip":       get_primary_ip(),
        "token_prefix":     TOKEN[:16] + "...",
        "manual_peers":     list(_load_manual_peers()),
    }


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Auto-teste: descobre peers por 15s e imprime
    print("discovery_v2 — status:")
    print(json.dumps(info(), indent=2, ensure_ascii=False))

    if len(sys.argv) > 1 and sys.argv[1] == "listen":
        print("\nEscutando UDP multicast por 15s...")
        found = []

        def on_peer(ip, port):
            found.append(f"{ip}:{port}")
            print(f"  [achou] {ip}:{port}")

        stop_flag = [False]

        def running():
            return not stop_flag[0]

        d = UDPDiscovery(
            tcp_port=int(os.environ.get("BRN_P2P_PORT", "6001")),
            node_uuid="selftest",
            on_peer=on_peer,
            running_flag=running,
        )
        d.start()

        try:
            time.sleep(15)
        except KeyboardInterrupt:
            pass
        finally:
            stop_flag[0] = True
            d.stop()

        print(f"\nResultado: {len(found)} peer(s)")
        for p in found:
            print(f"  - {p}")
    else:
        print("\nDica: rode com argumento 'listen' para escutar 15s")