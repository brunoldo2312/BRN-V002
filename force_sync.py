#!/usr/bin/env python3
"""
force_sync.py — Força a sincronização via HTTP ignorando o SyncManager.
"""
import os
import sys
import json
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

if os.path.exists("brn_network.env"):
    for line in open("brn_network.env", encoding="utf-8", errors="ignore").read().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

ORIGIN_URL = "http://100.111.253.8:5000"

def get_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brn-sync/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}

def main():
    print("=" * 60)
    print(" 🚀 FORCE SYNC (BYPASS SYNC_MANAGER)")
    print("=" * 60)

    from blockchain import Blockchain
    from trust_network import TrustNetwork

    print("[*] Abrindo banco local...")
    bc = Blockchain("brn_v2_chain.db", auto_genesis=False)
    bc.set_trust_net(TrustNetwork("trust_network.json"))
    
    local_h = bc.db.height()
    print(f"[*] Altura local atual: {local_h}")

    # Pega a altura do Ubuntu
    status = get_json(f"{ORIGIN_URL}/api/status")
    if "error" in status:
        print(f"[X] Origem inacessível: {status['error']}")
        return

    remote_h = status.get("height", 0)
    print(f"[*] Altura da Origem (Ubuntu): {remote_h}")

    if local_h >= remote_h:
        print("[OK] Já está sincronizado (ou à frente).")
        return

    # Baixa os blocos em lotes de 50
    batch_size = 50
    cursor = local_h + 1

    while cursor <= remote_h:
        end = min(cursor + batch_size, remote_h + 1)
        print(f"[*] Baixando blocos {cursor} até {end-1}...")
        
        data = get_json(f"{ORIGIN_URL}/api/blocks?start={cursor}&limit={batch_size}")
        if "error" in data or "blocks" not in data:
            print(f"[X] Falha ao baixar lote {cursor}-{end}: {data.get('error')}")
            break

        blocks = data["blocks"]
        if not blocks:
            print("[!] Origem retornou lista vazia.")
            break

        for blk in blocks:
            h = blk.get("height")
            ok, msg = bc.accept_block(blk)
            if ok:
                print(f"  [+] Bloco #{h} aplicado com sucesso.")
            else:
                print(f"  [X] Bloco #{h} rejeitado: {msg}")
                print("      Abortando sincronização para evitar corrupção.")
                bc.db.close()
                return

        cursor = end

    print("\n" + "=" * 60)
    print(f" ✅ Sincronização concluída! Nova altura: {bc.db.height()}")
    print("=" * 60)
    bc.db.close()

if __name__ == "__main__":
    main()
