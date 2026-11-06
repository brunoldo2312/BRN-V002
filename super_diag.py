#!/usr/bin/env python3
"""
super_diag.py — Diagnóstico Completo de Consenso, Assinaturas e Rede.
"""
import os
import sys
import json
import time
import socket
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# Carrega .env
if os.path.exists("brn_network.env"):
    for line in open("brn_network.env", encoding="utf-8", errors="ignore").read().splitlines():
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

def check_tcp(ip, port=6001):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((ip, port))
        s.close()
        return True
    except:
        return False

def main():
    print("=" * 75)
    print(" 🩺 SUPER DIAGNÓSTICO DE CONSENSO E ASSINATURA")
    print("=" * 75)

    # 1. CONFIGURAÇÃO LOCAL
    print("\n▶ [1] CONFIGURAÇÃO DO .ENV")
    print(f"  BRN_IS_ORIGIN        : {os.environ.get('BRN_IS_ORIGIN', '0')}")
    print(f"  BRN_ASSUME_VALID_UNTIL: {os.environ.get('BRN_ASSUME_VALID_UNTIL', '0')}")
    print(f"  BRN_QUORUM           : {os.environ.get('BRN_VALIDATOR_QUORUM', '0.67')}")
    
    pwd = os.environ.get("BRN_WALLET_SESSION_PASS") or os.environ.get("BRN_NODE_PASSWORD")
    if not pwd:
        print("  \033[91m[X] ERRO: BRN_WALLET_SESSION_PASS ou BRN_NODE_PASSWORD não configurado.\033[0m")
    else:
        print(f"  Senha da carteira    : [OK] (configurada)")

    # 2. CARTEIRA LOCAL
    print("\n▶ [2] CARTEIRA LOCAL (CHAVE PRIVADA)")
    local_pubkey = ""
    try:
        from secure_store import load_current_wallet
        if pwd:
            w = load_current_wallet(pwd)
            if w and w.get("ok"):
                local_pubkey = (w.get("public_key") or w.get("pub_hex") or "").lower()
                print(f"  \033[92m[OK] Carteira aberta!\033[0m")
                print(f"  Pubkey: {local_pubkey[:32]}...")
            else:
                print(f"  \033[91m[X] Carteira não abriu (senha errada ou arquivo inexistente).\033[0m")
        else:
            print("  \033[91m[X] Impossível testar sem senha.\033[0m")
    except Exception as e:
        print(f"  \033[91m[X] Erro ao ler carteira: {e}\033[0m")

    # 3. REDE E ALTURAS
    print("\n▶ [3] REDE P2P E ALTURAS")
    try:
        from blockchain import Blockchain
        from p2p_unified import P2PClient, CHAIN_ID
        bc = Blockchain("brn_v2_chain.db", auto_genesis=False)
        local_h = bc.db.height()
        print(f"  Altura Local : {local_h}")
        
        # Pega o bloco atual para o teste de assinatura
        test_block = bc.db.get_block(local_h)
        bc.db.close()
    except Exception as e:
        print(f"  \033[91m[X] Erro ao abrir banco: {e}\033[0m")
        return

    if not test_block:
        print("  \033[91m[X] Blockchain vazia. Minere pelo menos o genesis.\033[0m")
        return

    # Lista de IPs (adapte se necessário)
    peers = [
        "100.111.253.8",  # Ubuntu Origem
        "100.84.11.48",   # Mayra
        "100.64.151.67",  # Filipe
        "100.70.107.15",  # Desktop 61ltr
    ]
    
    # Remove o próprio IP da lista (se configurado)
    meu_ip = os.environ.get("BRN_SEU_ENDERECO", "").split(":")[0]
    
    valid_peers = []
    for ip in peers:
        if ip == meu_ip: continue
        
        if check_tcp(ip):
            try:
                msg = {"type": "get_chain_height", "_chain": CHAIN_ID, "_port": 6001}
                resp, _ = P2PClient.send_message(ip, 6001, msg, timeout=3.0)
                if resp:
                    h = resp.get("height", -1)
                    status = "\033[92m[SYNC]\033[0m" if h == local_h else f"\033[91m[DIFF: {h}]\033[0m"
                    print(f"  {ip:<16} TCP: OK | Altura: {h:<5} {status}")
                    valid_peers.append(ip)
            except Exception:
                print(f"  {ip:<16} TCP: OK | P2P falhou")
        else:
            print(f"  {ip:<16} TCP: \033[91m[FECHADO]\033[0m (Firewall ou offline)")

    # 4. TRUST NETWORK
    print("\n▶ [4] TRUST NETWORK E VALIDADORES")
    try:
        from trust_network import TrustNetwork
        tn = TrustNetwork("trust_network.json")
        print(f"  Nós no Trust Network: {len(tn.nodes)}")
        
        validators = tn.get_active_validators(count=10)
        if local_pubkey and local_pubkey not in validators:
            print(f"  \033[91m[X] ATENÇÃO: Sua chave não está na lista de validadores!\033[0m")
        elif local_pubkey:
            print(f"  \033[92m[OK] Sua chave está na lista de validadores.\033[0m")

    except Exception as e:
        print(f"  \033[91m[X] Erro no TrustNetwork: {e}\033[0m")

    # 5. TESTE REAL DE ASSINATURA
    print("\n▶ [5] TESTE REAL DE ASSINATURA (SIGN_BLOCK)")
    print(f"  Enviando bloco #{test_block.get('height')} para os peers...")
    print("  " + "-" * 60)

    if not valid_peers:
        print("  Nenhum peer válido para testar.")
        return

    for ip in valid_peers:
        try:
            msg = {"type": "sign_block", "block": test_block, "_chain": CHAIN_ID, "_port": 6001}
            resp, lat = P2PClient.send_message(ip, 6001, msg, timeout=8.0)
            
            if not resp:
                print(f"  {ip:<16} \033[91mSEM RESPOSTA (timeout)\033[0m")
                continue
                
            pk = (resp.get("pubkey") or "").strip()
            err = (resp.get("error") or "").strip()
            
            if pk:
                print(f"  {ip:<16} \033[92m[ASSINOU]\033[0m pk={pk[:16]}...")
            elif err:
                print(f"  {ip:<16} \033[91m[RECUSOU]\033[0m Motivo: {err}")
            else:
                print(f"  {ip:<16} \033[93m[RESPOSTA VAZIA]\033[0m")
        except Exception as e:
            print(f"  {ip:<16} \033[91m[ERRO DE REDE] {e}\033[0m")

    print("\n" + "=" * 75)
    print(" ✅ Fim do diagnóstico.")
    print("=" * 75)

if __name__ == "__main__":
    main()
