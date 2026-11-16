# consensus_manager.py
import sqlite3
import json
import time
import os
import logging
from auto_discovery import get_tailscale_peers

logging.basicConfig(level=logging.INFO, format="[CONSENSUS] %(asctime)s - %(message)s")
log = logging.getLogger("consensus")

DB_PATH = "brn_v2_chain.db"
TRUST_FILE = "trust_network.json"
MAX_VALIDATORS = 5

def heal_network():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT MAX(height) FROM blocks")
        row = cur.fetchone()
        current_height = row[0] if row and row[0] is not None else -1
        conn.close()
    except:
        return

    if current_height < 0:
        return

    peers = get_tailscale_peers()[:MAX_VALIDATORS]
    if not peers:
        return

    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cur = conn.cursor()
        cur.execute("DELETE FROM peers")
        for ip in peers:
            cur.execute("""
                INSERT INTO peers (
                    address, node_id, genesis_hash, version, height, 
                    is_miner, public_key, metadata, first_seen, last_seen, score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ip, ip, 'genesis', '1.0', current_height, 1, ip, '{}', time.time(), time.time(), 150.0))
        conn.commit()
        conn.close()
        log.info(f"✅ {len(peers)} validadores forçados na altura {current_height}.")
    except Exception as e:
        log.error(f"Erro no banco: {e}")

    try:
        trust_data = {"nodes": {}}
        for ip in peers:
            ip_only = ip.split(':')[0]
            for key in [ip, ip_only]:
                trust_data["nodes"][key] = {
                    "trust_score": 150.0,
                    "status": "trusted",
                    "last_seen": time.time()
                }
        with open(TRUST_FILE, 'w') as f:
            json.dump(trust_data, f, indent=2)
    except Exception as e:
        log.error(f"Erro no trust network: {e}")

if __name__ == "__main__":
    log.info("🛡️ Gerenciador de Consenso iniciado. Curando a rede a cada 10s...")
    while True:
        heal_network()
        time.sleep(10)
