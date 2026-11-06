#!/usr/bin/env python3
"""
monitor_mineracao.py — Painel de Mineração e Validadores BRN
================================================================
Mostra em tempo real:
  - Status do nó local (se está minerando, hashrate, solo ou pool)
  - Quem está minerando no Pool (se este nó for a Origem)
  - Lista de Peers com seus respectivos Trust Scores e se são Validadores
================================================================
"""
import json
import urllib.request
import urllib.error
import time
import os
import sys

API = os.environ.get("BRN_API_URL", "http://127.0.0.1:5000").rstrip("/")

# Cores ANSI
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    MAGENTA= "\033[95m"
    GRAY   = "\033[90m"

def get_json(path):
    try:
        req = urllib.request.Request(f"{API}{path}", headers={"User-Agent": "brn-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None

def fmt_hashrate(khs):
    if khs >= 1000:
        return f"{khs/1000:.2f} MH/s"
    return f"{khs:.1f} kH/s"

def main():
    print(f"{C.CYAN}{'=' * 75}{C.RESET}")
    print(f"{C.BOLD} ⛏️  MONITOR DE MINERAÇÃO E CONSENSO BRN{C.RESET}")
    print(f"{C.CYAN}{'=' * 75}{C.RESET}")

    # 1. Status Local
    status = get_json("/api/status")
    if not status:
        print(f"{C.RED}[X] Não foi possível conectar à API local em {API}{C.RESET}")
        print("    Verifique se o main.py está rodando.")
        sys.exit(1)

    miner = get_json("/api/miner/status") or {}
    pool_local = get_json("/api/pool/local_status") or {}
    
    print(f"\n{C.BOLD}▶ SEU NÓ ({status.get('height', 0)} blocos){C.RESET}")
    
    # Verifica se está minerando solo
    is_solo = miner.get("running", False) and not pool_local.get("running", False)
    # Verifica se está minerando em pool
    is_pool = pool_local.get("running", False)
    
    if is_solo:
        hr = miner.get("hashrate_khs", 0)
        print(f"  Status: {C.GREEN}MINERANDO SOLO{C.RESET}")
        print(f"  Hashrate: {C.BOLD}{fmt_hashrate(hr)}{C.RESET}")
        print(f"  Blocos minerados: {miner.get('blocks_mined', 0)}")
    elif is_pool:
        hr = pool_local.get("hashrate_khs", 0)
        print(f"  Status: {C.GREEN}MINERANDO EM POOL{C.RESET}")
        print(f"  Hashrate: {C.BOLD}{fmt_hashrate(hr)}{C.RESET}")
        print(f"  Shares Aceitos: {pool_local.get('shares_accepted', 0)}/{pool_local.get('shares_submitted', 0)}")
    else:
        print(f"  Status: {C.YELLOW}PARADO{C.RESET}")

    # 2. Mineradores no Pool (se este nó for a Origem/PoolManager)
    pool_stats = get_json("/api/pool/stats")
    pool_miners = get_json("/api/pool/miners")
    
    if pool_stats and pool_stats.get("ok"):
        print(f"\n{C.BOLD}▶ MINERADORES NO SEU POOL (Origem){C.RESET}")
        total = pool_stats.get("total", pool_stats.get("active_miners", 0))
        print(f"  Total de mineradores: {C.BOLD}{total}{C.RESET}")
        
        miners_list = pool_miners.get("miners", []) if pool_miners else []
        if miners_list:
            print(f"  {C.GRAY}{'Endereço / ID':<24} {'Hashrate':<12} {'Shares':<10}{C.RESET}")
            for m in miners_list:
                m_id = m.get("miner_id", m.get("address", "?"))[:20] + "..."
                m_hr = fmt_hashrate(m.get("hashrate", 0))
                m_shares = f"{m.get('valid_shares', 0)}/{m.get('shares_submitted', 0)}"
                print(f"  {m_id:<24} {m_hr:<12} {m_shares:<10}")
        else:
            print(f"  {C.GRAY}Nenhum minerador conectado ao pool no momento.{C.RESET}")

    # 3. Validadores e Trust Network
    trust = get_json("/api/trust/nodes?limit=50")
    if trust and trust.get("ok"):
        nodes = trust.get("nodes", [])
        validators = [n for n in nodes if n.get("status") == "validator" or n.get("trust_score", 0) >= 150]
        
        print(f"\n{C.BOLD}▶ REDE DE CONFIANÇA E VALIDADORES{C.RESET}")
        print(f"  Nós totais: {len(nodes)} | Validadores Ativos: {C.GREEN}{len(validators)}{C.RESET}")
        
        print(f"  {C.GRAY}{'Endereço':<22} {'Trust Score':<12} {'Validador?':<12} {'Altura':<8}{C.RESET}")
        for n in nodes:
            addr = n.get("address", "?")[:20]
            score = n.get("trust_score", 0)
            is_val = n.get("status") == "validator" or score >= 150
            h = n.get("height", 0)
            
            # Formatação de cores
            score_color = C.GREEN if score >= 150 else (C.YELLOW if score > 100 else C.GRAY)
            val_str = f"{C.GREEN}SIM{C.RESET}" if is_val else f"{C.GRAY}NÃO{C.RESET}"
            
            print(f"  {addr:<22} {score_color}{score:<12.1f}{C.RESET} {val_str:<12} {h:<8}")

    # 4. Resumo de Consenso (Último Bloco)
    cons = get_json("/api/consensus/validators")
    if cons and cons.get("ok"):
        req_sigs = cons.get("required_sigs", 0)
        print(f"\n{C.BOLD}▶ CONSENSO (ÚLTIMO BLOCO){C.RESET}")
        latest = get_json("/api/latest")
        if latest and isinstance(latest, list) and len(latest) > 0:
            blk = latest[0]
            sigs = blk.get("validators_sig", [])
            print(f"  Bloco #{blk.get('height')} tem {len(sigs)} assinatura(s). Quórum exigido: {req_sigs}")
            if len(sigs) >= req_sigs and req_sigs > 0:
                print(f"  Status: {C.GREEN}CONSENSO ATINGIDO{C.RESET}")
            elif req_sigs == 0:
                print(f"  Status: {C.YELLOW}MODO BOOTSTRAP (Sem quórum){C.RESET}")
            else:
                print(f"  Status: {C.RED}AGUARDANDO ASSINATURAS{C.RESET}")

    print(f"\n{C.CYAN}{'=' * 75}{C.RESET}")

if __name__ == "__main__":
    try:
        while True:
            os.system('cls' if os.name == 'nt' else 'clear')
            main()
            time.sleep(5) # Atualiza a cada 5 segundos
    except KeyboardInterrupt:
        print("\nMonitor encerrado.")
