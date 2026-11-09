import os
import json
import time

def patch_trust_network():
    filepath = "trust_network.py"
    if not os.path.exists(filepath):
        print("[X] trust_network.py não encontrado.")
        return

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Impede o remove_ghosts de apagar nós com score >= 500
    old_ghost = """                if age > max_age_s:
                    to_remove.append((nid, age))"""
    
    new_ghost = """                if age > max_age_s:
                    # [AUTO-FIX] Nunca apaga nós confiáveis
                    if n.trust_score >= 500:
                        continue
                    to_remove.append((nid, age))"""
    
    content = content.replace(old_ghost, new_ghost)

    # 2. Impede o _prune_inactive de apagar nós com score >= 500
    old_prune = """                if (now - n.last_seen) > NODE_TTL_SECONDS:
                    to_remove.append(nid)"""
                    
    new_prune = """                if (now - n.last_seen) > NODE_TTL_SECONDS:
                    # [AUTO-FIX] Nunca apaga nós confiáveis
                    if n.trust_score >= 500:
                        continue
                    to_remove.append(nid)"""

    content = content.replace(old_prune, new_prune)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print("[OK] trust_network.py corrigido (Proteção contra exclusão ativa).")

def forcar_mayra_no_json():
    filepath = "trust_network.json"
    MINHA_PK = "0248505dcf910a62775788220315a3ba02dab78f16f3cd83e37c3c16db896b9355"
    ENDERECO = "brn1ufzzzwu4ulwx5lygc671fky5nye7tk2wytfjv0"

    dados = {"nodes": {}}
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            try:
                dados = json.load(f)
            except:
                pass

    if "nodes" not in dados:
        dados["nodes"] = {}

    dados["nodes"][MINHA_PK] = {
        "node_id": MINHA_PK,
        "address": ENDERECO,
        "public_key": MINHA_PK,
        "peer_addr": "100.84.11.48:6001",
        "trust_score": 1000.0,
        "trust_level": "validator",
        "first_seen": time.time(),
        "last_seen": time.time(),
        "connected": True,
        "is_validator": True,
        "blocks_signed": 10,
        "blocks_validated": 10,
        "penalties": [],
        "banned": False
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2)
    
    if os.path.exists("trust_network.local.json"):
        with open("trust_network.local.json", "w", encoding="utf-8") as f:
            json.dump(dados, f, indent=2)
            
    print("[OK] Mayra injetada no trust_network.json do Bruno.")

if __name__ == "__main__":
    print("=" * 60)
    print(" 🛠️  CORREÇÃO DEFINITIVA NA ORIGEM (BRUNO)")
    print("=" * 60)
    patch_trust_network()
    forcar_mayra_no_json()
    print("=" * 60)
    print(" AGORA REINICIE O main.py DA ORIGEM!")
    print("=" * 60)
