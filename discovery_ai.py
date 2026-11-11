"""discovery_ai.py — Sugestão inteligente de bootstrap (v1.0)
================================================================
Persiste histórico de conexões e sugere os melhores peers para
usar como bootstrap. NÃO afeta consenso. Sem dependências externas.

Uso:
    from discovery_ai import DiscoveryAI
    dai = DiscoveryAI("peer_history.json")
    dai.record_attempt("100.1.1.1:6001", success=True, latency_ms=45)
    top = dai.suggest_bootstrap(top_n=5)
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

__version__ = "1.0"


class DiscoveryAI:
    def __init__(self, history_path: str = "peer_history.json"):
        self.path = Path(history_path)
        self._lock = threading.RLock()
        self.history: dict = self._load()

    def _load(self) -> dict:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = f"{self.path}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.history, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(self.path))
        except Exception:
            pass

    def record_attempt(self, addr: str, success: bool,
                       latency_ms: float = 0.0,
                       height: int = 0) -> None:
        if not addr:
            return
        with self._lock:
            now = time.time()
            h = self.history.setdefault(addr, {
                "tries": 0,
                "successes": 0,
                "avg_ms": 0.0,
                "first_seen": now,
                "latencies": [],
                "heights": [],
                "banned": False,
            })
            h["tries"] = int(h.get("tries", 0)) + 1
            if success:
                h["successes"] = int(h.get("successes", 0)) + 1
                # Média móvel exponencial
                prev = float(h.get("avg_ms", 0))
                h["avg_ms"] = prev * 0.8 + float(latency_ms) * 0.2
                # Histórico de latências (max 20)
                lats = h.get("latencies", [])
                lats.append(float(latency_ms))
                h["latencies"] = lats[-20:]
                if height > 0:
                    hs = h.get("heights", [])
                    hs.append(int(height))
                    h["heights"] = hs[-20:]
            h["last_seen"] = now

    def mark_banned(self, addr: str, reason: str = "") -> None:
        with self._lock:
            h = self.history.setdefault(addr, {})
            h["banned"] = True
            h["ban_reason"] = reason
            h["banned_at"] = time.time()
            self.save()

    def suggest_bootstrap(self, top_n: int = 5) -> list:
        """
        Retorna top_n melhores peers baseado em:
          - taxa de sucesso (≥ 3 sucessos)
          - recência (últimos 7 dias)
          - latência baixa
          - não banidos
        """
        now = time.time()
        candidates = []

        with self._lock:
            for addr, h in self.history.items():
                if h.get("banned"):
                    continue
                if int(h.get("successes", 0)) < 3:
                    continue
                last = float(h.get("last_seen", 0))
                if not last or (now - last) > 7 * 86400:
                    continue

                tries = max(1, int(h.get("tries", 1)))
                rate = int(h.get("successes", 0)) / tries
                recency = max(0.0, 1.0 - (now - last) / (7 * 86400))
                latency = float(h.get("avg_ms", 500))
                latency_penalty = max(0.1, 1.0 - latency / 5000.0)

                score = rate * recency * latency_penalty
                candidates.append((score, addr))

        candidates.sort(reverse=True)
        return [a for _s, a in candidates[:max(1, top_n)]]

    def stats(self) -> dict:
        with self._lock:
            total = len(self.history)
            good = sum(1 for h in self.history.values()
                       if int(h.get("successes", 0)) >= 3
                       and not h.get("banned"))
            banned = sum(1 for h in self.history.values() if h.get("banned"))
        return {"total": total, "good": good, "banned": banned}


if __name__ == "__main__":
    print("=" * 60)
    print(f" discovery_ai.py v{__version__} — self-test")
    print("=" * 60)

    import tempfile
    tmp = tempfile.mktemp(suffix=".json")

    dai = DiscoveryAI(tmp)
    now = time.time()

    # 5 tentativas: 5 boas
    for _ in range(5):
        dai.record_attempt("100.1.1.1:6001", success=True, latency_ms=40, height=5000)

    # 10 tentativas: 2 boas
    for i in range(10):
        dai.record_attempt("100.2.2.2:6001", success=(i < 2),
                           latency_ms=300 if i < 2 else 9999, height=5000)

    # 20 tentativas: 0 boas
    for _ in range(20):
        dai.record_attempt("100.3.3.3:6001", success=False, latency_ms=9999)

    dai.save()

    top = dai.suggest_bootstrap(top_n=2)
    print(f"  sugestão:    {top}")
    assert "100.1.1.1:6001" in top, "peer bom não foi sugerido"
    assert "100.3.3.3:6001" not in top, "peer morto foi sugerido"

    st = dai.stats()
    print(f"  stats:       {st}")
    assert st["total"] == 3

    # Recarrega do disco
    dai2 = DiscoveryAI(tmp)
    assert dai2.stats()["total"] == 3, "não persistiu no disco"

    try:
        os.unlink(tmp)
    except Exception:
        pass

    print("\n  [OK] Todos os testes passaram")
