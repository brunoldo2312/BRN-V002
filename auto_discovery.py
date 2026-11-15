# auto_discovery.py
import json
import subprocess
import logging
import os

log = logging.getLogger("auto_discovery")

def get_tailscale_peers() -> list:
    """
    Pede ao Tailscale a lista de todos os dispositivos na rede.
    Retorna uma lista de IPs (100.x.x.x) que estão online.
    """
    peers = []
    try:
        # Executa o comando do tailscale para pegar o status em JSON
        result = subprocess.run(
            ["tailscale", "status", "--json"], 
            capture_output=True, text=True, timeout=5
        )
        
        if result.returncode != 0:
            log.debug("[AUTO-DISCOVERY] Tailscale não está rodando ou falhou.")
            return []

        data = json.loads(result.stdout)
        
        # Pega o IP do próprio nó
        self_ip = data.get("Self", {}).get("TailscaleIPs", [])[0] if data.get("Self") else None

        # Pega os IPs dos outros nós
        for peer_id, peer_data in data.get("Peer", {}).items():
            if not peer_data.get("Online"):
                continue
                
            ts_ips = peer_data.get("TailscaleIPs", [])
            if ts_ips:
                ip = ts_ips[0]
                if ip != self_ip:
                    # Adiciona a porta P2P padrão do projeto
                    peers.append(f"{ip}:6001")
                    
        log.info("[AUTO-DISCOVERY] Encontrados %d peers via Tailscale.", len(peers))
        return peers
        
    except FileNotFoundError:
        log.debug("[AUTO-DISCOVERY] Comando 'tailscale' não encontrado no sistema.")
        return []
    except Exception as e:
        log.warning("[AUTO-DISCOVERY] Erro ao buscar peers no Tailscale: %s", e)
        return []

def auto_configure_bootstrap():
    """
    Atualiza a variável de ambiente BRN_BOOTSTRAP_PEERS com os IPs do Tailscale.
    """
    current_bootstrap = os.environ.get("BRN_BOOTSTRAP_PEERS", "").strip()
    current_list = [x.strip() for x in current_bootstrap.split(",") if x.strip()]
    
    # Busca os IPs online no Tailscale
    ts_peers = get_tailscale_peers()
    
    # Junta a lista manual (do .env) com a lista automática (do Tailscale)
    all_peers = set(current_list + ts_peers)
    
    if all_peers:
        new_bootstrap = ",".join(all_peers)
        os.environ["BRN_BOOTSTRAP_PEERS"] = new_bootstrap
        log.info("[AUTO-DISCOVERY] Bootstrap atualizado: %s", new_bootstrap)