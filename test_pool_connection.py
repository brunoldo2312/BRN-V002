#!/usr/bin/env python3
"""
test_pool_connection.py — Testa a conexão e o fluxo de trabalho do Pool BRN.
Verifica se o PoolManager está ativo e se o PoolClient está enviando shares.
"""
import os
import sys
import json
import time
import urllib.request
from pathlib import Path

API_LOCAL = "http://127.0.0.1:5000"
API_POOL = os.environ.get("BRN_POOL_URL", "http://127.0.0.1:5000").rstrip("/")

def get_json(base_url, path):
    try:
        req = urllib.request.Request(f"{base_url}{path}", headers={"User-Agent": "brn-test/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}

def main():
    print("=" * 70)
    print(" 🔍 TESTE DE CONEXÃO DO POOL BRN")
    print("=" * 70)
    
    # =========================================================
    # 1. TESTANDO O LADO DO CLIENTE (Local)
    # =========================================================
    print(f"\n▶ [1] STATUS DO SEU NÓ (PoolClient Local)")
    local_pool = get_json(API_LOCAL, "/api/pool/local_status")
    
    if "error" in local_pool:
        print(f"  [X] API local offline ({local_pool['error']})")
    else:
        running = local_pool.get("running", False)
        status_str = "\033[92mMINERANDO\033[0m" if running else "\033[93mPARADO\033[0m"
        print(f"  Status do PoolClient : {status_str}")
        print(f"  Conectado ao Pool em : {local_pool.get('pool_url', '?')}")
        print(f"  Hashrate local       : {local_pool.get('hashrate_khs', 0):.2f} kH/s")
        
        sub = local_pool.get("shares_submitted", 0)
        acc = local_pool.get("shares_accepted", 0)
        print(f"  Shares enviados      : {sub}")
        print(f"  Shares aceitos       : {acc}")
        
        if running and sub == 0:
            print("  \033[93m[!] Minerando, mas nenhum share enviado ainda. Aguarde...\033[0m")
        elif running and acc > 0:
            print("  \033[92m[OK] O cliente está minerando e enviando shares com sucesso!\033[0m")

    # =========================================================
    # 2. TESTANDO O LADO DO SERVIDOR (PoolManager)
    # =========================================================
    print(f"\n▶ [2] STATUS DO POOL MANAGER (Servidor em {API_POOL})")
    pool_stats = get_json(API_POOL, "/api/pool/stats")
    
    if "error" in pool_stats:
        print(f"  [X] Não foi possível conectar ao PoolManager em {API_POOL}")
        print(f"      Verifique se a Origem está rodando e a porta 5000 liberada.")
    else:
        miners_count = pool_stats.get("active_miners", pool_stats.get("total", 0))
        total_shares = pool_stats.get("total_shares", pool_stats.get("shares", 0))
        
        print(f"  Mineradores conectados : {miners_count}")
        print(f"  Shares processados     : {total_shares}")
        
        if miners_count > 0:
            print("  \033[92m[OK] O PoolManager está recebendo conexões!\033[0m")
            
            # Lista os mineradores
            miners_list = get_json(API_POOL, "/api/pool/miners")
            if "miners" in miners_list:
                print(f"  {'MINERADOR (ID/ENDEREÇO)':<24} {'HASHRATE':<12} {'SHARES OK':<10}")
                print("  " + "-" * 55)
                for m in miners_list["miners"]:
                    m_id = m.get("miner_id", m.get("address", "?"))[:22] + "..."
                    hr = m.get("hashrate", 0)
                    ok = m.get("valid_shares", 0)
                    print(f"  {m_id:<24} {hr:<12} {ok:<10}")
        else:
            print("  \033[91m[X] Nenhum cliente conectado ao pool.\033[0m")
            print("      Vá nas máquinas Windows e inicie o PoolClient (BRN_POOL_URL apontando para a Origem).")

    # =========================================================
    # 3. RESUMO
    # =========================================================
    print("\n" + "=" * 70)
    if "error" not in pool_stats and pool_stats.get("active_miners", pool_stats.get("total", 0)) > 0:
        print(" ✅ O Pool está ATIVO e recebendo trabalho dos clientes!")
    else:
        print(" ⚠️ O Pool está ONLINE, mas sem clientes enviando trabalho.")
        print("    Verifique o arquivo .env das máquinas Windows (BRN_POOL_URL).")
    print("=" * 70)

if __name__ == "__main__":
    main()
