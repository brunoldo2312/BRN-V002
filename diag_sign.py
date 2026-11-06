#!/usr/bin/env python3
"""
diag_sign.py — Testa se os peers estão se recusando a assinar blocos.
"""
import os, sys, json, time
from pathlib import Path

BASE = Path(__file__).resolve().parent
os.chdir(BASE)

# Carrega .env
if os.path.exists("brn_network.env"):
    for line in open("brn_network.env", encoding="utf-8").read().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def main():
    from blockchain import Blockchain
    from trust_network import TrustNetwork
    from secure_store import load_current_wallet
    from wallet import Wallet
    from p2p_unified import P2PClient, PeerDiscovery

    print("=" * 70)
    print(" DIAGNÓSTICO DE ASSINATURA (SIGN_BLOCK) BRN")
    print("=" * 70)

    chain = Blockchain("brn_v2_chain.db", auto_genesis=False)
    trust = TrustNetwork("trust_network.json")
    chain.set_trust_net(trust)
    
    pw = os.environ.get("BRN_WALLET_SESSION_PASS") or os.environ.get("BRN_NODE_PASSWORD", "")
    w = load_current_wallet(pw)
    if w and w.get("ok"):
        sk = Wallet(private_key_hex=w["private_key"])
        chain.set_local_validator_sk(sk)
        print(f"SK local carregada: {sk.pub_hex[:16]}...")
    
    h = chain.db.height()
    tip = chain.db.tip_hash()
    
    # Pega o último bloco real
    test_block = chain.db.get_block(h)
    if not test_block:
        print("[X] Blockchain vazia.")
        return
        
    print(f"Altura local : {h}")
    print(f"Tip hash     : {tip[:32]}...")

    validators = chain._get_active_validators()
    print(f"Validadores ativos: {len(validators)}")
    for v in validators:
        print(f"  - {v[:16]}...")

    disc = PeerDiscovery(6001, "diag")
    peers = disc.listar_peers()
    
    if not peers:
        print("\n[X] Nenhum peer P2P descoberto no momento.")
        print("    Verifique se o main.py está rodando nas outras máquinas.")
        return

    print(f"\nTestando sign_block em {len(peers)} peer(s):")
    print("-" * 70)
    
    for p in peers:
        if ":" not in p: continue
        ip, port_s = p.rsplit(":", 1)
        port = int(port_s)

        print(f"-> Enviando para {p}...", end=" ")
        try:
            raw, lat = P2PClient.send_message(
                ip, port,
                {"type": "sign_block", "block": test_block},
                timeout=5,
            )
        except Exception as e:
            print(f"ERRO DE REDE: {e}")
            continue

        if raw is None:
            print(f"SEM RESPOSTA (timeout)")
            continue

        pk = (raw.get("pubkey") or "").strip()
        sg = (raw.get("signature") or "").strip()
        err = raw.get("error", "")

        if pk and sg:
            print(f"OK! Assinou ({pk[:12]}...)")
        else:
            print(f"RECUSOU: {err}")

    print("=" * 70)

if __name__ == "__main__":
    main()
