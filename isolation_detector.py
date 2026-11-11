"""isolation_detector.py — Alertas de isolamento de rede (v1.0)
================================================================
Detecta quando o nó está sozinho ou travado e sugere ações.
NÃO afeta consenso. Só gera logs/alertas para o operador.

Uso:
    from isolation_detector import IsolationDetector
    detector = IsolationDetector(peer_timeout_s=300)
    # No loop watchdog:
    alert = detector.check(chain.db.height(), p2p.discovery.listar_peers())
    if alert:
        log.warning("[ISOLATION] %s: %s", alert.severity, alert.reason)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

__version__ = "1.0"


@dataclass
class IsolationAlert:
    severity: str          # "info" | "warning" | "critical"
    reason: str
    suggested_fix: list = field(default_factory=list)


class IsolationDetector:
    def __init__(self, peer_timeout_s: float = 300.0,
                 stall_timeout_s: float = 600.0):
        self.peer_timeout_s = float(peer_timeout_s)
        self.stall_timeout_s = float(stall_timeout_s)
        self._last_peer_seen: dict = {}
        self._last_height = -1
        self._height_stalled_since: float = 0.0

    def on_peer_seen(self, addr: str) -> None:
        if addr:
            self._last_peer_seen[str(addr)] = time.time()

    def check(self, local_height: int, known_peers) -> IsolationAlert | None:
        now = time.time()

        # ─── 1. Zero peers ativos ─────────────────────────────────
        active = [
            a for a, t in self._last_peer_seen.items()
            if now - t < self.peer_timeout_s
        ]
        if not active:
            return IsolationAlert(
                severity="critical",
                reason=f"Nenhum peer ativo há {int(self.peer_timeout_s/60)}min",
                suggested_fix=[
                    "tailscale status | grep active",
                    "curl http://100.111.253.8:5000/api/status",
                    "grep -n 'socks5\\|ALL_PROXY' start.sh (deve estar vazio)",
                    "POST /api/bootstrap/add com 1 peer manual",
                ],
            )

        # ─── 2. Altura travada + peers ativos = rede congelada ────
        try:
            lh = int(local_height)
        except (TypeError, ValueError):
            lh = -1

        if lh == self._last_height and lh >= 0:
            if self._height_stalled_since == 0:
                self._height_stalled_since = now
            stalled_for = now - self._height_stalled_since
            if stalled_for > self.stall_timeout_s:
                return IsolationAlert(
                    severity="warning",
                    reason=f"Altura travada em #{lh} há {int(stalled_for/60)}min",
                    suggested_fix=[
                        "Verifique se a Origin ainda está minerando",
                        "curl http://100.111.253.8:5000/api/pool/stats",
                        "python sync_global.py --strict",
                        "Confira multi-sig: /api/consensus/validators",
                    ],
                )
        else:
            self._height_stalled_since = 0
            self._last_height = lh

        # ─── 3. Poucos peers (< 2) ────────────────────────────────
        if len(active) == 1:
            return IsolationAlert(
                severity="info",
                reason=f"Apenas 1 peer ativo (rede frágil)",
                suggested_fix=[
                    "Adicione 2+ peers via /api/bootstrap/add",
                    "Verifique tailscale_discover.py",
                ],
            )

        return None


if __name__ == "__main__":
    print("=" * 60)
    print(f" isolation_detector.py v{__version__} — self-test")
    print("=" * 60)

    # Teste 1: sem peers
    det = IsolationDetector(peer_timeout_s=1.0, stall_timeout_s=1.0)
    alert = det.check(5004, [])
    print(f"  sem peers:   {alert.severity if alert else 'ok'}")
    assert alert and alert.severity == "critical"

    # Teste 2: 1 peer ativo
    det = IsolationDetector(peer_timeout_s=60, stall_timeout_s=600)
    det.on_peer_seen("100.1.1.1:6001")
    alert = det.check(5004, ["100.1.1.1:6001"])
    print(f"  1 peer:      {alert.severity if alert else 'ok'}")
    assert alert and alert.severity == "info"

    # Teste 3: 2+ peers, altura mudando
    det = IsolationDetector()
    det.on_peer_seen("100.1.1.1:6001")
    det.on_peer_seen("100.2.2.2:6001")
    alert = det.check(5004, ["100.1.1.1:6001", "100.2.2.2:6001"])
    print(f"  2 peers:     {alert.severity if alert else 'ok'}")
    assert alert is None

    # Teste 4: altura travada
    det = IsolationDetector(peer_timeout_s=60, stall_timeout_s=0.5)
    det.on_peer_seen("100.1.1.1:6001")
    det.on_peer_seen("100.2.2.2:6001")
    det.check(5004, [])  # registra 5004
    time.sleep(0.6)
    alert = det.check(5004, [])  # ainda 5004
    print(f"  travado:     {alert.severity if alert else 'ok'}")
    assert alert and alert.severity == "warning"

    print("\n  [OK] Todos os testes passaram")
