"""brn_autodiscover.py — Auto-descoberta unificada via Tailscale (v1.0)
================================================================
Substitui brn_tailscale_setup.py e tailscale_discover.py.

Diferenças:
  - Testa altura REAL via /api/chain-info em cada peer
  - Descobre a porta P2P correta (6001 ou 6002) por peer
  - Atualiza .env E bootstrap_peers.json de forma consistente
  - Estratégias: highest (maior altura), most_common (altura mais comum), exact=N
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENV_FILE = BASE / "brn_network.env"
BOOTSTRAP_FILE = BASE / "bootstrap_peers.json"

# Portas P2P candidatas (testadas em ordem)
P2P_CANDIDATES = [6002, 6001]


# ============================================================
# TAILSCALE
# ============================================================
def tailscale_cmd() -> list[str] | None:
    try:
        r = subprocess.run(["tailscale", "version"],
                           capture_output=True, timeout=5)
        if r.returncode == 0:
            return ["tailscale"]
    except Exception:
        pass
    for p in [r"C:\Program Files\Tailscale\tailscale.exe",
              r"C:\Program Files (x86)\Tailscale\tailscale.exe",
              "/usr/bin/tailscale", "/usr/local/bin/tailscale"]:
        if os.path.exists(p):
            return [p]
    return None


def list_tailscale_peers(ts_cmd: list[str]) -> list[dict]:
    try:
        out = subprocess.check_output(ts_cmd + ["status", "--json"],
                                       text=True, timeout=10,
                                       stderr=subprocess.DEVNULL)
        data = json.loads(out)
    except Exception as e:
        print(f"[X] tailscale status falhou: {e}")
        return []

    peers = []
    self_node = data.get("Self", {})
    for ip in self_node.get("TailscaleIPs", []):
        if ip.startswith("100."):
            peers.append({"ip": ip, "hostname": self_node.get("HostName", "self"),
                          "self": True})
            break

    for pid, p in data.get("Peer", {}).items():
        if not p.get("Online"):
            continue
        for ip in p.get("TailscaleIPs", []):
            if ip.startswith("100."):
                peers.append({"ip": ip,
                              "hostname": p.get("HostName", pid[:8]),
                              "self": False})
                break
    return peers


# ============================================================
# PROBES
# ============================================================
def probe_http_chain_info(ip: str, port: int = 5000,
                          timeout: float = 4.0) -> dict | None:
    """GET /api/chain-info. Retorna {'height','difficulty','tip'} ou None."""
    try:
        url = f"http://{ip}:{port}/api/chain-info"
        req = urllib.request.Request(url, headers={"User-Agent": "brn-discover/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            return {
                "height": int(data.get("height", -1)),
                "difficulty": int(data.get("difficulty", 0)),
                "tip": str(data.get("tip_hash", ""))[:16],
            }
    except Exception:
        return None


def probe_p2p(ip: str, port: int, timeout: float = 2.0) -> bool:
    """Testa se a porta P2P está aberta (TCP)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.close()
        return True
    except Exception:
        return False


def find_p2p_port(ip: str) -> int | None:
    """Descobre a porta P2P (testa 6002, 6001)."""
    for port in P2P_CANDIDATES:
        if probe_p2p(ip, port):
            return port
    return None


# ============================================================
# ESTRATÉGIAS
# ============================================================
def choose_peer(probed: list[dict], strategy: str,
                exact: int | None = None) -> dict | None:
    """Escolhe o melhor peer baseado na estratégia."""
    reachable = [p for p in probed if p.get("reachable") and not p.get("self")]
    if not reachable:
        return None

    if strategy == "exact":
        if exact is None:
            print("[X] --strategy=exact requer --height N")
            return None
        matching = [p for p in reachable if p["height"] == exact]
        if not matching:
            print(f"[!] Nenhum peer com altura={exact}")
            return None
        reachable = matching

    elif strategy == "most_common":
        counts = Counter(p["height"] for p in reachable)
        if counts:
            most = counts.most_common(1)[0][0]
            matching = [p for p in reachable if p["height"] == most]
            if matching:
                reachable = matching

    # 'highest' (default): ordena por altura desc
    reachable.sort(key=lambda p: (p["height"], p["difficulty"]), reverse=True)
    return reachable[0]


