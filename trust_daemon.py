#!/usr/bin/env python3
"""
trust_daemon.py v2 — Mantém a Trust Network sincronizada em background.
Agora força o download do JSON assinado se for um nó Cliente.
"""
import os
import time
import re
import json
import logging
import urllib.request
from pathlib import Path

log = logging.getLogger("trust_daemon")

def start_daemon(chain, trust, p2p, password, is_origin, stop_event):
    """Inicia a thread de auto-gerenciamento da Trust Network."""
    import threading
    
    def _loop():
        log.info("[TrustDaemon] Iniciado (varredura a cada 120s)")
        
        try:
            from db import ChainDB
            from secure_store import load_current_wallet
            from bech32 import address_from_pubkey
            from p2p_unified import P2PClient, CHAIN_ID
        except ImportError as e:
            log.error(f"[TrustDaemon] Falha ao importar modulos: {e}")
            return

        if stop_event.wait(15):
            return

        while not stop_event.is_set():
            try:
                # ==========================================
                # [NOVO] SE FOR CLIENTE, BAIXA A TRUST DA ORIGEM
                # ==========================================
                if not is_origin:
                    origin_ip = ""
                    # Tenta pegar do BRN_POOL_URL ou do primeiro BOOTSTRAP_PEER
                    pool_url = os.environ.get("BRN_POOL_URL", "").strip()
                    if pool_url.startswith("http"):
                        origin_ip = pool_url.replace("http://", "").split(":")[0]
                    else:
                        bootstrap = os.environ.get("BRN_BOOTSTRAP_PEERS", "").split(",")[0]
                        if bootstrap:
                            origin_ip = bootstrap.split(":")[0]

                    if origin_ip:
                        try:
                            url = f"http://{origin_ip}:5000/api/trust/download"
                            req = urllib.request.Request(url, headers={"User-Agent": "brn-daemon/2.0"})
                            with urllib.request.urlopen(req, timeout=5) as r:
                                data = json.loads(r.read().decode("utf-8"))
                                
                                # Mescla os nós baixados da Origem
                                from trust_network import NodeRecord
                                merged = 0
                                for nid, nd in data.get("nodes", {}).items():
                                    try:
                                        trust.nodes[nid] = NodeRecord.from_dict(nd)
                                        merged += 1
                                    except Exception:
                                        pass
                                trust._mark_dirty()
                                trust.flush()
                                log.info(f"[TrustDaemon] Trust Network baixada da Origem ({merged} nós).")
                        except Exception as e:
                            log.debug(f"[TrustDaemon] Falha ao baixar trust do Origem: {e}")

                # ==========================================
                # PROCESSO LOCAL (Adiciona chaves, remove fantasmas)
                # ==========================================
                h = chain.db.height()
                test_block = chain.db.get_block(h)
                if not test_block:
                    stop_event.wait(120)
                    continue

                # 1. Garante que a chave local está no Trust
                local_pk = ""
                if password:
                    try:
                        w = load_current_wallet(password)
                        if w and w.get("ok"):
                            local_pk = (w.get("public_key") or w.get("pub_hex") or "").strip().lower()
                            if len(local_pk) == 66 and local_pk not in trust.nodes:
                                addr = address_from_pubkey(bytes.fromhex(local_pk))
                                trust.register_node(node_id=local_pk, address=addr, public_key=local_pk, peer_addr="127.0.0.1:6001")
                                trust.nodes[local_pk].trust_score = 150.0
                                trust.nodes[local_pk].is_validator = True
                                log.info(f"[TrustDaemon] Chave local adicionada ao Trust: {local_pk[:16]}...")
                    except Exception as e:
                        log.debug(f"[TrustDaemon] Erro lendo carteira local: {e}")

                # 2. Varre os peers ativos no banco de dados (se tiver P2P)
                if p2p and is_origin: # Apenas a Origem faz a varredura ativa no banco
                    db = ChainDB("brn_v2_chain.db")
                    peers = db.listar_peers(apenas_ativos=True, janela=1800)
                    db.close()

                    for p in peers:
                        pk = (p.get("public_key") or "").strip().lower()
                        peer_addr = p.get("address", "")
                        
                        if "127.0.0.1" in peer_addr:
                            continue
                        
                        meu_ip = os.environ.get("BRN_SEU_ENDERECO", "")
                        if meu_ip and peer_addr == meu_ip:
                            continue

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
                                pass
                        
                        if len(pk) == 66 and pk.startswith(("02", "03")):
                            if pk not in trust.nodes:
                                addr = address_from_pubkey(bytes.fromhex(pk))
                                trust.register_node(node_id=pk, address=addr, public_key=pk, peer_addr=peer_addr)
                                trust.nodes[pk].trust_score = 150.0
                                trust.nodes[pk].is_validator = True
                                trust.nodes[pk].connected = True
                                log.info(f"[TrustDaemon] Novo validador adicionado: {pk[:16]}...")
                            else:
                                trust.nodes[pk].last_seen = time.time()
                                trust.nodes[pk].connected = True
                                if trust.nodes[pk].trust_score < 150:
                                    trust.nodes[pk].trust_score = 150.0
                                trust.nodes[pk].is_validator = True

                # 3. Remove os "fantasmas" (Offline há mais de 180 segundos)
                removidos = trust.remove_ghosts(max_age_s=180)
                if removidos > 0:
                    log.warning(f"[TrustDaemon] Removidos {removidos} validador(es) inativo(s).")

                # 4. Salva o arquivo
                trust.flush()
                
                validadores_atuais = [n for n in trust.nodes.values() if n.is_validator]
                log.info(f"[TrustDaemon] Rede verificada. Validadores ativos: {len(validadores_atuais)}")

            except Exception as e:
                log.error(f"[TrustDaemon] Erro no loop: {e}")

            if stop_event.wait(120):
                break
                
        log.info("[TrustDaemon] Encerrado.")

    t = threading.Thread(target=_loop, daemon=True, name="TrustDaemon")
    t.start()
    return t
