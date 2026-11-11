# discovery_ai.py — recomenda peers com base em histórico compartilhado
"""
Funciona OFFLINE. Sem API externa.
Usa histórico local + peers conhecidos para sugerir "super peers".
"""
import json
from pathlib import Path


class DiscoveryAI:
    def __init__(self, history_path: str = "peer_history.json"):
        self.path = Path(history_path)
        self.history: dict[str, dict] = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self.history, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def record_attempt(self, addr: str, success: bool, latency_ms: float = 0.0) -> None:
        h = self.history.setdefault(addr, {
            "tries": 0, "successes": 0, "avg_ms": 0.0,
            "first_seen": __import__("time").time(),
            "latencies": [], "heights": [],
        })
        h["tries"] += 1
        if success:
            h["successes"] += 1
            # Média móvel exponencial
            h["avg_ms"] = h["avg_ms"] * 0.8 + latency_ms * 0.2
            h["latencies"] = (h["latencies"] + [latency_ms])[-20:]
        h["last_seen"] = __import__("time").time()

    def suggest_bootstrap(self, top_n: int = 5) -> list[str]:
        """
        Retorna os top_n melhores peers para usar como bootstrap.
        Ordenados por: sucesso × recência × baixa latência.
        """
        import time
        now = time.time()
        candidates = []

        for addr, h in self.history.items():
            if h.get("successes", 0) < 3:
                continue
            hours_since = (now - h.get("last_seen", 0)) / 3600
            if hours_since > 168:  # 1 semana
                continue

            # Score: sucesso% × recência × baixa latência
            rate = h["successes"] / max(1, h["tries"])
            recency = max(0, 1 - hours_since / 168)
            latency_penalty = max(0.1, 1 - h.get("avg_ms", 200) / 5000)
            score = rate * recency * latency_penalty

            candidates.append((score, addr))

        candidates.sort(reverse=True)
        return [addr for _score, addr in candidates[:top_n]]
