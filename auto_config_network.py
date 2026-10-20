"""
auto_config_network.py — Configura a rede BRN automaticamente
================================================================
Detecta o IP Tailscale da sua máquina e atualiza:
  - brn_network.env (BRN_TRACKER e BRN_BOOTSTRAP_PEERS)
  - bootstrap_peers.json
  - brn_tailscale_peer.txt
================================================================
"""
import os
import sys
import json
import subprocess
import platform
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "brn_network.env"
BOOTSTRAP_FILE = BASE_DIR / "bootstrap_peers.json"
PEER_FILE = BASE_DIR / "brn_tailscale_peer.txt"

def get_tailscale_ip():
    """Tenta pegar o IP do Tailscale desta máquina."""
    try:
        # Tenta no Windows
        if platform.system().lower().startswith("win"):
            paths = [r"C:\Program Files\Tailscale\tailscale.exe", "tailscale"]
            for p in paths:
                try:
                    out = subprocess.check_output([p, "ip", "-4"], text=True, stderr=subprocess.DEVNULL)
                    for line in out.splitlines():
                        if line.startswith("100."):
                            return line.strip()
                except Exception:
                    continue
        else:
            # Tenta no Linux
            out = subprocess.check_output(["tailscale", "ip", "-4"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if line.startswith("100."):
                    return line.strip()
    except Exception:
        pass
    return None

def update_env_file(tracker_url, bootstrap_ip):
    """Atualiza as variáveis no brn_network.env"""
    if not ENV_FILE.exists():
        print("[!] brn_network.env não encontrado. Criando um novo...")
        ENV_FILE.write_text("")
    
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    new_lines = []
    
    has_tracker = False
    has_bootstrap = False
    
    for line in lines:
        if line.startswith("BRN_TRACKER="):
            new_lines.append(f"BRN_TRACKER={tracker_url}")
            has_tracker = True
        elif line.startswith("BRN_BOOTSTRAP_PEERS="):
            new_lines.append(f"BRN_BOOTSTRAP_PEERS={bootstrap_ip}")
            has_bootstrap = True
        else:
            new_lines.append(line)
            
    if not has_tracker:
        new_lines.append(f"BRN_TRACKER={tracker_url}")
    if not has_bootstrap:
        new_lines.append(f"BRN_BOOTSTRAP_PEERS={bootstrap_ip}")
        
    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    print(f"[OK] brn_network.env atualizado.")

def update_bootstrap_json(peer_list):
    """Atualiza o bootstrap_peers.json"""
    try:
        BOOTSTRAP_FILE.write_text(json.dumps(peer_list, indent=2), encoding="utf-8")
        print(f"[OK] bootstrap_peers.json atualizado: {peer_list}")
    except Exception as e:
        print(f"[X] Erro ao atualizar bootstrap_peers.json: {e}")

def main():
    print("=" * 60)
    print(" BRN - Auto Configurador de Rede")
    print("=" * 60)
    
    my_ip = get_tailscale_ip()
    if my_ip:
        print(f"[i] Seu IP Tailscale: {my_ip}")
    else:
        print("[!] Tailscale não encontrado. Usando IP local (LAN).")

    print("\nOnde está rodando o Tracker e o Validador (Origem)?")
    print("  1. Nesta máquina (serei o Tracker)")
    print("  2. Em outra máquina (vou me conectar a ela)")
    
    opcao = input("\nEscolha (1 ou 2): ").strip()
    
    if opcao == "1":
        # Sou o tracker
        ip_servidor = my_ip if my_ip else "127.0.0.1"
        tracker_url = f"http://{ip_servidor}:9000"
        bootstrap_ip = f"{ip_servidor}:6001"
        
        update_env_file(tracker_url, bootstrap_ip)
        update_bootstrap_json([bootstrap_ip])
        
        if my_ip:
            PEER_FILE.write_text(bootstrap_ip + "\n", encoding="utf-8")
            
        print(f"\n[SUCESSO] Esta máquina é o Tracker!")
        print(f"Para outras máquinas se conectarem, elas precisam usar o IP: {ip_servidor}")
        
    elif opcao == "2":
        # Sou cliente
        ip_servidor = input("Digite o IP do Servidor (ex: 100.111.253.8): ").strip()
        if not ip_servidor:
            print("[X] IP inválido.")
            return
            
        tracker_url = f"http://{ip_servidor}:9000"
        bootstrap_ip = f"{ip_servidor}:6001"
        
        update_env_file(tracker_url, bootstrap_ip)
        update_bootstrap_json([bootstrap_ip])
        PEER_FILE.write_text(bootstrap_ip + "\n", encoding="utf-8")
        
        print(f"\n[SUCESSO] Conectado ao Servidor em {ip_servidor}!")
        print(f"Tracker: {tracker_url}")
        print(f"Bootstrap: {bootstrap_ip}")
        
    else:
        print("[X] Opção inválida.")

if __name__ == "__main__":
    main()