# ============================================================
# UPDATE CONFIGS
# ============================================================
def update_env(ip: str, p2p_port: int, http_port: int = 5000) -> bool:
    if not ENV_FILE.exists():
        print(f"[!] {ENV_FILE.name} não existe")
        return False

    new_bootstrap = f"{ip}:{p2p_port}"
    new_pool = f"http://{ip}:{http_port}"

    lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
    new_lines = []
    found_bootstrap = False
    found_pool = False
    found_fallback = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("BRN_BOOTSTRAP_PEERS="):
            new_lines.append(f"BRN_BOOTSTRAP_PEERS={new_bootstrap}")
            found_bootstrap = True
        elif stripped.startswith("BRN_POOL_URL="):
            new_lines.append(f"BRN_POOL_URL={new_pool}")
            found_pool = True
        elif stripped.startswith("BRN_FALLBACK_PEERS="):
            new_lines.append(f"BRN_FALLBACK_PEERS={new_bootstrap}")
            found_fallback = True
        else:
            new_lines.append(line)

    if not found_bootstrap:
        new_lines.append(f"BRN_BOOTSTRAP_PEERS={new_bootstrap}")
    if not found_pool:
        new_lines.append(f"BRN_POOL_URL={new_pool}")
    if not found_fallback:
        new_lines.append(f"BRN_FALLBACK_PEERS={new_bootstrap}")

    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return True


def update_bootstrap_json(ip: str, p2p_port: int) -> bool:
    entry = f"{ip}:{p2p_port}"
    data = [entry]
    try:
        if BOOTSTRAP_FILE.exists():
            existing = json.loads(BOOTSTRAP_FILE.read_text(encoding="utf-8"))
            if existing == data:
                return False
    except Exception:
        pass
    BOOTSTRAP_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


# ============================================================
# MAIN
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(
        description="BRN — auto-descoberta unificada via Tailscale")
    ap.add_argument("--strategy", choices=["highest", "most_common", "exact"],
                    default="highest",
                    help="Como escolher o peer (default: highest)")
    ap.add_argument("--height", type=int, default=None,
                    help="Altura exata (com --strategy=exact)")
    ap.add_argument("--watch", action="store_true",
                    help="Loop contínuo")
    ap.add_argument("--interval", type=int, default=60,
                    help="Intervalo do --watch (s)")
    ap.add_argument("--no-update", action="store_true",
                    help="Só mostra, não altera .env/bootstrap")
    ap.add_argument("--self", action="store_true",
                    help="Inclui a própria máquina no scan")
    args = ap.parse_args()

    print("=" * 66)
    print(" BRN — Auto-descoberta via Tailscale")
    print(f" Estratégia: {args.strategy}" +
          (f" (altura={args.height})" if args.strategy == "exact" else ""))
    print("=" * 66)

    ts = tailscale_cmd()
    if not ts:
        print("[X] Tailscale não instalado. Use --skip-tailscale se não quiser.")
        return 1

    while True:
        print(f"\n[{time.strftime('%H:%M:%S')}] Consultando Tailscale...")
        peers = list_tailscale_peers(ts)
        if not peers:
            print("[!] Nenhum peer online.")
            if not args.watch:
                return 1
            time.sleep(args.interval)
            continue

        if not args.self:
            peers = [p for p in peers if not p["self"]]

        print(f"[*] {len(peers)} peer(s) para testar\n")
        print(f"{'IP':<16} {'Hostname':<22} {'Altura':>8} {'Diff':>5} {'P2P':>6}  Status")
        print("-" * 76)

        probed = []
        for p in peers:
            info = probe_http_chain_info(p["ip"], 5000)
            if info:
                p2p = find_p2p_port(p["ip"])
                probed.append({
                    **p, **info,
                    "reachable": True,
                    "p2p_port": p2p,
                })
                p2p_str = str(p2p) if p2p else "—"
                print(f"{p['ip']:<16} {p['hostname'][:21]:<22} "
                      f"{info['height']:>8} {info['difficulty']:>5} "
                      f"{p2p_str:>6}  OK")
            else:
                probed.append({**p, "reachable": False, "height": -1,
                               "p2p_port": None})
                print(f"{p['ip']:<16} {p['hostname'][:21]:<22} "
                      f"{'—':>8} {'—':>5} {'—':>6}  offline")

        best = choose_peer(probed, args.strategy, args.height)
        if not best:
            print("\n[!] Nenhum peer respondeu em HTTP:5000.")
            if not args.watch:
                return 1
            time.sleep(args.interval)
            continue

        print()
        print(f">>> MELHOR: {best['ip']} ({best['hostname']})")
        print(f"    Altura: {best['height']} | Diff: {best['difficulty']} "
              f"| P2P port: {best['p2p_port'] or '?'}")

        if best["p2p_port"] is None:
            print("[X] Porta P2P não encontrada (6001/6002 fechadas).")
            if not args.watch:
                return 1
            time.sleep(args.interval)
            continue

        if not args.no_update:
            if update_env(best["ip"], best["p2p_port"], 5000):
                print(f"    [ok] brn_network.env atualizado")
            if update_bootstrap_json(best["ip"], best["p2p_port"]):
                print(f"    [ok] bootstrap_peers.json atualizado")

        if not args.watch:
            return 0
        print(f"\n[*] Próximo scan em {args.interval}s... (Ctrl+C para parar)")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[*] Cancelado.")
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[*] Cancelado.")
        sys.exit(0)
