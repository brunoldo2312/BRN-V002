#!/usr/bin/env python3
"""
test_pool_signing.py — Simula o Pool Manager achando um bloco e coletando assinaturas.
"""
import os
import sys
import json
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

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
    log(" 🛡️  TESTE DE ASSINATURA DO POOL (ORIGEM)")
    log("=" * 65)

    try:
        from blockchain import Blockchain, make_coinbase, compute_merkle_root, _pow_search, txid
        from trust_network import TrustNetwork
        from secure_store import load_current_wallet
        from wallet import Wallet
        from brn_multisig import build_collector
    except ImportError as e:
        log(f"[X] Erro de importação: {e}", "red")
        return

    # 1. Carrega Chain e Trust
    bc = Blockchain("brn_v2_chain.db", auto_genesis=False)
    trust = TrustNetwork("trust_network.json", is_origin=os.environ.get("BRN_IS_ORIGIN") == "1")
    bc.set_trust_net(trust)

    # 2. Carrega Carteira (O Pool precisa dela para assinar)
    pwd = os.environ.get("BRN_NODE_PASSWORD") or os.environ.get("BRN_WALLET_SESSION_PASS")
    w_data = load_current_wallet(pwd)
    if not w_data or not w_data.get("ok"):
        log("[X] Falha ao abrir a carteira. O Pool não pode assinar.", "red")
        return
    
    w = Wallet(private_key_hex=w_data["private_key"])
    bc.set_local_validator_sk(w)
    log(f"[OK] Carteira carregada: {w.pub_hex[:16]}...", "green")

    # 3. Pede ao P2P real para coletar assinaturas
    from p2p_unified import P2PManager
    p2p = P2PManager(bc, node_id_priv=None, tcp_port=6001) # Mock simplificado para teste
    # Nota: Em produção, o p2p já está rodando no main.py. 
    # Para este teste, o build_collector tentará conectar na porta 6001 dos peers.
    
    # Instala o coletor de assinaturas
    collector = build_collector(bc, w, p2p, timeout=5.0)
    bc.set_sign_collector(collector)

    # 4. Simula o Pool achando um bloco
    log("\n▶ Simulando o Pool Manager achando um bloco...", "yellow")
    
    height = bc.db.height() + 1
    reward = bc.current_reward(height)
    diff = bc.current_difficulty()
    
    # Cria a coinbase para a carteira local
    cb = make_coinbase(w.address, w.pub_hex, height, reward)
    
    # Faz o PoW (simulando o resultado do cliente)
    merkle = compute_merkle_root([cb["txid"]])
    prev_hash = bc.db.tip_hash()
    ts = int(time.time())
    
    log(f"  Minerando bloco #{height} (dificuldade {diff})...")
    nonce, h_hex, _ = _pow_search(prev_hash, merkle, ts, diff)
    
    block = {
        "height": height, "hash": h_hex, "prev_hash": prev_hash,
        "timestamp": ts, "nonce": nonce, "merkle": merkle,
        "difficulty": diff, "transactions": [cb],
        "validators_sig": [],
    }
    bc._attach_auth_tag(block)
    log(f"  Bloco #{height} encontrado! Hash: {h_hex[:16]}...", "green")

    # 5. Coleta as assinaturas via P2P
    log("\n▶ Coletando assinaturas dos peers...", "yellow")
    t0 = time.time()
    sigs = collector(block)
    dt = (time.time() - t0) * 1000
    
    # 6. Valida e processa
    validators = bc._get_active_validators()
    required = bc._required_signatures(validators)
    
    log(f"\n  Tempo de coleta: {dt:.0f}ms")
    log(f"  Assinaturas recebidas: {len(sigs)}")
    log(f"  Quórum exigido: {required}")
    
    for s in sigs:
        log(f"    - {s.get('pubkey')[:24]}...", "green")

    if len(sigs) >= required:
        log("\n ✅ SUCESSO! O Pool coletou assinaturas suficientes.", "green")
        log("    O bloco será aceito na rede.")
        
        # Opcional: Salvar na blockchain para testar
        block["validators_sig"] = sigs
        ok, msg = bc.accept_block(block)
        if ok:
            log(f"    Bloco #{height} minerado e aceito!", "green")
        else:
            log(f"    Erro ao aceitar bloco: {msg}", "red")
    else:
        log("\n ❌ FALHA! O Pool não conseguiu as assinaturas necessárias.", "red")
        log("    Verifique se os outros PCs estão online e com as carteiras abertas.")
        log("    Verifique também se o trust_network.json tem as chaves corretas.")

if __name__ == "__main__":
    main()