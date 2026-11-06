#!/usr/bin/env python3
"""
auto_trust_daemon.py — Mantém a Trust Network sincronizada com a realidade.
Roda em loop, adicionando nós ativos e removendo os inativos.
"""
import os
import sys
import time
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

if os.path.exists("brn_network.env"):
    for line in open("brn_network.env", encoding="utf-8").read().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)

def main():
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
    
    if not is_origin:
        log("Este script deve rodar preferencialmente na Origem (BRN_IS_ORIGIN=1).", "WARN")

    node_sk = None
    if is_origin and password:
        try:
            data = load_node_identity(password, "node_identity.enc")
            if data and data.get("sk"):
                node_sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
        except Exception as e:
            log(f"Aviso: Identidade do Origem falhou: {e}", "WARN")

    log("=" * 60)
    log(" AUTO-TRUST DAEMON INICIADO (Loop a cada 120s)")
    log("=" * 60)

    while True:
        try:
            chain = Blockchain("brn_v2_chain.db", auto_genesis=False)
            h = chain.db.height()
            test_block = chain.db.get_block(h)
            chain.db.close()
            
            if not test_block:
                log("Blockchain vazia. Aguardando...", "WARN")
                time.sleep(30)
                continue

            trust = TrustNetwork("trust_network.json", is_origin=is_origin, node_sk=node_sk)
            
            # 1. Garante que a chave local está no Trust
            local_pk = ""
            if password:
                w = load_current_wallet(password)
                if w and w.get("ok"):
                    local_pk = (w.get("public_key") or w.get("pub_hex") or "").strip().lower()
                    if len(local_pk) == 66:
                        if local_pk not in trust.nodes:
                            addr = address_from_pubkey(bytes.fromhex(local_pk))
                            trust.register_node(node_id=local_pk, address=addr, public_key=local_pk, peer_addr="127.0.0.1:6001")
                            log(f"Chave local adicionada: {local_pk[:16]}...")

            # 2. Varre os peers ativos no banco
            db = ChainDB("brn_v2_chain.db")
            peers = db.listar_peers(apenas_ativos=True, janela=1800)
            db.close()

            active_pks = set()
            if local_pk: active_pks.add(local_pk)
            
            for p in peers:
                pk = (p.get("public_key") or "").strip().lower()
                peer_addr = p.get("address", "")
                
                if "127.0.0.1" in peer_addr or "100.111.253.8" in peer_addr:
                    continue

                # Se não tem pubkey válida no banco, busca via P2P
                if len(pk) != 66 or not pk.startswith(("02", "03")):
                    try:
                        ip, port_s = peer_addr.rsplit(":", 1)
                        msg = {"type": "sign_block", "block": test_block, "_chain": CHAIN_ID, "_port": 6001}
                        resp, _ = P2PClient.send_message(ip, int(port_s), msg, timeout=2.0)
                        if resp:
                            new_pk = (resp.get("pubkey") or "").strip().lower()
                            if not new_pk and resp.get("error"):
                                m = re.search(r'my_pub=([0-9a-fA-F]+)', resp["error"])
                                if m: new_pk = m.group(1).lower()
                            
                            if len(new_pk) == 66 and new_pk.startswith(("02", "03")):
                                pk = new_pk
                    except Exception:
                        pass # Timeout, offline
                
                if len(pk) == 66 and pk.startswith(("02", "03")):
                    active_pks.add(pk)
                    # Adiciona ou atualiza no Trust
                    if pk not in trust.nodes:
                        addr = address_from_pubkey(bytes.fromhex(pk))
                        trust.register_node(node_id=pk, address=addr, public_key=pk, peer_addr=peer_addr)
                        trust.nodes[pk].trust_score = 150.0
                        trust.nodes[pk].is_validator = True
                        trust.nodes[pk].connected = True
                        log(f"Novo validador ativo adicionado: {pk[:16]}...")
                    else:
                        trust.nodes[pk].last_seen = time.time()
                        trust.nodes[pk].connected = True
                        if trust.nodes[pk].trust_score < 150:
                            trust.nodes[pk].trust_score = 150.0
                        trust.nodes[pk].is_validator = True

            # 3. Remove os "fantasmas" (Offline há mais de 180 segundos)
            # O próprio TrustNetwork já tem essa função!
            removidos = trust.remove_ghosts(max_age_s=180)
            if removidos > 0:
                log(f"Removidos {removidos} validador(es) inativo(s).", "WARN")

            # 4. Salva o arquivo (e assina se for Origem)
            trust.flush()
            
            validadores_atuais = [n for n in trust.nodes.values() if n.is_validator]
            log(f"Rede atualizada. Validadores ativos: {len(validadores_atuais)}")

        except Exception as e:
            log(f"Erro no loop de auto-trust: {e}", "ERROR")

        time.sleep(120) # Espera 2 minutos antes de rodar de novo

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Encerrando daemon...", "INFO")
