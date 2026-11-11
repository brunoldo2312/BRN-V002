"""peer_anomaly.py — Detecção de peers zumbis (v1.0)
================================================================
Identifica peers que fingem estar online mas nunca entregam nada.
NÃO afeta consenso. Só remove do peers_discovered.json.

Uso:
    from peer_anomaly import detect_zombie_peer
    is_zombie, reason = detect_zombie_peer(history_entry)
"""
from __future__ import annotations

import time

__version__ = "1.0"


def detect_zombie_peer(history: dict) -> tuple:
    """
    Detecta peers zumbis baseado no histórico.

    Retorna (is_zombie: bool, reason: str).

    Regras determinísticas:
      1. Falhou > 20 tentativas sem sucesso
      2. Respondeu 1x e sumiu por 6h+
      3. Latência > 10s em 5 tentativas recentes
      4. Parado 100+ blocos atrás por > 10 tentativas
    """
    if not isinstance(history, dict):
        return False, ""

    tries = int(history.get("tries", 0))
    successes = int(history.get("successes", 0))
    now = time.time()

    # 1. Muitas tentativas, zero sucesso
    if tries > 20 and successes == 0:
        return True, f"falhou {tries}x sem sucesso"

    # 2. Respondeu 1x, sumiu
    if successes == 1 and tries > 10:
        last = float(history.get("last_seen", 0))
        if last > 0:
            hours = (now - last) / 3600.0
            if hours > 6:
                return True, f"respondeu 1x há {int(hours)}h e nunca mais"

    # 3. Latência explosiva
    latencies = history.get("latencies", [])
    if isinstance(latencies, list) and len(latencies) >= 5:
        recent = latencies[-5:]
        try:
            recent = [float(x) for x in recent]
            if all(x > 10000 for x in recent):
                avg = sum(recent) / len(recent)
                return True, f"latência {int(avg)}ms em 5 tentativas"
        except (ValueError, TypeError):
            pass

    # 4. Altura travada e muito atrás
    heights = history.get("heights", [])
    local_h = int(history.get("local_height", 0))
    if isinstance(heights, list) and len(heights) >= 10:
        try:
            heights_int = [int(x) for x in heights[-10:]]
            if (max(heights_int) - min(heights_int) < 2
                    and local_h > 0
                    and heights_int[-1] < local_h - 100):
                return True, f"parado em #{heights_int[-1]} (local #{local_h})"
        except (ValueError, TypeError):
            pass

    return False, ""


if __name__ == "__main__":
    print("=" * 60)
    print(f" peer_anomaly.py v{__version__} — self-test")
    print("=" * 60)

    now = time.time()

    # Teste 1: peer ativo (não é zumbi)
    active = {"tries": 50, "successes": 48, "last_seen": now,
              "latencies": [45, 50, 48, 42, 47], "heights": [5000, 5001, 5002, 5003, 5004]}
    z, r = detect_zombie_peer(active)
    print(f"  ativo:    zombie={z}  ({r or 'ok'})")
    assert not z

    # Teste 2: falhou sempre
    dead = {"tries": 30, "successes": 0, "last_seen": now}
    z, r = detect_zombie_peer(dead)
    print(f"  morto:    zombie={z}  ({r})")
    assert z

    # Teste 3: respondeu 1x, sumiu
    ghost = {"tries": 20, "successes": 1, "last_seen": now - 8 * 3600}
    z, r = detect_zombie_peer(ghost)
    print(f"  fantasma: zombie={z}  ({r})")
    assert z

    # Teste 4: latência explosiva
    slow = {"tries": 10, "successes": 5, "last_seen": now,
            "latencies": [12000, 15000, 13000, 11000, 14000]}
    z, r = detect_zombie_peer(slow)
    print(f"  lento:    zombie={z}  ({r})")
    assert z

    # Teste 5: altura travada atrás
    stalled = {"tries": 20, "successes": 18, "last_seen": now,
               "heights": [4800, 4801, 4800, 4801, 4800, 4801, 4800, 4801, 4800, 4801],
               "local_height": 5004}
    z, r = detect_zombie_peer(stalled)
    print(f"  travado:  zombie={z}  ({r})")
    assert z

    print("\n  [OK] Todos os testes passaram")
