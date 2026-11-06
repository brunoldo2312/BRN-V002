#!/usr/bin/env python3
"""
test_pool_register.py — Descobre por que o Pool está rejeitando a conexão.
"""
import os
import sys
import json
import urllib.request
import urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# Carrega .env
if os.path.exists("brn_network.env"):
    for line in open("brn_network.env", encoding="utf-8").read().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

POOL_URL = os.environ.get("BRN_POOL_URL", "http://127.0.0.1:5000").rstrip("/")

def get_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brn-test/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except:
            return e.code, {"error": "Não foi possível ler a resposta do erro"}
    except Exception as e:
        return -1, {"error": str(e)}

def main():
    print("=" * 70)
    print(" 🔍 DIAGNÓSTICO DE CONEXÃO AO POOL (REGISTER)")
    print("=" * 70)
    print(f" Alvo do Pool (BRN_POOL_URL) : {POOL_URL}")
    
    # 1. Verifica se o servidor está online e com o Pool ativo
    print(f"\n▶ [1] Testando saúde do servidor Origem...")
    status, health = get_json(f"{POOL_URL}/health")
    
    if status != 200:
        print(f"  \033[91m[X] Servidor inacessível (Status {status})\033[0m")
        print(f"      Detalhe: {health}")
        print("      Verifique se o main.py está rodando na Origem e o firewall liberado.")
        return
        
    print(f"  \033[92m[OK] Servidor respondeu HTTP 200\033[0m")
    pool_injected = health.get("pool_injected", False)
    
    if pool_injected:
        print(f"  \033[92m[OK] PoolManager está ativo no servidor (pool_injected=True)\033[0m")
    else:
        print(f"  \033[91m[X] PoolManager NÃO está ativo no servidor (pool_injected=False)\033[0m")
        print("      Motivo: O nó Origem não iniciou o PoolManager.")
        print("      Solução: Verifique se BRN_IS_ORIGIN=1 e BRN_POOL_ENABLED=1 no .env da Origem.")

    # 2. Tenta pegar uma carteira local válida para o teste
    address = "brn1qxyzk7y0v2j4g0a8d9n5t2k7h4s6w8c9p2e" # Fallback mock
    pubkey = "02" + "0" * 64 # Fallback mock
    
    try:
        from secure_store import load_current_wallet
        pwd = os.environ.get("BRN_WALLET_SESSION_PASS") or os.environ.get("BRN_NODE_PASSWORD")
        if pwd:
            w = load_current_wallet(pwd)
            if w and w.get("ok"):
                address = w.get("address", address)
                pubkey = w.get("public_key") or w.get("pub_hex") or pubkey
                print(f"\n▶ [2] Usando carteira local para o teste: {address}")
            else:
                print(f"\n▶ [2] Carteira local trancada. Usando dados mock para o teste.")
        else:
            print(f"\n▶ [2] Sem senha no .env. Usando dados mock para o teste.")
    except Exception as e:
        print(f"\n▶ [2] Erro ao ler carteira local ({e}). Usando dados mock.")

    # 3. Envia o pedido de registro
    print(f"\n▶ [3] Tentando REGISTRAR no Pool...")
    payload = {
        "address": address,
        "pubkey": pubkey,
        "miner_id": "test_diag_" + os.urandom(4).hex()
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

    # 4. Analisa a resposta
    print(f"  Status HTTP : {resp_status}")
    print(f"  Resposta    : {json.dumps(resp_data)}")
    
    print("\n" + "=" * 70)
    if resp_status == 200 and resp_data.get("ok"):
        print(" ✅ O POOL ACEITOU A CONEXÃO!")
        print("    Se os clientes reais falham, pode ser problema de rede (Tailscale/LAN).")
    else:
        print(" ❌ O POOL REJEITOU A CONEXÃO.")
        msg = resp_data.get("msg", "Erro desconhecido")
        print(f"    MOTIVO EXATO: {msg}")
        
        if "address invalido" in msg:
            print("    -> A carteira do cliente não está no formato 'brn1...'.")
        elif "pubkey obrigatoria" in msg:
            print("    -> O cliente não enviou a Pubkey.")
        elif "pool nao disponivel" in msg:
            print("    -> O servidor não tem PoolManager e não configurou BRN_POOL_URL.")
        elif "503" in str(resp_status):
            print("    -> O servidor está sobrecarregado ou o PoolManager travou.")
    print("=" * 70)

if __name__ == "__main__":
    main()
