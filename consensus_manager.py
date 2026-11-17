# consensus_manager.py — Gerenciador de Consenso BRN (v2.0)
# ============================================================
# v2.0 (2026-11-17) — REESCRITA SEGURA:
#   [FIX CRÍTICO] NÃO apaga mais todos os peers (DELETE FROM peers removido)
#   [FIX CRÍTICO] NÃO forja validadores: IP NÃO é public_key, score NÃO é 150 inventado
#   [FIX] NÃO sobrescreve trust_network.json — só ADICIONA nós novos com score 0
#   [FIX] Usa ChainDB (db.py) em vez de sqlite3 cru com colunas inexistentes
#         (metadata / first_seen / score não existem na tabela peers)
#   [FIX] Loop com parada limpa (Ctrl+C) e log de verdade, sem except pelado
#   [KEEP] Descobre peers via auto_discovery.get_tailscale_peers()
# ============================================================
from __future__ import annotations

import json
import logging
import signal
import time
from pathlib import Path

from db import ChainDB

logging.basicConfig(level=logging.INFO, format="[CONSENSUS] %(asctime)s - %(message)s")
log = logging.getLogger("consensus")

TRUST_FILE = "trust_network.json"
MAX_VALIDATORS = 5
INTERVAL_S = 10

_stop = False

def _request_stop(signum, frame):
    global _stop
    _stop = True
    log.info("Parada solicitada (sinal %s) — encerrando...", signum)

def _load_trust() -> dict:
    p = Path(TRUST_FILE)
    if not p.exists():
        return {"nodes": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"nodes": {}}
        data.setdefault("nodes", {})
        return data
    except Exception as e:
        log.warning("trust_network.json corrompido (%s) — começando vazio", e)
        return {"nodes": {}}

def _save_trust(data: dict) -> None:
    Path(TRUST_FILE).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )

def heal_network() -> dict:
    """
    Cura SEM destruir:
      1) Lê altura atual do ChainDB
      2) Descobre peers Tailscale
      3) Só ADICIONA peer que ainda não existe (upsert com public_key vazia,
         sem forjar pubkey/score)
      4) Só ADICIONA nó novo no trust_network.json com score 0 (não confiável
         até provar que assina blocos de verdade)
    Retorna dict com o que foi feito.
    """
    db = ChainDB()
    current_height = db.height()
    if current_height < 0:
        log.debug("Chain vazia (height=-1) — aguardando gênesis/sync")
        return {"height": -1, "discovered": 0, "added_db": 0, "added_trust": 0}

    try:
        from auto_discovery import get_tailscale_peers
        peers = get_tailscale_peers() or []
    except Exception as e:
        log.warning("get_tailscale_peers falhou: %s", e)
        return {"height": current_height, "discovered": 0, "added_db": 0, "added_trust": 0}

    peers = peers[:MAX_VALIDATORS]
    if not peers:
        return {"height": current_height, "discovered": 0, "added_db": 0, "added_trust": 0}

    # Peers que JÁ existem — não vamos duplicar nem apagar
    try:
        existentes = {p.get("address") for p in db.listar_peers(apenas_ativos=False)}
        existentes |= {p.get("node_id") for p in db.listar_peers(apenas_ativos=False)}
    except Exception:
        existentes = set()

    added_db = 0
    for ip in peers:
        ip = str(ip).strip()
        if not ip or ip in existentes:
            continue
        try:
            # NÃO inventa public_key nem score.
            # public_key="" -> peers_menu ignora até o peer provar identidade real via P2P.
            db.upsert_peer(
                node_id=ip,
                address=ip,
                genesis_hash="",
                version="",
                height=current_height,
                is_miner=False,
                public_key="",
            )
            added_db += 1
        except Exception as e:
            log.warning("upsert_peer(%s) falhou: %s", ip, e)

    # Trust: só ADICIONA, nunca sobrescreve / nunca dá score inventado
    trust = _load_trust()
    nodes = trust.get("nodes", {})
    added_trust = 0
    for ip in peers:
        ip = str(ip).strip()
        ip_only = ip.split(":")[0]
        for key in {ip, ip_only}:
            if key not in nodes:
                nodes[key] = {
                    "trust_score": 0.0,  # 0 = desconhecido, ganha trust assinando blocos
                    "status": "discovered",  # NÃO "trusted" — trusted só com prova
                    "last_seen": time.time(),
                    "source": "tailscale_discovery",
                }
                added_trust += 1

    if added_trust > 0:
        try:
            trust["nodes"] = nodes
            _save_trust(trust)
        except Exception as e:
            log.error("Erro ao salvar trust_network.json: %s", e)

    if added_db or added_trust:
        log.info(
            "✅ Cura: altura %d | descobertos %d | +%d peers no DB | +%d nós no trust",
            current_height, len(peers), added_db, added_trust,
        )
    return {
        "height": current_height,
        "discovered": len(peers),
        "added_db": added_db,
        "added_trust": added_trust,
    }

def main():
    signal.signal(signal.SIGINT, _request_stop)
    try:
        signal.signal(signal.SIGTERM, _request_stop)
    except Exception:
        pass
    log.info("🛡️ Gerenciador de Consenso v2.0 iniciado. Curando a rede a cada %ds...", INTERVAL_S)
    log.info("Modo SEGURO: nunca apaga peers, nunca forja validador, nunca inventa trust_score.")
    while not _stop:
        try:
            heal_network()
        except Exception as e:
            log.error("heal_network falhou: %s", e)
        # Sleep em fatias de 1s pra responder rápido ao Ctrl+C
        for _ in range(INTERVAL_S):
            if _stop:
                break
            time.sleep(1)
    log.info("Gerenciador de Consenso encerrado.")

if __name__ == "__main__":
    main()
