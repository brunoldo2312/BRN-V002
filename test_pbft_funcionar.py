#!/usr/bin/env python3
"""
test_pbft_readiness.py — Verifica se a rede suporta ligar o PBFT sem travar.
"""
import os
import sys
import json
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# Carrega .env
if os.path.exists("brn_network.env"):
    for line in open("brn_network.env", encoding="utf-8").read().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def log(msg, color=None):
    if color == "green": print(f"\033[92m{msg}\033[0m")
    elif color == "red": print(f"\033[91m{msg}\033[0m")
    elif color == "yellow": print(f"\033[93m{msg}\033[0m")
    else: print(msg)

def main():
    log("=" * 65)
    log(" 🛡️  TESTE DE PRONTIDÃO PARA O PBFT (SAFE PBFT)")
    log("=" * 65)

    try:
        from blockchain import Blockchain
        from trust_network import TrustNetwork
        from p2p_unified import P2PClient, CHAIN_ID
    except ImportError as e:
        log(f"[X] Erro de importação: {e}", "red")
        return

    bc = Blockchain("brn_v2_chain.db", auto_genesis=False)
    trust = TrustNetwork("trust_network.json", is_origin=os.environ.get("BRN_IS_ORIGIN") == "1")
    bc.set_trust_net(trust)
    
    # 1. Verifica Carteira Local
    pwd = os.environ.get("BRN_NODE_PASSWORD") or os.environ.get("BRN_WALLET_SESSION_PASS")
    if not pwd or bc._local_validator_sk is None:
        log("\n[X] FALHA: Sua carteira não está carregada na memória.", "red")
        log("    O PBFT precisa que a sua SK esteja ativa para votar.", "red")
        return
        
    local_pk = bc._local_validator_sk.pub_hex
    log(f"\n[OK] Carteira local ativa: {local_pk[:16]}...", "green")

    # 2. Verifica Validadores e Quórum Matemático
    validators = bc._get_active_validators()
    n = len(validators)
    f = (n - 1) // 3
    quorum = 2 * f + 1
    
    log(f"\n▶ Matemática do PBFT:", "yellow")
    log(f"  Validadores na rede (n): {n}")
    log(f"  Bizantinos tolerados (f): {f}")
    log(f"  Quórum exigido (2f+1)  : {quorum}")
    
    if n < 3:
        log("\n[!] AVISO: Com menos de 3 validadores, o PBFT exige 100% de presença.", "yellow")

    # 3. Verifica Conexão P2P e Altura
    log(f"\n▶ Testando conexão com os {n-1} peers...", "yellow")
    
    p2p_peers = set()
    try:
        from p2p_unified import PeerDiscovery
        disc = PeerDiscovery(6001, "diag")
        p2p_peers = disc.listar_peers()
    except Exception as e:
        log(f"[X] Falha ao inicializar P2P: {e}", "red")
        return

    online_peers = 0
    sync_peers = 0
    local_h = bc.db.height()
    
    test_block = bc.db.get_block(local_h)
    if not test_block:
        log("[X] Blockchain vazia. Minere um bloco primeiro.", "red")
        return

    for p in p2p_peers:
        if ":" not in p: continue
        ip, port_s = p.rsplit(":", 1)
        
        # Ignora o próprio IP
        if ip == "127.0.0.1" or ip == os.environ.get("BRN_SEU_ENDERECO", "").split(":")[0]:
            continue
            
        try:
            # Testa altura
            msg_h = {"type": "get_chain_height", "_chain": CHAIN_ID, "_port": 6001}
            resp_h, _ = P2PClient.send_message(ip, int(port_s), msg_h, timeout=3.0)
            
            if resp_h:
                online_peers += 1
                ph = resp_h.get("height", -1)
                
                # Testa se ele assina (proxy para saber se a carteira está aberta)
                msg_sig = {"type": "sign_block", "block": test_block, "_chain": CHAIN_ID, "_port": 6001}
                resp_sig, _ = P2PClient.send_message(ip, int(port_s), msg_sig, timeout=3.0)
                
                pk = (resp_sig.get("pubkey") or "").lower() if resp_sig else ""
                signed = bool(pk)
                
                status_h = "OK" if ph >= local_h - 1 else "ATRASADO"
                status_s = "ASSINOU" if signed else "RECUSOU"
                
                if status_h == "OK" and status_s == "ASSINOU":
                    sync_peers += 1
                    log(f"  [✅] {ip:<15} H:{ph} | {status_s}", "green")
                else:
                    log(f"  [❌] {ip:<15} H:{ph} ({status_h}) | {status_s}", "red")
            else:
                log(f"  [❌] {ip:<15} OFFLINE", "red")
        except Exception as e:
            log(f"  [❌] {ip:<15} Erro: {e}", "red")

    # 4. Veredito Final
    log("\n" + "=" * 65)
    log(" VEREDITO FINAL")
    log("=" * 65)
    
    # O quórum precisa do líder (1) + (quorum - 1) peers.
    # Mas se f=0, quorum=1, ele mesmo decide.
    peers_necessarios = max(0, quorum - 1)
    
    log(f"  Você (Líder): 1 voto")
    log(f"  Peers prontos: {sync_peers} de {peers_necessarios} necessários")
    
    if sync_peers >= peers_necessarios:
        log(f"\n ✅ REDE APTA! O PBFT pode ser ligado com segurança.", "green")
        log(f"    Motivo: Você tem o próprio voto + {sync_peers} peers = {sync_peers+1} votos (quórum={quorum}).")
        log(f"    Para ativar, coloque no .env: BRN_PBFT_ENFORCE=1", "green")
    else:
        log(f"\n ❌ REDE NÃO APTA! O PBFT vai travar a rede se for ligado.", "red")
        log(f"    Motivo: O quórum exige {quorum} votos, mas você só tem {sync_peers+1}.")
        log(f"    Faltam {peers_necessarios - sync_peers} peer(s) online com a carteira aberta.")
        log(f"    Corrija as luzes vermelhas acima antes de ligar o PBFT.", "red")

if __name__ == "__main__":
    main()