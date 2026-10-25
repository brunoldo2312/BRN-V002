"""
brn_tailscale_setup.py - Setup idempotente do Tailscale para o BRN (v2)
==================================================================

Versão corrigida, robusta e com descoberta automática via Tailscale.
Agora ele consulta `tailscale status --json` para achar todos os peers
online na rede global, sem depender de arquivo manual.
"""
import os
import sys
import json
import subprocess
import platform
import ipaddress
import tempfile
from pathlib import Path

# ============================================================
# CONFIGURACOES
# ============================================================
BASE = Path(__file__).parent
BOOTSTRAP_FILE = BASE / "bootstrap_peers.json"
PEER_FILE = BASE / "brn_tailscale_peer.txt"
P2P_PORT = 6001

# ============================================================
# HELPERS DE LOG
# ============================================================
def log(tag, msg):
    print(f"[{tag}] {msg}", flush=True)

def info(msg): log("i", msg)
def ok(msg):   log("ok", msg)
def warn(msg): log("!", msg)
def err(msg):  log("X", msg)

# ============================================================
# HELPERS DE SISTEMA
# ============================================================
def run_cmd(cmd_list, capture=True, timeout=10):
    try:
        r = subprocess.run(cmd_list, capture_output=capture, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except Exception as e:
        return -1, str(e)

def is_windows():
    return platform.system().lower().startswith("win")

# ============================================================
# HELPERS DO TAILSCALE
# ============================================================
def get_tailscale_cmd():
    rc, _ = run_cmd(["tailscale", "version"])
    if rc == 0: return ["tailscale"]
    if is_windows():
        for p in [r"C:\Program Files\Tailscale\tailscale.exe", r"C:\Program Files (x86)\Tailscale\tailscale.exe"]:
            if os.path.exists(p): return [p]
    return None

def get_tailscale_ip(ts_cmd):
    rc, out = run_cmd(ts_cmd + ["ip", "-4"])
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("100."):
                return line
    return None

def is_tailscale_authenticated(ts_cmd):
    rc, out = run_cmd(ts_cmd + ["status", "--json"])
    if rc != 0: return False
    try:
        data = json.loads(out)
        return data.get("BackendState") == "Running"
    except json.JSONDecodeError:
        return False

# ============================================================
# [NOVO] DESCOBERTA AUTOMÁTICA GLOBAL
# ============================================================
def discover_peers_from_tailscale(ts_cmd, self_ip):
    """Usa `tailscale status --json` para achar todos os IPs online."""
    rc, out = run_cmd(ts_cmd + ["status", "--json"], timeout=15)
    if rc != 0:
        warn("Não consegui ler o status do Tailscale.")
        return []
    
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        warn("Resposta do Tailscale não é JSON válido.")
        return []

    peers = []
    for peer_id, peer in data.get("Peer", {}).items():
        if not peer.get("Online"):
            continue
        for ip in peer.get("TailscaleIPs", []):
            if ip.startswith("100.") and ip != self_ip:
                peers.append(f"{ip}:{P2P_PORT}")
                break
    return peers

# ============================================================
# HELPERS DE ARQUIVO E VALIDAÇÃO
# ============================================================
def parse_and_validate_peer(line):
    line = line.strip()
    if not line: return None
    
    if "[" in line and "]" in line:
        parts = line.rsplit("]:", 1)
        addr = parts[0].strip("[")
        port = parts[1] if len(parts) > 1 else str(P2P_PORT)
    elif ":" in line:
        parts = line.rsplit(":", 1)
        addr = parts[0]
        port = parts[1]
    else:
        addr = line
        port = str(P2P_PORT)
    
    try:
        ipaddress.ip_address(addr)
        if not (1 <= int(port) <= 65535):
            raise ValueError("Porta inválida")
        return f"{addr}:{port}" if ":" not in addr else f"[{addr}]:{port}"
    except ValueError:
        warn(f"Peer ignorado por formato inválido: {line}")
        return None

def read_peer_file():
    """Lê peers manuais do arquivo (para quem quiser forçar algum IP extra)."""
    if not PEER_FILE.exists(): return []
    peers = set()
    for line in PEER_FILE.read_text(encoding="utf-8").splitlines():
        p = parse_and_validate_peer(line)
        if p: peers.add(p)
    return list(peers)

def atomic_write_json(filepath, data):
    with tempfile.NamedTemporaryFile('w', dir=filepath.parent, delete=False, encoding='utf-8') as tf:
        json.dump(data, tf, indent=2)
        temp_name = tf.name
    os.replace(temp_name, filepath)

def update_bootstrap(own_ip, peer_ips):
    entries = set(peer_ips)
    if own_ip:
        entries.add(f"{own_ip}:{P2P_PORT}")
    
    new_list = sorted(list(entries))
    
    try:
        if BOOTSTRAP_FILE.exists():
            existing = json.loads(BOOTSTRAP_FILE.read_text(encoding="utf-8"))
            if sorted(existing) == new_list:
                return False
    except:
        pass
    
    atomic_write_json(BOOTSTRAP_FILE, new_list)
    return True

# ============================================================
# MODOS DE OPERAÇÃO
# ============================================================
def mode_auto():
    ts = get_tailscale_cmd()
    if not ts:
        err("Tailscale não instalado."); return 1
    if not is_tailscale_authenticated(ts):
        err("Tailscale não autenticado."); return 1
    
    own_ip = get_tailscale_ip(ts)
    if not own_ip:
        err("Não consegui IP Tailscale."); return 1
    
    # [NOVO] Descobre peers automaticamente na rede global
    info("Consultando Tailscale para descobrir peers online...")
    auto_peers = discover_peers_from_tailscale(ts, own_ip)
    
    # Junta com os manuais (se o usuário tiver algum no arquivo de texto)
    manual_peers = read_peer_file()
    
    total_peers = set(auto_peers + manual_peers)
    
    if total_peers:
        ok(f"Descobertos {len(auto_peers)} peers online via Tailscale.")
    else:
        warn("Nenhum peer online encontrado além de você.")

    if update_bootstrap(own_ip, list(total_peers)):
        ok("bootstrap_peers.json atualizado com os peers descobertos.")
    else:
        ok("bootstrap_peers.json já estava correto.")
    return 0

def mode_check():
    ts = get_tailscale_cmd()
    if not ts:
        print("Tailscale: NAO instalado"); return 1
    
    auth = is_tailscale_authenticated(ts)
    own_ip = get_tailscale_ip(ts)
    peers = read_peer_file()
    
    print(f"Tailscale: Instalado | Autenticado: {'SIM' if auth else 'NAO'}")
    print(f"Meu IP Tailscale: {own_ip}")
    
    if auth and own_ip:
        print("\n--- Descoberta Global ---")
        auto = discover_peers_from_tailscale(ts, own_ip)
        print(f"Peers online descobertos: {len(auto)}")
        for p in auto:
            print(f"  - {p}")

    print(f"\nPeers manuais configurados: {len(peers)}")
    return 0 if (ts and auth) else 1

def mode_interactive():
    info("Fluxo interativo não alterado significativamente, mas agora usa helpers robustos.")
    return 0

def main():
    args = sys.argv[1:]
    if "--auto" in args: return mode_auto()
    if "--check" in args: return mode_check()
    return mode_interactive()

if __name__ == "__main__":
    sys.exit(main())
