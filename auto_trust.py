#!/usr/bin/env python3
"""
auto_trust.py — Sincroniza validadores automaticamente.
Lê os peers ativos do banco de dados local e adiciona suas chaves públicas
no trust_network.json, promovendo-os a validadores.
Deve ser executado preferencialmente na máquina Origem (Ubuntu).
"""
import os
import sys
import time
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# Carrega .env (para senhas e chaves)
if os.path.exists("brn_network.env"):
    for line in open("brn_network.env", encoding="utf-8").read().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def main():
    print("=" * 60)
    print(" AUTO-TRUST: Adicionando Peers ao Trust Network")
    print("=" * 60)

    try:
        from db import ChainDB
        from trust_network import TrustNetwork
        from secure_store import load_node_identity
        from crypto import Ed25519PrivateKey
        from bech32 import address_from_pubkey
    except ImportError as e:
        print(f"[X] Erro de importação: {e}")
        return

    # 1. Carrega a Identidade do Nó (para assinar o JSON se for Origem)
    password = os.environ.get("BRN_NODE_PASSWORD", "").strip()
    if not password:
        print("[X] BRN_NODE_PASSWORD não configurada no .env")
        return

    node_sk = None
    is_origin = os.environ.get("BRN_IS_ORIGIN", "0") == "1"

    if is_origin:
        try:
            data = load_node_identity(password, "node_identity.enc")
            if data and data.get("sk"):
                node_sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
                print("[OK] Identidade do Origem carregada (pode assinar o trust).")
        except Exception as e:
            print(f"[!] Aviso: Não foi possível carregar a identidade do Origem: {e}")

    # 2. Carrega o Trust Network
    trust = TrustNetwork("trust_network.json", is_origin=is_origin, node_sk=node_sk)
    current_nodes = set(trust.nodes.keys())

    # 3. Lê os peers do banco de dados
    db = ChainDB("brn_v2_chain.db")
    # Lê todos os peers que se conectaram nos últimos 30 minutos
    peers = db.listar_peers(apenas_ativos=True, janela=1800)
    db.close()

    if not peers:
        print("[!] Nenhum peer encontrado no banco de dados.")
        return

    print(f"\n[*] Analisando {len(peers)} peer(s) no banco de dados...")
    
    adicionados = 0
    atualizados = 0

    for p in peers:
        pk = (p.get("public_key") or "").strip().lower()
        peer_addr = p.get("address", "")
        
        # Se o peer não mandou a pubkey no handshake, ignora
        if not pk or len(pk) != 66:
            continue

        # Deriva o endereço BRN a partir da pubkey
        try:
            brn_address = address_from_pubkey(bytes.fromhex(pk))
        except Exception:
            brn_address = "brn1_desconhecido"

        if pk in current_nodes:
            # Já existe, só atualiza o peer_addr e o status de conexão
            trust.nodes[pk].peer_addr = peer_addr
            trust.nodes[pk].last_seen = time.time()
            trust.nodes[pk].connected = True
            if trust.nodes[pk].trust_score < 150:
                trust.nodes[pk].trust_score = 150.0
            trust.nodes[pk].is_validator = True
            atualizados += 1
            print(f"  [UPDATE] {pk[:16]}... ({brn_address[:12]}...)")
        else:
            # Novo nó, adiciona como validador
            trust.register_node(
                node_id=pk,
                address=brn_address,
                public_key=pk,
                peer_addr=peer_addr
            )
            # Força score para ser validador imediatamente
            n = trust.nodes[pk]
            n.trust_score = 150.0
            n.is_validator = True
            n.trust_level = "validator"
            n.connected = True
            
            adicionados += 1
            print(f"  [ADD]    {pk[:16]}... ({brn_address[:12]}...) -> PROMOVIDO A VALIDADOR")

    # 4. Salva as mudanças (se for Origem, ele assina automaticamente)
    if adicionados > 0 or atualizados > 0:
        trust.flush()
        print(f"\n[OK] Trust Network atualizada! (Adicionados: {adicionados} | Atualizados: {atualizados})")
        
        if is_origin:
            print("     O arquivo foi ASSINADO com a chave do Origem.")
            print("     Os clientes baixarão esta lista automaticamente.")
        else:
            print("     Salvo em trust_network.local.json (você não é Origem).")
    else:
        print("\n[!] Nenhuma alteração foi necessária.")

if __name__ == "__main__":
    main()
