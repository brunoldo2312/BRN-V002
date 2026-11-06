#!/usr/bin/env python3
"""
diagnostico_completo.py — Raio-X da rede BRN.
"""
import os
import sys
import json
import socket
import urllib.request
import urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# Carrega .env
if os.path.exists("brn_network.env"):
    for line in open("brn_network.env", encoding="utf-8", errors="ignore").read().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

API = "http://127.0.0.1:5000"

def get_json(path):
    try:
        req = urllib.request.Request(f"{API}{path}", headers={"User-Agent": "brn-diag/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}

def test_tcp(ip, port):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect((ip, port))
        s.close()
        return True
    except:
        return False

def main():
    print("=" * 75)
    print(" 🩺 DIAGNÓSTICO COMPLETO DA REDE BRN")
    print("=" * 75)

    # 1. Verifica o Banco Local
    print("\n▶ [1] ESTADO DO BANCO LOCAL (SQLITE)")
    try:
        from db import ChainDB
        db = ChainDB("brn_v2_chain.db")
        h = db.height()
        tip = db.tip_hash()
        peers_db = db.listar_peers(apenas_ativos=False)
        db.close()
        print(f"  Altura no banco     : {h}")
        print(f"  Tip hash            : {tip[:24]}...")
        print(f"  Peers na tabela     : {len(peers_db)}")
    except Exception as e:
        print(f"  \033[91m[X] Erro ao abrir banco: {e}\033[0m")
        return

    # 2. API Local
    print("\n▶ [2] API LOCAL DO NÓ")
    status = get_json("/api/status")
    if "error" in status:
        print(f"  \033[91m[X] API offline: {status['error']}\033[0m")
        print("      Verifique se o main.py está rodando.")
        return
    print(f"  Altura via API      : {status.get('height')}")
    print(f"  Peers ativos        : {status.get('peers')}")

    sync = get_json("/api/sync/status")
    if "error" not in sync:
        print(f"  Sync status         : {sync.get('syncing')} (Alvo: {sync.get('target_height')})")

    # 3. Teste P2P Direto com os IPs conhecidos
    print("\n▶ [3] TESTE P2P REAL COM PEERS (Porta 6001)")
    known_ips = [
        "100.84.11.48",   # Mayra
        "100.64.151.67",  # Filipe
        "100.70.107.15",  # Desktop 61ltr
        "100.111.253.8",  # Ubuntu Origem
        "192.168.0.9",
        "192.168.0.7"
    ]

    try:
        from p2p_unified import P2PClient, CHAIN_ID
    except ImportError:
        print("  \033[91m[X] p2p_unified.py não encontrado.\033[0m")
        return

    print(f"  {'IP DO PEER':<18} {'TCP 6001':<10} {'ALTURA REAL (P2P)':<20} {'STATUS'}")
    print("  " + "-" * 65)

    for ip in known_ips:
        if ip in os.environ.get("BRN_SEU_ENDERECO", ""):
            continue # Pula o próprio IP
            
        tcp_ok = test_tcp(ip, 6001)
        tcp_str = "\033[92m[OK]\033[0m" if tcp_ok else "\033[91m[FECHADO]\033[0m"
        
        real_h = "?"
        status_msg = ""
        
        if tcp_ok:
            try:
                msg = {"type": "get_chain_height", "_chain": CHAIN_ID, "_port": 6001}
                resp, lat = P2PClient.send_message(ip, 6001, msg, timeout=5.0)
                if resp:
                    real_h = resp.get("height", -1)
                    tip = resp.get("tip", "")[:12]
                    if real_h > h:
                        status_msg = "\033[93m[A FRENTE]\033[0m"
                    elif real_h < h:
                        status_msg = "\033[91m[ATRASADO]\033[0m"
                    else:
                        status_msg = "\033[92m[SYNC]\033[0m"
                else:
                    status_msg = "\033[91m[Sem resposta JSON]\033[0m"
            except Exception as e:
                status_msg = f"\033[91m[Erro P2P: {e}]\033[0m"
        else:
            status_msg = "\033[90m[Offline ou Firewall]\033[0m"

        print(f"  {ip:<18} {tcp_str:<18} {str(real_h):<20} {status_msg}")

    print("\n" + "=" * 75)
    print(" ✅ Fim do diagnóstico.")
    print("=" * 75)

if __name__ == "__main__":
    main()
