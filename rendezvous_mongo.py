"""rendezvous_mongo.py — Descoberta de peers via MongoDB (v1)
================================================================
Substitui o rendezvous HTTP e o tracker.py. Cada nó:
  1. Anuncia seu peer na coleção `rendezvous` a cada N segundos
  2. Lê todos os peers ativos da coleção e chama on_peer() para os novos
  3. MongoDB cuida do TTL (auto-expira entradas antigas)

Esquema da coleção:
    {
        _id: "<uuid>",
        uuid: "<pubkey hex>",
        ip:   "100.111.253.8",
        port: 6001,
        height: 2046,
        work: <int>,
        ts: <timestamp>,           # TTL index (expireAfterSeconds=600)
        ua: "brn-node-rdv/1",
    }

Índices criados automaticamente:
    - TTL em `ts` (expira após BRN_RDV_TTL segundos)
    - único em `uuid`

Uso:
    from rendezvous_mongo import RendezvousMongo

    rdv = RendezvousMongo(
        tcp_port=6001,
        node_uuid=node_id_pub,
        on_peer=lambda ip, port: p2p.add_peer(ip, port),
        running_flag=lambda: not _shutdown.is_set(),
        blockchain=chain,
    )
    rdv.start()
    ...
    rdv.stop()

Env:
    BRN_RDV_INTERVAL        (default 120s)
    BRN_RDV_TTL             (default 600s)
    BRN_RDV_INITIAL_DELAY   (default 5s)
    BRN_RDV_MAX_RETURN      (default 500)
    BRN_PUBLIC_IP           (override do IP anunciado)

Requer:
    mongo_client.py com is_configured(), db()
================================================================
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("rendezvous_mongo")


# ============================================================
# CONFIG
# ============================================================
DEFAULT_INTERVAL  = int(os.environ.get("BRN_RDV_INTERVAL", "120"))
DEFAULT_TTL_S     = int(os.environ.get("BRN_RDV_TTL", "600"))
INITIAL_DELAY     = int(os.environ.get("BRN_RDV_INITIAL_DELAY", "5"))
MAX_PEERS_RETURN  = int(os.environ.get("BRN_RDV_MAX_RETURN", "500"))
UA                = "brn-node-rdv/1"


# ============================================================
# HELPERS
# ============================================================
def _get_local_ip() -> str:
    """Descobre o IP local da interface principal (fallback do anunciado)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ============================================================
