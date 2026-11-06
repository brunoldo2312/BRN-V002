#!/usr/bin/env python3
"""
diag_fork.py — Descobre por que um nó está rejeitando blocos de outro.
"""
import os
import sys
import json
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

if os.path.exists("brn_network.env"):
    for line in open("brn_network.env", encoding="utf-8").read().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def get_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brn-diag/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}

def main():
    print("=" * 70)
    print(" 🩺 DIAGNÓSTICO DE FORK (POR QUE O BLOCO FOI REJEITADO)")
    print("=" * 70)

    from blockchain import Blockchain
    from trust_network import TrustNetwork
    
    # 1. Carrega o estado local
    try:
        bc = Blockchain("brn_v2_chain.db", auto_genesis=False)
        bc.set_trust_net(TrustNetwork("trust_network.json"))
        local_h = bc.db.height()
        print(f"[*] Altura Local: {local_h}")
    except Exception as e:
        print(f"[X] Erro ao abrir banco local: {e}")
        return

    # 2. Define de onde vamos puxar o bloco novo (Origem)
    # Substitua se o IP do seu Ubuntu for outro
    origin_url = "http://100.111.253.8:5000"
    
    # 3. Tenta baixar o próximo bloco (local_h + 1)
    target_h = local_h + 1
    print(f"[*] Baixando o bloco #{target_h} da Origem ({origin_url})...")
    
    remote_block = get_json(f"{origin_url}/api/block/{target_h}")
    if "error" in remote_block or "height" not in remote_block:
        print(f"[X] Não foi possível baixar o bloco #{target_h} da Origem.")
        return

    print(f"[*] Bloco baixado. Hash: {remote_block.get('hash')[:16]}...")

    # 4. Tenta validar o bloco localmente
    print(f"\n[*] TESTANDO VALIDAÇÃO DO BLOCO #{target_h}...")
    prev_block = bc.db.get_block(local_h)
    
    ok, msg = bc.validate_block(remote_block, prev_block=prev_block)
    
    print("=" * 70)
    if ok:
        print(f" ✅ O bloco #{target_h} É VÁLIDO.")
        print("    Se ele é válido, o seu SyncManager deveria ter baixado.")
        print("    Verifique se o sync_manager.py está rodando no main.py.")
    else:
        print(f" ❌ O bloco #{target_h} FOI REJEITADO PELO CONSENSO!")
        print(f"\n MOTIVO EXATO: {msg}")
        
        if "prev_hash" in msg:
            print("\n DIAGNÓSTICO: Fork real.")
            print("   O hash do bloco anterior do Ubuntu não bate com o seu.")
            print("   Solução: Você precisa apagar o banco local e sincronizar do zero para seguir o Ubuntu.")
        elif "pbft" in msg:
            print("\n DIAGNÓSTICO: Falha no PBFT.")
            print("   O bloco não tem os commits necessários ou o tempo expirou.")
        elif "SIG" in msg or "assinatura" in msg.lower():
            print("\n DIAGNÓSTICO: Falha de Multi-sig.")
            print("   O bloco do Ubuntu não tem as 3 assinaturas e o bypass não está ativo.")
    print("=" * 70)
    bc.db.close()

if __name__ == "__main__":
    main()
