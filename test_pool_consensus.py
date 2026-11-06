#!/usr/bin/env python3
"""
test_pool_consensus.py — Valida se os blocos minerados pelo Pool estão
recebendo as assinaturas corretas (Multi-sig) e PBFT.
"""
import os
import sys
import json
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:5000"

def get_json(path):
    try:
        req = urllib.request.Request(f"{API}{path}", headers={"User-Agent": "brn-test/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}

def main():
    print("=" * 70)
    print(" 🛡️  TESTE DE CONSENSO: POOL MINING + MULTI-SIG")
    print("=" * 70)

    # 1. Status do Nó
    status = get_json("/api/status")
    if "error" in status:
        print(f"[X] Nó offline. O main.py está rodando? ({status['error']})")
        return

    print(f" Altura Local : {status.get('height')}")
    print(f" Dificuldade  : {status.get('difficulty')}")

    # 2. Status do Pool
    pool_stats = get_json("/api/pool/stats")
    pool_miners = get_json("/api/pool/miners")
    
    if "error" not in pool_stats:
        print(f"\n▶ STATUS DO POOL (Origem)")
        print(f"  Mineradores ativos: {pool_stats.get('total', pool_stats.get('active_miners', 0))}")
        print(f"  Shares processados: {pool_stats.get('shares', pool_stats.get('total_shares', 0))}")
        print(f"  Blocos achados    : {pool_stats.get('blocks', pool_stats.get('blocks_found', 0))}")
        
        miners = pool_miners.get("miners", [])
        if miners:
            print(f"  {'MINERADOR':<22} {'HASHRATE':<12} {'SHARES OK':<10}")
            for m in miners:
                mid = m.get("miner_id", m.get("address", "?"))[:20]
                hr = m.get("hashrate", 0)
                shares = m.get("valid_shares", 0)
                print(f"  {mid:<22} {hr:<12} {shares:<10}")
    else:
        print("\n[!] PoolManager não está ativo (nó não é Origem ou desativado).")

    # 3. Verificar Consenso (Assinaturas e PBFT)
    print(f"\n▶ VERIFICANDO BLOCOS RECENTES")
    
    cons = get_json("/api/consensus/validators")
    req_sigs = cons.get("required_sigs", 0)
    validators = cons.get("validators", [])
    
    latest = get_json("/api/latest")
    if "error" in latest or not isinstance(latest, list) or not latest:
        print("[X] Não foi possível obter os últimos blocos.")
        return

    print(f"  Quórum exigido : {req_sigs} (de {len(validators)} validadores)")
    print(f"  {'BLOCO':<8} {'MINEROU':<15} {'SIGS':<6} {'PBFT':<6} {'STATUS'}")
    print("  " + "-" * 60)

    for blk in latest[:5]: # Pega os últimos 5 blocos
        h = blk.get("height")
        sigs = blk.get("validators_sig", [])
        pbft = blk.get("pbft_commits", [])
        
        # Tenta descobrir quem minerou (pela primeira assinatura ou pelo coinbase)
        miner = "Desconhecido"
        if sigs:
            miner = sigs[0].get("pubkey", "")[:12] + "..."
        elif blk.get("transactions"):
            cb = blk["transactions"][0]
            if cb.get("outputs"):
                miner = cb["outputs"][0].get("address", "?")[:12] + "..."

        n_sigs = len(sigs)
        n_pbft = len(pbft)
        
        status_ok = True
        if n_sigs < req_sigs:
            status_ok = False
            status_msg = f"\033[91m[FALHOU] Faltam {req_sigs - n_sigs} sigs\033[0m"
        else:
            status_msg = "\033[92m[OK]\033[0m"

        # Se o PBFT estiver ativo e enforce ligado, checa
        if pbft or os.environ.get("BRN_PBFT_ENFORCE") == "1":
            if n_pbft == 0:
                status_msg += " (Sem PBFT)"
                
        print(f"  #{h:<7} {miner:<15} {n_sigs}/{req_sigs}   {n_pbft:<6} {status_msg}")

    print("\n" + "=" * 70)
    if req_sigs > 0 and latest[0].get("validators_sig") and len(latest[0]["validators_sig"]) >= req_sigs:
         print(" ✅ O Pool está conseguindo coletar as assinaturas corretamente!")
         print("    A rede está segura.")
    else:
         print(" ⚠️ O último bloco não atingiu o quórum necessário.")
         print("    Se o Pool acabou de minerar, espere 15s (BRN_SIG_COLLECT_TIMEOUT).")
    print("=" * 70)

if __name__ == "__main__":
    main()