# RendezvousMongo
# ============================================================
class RendezvousMongo(threading.Thread):
    """
    Anúncio + descoberta de peers via MongoDB.
    Não requer servidor HTTP — só precisa do mongo_client configurado.
    """

    def __init__(self,
                 tcp_port: int,
                 node_uuid: str,
                 on_peer: Callable[[str, int], None],
                 running_flag: Callable[[], bool],
                 ip_hint: Optional[str] = None,
                 interval: int = DEFAULT_INTERVAL,
                 ttl_s: int = DEFAULT_TTL_S,
                 blockchain=None) -> None:
        super().__init__(daemon=True, name="RendezvousMongo")

        self.tcp_port = int(tcp_port)
        self.node_uuid = str(node_uuid or "")
        self.on_peer = on_peer
        self.running_flag = running_flag
        self.blockchain = blockchain
        self.interval = int(interval)
        self.ttl_s = int(ttl_s)

        self.ip_hint = (ip_hint
                        or os.environ.get("BRN_PUBLIC_IP", "")
                        or _get_local_ip())

        self._stop_evt = threading.Event()
        self._seen: dict = {}
        self._seen_lock = threading.Lock()
        self._indexes_ok = False

        # Métricas
        self._m_lock = threading.Lock()
        self._metrics = {
            "announces_ok":     0,
            "announces_fail":   0,
            "peers_added":      0,
            "last_ok_ts":       0.0,
            "last_fail_ts":     0.0,
            "last_error":       "",
            "last_peer_count":  0,
            "consecutive_fail": 0,
        }

    # ============================================================
    # COLECÇÃO MONGO
    # ============================================================
    def _get_collection(self):
        """Retorna a collection `rendezvous` ou None se Mongo indisponível."""
        try:
            from mongo_client import mongo
            if not mongo.is_configured():
                return None
            db = mongo.db()
            if db is None:
                return None
            coll = db.rendezvous

            # Cria índices só uma vez por processo
            if not self._indexes_ok:
                try:
                    coll.create_index(
                        "ts",
                        expireAfterSeconds=self.ttl_s,
                        name="ttl_ts",
                        background=True,
                    )
                    coll.create_index(
                        "uuid", unique=True,
                        name="uniq_uuid",
                        background=True,
                    )
                    self._indexes_ok = True
                except Exception as e:
                    log.debug("rdv indexes: %s", e)
            return coll
        except Exception as e:
            log.debug("rdv mongo indisponível: %s", e)
            return None

    # ============================================================
    # MÉTRICAS
    # ============================================================
    def _set_error(self, msg: str) -> None:
        with self._m_lock:
            self._metrics["last_error"] = str(msg)[:200]
            self._metrics["last_fail_ts"] = time.time()
            self._metrics["consecutive_fail"] += 1

    def _clear_error(self) -> None:
        with self._m_lock:
            self._metrics["last_error"] = ""
            self._metrics["last_ok_ts"] = time.time()
            self._metrics["consecutive_fail"] = 0

    # ============================================================
    # STATE PARA O ANÚNCIO
    # ============================================================
    def _current_height(self) -> int:
        if self.blockchain is None:
            return 0
        try:
            return int(self.blockchain.db.height())
        except Exception:
            return 0

    def _current_work(self) -> int:
        if self.blockchain is None:
            return 0
        try:
            return int(self.blockchain.cumulative_work())
        except Exception:
            return 0

    # ============================================================
    # ANNOUNCE
    # ============================================================
    def _announce(self) -> bool:
        coll = self._get_collection()
        if coll is None:
            self._set_error("mongo indisponivel")
            with self._m_lock:
                self._metrics["announces_fail"] += 1
            return False

        try:
            doc = {
                "uuid":   self.node_uuid,
                "ip":     self.ip_hint,
                "port":   int(self.tcp_port),
                "height": self._current_height(),
                "work":   self._current_work(),
                "ts":     time.time(),
                "ua":     UA,
            }
            coll.update_one(
                {"uuid": self.node_uuid},
                {"$set": doc},
                upsert=True,
            )
            with self._m_lock:
                self._metrics["announces_ok"] += 1
            self._clear_error()
            return True
        except Exception as e:
            log.debug("announce falhou: %s", e)
            self._set_error(str(e))
            with self._m_lock:
                self._metrics["announces_fail"] += 1
            return False

    # ============================================================
    # DISPATCH
    # ============================================================
    def _dispatch(self, peers: list) -> int:
        """Chama on_peer() para peers ainda não vistos."""
        added = 0
        now = time.time()

        # clean: se dict ficou grande, remove antigos
        with self._seen_lock:
            if len(self._seen) > 2000:
                cutoff = now - 300
                for k in [k for k, t in self._seen.items() if t < cutoff]:
                    del self._seen[k]

        for p in peers[:MAX_PEERS_RETURN]:
            try:
                ip = str(p.get("ip", "")).strip()
                port = int(p.get("port", 0))
                uuid = str(p.get("uuid", ""))
            except (TypeError, ValueError):
                continue

            if not ip or port <= 0:
                continue
            if uuid == self.node_uuid:
                continue

            key = f"{ip}:{port}"

            with self._seen_lock:
                if key in self._seen and (now - self._seen[key]) < 300:
                    continue
                self._seen[key] = now

            try:
                self.on_peer(ip, port)
                added += 1
            except Exception as e:
                log.debug("on_peer(%s) falhou: %s", key, e)

        return added

    # ============================================================
    # FETCH
    # ============================================================
    def _fetch_peers(self) -> list:
        coll = self._get_collection()
        if coll is None:
            return []
        try:
            cutoff = time.time() - self.ttl_s
            cursor = coll.find(
                {"ts": {"$gte": cutoff},
                 "uuid": {"$ne": self.node_uuid}},
                {"_id": 0, "uuid": 1, "ip": 1, "port": 1, "height": 1},
            ).limit(MAX_PEERS_RETURN)
            return list(cursor)
        except Exception as e:
            log.debug("fetch_peers falhou: %s", e)
            return []

    # ============================================================
    # LOOP
    # ============================================================
    def run(self) -> None:
        if self._get_collection() is None:
            log.warning("RendezvousMongo: mongo indisponivel — pulando")
            return

        log.info("RendezvousMongo ativo (intervalo=%ds ttl=%ds ip=%s)",
                 self.interval, self.ttl_s, self.ip_hint)

        # Delay inicial para não competir com o boot
        if self._stop_evt.wait(INITIAL_DELAY):
            return

        while self.running_flag() and not self._stop_evt.is_set():
            try:
                ok = self._announce()
                if ok:
                    peers = self._fetch_peers()
                    with self._m_lock:
                        self._metrics["last_peer_count"] = len(peers)
                    n = self._dispatch(peers)
                    if n > 0:
                        with self._m_lock:
                            self._metrics["peers_added"] += n
                        log.info("rendezvous: +%d peer(s) novos (total=%d)",
                                 n, len(peers))
            except Exception as e:
                log.warning("rendezvous loop: %s", e)
                self._set_error(str(e))

            # Backoff se em falha
            with self._m_lock:
                fails = self._metrics["consecutive_fail"]
            wait = self.interval if fails == 0 else min(self.interval * 2, 600)
            if self._stop_evt.wait(wait):
                break

        log.info("RendezvousMongo encerrado")

    # ============================================================
    # STOP
    # ============================================================
    def stop(self, wait: bool = True, timeout: float = 5.0) -> None:
        # Remove nosso anúncio para que outros não tentem conectar
        try:
            coll = self._get_collection()
            if coll is not None:
                coll.delete_one({"uuid": self.node_uuid})
        except Exception:
            pass
        self._stop_evt.set()
        if wait and self.is_alive():
            self.join(timeout=timeout)

    # ============================================================
    # STATUS
    # ============================================================
    def status(self) -> dict:
        with self._m_lock:
            m = dict(self._metrics)
        now = time.time()
        return {
            "backend":           "mongo",
            "interval_s":        self.interval,
            "ttl_s":             self.ttl_s,
            "ip_hint":           self.ip_hint,
            "running":           self.is_alive() and not self._stop_evt.is_set(),
            "announces_ok":      m["announces_ok"],
            "announces_fail":    m["announces_fail"],
            "peers_added":       m["peers_added"],
            "last_peer_count":   m["last_peer_count"],
            "consecutive_fail":  m["consecutive_fail"],
            "last_ok_ago_s":     (int(now - m["last_ok_ts"])
                                  if m["last_ok_ts"] else None),
            "last_fail_ago_s":   (int(now - m["last_fail_ts"])
                                  if m["last_fail_ts"] else None),
            "last_error":        m["last_error"],
        }

    # ============================================================
    # API EXTRA
    # ============================================================
    def force_announce(self) -> bool:
        """Força um announce imediato (útil em testes)."""
        return self._announce()

    def list_remote_peers(self) -> list:
        """Retorna todos os peers remotos (sem filtrar por visto)."""
        return self._fetch_peers()


