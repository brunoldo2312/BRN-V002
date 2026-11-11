"""peer_ai.py — Priorização inteligente de peers (v1.0)
================================================================
Classifica peers descobertos por QUALIDADE de conexão.
NÃO afeta consenso — só escolhe com quem tentar handshake.

Determinístico: mesmos features → mesma classificação em qualquer nó.
Sem dependências externas. Sem LLM. Sem API.

Uso:
    from peer_ai import prioritize_peers, compute_peer_score, PeerFeatures

    history = {
        "100.111.253.8:6001": {"tries": 50, "successes": 48, "avg_ms": 45,
                                "blocks": 200, "first_seen": 1699999999,
                                "last_seen": 1700000000, "banned": False},
        ...
    }
    top_peers = prioritize_peers(peers_list, history, local_height=5004, top_n=20)
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

__version__ = "1.0"


@dataclass
class PeerFeatures:
    """Features extraídas do histórico de tentativas."""
    addr: str = ""
    connect_success_rate: float = 0.5
    avg_handshake_ms: float = 200.0
    blocks_contributed: int = 0
    age_hours: float = 0.0
    last_seen_hours_ago: float = 0.0
    uptime_pct: float = 75.0
    height_delta: int = 0
    version: int = 1
    is_banned: bool = False
    is_tailscale: bool = False


def compute_peer_score(f: PeerFeatures) -> float:
    """
    Score 0-100. Determinístico.
    Fórmula auditável, sem LLM, sem ML caixa-preta.

    Pesos:
        30 pts — taxa de sucesso
        20 pts — uptime
        15 pts — recência (decay exponencial, meia-vida 24h)
        15 pts — latência (log scale)
        10 pts — altura próxima
        10 pts — contribuição histórica
        +5 pts — bônus Tailscale
    """
    if f.is_banned:
        return 0.0

    score = 0.0

    # 1. Taxa de sucesso (peso: 30)
    rate = max(0.0, min(1.0, float(f.connect_success_rate)))
    score += rate * 30.0

    # 2. Uptime (peso: 20)
    uptime = max(0.0, min(100.0, float(f.uptime_pct)))
    score += (uptime / 100.0) * 20.0

    # 3. Recência (peso: 15) — meia-vida de 24h
    hours = max(0.0, float(f.last_seen_hours_ago))
    recency = math.exp(-hours / 24.0)
    score += recency * 15.0

    # 4. Latência (peso: 15) — 10ms=~15pts, 1000ms=0pts
    lat = max(0.1, float(f.avg_handshake_ms))
    latency_pts = max(0.0, 15.0 - 5.0 * math.log10(max(1.0, lat / 10.0)))
    score += latency_pts

    # 5. Altura (peso: 10)
    delta = max(0, int(f.height_delta))
    score += max(0.0, 10.0 - delta)

    # 6. Contribuição (peso: 10, teto em 100 blocos)
    contrib = max(0, int(f.blocks_contributed))
    score += min(10.0, contrib / 10.0)

    # 7. Bônus Tailscale (+5) — IPs 100.x são mais estáveis
    if f.is_tailscale:
        score += 5.0

    # 8. Penalidade por peer muito novo (pode ser bot)
    if f.age_hours < 1.0:
        score *= 0.5
    elif f.age_hours < 6.0:
        score *= 0.8

    return round(min(100.0, max(0.0, score)), 2)


def _peer_addr(p: dict) -> str:
    """Extrai 'ip:port' de um dict de peer em vários formatos."""
    if not isinstance(p, dict):
        return str(p) if p else ""
    addr = p.get("address")
    if addr:
        return str(addr)
    ip = p.get("ip")
    port = p.get("port", p.get("tcp_port", 6001))
    if ip:
        return f"{ip}:{int(port)}"
    return ""


def prioritize_peers(
    peers: list,
    history: dict,
    local_height: int,
    top_n: int = 20,
) -> list:
    """
    Ordena peers por score e retorna os top_n.
    Histórico por addr:
        {"tries", "successes", "avg_ms", "blocks",
         "first_seen", "last_seen", "banned"}
    """
    now = time.time()
    scored = []

    for p in peers:
        addr = _peer_addr(p)
        if not addr:
            continue

        h = history.get(addr, {}) if isinstance(history, dict) else {}
        tries = int(h.get("tries", 0))
        successes = int(h.get("successes", 0))
        rate = (successes / tries) if tries > 0 else 0.5

        first_seen = float(h.get("first_seen", now))
        age_hours = max(0.0, (now - first_seen) / 3600.0)
        last_seen = float(h.get("last_seen", now))
        last_hours = max(0.0, (now - last_seen) / 3600.0)

        uptime_pct = rate * 100.0 if tries >= 3 else 75.0

        try:
            p_height = int(p.get("height", 0)) if isinstance(p, dict) else 0
        except Exception:
            p_height = 0

        features = PeerFeatures(
            addr=addr,
            connect_success_rate=rate,
            avg_handshake_ms=float(h.get("avg_ms", 200.0)),
            blocks_contributed=int(h.get("blocks", 0)),
            age_hours=age_hours,
            last_seen_hours_ago=last_hours,
            uptime_pct=uptime_pct,
            height_delta=abs(p_height - local_height),
            version=int(h.get("version", 1)),
            is_banned=bool(h.get("banned", False)),
            is_tailscale=addr.startswith("100."),
        )

        scored.append((compute_peer_score(features), p, features))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _score, p, _f in scored[:max(1, top_n)]]


if __name__ == "__main__":
    print("=" * 60)
    print(f" peer_ai.py v{__version__} — self-test")
    print("=" * 60)

    # Teste 1: peer bom
    good = PeerFeatures(
        addr="100.111.253.8:6001",
        connect_success_rate=0.98,
        avg_handshake_ms=45.0,
        blocks_contributed=500,
        age_hours=72.0,
        last_seen_hours_ago=0.05,
        uptime_pct=98.0,
        height_delta=0,
        is_tailscale=True,
    )
    s1 = compute_peer_score(good)
    print(f"  peer BOM       score={s1}  (esperado > 80)")
    assert s1 > 80, f"score baixo demais: {s1}"

    # Teste 2: peer zumbi
    zombie = PeerFeatures(
        addr="100.64.149.63:6001",
        connect_success_rate=0.0,
        avg_handshake_ms=9999.0,
        blocks_contributed=0,
        age_hours=48.0,
        last_seen_hours_ago=720.0,
        uptime_pct=0.0,
        height_delta=100,
    )
    s2 = compute_peer_score(zombie)
    print(f"  peer ZUMBI     score={s2}  (esperado < 20)")
    assert s2 < 20, f"score alto demais: {s2}"

    # Teste 3: banned sempre 0
    banned = PeerFeatures(addr="1.2.3.4:6001", is_banned=True)
    s3 = compute_peer_score(banned)
    print(f"  peer BANNED    score={s3}  (esperado 0)")
    assert s3 == 0.0

    # Teste 4: priorização
    peers = [
        {"address": "100.1.1.1:6001", "height": 5000},
        {"address": "100.2.2.2:6001", "height": 4990},
        {"address": "100.3.3.3:6001", "height": 5004},
    ]
    history = {
        "100.1.1.1:6001": {"tries": 50, "successes": 49, "avg_ms": 40,
                           "blocks": 300, "last_seen": time.time()},
        "100.2.2.2:6001": {"tries": 50, "successes": 0, "avg_ms": 9999,
                           "blocks": 0, "last_seen": time.time() - 86400},
        "100.3.3.3:6001": {"tries": 50, "successes": 48, "avg_ms": 55,
                           "blocks": 200, "last_seen": time.time()},
    }
    top = prioritize_peers(peers, history, local_height=5004, top_n=2)
    print(f"  priorizados: {[p['address'] for p in top]}")
    assert "100.2.2.2:6001" not in [p["address"] for p in top], "zumbi não foi filtrado"

    print("\n  [OK] Todos os testes passaram")
