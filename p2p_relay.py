"""p2p_relay.py — Relay E2E (v2)
================================================================
v2:
  [FIX] CRÍTICO: _send_to_peer() agora usa
        p2p.discovery.get_addr_by_uuid() + P2PClient.send_message()
        que EXISTEM no P2PManager v6.6.4.
        Antes procurava p2p.send_to_peer e p2p._peers (inexistentes)
        e o relay nunca entregava pacotes.
v1:
  Relay com capabilities, rotas, ROUTE_TTL, etc.
================================================================
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from typing import Optional


log = logging.getLogger("relay")


# ============================================================
# CONFIG
# ============================================================
MAX_ROUTES           = int(os.environ.get("BRN_RELAY_MAX_ROUTES", "100"))
ROUTE_TTL_S          = int(os.environ.get("BRN_RELAY_ROUTE_TTL", "300"))
MAX_PAYLOAD_BYTES    = int(os.environ.get("BRN_RELAY_MAX_PAYLOAD_BYTES",
                                          str(1024 * 1024)))
MIN_TIER             = os.environ.get("BRN_RELAY_MIN_TIER", "low").lower()

TIER_RANK = {
    "unknown": 0,
    "low":     1,
    "medium":  2,
    "high":    3,
}

ROUTE_PENDING   = "pending"
ROUTE_ACCEPTED  = "accepted"
ROUTE_EXPIRED   = "expired"
ROUTE_CLOSED    = "closed"


def _gen_route_id() -> str:
    return "rt_" + secrets.token_hex(8)


def _now() -> float:
    return time.time()


def _payload_size(obj) -> int:
    try:
        import json
        return len(json.dumps(obj, separators=(",", ":")).encode("utf-8"))
    except Exception:
        return 0


# ============================================================
# RelayManager
# ============================================================
class RelayManager:
    def __init__(self, p2p, node_id_priv):
        self.p2p = p2p
        self.priv = node_id_priv

        self._caps: dict = {}
        self._routes: dict = {}

        self._lock = threading.RLock()
        self._stop = threading.Event()

        self._stats = {
            "capabilities_registered": 0,
            "routes_created":          0,
            "routes_accepted":         0,
            "routes_rejected":         0,
            "routes_expired":          0,
            "packets_relayed":         0,
            "bytes_relayed":           0,
            "packets_dropped":         0,
            "auth_rejected":           0,
        }

        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="Relay-Cleanup")
        self._cleanup_thread.start()

        log.info("RelayManager iniciado (max_routes=%d ttl=%ds)",
                 MAX_ROUTES, ROUTE_TTL_S)

    # ============================================================
    # PROPRIEDADES
    # ============================================================
    @property
    def routes(self) -> dict:
        with self._lock:
            return dict(self._routes)

    @property
    def capabilities(self) -> dict:
        with self._lock:
            return dict(self._caps)

    # ============================================================
    # CAPABILITIES
    # ============================================================
    def register_capability(self, peer_id: str, caps: dict,
                            verified: bool = False) -> None:
        if not peer_id or not isinstance(caps, dict):
            return

        can_relay = bool(caps.get("can_relay", False))
        tier = str(caps.get("bandwidth_tier", "unknown")).lower()
        if tier not in TIER_RANK:
            tier = "unknown"

        with self._lock:
            self._caps[peer_id] = {
                "can_relay":      can_relay,
                "tier":           tier,
                "tier_rank":      TIER_RANK[tier],
                "verified":       bool(verified),
                "last_seen":      _now(),
            }
            self._stats["capabilities_registered"] += 1

        log.debug("capability: %s can_relay=%s tier=%s verified=%s",
                  peer_id[:12], can_relay, tier, verified)

    def _peer_can_relay(self, peer_id: str) -> bool:
        with self._lock:
            entry = self._caps.get(peer_id)
        if not entry:
            return False
        if not entry.get("can_relay"):
            return False
        min_rank = TIER_RANK.get(MIN_TIER, 0)
        return entry.get("tier_rank", 0) >= min_rank

    # ============================================================
    # ROUTES
    # ============================================================
    def _make_route(self, origin_peer: str, target_peer: str,
                    origin_ip: str = "", origin_port: int = 0) -> dict:
        route_id = _gen_route_id()
        now = _now()
        return {
            "route_id":       route_id,
            "origin_peer":    origin_peer,
            "target_peer":    target_peer,
            "origin_ip":      origin_ip,
            "origin_port":    int(origin_port or 0),
            "target_ip":      "",
            "target_port":    0,
            "state":          ROUTE_PENDING,
            "created_at":     now,
            "last_activity":  now,
            "bytes_relayed":  0,
            "packets_relayed": 0,
        }

    def _prune_routes(self):
        now = _now()
        with self._lock:
            expired = []
            for rid, r in list(self._routes.items()):
                if now - r["last_activity"] > ROUTE_TTL_S:
                    r["state"] = ROUTE_EXPIRED
                    expired.append(rid)
            for rid in expired:
                self._routes.pop(rid, None)
                self._stats["routes_expired"] += 1

            if len(self._routes) > MAX_ROUTES:
                ordered = sorted(
                    self._routes.items(),
                    key=lambda kv: kv[1]["last_activity"]
                )
                excess = len(self._routes) - MAX_ROUTES
                for rid, _ in ordered[:excess]:
                    self._routes.pop(rid, None)

    def _cleanup_loop(self):
        while not self._stop.wait(60):
            try:
                self._prune_routes()
            except Exception as e:
                log.debug("cleanup: %s", e)

    # ============================================================
    # HANDLER: relay_connect_request
    # ============================================================
    def handle_relay_request(self, ip: str, peer_id: Optional[str],
                             msg: dict) -> dict:
        if not peer_id:
            self._stats["routes_rejected"] += 1
            return {"ok": False, "error": "sem peer_id"}

        target = str(msg.get("target", "")).strip()
        if not target:
            self._stats["routes_rejected"] += 1
            return {"ok": False, "error": "target vazio"}

        if target == peer_id:
            self._stats["routes_rejected"] += 1
            return {"ok": False, "error": "target == origin"}

        my_uuid = getattr(self.p2p, "node_uuid", "") if self.p2p else ""
        if target == my_uuid:
            self._stats["routes_rejected"] += 1
            return {"ok": False, "error": "target == self"}

        self._prune_routes()
        origin_port = int(msg.get("origin_port", 0) or 0)

        with self._lock:
            if len(self._routes) >= MAX_ROUTES:
                self._stats["routes_rejected"] += 1
                return {"ok": False, "error": "relay lotado"}

            route = self._make_route(
                origin_peer=peer_id,
                target_peer=target,
                origin_ip=ip,
                origin_port=origin_port,
            )
            self._routes[route["route_id"]] = route
            self._stats["routes_created"] += 1

        log.info("relay: rota %s — %s → %s",
                 route["route_id"][:12], peer_id[:12], target[:12])

        forwarded = self._forward_to_target(
            route=route,
            message={
                "type":       "relay_connect_incoming",
                "route_id":   route["route_id"],
                "from_uuid":  peer_id,
            },
        )
        if not forwarded:
            with self._lock:
                self._routes.pop(route["route_id"], None)
            self._stats["routes_rejected"] += 1
            return {"ok": False, "error": "target nao alcancavel"}

        return {"ok": True, "route_id": route["route_id"]}

    # ============================================================
    # HANDLER: relay_connect_incoming
    # ============================================================
    def handle_incoming_relay(self, ip: str, peer_id: Optional[str],
                              msg: dict) -> dict:
        route_id = str(msg.get("route_id", "")).strip()
        from_uuid = str(msg.get("from_uuid", "")).strip()
        if not route_id or not from_uuid:
            return {"ok": False, "error": "campos faltando"}

        log.info("relay: incoming %s de %s",
                 route_id[:12], from_uuid[:12])
        return {"ok": True, "accepted": True}

    # ============================================================
    # HANDLER: relay_connect_accepted
    # ============================================================
    def handle_route_accepted(self, msg: dict) -> bool:
        route_id = str(msg.get("route_id", "")).strip()
        if not route_id:
            return False

        with self._lock:
            route = self._routes.get(route_id)
            if not route:
                return False
            route["state"] = ROUTE_ACCEPTED
            route["last_activity"] = _now()
            self._stats["routes_accepted"] += 1

        log.info("relay: rota %s ACEITA (%s → %s)",
                 route_id[:12],
                 route.get("origin_peer", "")[:12],
                 route.get("target_peer", "")[:12])
        return True

    # ============================================================
    # HANDLER: relay_data
    # ============================================================
    def handle_relay_data(self, ip: str, peer_id: Optional[str],
                          msg: dict) -> dict:
        route_id = str(msg.get("route_id", "")).strip()
        if not route_id:
            self._stats["packets_dropped"] += 1
            return {"ok": False, "error": "sem route_id"}

        payload = msg.get("payload")
        origin = str(msg.get("origin", "")).strip().lower()

        size = _payload_size(payload)
        if size > MAX_PAYLOAD_BYTES:
            self._stats["packets_dropped"] += 1
            return {"ok": False, "error": f"payload > {MAX_PAYLOAD_BYTES}"}

        with self._lock:
            route = self._routes.get(route_id)
            if not route:
                self._stats["packets_dropped"] += 1
                return {"ok": False, "error": "rota inexistente"}

            if route["state"] != ROUTE_ACCEPTED:
                self._stats["packets_dropped"] += 1
                return {"ok": False, "error": f"rota em {route['state']}"}

            if origin == "target":
                dest_uuid = route["origin_peer"]
            else:
                dest_uuid = route["target_peer"]

            route["last_activity"] = _now()
            route["bytes_relayed"] += size
            route["packets_relayed"] += 1

        forwarded = self._send_to_peer(dest_uuid, {
            "type":       "relay_data",
            "route_id":   route_id,
            "payload":    payload,
            "origin":     "relay",
        })

        if forwarded:
            self._stats["packets_relayed"] += 1
            self._stats["bytes_relayed"] += size
            return {"ok": True}
        else:
            self._stats["packets_dropped"] += 1
            return {"ok": False, "error": "peer destino offline"}

    # ============================================================
    # FORWARDING
    # ============================================================
    def _forward_to_target(self, route: dict, message: dict) -> bool:
        target_uuid = route.get("target_peer", "")
        if not target_uuid:
            return False
        return self._send_to_peer(target_uuid, message)

    def _send_to_peer(self, peer_uuid: str, message: dict) -> bool:
        """
        [FIX v2] Usa discovery.get_addr_by_uuid() + P2PClient.send_message()
        do P2PManager v6.6.4. Antes procurava atributos inexistentes.
        """
        if not self.p2p or not peer_uuid:
            return False

        # Caminho correto: PeerDiscovery tem directory uuid→(ip,port)
        try:
            disc = getattr(self.p2p, "discovery", None)
            if disc is None:
                log.debug("send_to_peer: p2p.discovery ausente")
                return False

            addr = None
            if hasattr(disc, "get_addr_by_uuid"):
                addr = disc.get_addr_by_uuid(peer_uuid)

            if not addr:
                log.debug("send_to_peer: uuid %s não no directory",
                          peer_uuid[:12])
                return False

            ip, port = addr
            from p2p_unified import P2PClient
            resp, _ = P2PClient.send_message(ip, int(port), message)
            return resp is not None
        except Exception as e:
            log.debug("send_to_peer falhou: %s", e)
            return False

    # ============================================================
    # STATUS / STOP
    # ============================================================
    def status(self) -> dict:
        with self._lock:
            n_routes = len(self._routes)
            n_pending = sum(1 for r in self._routes.values()
                            if r["state"] == ROUTE_PENDING)
            n_accepted = sum(1 for r in self._routes.values()
                             if r["state"] == ROUTE_ACCEPTED)
            n_caps = len(self._caps)
            n_relayers = sum(1 for c in self._caps.values()
                             if c.get("can_relay"))

        return {
            "routes":            n_routes,
            "routes_pending":    n_pending,
            "routes_accepted":   n_accepted,
            "capabilities":      n_caps,
            "peers_can_relay":   n_relayers,
            "max_routes":        MAX_ROUTES,
            "route_ttl_s":       ROUTE_TTL_S,
            "min_tier":          MIN_TIER,
            **self._stats,
        }

    def list_routes(self) -> list:
        with self._lock:
            return [
                {
                    "route_id":      r["route_id"],
                    "origin_peer":   r["origin_peer"],
                    "target_peer":   r["target_peer"],
                    "state":         r["state"],
                    "created_at":    r["created_at"],
                    "last_activity": r["last_activity"],
                    "packets":       r["packets_relayed"],
                    "bytes":         r["bytes_relayed"],
                }
                for r in self._routes.values()
            ]

    def stop(self):
        self._stop.set()
        if self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=2)
        with self._lock:
            self._routes.clear()
            self._caps.clear()
        log.info("RelayManager encerrado")


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("Testando RelayManager v2...")

    class FakeDiscovery:
        def __init__(self):
            self.directory = {}
        def get_addr_by_uuid(self, u):
            return self.directory.get(u)

    class FakeP2P:
        def __init__(self):
            self.node_uuid = "aaaa"
            self.discovery = FakeDiscovery()
            self.sent = []
        def _send(self, ip, port, msg):
            self.sent.append((ip, port, msg))
            return {"ok": True}

    class FakePriv:
        def sign(self, data):
            return b"fakesig"

    p2p = FakeP2P()
    relay = RelayManager(p2p, FakePriv())

    # 1. Capability
    relay.register_capability("peer_alpha",
                              {"can_relay": True, "bandwidth_tier": "high"},
                              verified=True)
    caps = relay.capabilities
    assert "peer_alpha" in caps and caps["peer_alpha"]["can_relay"]
    print("  [ok] register_capability")

    # 2. Sem can_relay
    relay.register_capability("peer_beta", {"can_relay": False})
    assert not relay._peer_can_relay("peer_beta")
    assert relay._peer_can_relay("peer_alpha")
    print("  [ok] _peer_can_relay")

    # 3. Coloca o target no directory
    p2p.discovery.directory["peer_target"] = ("10.0.0.2", 6001)
    p2p.discovery.directory["peer_origin"] = ("10.0.0.1", 6001)

    # Patch: FakeP2P não tem P2PClient real. Vamos interceptar.
    import sys, types
    fake_mod = types.ModuleType("p2p_unified")
    class FakeClient:
        @staticmethod
        def send_message(ip, port, msg):
            p2p.sent.append((ip, port, msg))
            return ({"ok": True}, 5.0)
    fake_mod.P2PClient = FakeClient
    sys.modules["p2p_unified"] = fake_mod

    # 4. Request → accept → data
    r = relay.handle_relay_request(
        "10.0.0.1", "peer_origin",
        {"target": "peer_target", "origin_port": 6001},
    )
    assert r["ok"], r
    route_id = r["route_id"]
    print(f"  [ok] handle_relay_request → {route_id[:12]}")

    ok = relay.handle_route_accepted({"route_id": route_id})
    assert ok
    print("  [ok] handle_route_accepted")

    r2 = relay.handle_incoming_relay(
        "10.0.0.2", "peer_target",
        {"route_id": route_id, "from_uuid": "peer_origin"},
    )
    assert r2["ok"]
    print("  [ok] handle_incoming_relay")

    # 5. Data nos dois sentidos
    r3 = relay.handle_relay_data(
        "10.0.0.1", "peer_origin",
        {"route_id": route_id, "payload": {"hello": "target"},
         "origin": "origin"},
    )
    assert r3["ok"], r3
    r4 = relay.handle_relay_data(
        "10.0.0.2", "peer_target",
        {"route_id": route_id, "payload": {"hello": "origin"},
         "origin": "target"},
    )
    assert r4["ok"], r4
    print("  [ok] handle_relay_data nos dois sentidos")

    # 6. Status
    st = relay.status()
    print(f"  [info] {json.dumps(st, ensure_ascii=False)}")
    assert st["routes"] == 1
    assert st["routes_accepted"] == 1
    assert st["packets_relayed"] == 2

    # 7. Payload grande
    r5 = relay.handle_relay_data(
        "10.0.0.1", "peer_origin",
        {"route_id": route_id, "payload": "x" * (MAX_PAYLOAD_BYTES + 1),
         "origin": "origin"},
    )
    assert not r5["ok"]
    print(f"  [ok] payload grande rejeitado")

    # 8. Rota inválida
    r6 = relay.handle_relay_data(
        "10.0.0.1", "peer_origin",
        {"route_id": "rt_inexistente", "payload": "x", "origin": "origin"},
    )
    assert not r6["ok"]
    print("  [ok] rota inexistente rejeitada")

    # 9. Auto-relay
    r7 = relay.handle_relay_request(
        "10.0.0.1", "peer_x",
        {"target": "peer_x"},
    )
    assert not r7["ok"]
    print("  [ok] auto-relay rejeitado")

    # 10. Directory vazio
    r8 = relay.handle_relay_request(
        "10.0.0.1", "peer_origin",
        {"target": "peer_inexistente"},
    )
    assert not r8["ok"]
    print("  [ok] target fora do directory → rejeitado")

    relay.stop()
    print("RelayManager v2 OK.")
