#!/usr/bin/env python3
"""
auto.py — Sincroniza validadores automaticamente.
Lê a carteira local, busca as carteiras dos peers ativos via P2P,
e atualiza o trust_network.json (assinando se for Origem).
"""
import os
import sys
import time
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# Carrega .env
if os.path.exists("brn_network.env"):
    for line in open("brn_network.env", encoding="utf-8").read().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)

def main():
    log("=" * 60)
    log(" AUTO-TRUST: Atualizando Trust Network")
    log("=" * 60)

    try:
        from db import ChainDB
        from trust_network import TrustNetwork
        from secure_store import load_node_identity, load_current_wallet
        from crypto import Ed25519PrivateKey
        from bech32 import address_from_pubkey
        from p2p_unified import P2PClient, CHAIN_ID
        from blockchain import Blockchain
    except ImportError as e:
        log(f"Erro de importação: {e}", "ERROR")
        return

    password = os.environ.get("BRN_NODE_PASSWORD", "").strip()
    is_origin = os.environ.get("BRN_IS_ORIGIN", "0") == "1"
    node_sk = None

    # 1. Carrega Identidade do Origem (para assinar o JSON)
    if is_origin and password:
        try:
            data = load_node_identity(password, "node_identity.enc")
            if data and data.get("sk"):
                node_sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
                log("Identidade do Origem carregada (pode assinar o trust).", "OK")
        except Exception as e:
            log(f"Aviso: Identidade do Origem falhou: {e}", "WARN")

    # 2. Carrega Trust Network
    trust = TrustNetwork("trust_network.json", is_origin=is_origin, node_sk=node_sk)
    current_nodes = set(trust.nodes.keys())

    # 3. Adiciona a Carteira Local (Origem)
    local_pk = ""
    if password:
        try:
            w = load_current_wallet(password)
            if w and w.get("ok"):
                local_pk = (w.get("public_key") or w.get("pub_hex") or "").strip().lower()
                if len(local_pk) == 66 and local_pk.startswith(("02", "03")):
                    if local_pk not in current_nodes:
                        brn_addr = address_from_pubkey(bytes.fromhex(local_pk))
                        trust.register_node(node_id=local_pk, address=brn_addr, public_key=local_pk, peer_addr="127.0.0.1:6001")
                        n = trust.nodes[local_pk]
                        n.trust_score = 150.0
                        n.is_validator = True
                        n.trust_level = "validator"
                        n.connected = True
                        log(f"[ADD] Carteira local ({local_pk[:16]}...) adicionada como validador.", "OK")
                    else:
                        log(f"[OK] Carteira local ({local_pk[:16]}...) ja esta no Trust.")
        except Exception as e:
            log(f"Aviso: Falha ao carregar carteira local: {e}", "WARN")

    # 4. Pega um bloco real para testar assinatura
    chain = Blockchain("brn_v2_chain.db", auto_genesis=False)
    h = chain.db.height()
    test_block = chain.db.get_block(h)
    chain.db.close()

    if not test_block:
        log("Blockchain vazia. Não é possível testar assinatura.", "ERROR")
        return

    # 5. Lê os peers ativos do banco
    db = ChainDB("brn_v2_chain.db")
    peers = db.listar_peers(apenas_ativos=True, janela=3600)
    db.close()

    if not peers:
        log("Nenhum peer encontrado no banco de dados.", "WARN")
    else:
        log(f"Analisando {len(peers)} peer(s) ativos...")
    
    adicionados = 0
    atualizados = 0

    for p in peers:
        pk = (p.get("public_key") or "").strip().lower()
        peer_addr = p.get("address", "")
        
        # Ignora o próprio IP local
        if "127.0.0.1" in peer_addr or "100.111.253.8" in peer_addr:
            continue

        # Se a pubkey não for válida (66 chars, começa com 02/03), busca via P2P
        if len(pk) != 66 or not pk.startswith(("02", "03")):
            log(f"Buscando chave da Wallet via P2P para {peer_addr}...")
            try:
                ip, port_s = peer_addr.rsplit(":", 1)
                
                msg = {"type": "sign_block", "block": test_block}
                msg["_chain"] = CHAIN_ID
                msg["_port"] = int(os.environ.get("BRN_P2P_PORT", "6001"))

                resp, _ = P2PClient.send_message(ip, int(port_s), msg, timeout=5.0)
                
                if resp:
                    new_pk = (resp.get("pubkey") or "").strip().lower()
                    
                    # Se não veio no campo, tenta extrair do erro (ex: "my_pub=0324...")
                    if not new_pk and resp.get("error"):
                        match = re.search(r'my_pub=([0-9a-fA-F]+)', resp.get("error"))
                        if match:
                            new_pk = match.group(1).lower()

                    if len(new_pk) == 66 and new_pk.startswith(("02", "03")):
                        pk = new_pk
                        log(f"  -> Encontrada: {pk[:16]}...")
                    else:
                        log(f"  -> Peer não retornou chave válida (len={len(new_pk)}).")
                else:
                    log(f"  -> Sem resposta do peer (offline?).")
            except Exception as e:
                log(f"  -> Falha ao buscar via P2P: {e}")

        if len(pk) != 66 or not pk.startswith(("02", "03")):
            log(f"[SKIP] {peer_addr} -> Pubkey inválida ou ausente.", "WARN")
            continue

        try:
            brn_address = address_from_pubkey(bytes.fromhex(pk))
        except Exception:
            brn_address = "brn1_desconhecido"

        if pk in current_nodes:
            trust.nodes[pk].peer_addr = peer_addr
            trust.nodes[pk].last_seen = time.time()
            trust.nodes[pk].connected = True
            if trust.nodes[pk].trust_score < 150:
                trust.nodes[pk].trust_score = 150.0
            trust.nodes[pk].is_validator = True
            atualizados += 1
            log(f"[UPDATE] {pk[:16]}... ({brn_address[:12]}...)")
        else:
            trust.register_node(
                node_id=pk,
                address=brn_address,
                public_key=pk,
                peer_addr=peer_addr
            )
            n = trust.nodes[pk]
            n.trust_score = 150.0
            n.is_validator = True
            n.trust_level = "validator"
            n.connected = True
            
            adicionados += 1
            log(f"[ADD] {pk[:16]}... ({brn_address[:12]}...) -> PROMOVIDO A VALIDADOR", "OK")

    # 6. Salva e assina o arquivo
    if adicionados > 0 or atualizados > 0 or local_pk:
        trust.flush()
        log(f"Trust Network atualizada! (Adicionados: {adicionados} | Atualizados: {atualizados})", "OK")
        if is_origin:
            log("O arquivo foi ASSINADO com a chave do Origem.", "OK")
    else:
        log("Nenhuma alteração foi necessária.")

    log("\nValidadores atuais no Trust:")
    for nid in trust.nodes:
        print(f"  - {nid[:24]}...")

if __name__ == "__main__":
    main()
