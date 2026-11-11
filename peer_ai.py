# peer_ai.py — IA determinística para priorizar peers
"""
Classifica peers descobertos por QUALIDADE de conexão.
NÃO afeta consenso — só escolhe com quem tentar handshake.
Determinístico: mesmos features → mesma classificação em qualquer nó.
"""
from __future__ import annotations
import math
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class PeerFeatures:
    """Features extraídas do histórico de tentativas."""
    addr: str
    # Sucesso
    connect_success_rate: float      # 0.0-1.0 (conectou / tentou)
    avg_handshake_ms: float          # latência média do ping
    blocks_contributed: int          # blocos que já trouxe
    # Tempo
    age_hours: float                 # desde a primeira vez que apareceu
    last_seen_hours_ago: float       # quão recente
    uptime_pct: float                # % do tempo que esteve online
    # Qualidade
    height_delta: int                # diferença de altura vs local
    version: int                     # versão do protocolo
    is_banned: bool
    is_tailscale: bool


def compute_peer_score(f: PeerFeatures) -> float:
    """
    Score 0-100. Determinístico (mesmos features = mesmo score).
    Fórmula auditável, sem LLM.
    """
    if f.is_banned:
        return 0.0

    score = 0.0

    # 1. Taxa de sucesso (peso: 30)
    score += f.connect_success_rate * 30

    # 2. Uptime (peso: 20)
    score += (f.uptime_pct / 100.0) * 20

    # 3. Recência (peso: 15) — decai exponencialmente
    recency = math.exp(-f.last_seen_hours_ago / 24.0)  # meia-vida: 24h
    score += recency * 15

    # 4. Latência (peso: 15) — log scale, 10ms=15pts, 1000ms=0pts
    if f.avg_handshake_ms > 0:
        latency_pts = max(0, 15 - 5 * math.log10(max(1, f.avg_handshake_ms / 10)))
        score += latency_pts

    # 5. Altura (peso: 10) — peers próximos são melhores
    score += max(0, 10 - abs(f.height_delta))

    # 6. Contribuição histórica (peso: 10) — teto em 100 blocos
    score += min(10, f.blocks_contributed / 10.0)

    # 7. Bônus Tailscale (+5) — mais estável que IP público
    if f.is_tailscale:
        score += 5

    # 8. Idade: peers novos têm penalidade (podem ser bots)
    if f.age_hours < 1:
        score *= 0.5
    elif f.age_hours < 6:
        score *= 0.8

    return round(min(100.0, score), 2)


def prioritize_peers(peers: list[dict], history: dict[str, dict],
                     local_height: int, top_n: int = 20) -> list[dict]:
    """
    Ordena peers por score e retorna os top_n.
    `history[addr]` = {"tries": N, "successes": M, "avg_ms": X, ...}
    """
    now = time.time()
    scored = []

    for p in peers:
        addr = p.get("address") or f"{p.get('ip')}:{p.get('port', 6001)}"
        h = history.get(addr, {})

        tries = h.get("tries", 0)
        successes = h.get("successes", 0)
        rate = (successes / tries) if tries > 0 else 0.5  # 0.5 = neutro

        first_seen = h.get("first_seen", now)
        age_hours = (now - first_seen) / 3600.0
        last_seen = h.get("last_seen", now)
        last_hours = (now - last_seen) / 3600.0

        # Uptime: assume 100% se nunca falhou, senão proporcional
        uptime_pct = rate * 100 if tries >= 3 else 75.0

        features = PeerFeatures(
            addr=addr,
            connect_success_rate=rate,
            avg_handshake_ms=h.get("avg_ms", 200.0),
            blocks_contributed=h.get("blocks", 0),
            age_hours=age_hours,
            last_seen_hours_ago=last_hours,
            uptime_pct=uptime_pct,
            height_delta=abs(p.get("height", 0) - local_height),
            version=p.get("version", 1),
            is_banned=h.get("banned", False),
            is_tailscale=addr.startswith("100."),
        )

        scored.append((compute_peer_score(features), p, features))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _score, p, _f in scored[:top_n]]
