#!/usr/bin/env python3
"""
test_pool_windows.py — Diagnóstico de conexão do Cliente (Windows) ao Pool (Ubuntu).
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

POOL_URL = os.environ.get("BRN_POOL_URL", "").rstrip("/")

def test_tcp(host, port=5000):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect((host, port))
        s.close()
        return True
    except Exception as e:
        return False

def main():
    print("=" * 70)
    print(" 🔍 DIAGNÓSTICO DO CLIENTE (WINDOWS) -> POOL")
    print("=" * 70)

    if not POOL_URL:
        print("\n\033[91m[X] ERRO CRÍTICO: BRN_POOL_URL está vazio no seu brn_network.env!\033[0m")
        print("    O Windows não sabe para onde enviar os shares.")
        print("    Solução: Abra o .env e adicione: BRN_POOL_URL=http://100.111.253.8:5000")
        return

    print(f"\n▶ [1] URL configurada no .env: {POOL_URL}")
    if "127.0.0.1" in POOL_URL or "localhost" in POOL_URL:
        print("\033[93m[!] AVISO: Você está apontando para 127.0.0.1.\033[0m")
        print("    Isso significa que o Windows está tentando se conectar a ele mesmo, e não à Origem.")
        print("    Mude para: BRN_POOL_URL=http://100.111.253.8:5000")

    # Extrai IP
    try:
        host = POOL_URL.replace("http://", "").replace("https://", "").split(":")[0]
    except:
        host = "desconhecido"

    print(f"\n▶ [2] Testando conexão TCP com {host}:5000...")
    if test_tcp(host, 5000):
        print(f"  \033[92m[OK] Porta 5000 acessível.\033[0m")
    else:
        print(f"  \033[91m[X] Falha ao conectar na porta 5000.\033[0m")
        print("      Possíveis causas: O main.py do Ubuntu não está rodando, ou o Firewall do Ubuntu está bloqueando o IP.")
        return

    # Verifica a carteira local
    print(f"\n▶ [3] Verificando carteira local...")
    address = ""
    pubkey = ""
    try:
        from secure_store import load_current_wallet
        pwd = os.environ.get("BRN_WALLET_SESSION_PASS") or os.environ.get("BRN_NODE_PASSWORD")
        if not pwd:
            print("  \033[91m[X] Sem senha no .env.\033[0m")
        else:
            w = load_current_wallet(pwd)
            if w and w.get("ok"):
                address = w.get("address")
                pubkey = w.get("public_key") or w.get("pub_hex")
                print(f"  \033[92m[OK] Carteira aberta: {address}\033[0m")
            else:
                print(f"  \033[91m[X] Carteira trancada (senha errada?)\033[0m")
    except Exception as e:
        print(f"  \033[91m[X] Erro ao ler carteira: {e}\033[0m")

    if not address or not pubkey:
        print("      O Pool vai rejeitar a conexão sem um endereço e pubkey válidos.")
        return

    # Testa o registro
    print(f"\n▶ [4] Tentando registrar no Pool...")
    payload = {
        "address": address,
        "pubkey": pubkey,
        "miner_id": "win_diag_" + os.urandom(4).hex()
    }
    
    try:
        req = urllib.request.Request(
            f"{POOL_URL}/api/pool/register",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "brn-test/1.0"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            resp_status = r.status
            resp_data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        resp_status = e.code
        try:
            resp_data = json.loads(e.read().decode())
        except:
            resp_data = {"error": "Corpo do erro vazio"}
    except Exception as e:
        resp_status = -1
        resp_data = {"error": str(e)}

    print(f"  Status HTTP : {resp_status}")
    print(f"  Resposta    : {json.dumps(resp_data)}")
    
    print("\n" + "=" * 70)
    if resp_status == 200 and resp_data.get("ok"):
        print(" ✅ SUCESSO! O Windows conseguiu se registrar no Pool do Ubuntu.")
        print("    Se o main.py ainda não está minerando, reinicie-o.")
    else:
        print(" ❌ FALHOU. O motivo está na resposta acima.")
        if resp_status == 401 or resp_status == 403:
             print("    -> Pode ser bloqueio de ADMIN_TOKEN ou firewall.")
        elif resp_status == -1:
             print("    -> O servidor caiu no meio da requisição.")
    print("=" * 70)

if __name__ == "__main__":
    main()