# ============================================================
# AUTO-TESTE (modo dry-run — não precisa de Mongo)
# ============================================================
if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("Testando RendezvousMongo (dry-run)...")

    # ---- 1. Instanciação ----
    stop = [False]

    def running():
        return not stop[0]

    received = []

    def on_peer(ip, port):
        received.append((ip, port))

    rdv = RendezvousMongo(
        tcp_port=6001,
        node_uuid="abc" * 21 + "def"[:2],  # 64+ chars fictícios
        on_peer=on_peer,
        running_flag=running,
        blockchain=None,
    )

    print(f"  ip_hint:    {rdv.ip_hint}")
    print(f"  interval:   {rdv.interval}")
    print(f"  ttl_s:      {rdv.ttl_s}")
    print(f"  backend:    mongo (lazy)")

    # ---- 2. Status antes de rodar ----
    st = rdv.status()
    print(f"  running:    {st['running']}")
    assert st["backend"] == "mongo"
    assert st["running"] is False

    # ---- 3. _dispatch funciona sem Mongo ----
    fake_peers = [
        {"uuid": "peer1", "ip": "10.0.0.1", "port": 6001},
        {"uuid": "peer2", "ip": "10.0.0.2", "port": 6001},
        {"uuid": "abc" * 21 + "def"[:2], "ip": "10.0.0.9", "port": 6001},  # próprio
    ]
    n = rdv._dispatch(fake_peers)
    print(f"  dispatch:   +{n} peer(s) (filtrou self)")
    assert n == 2, f"esperado 2, obteve {n}"
    assert len(received) == 2

    # ---- 4. Dispatch duplicado ----
    n2 = rdv._dispatch(fake_peers)
    print(f"  dispatch 2: +{n2} peer(s) (dedup)")
    assert n2 == 0, f"esperado 0 (dedup), obteve {n2}"

    # ---- 5. Status final ----
    print(f"  status:     {json.dumps(rdv.status(), ensure_ascii=False)}")

    print("RendezvousMongo OK (mongo real nao testado).")