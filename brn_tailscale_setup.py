"""
brn_tailscale_setup.py - Setup idempotente do Tailscale para o BRN (v3)
==================================================================
v3:
  [NEW] Auto-login usando TS_AUTH_KEY do .env
"""
import os
import sys
import json
import subprocess
import platform
import ipaddress
import tempfile
from pathlib import Path

BASE = Path(__file__).parent
BOOTSTRAP_FILE = BASE / "bootstrap_peers.json"
PEER_FILE = BASE / "brn_tailscale_peer.txt"
P2P_PORT = 6001

def log(tag, msg): print(f"[{tag}] {msg}", flush=True)
def info(msg): log("i", msg)
def ok(msg):   log("ok", msg)
def warn(msg): log("!", msg)
def err(msg):  log("X", msg)

def run_cmd(cmd_list, capture=True, timeout=15):
    try:
        r = subprocess.run(cmd_list, capture_output=capture, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except Exception as e:
        return -1, str(e)

def is_windows():
    return platform.system().lower().startswith("win")

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
# [NOVO] AUTO-LOGIN COM TS_AUTH_KEY
# ============================================================
def login_with_key(ts_cmd, auth_key):
    info("Tentando login automatico com TS_AUTH_KEY...")
    rc, out = run_cmd(ts_cmd + ["up", "--authkey", auth_key, "--accept-routes=false", "--hostname", "brn-node"], timeout=60)
    if rc == 0:
        ok("Login automatico concluido com sucesso!")
        return True
    else:
        err(f"Falha no login automatico: {out}")
        return False

# ============================================================
# DESCOBERTA GLOBAL
# ============================================================
def discover_peers_from_tailscale(ts_cmd, self_ip):
    rc, out = run_cmd(ts_cmd + ["status", "--json"], timeout=15)
    if rc != 0: return []
    try: data = json.loads(out)
    except: return []

    peers = []
    for peer_id, peer in data.get("Peer", {}).items():
        if not peer.get("Online"): continue
        for ip in peer.get("TailscaleIPs", []):
            if ip.startswith("100.") and ip != self_ip:
                peers.append(f"{ip}:{P2P_PORT}")
                break
    return peers

def parse_and_validate_peer(line):
    line = line.strip()
    if not line: return None
    if "[" in line and "]" in line:
        parts = line.rsplit("]:", 1); addr = parts[0].strip("["); port = parts[1] if len(parts) > 1 else str(P2P_PORT)
    elif ":" in line:
        parts = line.rsplit(":", 1); addr = parts[0]; port = parts[1]
    else: addr = line; port = str(P2P_PORT)
    try:
        ipaddress.ip_address(addr)
        if not (1 <= int(port) <= 65535): raise ValueError()
        return f"{addr}:{port}" if ":" not in addr else f"[{addr}]:{port}"
    except: return None

def read_peer_file():
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
    if own_ip: entries.add(f"{own_ip}:{P2P_PORT}")
    new_list = sorted(list(entries))
    try:
        if BOOTSTRAP_FILE.exists():
            existing = json.loads(BOOTSTRAP_FILE.read_text(encoding="utf-8"))
            if sorted(existing) == new_list: return False
    except: pass
    atomic_write_json(BOOTSTRAP_FILE, new_list)
    return True

# ============================================================
# MODOS
# ============================================================
def mode_auto():
    ts = get_tailscale_cmd()
    if not ts:
        err("Tailscale não encontrado no sistema.")
        return 1
    
    if not is_tailscale_authenticated(ts):
        warn("Tailscale instalado, mas nao autenticado.")
        auth_key = os.environ.get("TS_AUTH_KEY", "").strip()
        if auth_key:
            if not login_with_key(ts, auth_key):
                err("Nao foi possivel logar automaticamente. Verifique o TS_AUTH_KEY.")
                return 1
        else:
            err("TS_AUTH_KEY vazia no .env. Abra o Tailscale e faca login manualmente.")
            return 1
    
    own_ip = get_tailscale_ip(ts)
    if not own_ip:
        err("Nao consegui obter o IP do Tailscale.")
        return 1
    
    info("Consultando Tailscale para descobrir peers online...")
    auto_peers = discover_peers_from_tailscale(ts, own_ip)
    manual_peers = read_peer_file()
    total_peers = set(auto_peers + manual_peers)
    
    if total_peers: ok(f"Descobertos {len(auto_peers)} peers online via Tailscale.")
    else: warn("Nenhum peer online encontrado.")

    if update_bootstrap(own_ip, list(total_peers)):
        ok("bootstrap_peers.json atualizado.")
    else:
        ok("bootstrap_peers.json ja estava correto.")
    return 0

def mode_check():
    ts = get_tailscale_cmd()
    if not ts: print("Tailscale: NAO instalado"); return 1
    auth = is_tailscale_authenticated(ts)
    print(f"Tailscale: Instalado | Autenticado: {'SIM' if auth else 'NAO'}")
    if auth:
        own_ip = get_tailscale_ip(ts)
        print(f"Meu IP Tailscale: {own_ip}")
        auto = discover_peers_from_tailscale(ts, own_ip)
        print(f"Peers online descobertos: {len(auto)}")
        for p in auto: print(f"  - {p}")
    return 0 if (ts and auth) else 1

def main():
    args = sys.argv[1:]
    if "--auto" in args: return mode_auto()
    if "--check" in args: return mode_check()
    return 0

if __name__ == "__main__":
    sys.exit(main())
