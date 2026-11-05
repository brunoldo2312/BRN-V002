#!/usr/bin/env python3
"""
curador_peers.py — Limpa a lista de bootstrap mantendo apenas peers saudáveis.
================================================================
Uso:
    python curador_peers.py               # Analisa e atualiza bootstrap_peers.json
    python curador_peers.py --dry-run     # Apenas mostra o que seria feito
    python curador_peers.py --tolerance 2 # Considera saudável quem está a até 2 blocos do topo

Lógica:
  1. Lê peers_discovered.json, bootstrap_peers.json e BRN_BOOTSTRAP_PEERS (env)
  2. Consulta /api/chain-info de cada peer (porta 5000)
  3. Descobre a MAIOR ALTURA da rede
  4. Mantém apenas os peers cuja altura >= (Maior Altura - Tolerância)
  5. Salva a lista limpa de volta no bootstrap_peers.json e brn_network.env
================================================================
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
import urllib.request
import urllib.error

BASE = Path(__file__).resolve().parent
BOOTSTRAP_FILE = BASE / "bootstrap_peers.json"
PEERS_FILE = BASE / "peers_discovered.json"
ENV_FILE = BASE / "brn_network.env"

def fetch_peer_info(ip, port=5000, timeout=3.0):
    """Consulta /api/chain-info para saber a altura e o tip do peer."""
    try:
        url = f"http://{ip}:{port}/api/chain-info"
        req = urllib.request.Request(url, headers={"User-Agent": "brn-curador/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
            return {
                "height": int(data.get("height", -1)),
                "tip": data.get("tip_hash", ""),
                "error": None
            }
    except Exception as e:
        return {"height": -1, "tip": "", "error": str(e)}

def load_all_peers():
    """Carrega peers de todos os lugares possíveis."""
    peers = set()
    
    # 1. Do arquivo bootstrap
    if BOOTSTRAP_FILE.exists():
        try:
            data = json.loads(BOOTSTRAP_FILE.read_text())
            if isinstance(data, list):
                peers.update(data)
        except Exception:
            pass
            
    # 2. Do arquivo descoberto
    if PEERS_FILE.exists():
        try:
            data = json.loads(PEERS_FILE.read_text())
            if isinstance(data, list):
                peers.update(data)
        except Exception:
            pass

    # 3. Do .env
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("BRN_BOOTSTRAP_PEERS="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                for p in val.split(","):
                    if p.strip():
                        peers.add(p.strip())
                        
    # 4. Do ambiente atual
    env_peers = os.environ.get("BRN_BOOTSTRAP_PEERS", "")
    for p in env_peers.split(","):
        if p.strip():
            peers.add(p.strip())
            
    return list(peers)

def update_bootstrap(new_peers):
    """Salva a nova lista nos arquivos."""
    # 1. Salva JSON
    BOOTSTRAP_FILE.write_text(json.dumps(new_peers, indent=2), encoding="utf-8")
    
    # 2. Atualiza .env
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        new_lines = []
        found = False
        for line in lines:
            if line.startswith("BRN_BOOTSTRAP_PEERS="):
                new_lines.append(f'BRN_BOOTSTRAP_PEERS="{",".join(new_peers)}"')
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f'BRN_BOOTSTRAP_PEERS="{",".join(new_peers)}"')
        ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tolerance", type=int, default=5, help="Tolerância de blocos atrás do topo (default: 5)")
    ap.add_argument("--dry-run", action="store_true", help="Apenas mostra, não altera arquivos")
    args = ap.parse_args()

    print("=" * 70)
    print(f" CURADOR DE PEERS BRN (Tolerância: {args.tolerance} blocos)")
    print("=" * 70)

    all_peers = load_all_peers()
    if not all_peers:
        print("[!] Nenhum peer encontrado nos arquivos.")
        return

    print(f"\n[*] Testando {len(all_peers)} peer(s) na rede (Porta 5000)...")
    
    results = []
    max_height = -1
    
    for p in all_peers:
        # Extrai IP (ignora a porta P2P, usa 5000 para API)
        ip = p.split(":")[0] if ":" in p else p
        info = fetch_peer_info(ip, port=5000)
        
        if info["error"]:
            print(f"  [X] {ip:<15} OFFLINE ({info['error']})")
        else:
            h = info["height"]
            print(f"  [OK] {ip:<15} Altura: {h} | Tip: {info['tip'][:16]}...")
            if h > max_height:
                max_height = h
            results.append({"peer": p, "ip": ip, "height": h, "tip": info["tip"]})

    if not results:
        print("\n[!] Nenhum peer respondeu. A rede está isolada ou todos estão offline.")
        return

    print(f"\n[*] Maior altura encontrada na rede: {max_height}")
    
    # Filtra os que estão dentro da tolerância
    keep = []
    excluded = []
    
    min_acceptable = max(0, max_height - args.tolerance)
    
    for r in results:
        if r["height"] >= min_acceptable:
            keep.append(r["peer"])
        else:
            excluded.append(r["peer"])

    print(f"\n[>] PEERS MANTIDOS ({len(keep)}):")
    for p in keep:
        print(f"    ✅ {p}")
        
    print(f"\n[>] PEERS EXCLUÍDOS ({len(excluded)}):")
    for p in excluded:
        print(f"    ❌ {p} (Atrasado)")

    if args.dry_run:
        print("\n[!] MODO DRY-RUN: Nenhum arquivo foi alterado.")
        return

    if keep:
        update_bootstrap(keep)
        print("\n[OK] bootstrap_peers.json e brn_network.env atualizados com os peers saudáveis!")
        print("     Reinicie o main.py para aplicar as mudanças na sincronização.")
    else:
        print("\n[!] Nenhum peer saudável encontrado. Os arquivos NÃO foram alterados para evitar desconexão total.")

if __name__ == "__main__":
    main()
