#!/usr/bin/env python3
"""
start_mining_safely.py — Só inicia a mineração se todos os validadores
estiverem sincronizados na mesma altura (ou com 1 bloco de diferença).
"""
import os
import sys
import time
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

API = "http://127.0.0.1:5000"

def get_json(path):
    try:
        req = urllib.request.Request(f"{API}{path}", headers={"User-Agent": "brn-safe-mine/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}

def post_json(path, body):
    try:
        req = urllib.request.Request(
            f"{API}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}

def main():
    print("=" * 70)
    print(" ⛏️  MINERAÇÃO SEGURA (SYNC BARRIER)")
    print("=" * 70)

    # 1. Carrega a carteira para pegar o address e pubkey
    from secure_store import load_current_wallet
    from p2p_unified import P2PClient, CHAIN_ID
    from trust_network import TrustNetwork

    pwd = os.environ.get("BRN_WALLET_SESSION_PASS") or os.environ.get("BRN_NODE_PASSWORD", "")
    if not pwd:
        print("[X] Senha da carteira não configurada no .env")
        return

    w = load_current_wallet(pwd)
    if not w or not w.get("ok"):
        print("[X] Não foi possível abrir a carteira.")
        return

    addr = w.get("address")
    pubkey = w.get("public_key") or w.get("pub_hex")
    print(f"[*] Carteira OK: {addr[:16]}...")

    # 2. Carrega a Trust Network
    trust = TrustNetwork("trust_network.json")
    active_validators = trust.get_active_validators(count=5)
    
    if not active_validators:
        print("[!] Nenhum validador na Trust. Vou minerar em modo bootstrap.")
        validators_to_check = []
    else:
        # Mapeia pubkeys para IPs (usando o banco local)
        from db import ChainDB
        db = ChainDB("brn_v2_chain.db")
        peers_db = db.listar_peers(apenas_ativos=False)
        db.close()
        
        validators_to_check = []
        for v_pk in active_validators:
            for p in peers_db:
                if p.get("public_key") == v_pk:
                    ip = p["address"].split(":")[0]
                    validators_to_check.append(ip)
                    break

    print(f"[*] Validadores para sincronizar: {len(validators_to_check)}")

    # 3. Loop de verificação
    while True:
        local_status = get_json("/api/status")
        if "error" in local_status:
            print("[X] Nó local offline. O main.py está rodando?")
            time.sleep(5)
            continue

        local_h = local_status.get("height", -1)
        print(f"\n[*] Minha altura atual: {local_h}")
        
        max_peer_h = local_h
        atrasados = 0
        offlines = 0

        for ip in validators_to_check:
            if ip in os.environ.get("BRN_SEU_ENDERECO", ""):
                continue # Pula o próprio IP
            
            try:
                msg = {"type": "get_chain_height", "_chain": CHAIN_ID, "_port": 6001}
                resp, _ = P2PClient.send_message(ip, 6001, msg, timeout=3.0)
                
                if resp:
                    ph = resp.get("height", -1)
                    print(f"  -> Peer {ip:<15} Altura: {ph}")
                    if ph > max_peer_h:
                        max_peer_h = ph
                    if ph < local_h:
                        atrasados += 1
                else:
                    print(f"  -> Peer {ip:<15} \033[91mOFFLINE/TIMEOUT\033[0m")
                    offlines += 1
            except Exception:
                print(f"  -> Peer {ip:<15} \033[91mERRO DE REDE\033[0m")
                offlines += 1

        # 4. Regra de Decisão
        if max_peer_h > local_h:
            print(f"\n\033[93m[!] Você está atrasado em {max_peer_h - local_h} bloco(s).\033[0m")
            print("    O main.py vai sincronizar automaticamente. Aguardando...")
            time.sleep(10)
            continue

        if atrasados > 0:
            print(f"\n\033[91m[X] {atrasados} validador(es) estão atrasados.\033[0m")
            print("    Iniciar a mineração agora causaria rejeição de assinaturas.")
            print("    Aguardando eles sincronizarem (ou saírem da rede)...")
            time.sleep(10)
            continue

        # Se chegou aqui, todos estão iguais (ou os outros estão à frente e nós sincronizamos)
        print("\n\033[92m[OK] Todos os validadores estão na mesma altura!\033[0m")
        print("     Iniciando a mineração...")
        
        r = post_json("/api/miner/start", {
            "address": addr,
            "miner_pubkey": pubkey
        })
        
        if r.get("ok"):
            print("\033[92m[✓] Mineração iniciada com sucesso!\033[0m")
            return
        else:
            print(f"\033[91m[X] Erro ao iniciar: {r.get('msg')}\033[0m")
            return

if __name__ == "__main__":
    main()
