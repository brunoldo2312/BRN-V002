"""
tailscale_discover.py — Descobre o Origem automaticamente via Tailscale
================================================================
Problema: O Tailscale bloqueia Multicast (UDP 50007), então o
discovery_v2.py não acha peers automaticamente.

Solução: Este script usa `tailscale status --json` para listar
todos os peers, testa o endpoint /api/chain-info de cada um,
e escolhe o que tem a MAIOR ALTURA (blockchain completa).
Depois, atualiza o BRN_BOOTSTRAP_PEERS no brn_network.env.
================================================================
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "brn_network.env"


def get_tailscale_peers() -> list:
    """Retorna lista de IPs Tailscale (incluindo a própria máquina)."""
    try:
        out = subprocess.check_output(
            ["tailscale", "status", "--json"],
            text=True, timeout=10, stderr=subprocess.DEVNULL,
        )
        data = json.loads(out)
        peers = []

        # Própria máquina
        self_node = data.get("Self", {})
        for ip in self_node.get("TailscaleIPs", []):
            if ip.startswith("100."):
                peers.append({
                    "ip": ip,
                    "hostname": self_node.get("HostName", "self"),
                    "self": True,
                })
                break

        # Outros peers online
        for pid, p in data.get("Peer", {}).items():
            if not p.get("Online"):
                continue
            for ip in p.get("TailscaleIPs", []):
                if ip.startswith("100."):
                    peers.append({
                        "ip": ip,
                        "hostname": p.get("HostName", pid[:8]),
                        "self": False,
                    })
                    break
        return peers
    except FileNotFoundError:
        print("[X] Tailscale não encontrado. Instale ou rode --skip-tailscale.")
        return []
    except subprocess.TimeoutExpired:
        print("[X] Tailscale demorou demais para responder.")
        return []
    except Exception as e:
        print(f"[X] Erro ao consultar Tailscale: {e}")
        return []


def probe_brn(ip: str, port: int = 5000, timeout: float = 3.0):
    """Pergunta /api/chain-info para saber a altura do nó."""
    try:
        url = f"http://{ip}:{port}/api/chain-info"
        req = urllib.request.Request(url, headers={"User-Agent": "brn-discover/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {
                "height": data.get("height", -1),
                "difficulty": data.get("difficulty", 0),
                "tip": data.get("tip_hash", "")[:16],
            }
    except Exception:
        return None


def scan_peers(peers: list, port: int = 5000) -> list:
    """Testa cada peer e retorna a lista enriquecida."""
    results = []
    for p in peers:
        info = probe_brn(p["ip"], port)
        if info:
            results.append({**p, **info, "reachable": True})
        else:
            results.append({**p, "reachable": False, "height": -1})
    return results


def find_best_peer(results: list):
    """Encontra o peer com maior altura (blockchain mais completa)."""
    reachable = [r for r in results if r.get("reachable") and not r.get("self")]
    if not reachable:
        reachable = [r for r in results if r.get("reachable")]
    if not reachable:
        return None
    # Ordena por altura (maior primeiro), depois por difficulty (maior)
    reachable.sort(key=lambda x: (x["height"], x["difficulty"]), reverse=True)
    return reachable[0]


def update_env(peer_ip: str, peer_port: int = 6001) -> bool:
    """Atualiza BRN_BOOTSTRAP_PEERS no brn_network.env."""
    if not ENV_FILE.exists():
        print(f"[!] {ENV_FILE.name} não existe")
        return False

    new_value = f"{peer_ip}:{peer_port}"
    lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
    new_lines, found = [], False
    for line in lines:
        if line.strip().startswith("BRN_BOOTSTRAP_PEERS="):
            new_lines.append(f"BRN_BOOTSTRAP_PEERS={new_value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"BRN_BOOTSTRAP_PEERS={new_value}")

    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="BRN — Descoberta automática via Tailscale")
    p.add_argument("--watch", action="store_true", help="Roda em loop contínuo")
    p.add_argument("--interval", type=int, default=60, help="Intervalo entre scans (s)")
    p.add_argument("--port", type=int, default=5000, help="Porta HTTP da API")
    p.add_argument("--p2p-port", type=int, default=6001, help="Porta P2P do bootstrap")
    p.add_argument("--no-update", action="store_true", help="Só imprime, não altera o .env")
    p.add_argument("--self", action="store_true", help="Inclui a própria máquina no scan")
    args = p.parse_args()

    print("=" * 62)
    print(" BRN — Descoberta Automática via Tailscale")
    print("=" * 62)

    while True:
        print(f"\n[{time.strftime('%H:%M:%S')}] Consultando Tailscale...")
        peers = get_tailscale_peers()
        if not peers:
            print("[!] Nenhum peer Tailscale online encontrado.")
            if not args.watch:
                return 1
            time.sleep(args.interval)
            continue

        if not args.self:
            peers = [p for p in peers if not p["self"]]

        print(f"[*] {len(peers)} peer(s) online. Testando porta {args.port}...\n")
        results = scan_peers(peers, args.port)

        # Tabela
        print(f"{'IP':<18} {'Hostname':<22} {'Altura':>8} {'Diff':>5}  Status")
        print("-" * 70)
        for r in results:
            if r["reachable"]:
                print(f"{r['ip']:<18} {r['hostname'][:21]:<22} {r['height']:>8} {r['difficulty']:>5}  ✅")
            else:
                print(f"{r['ip']:<18} {r['hostname'][:21]:<22} {'—':>8} {'—':>5}  ❌ offline")

        best = find_best_peer(results)
        if best:
            print()
            print(f">>> MELHOR PEER: {best['ip']} ({best['hostname']})")
            print(f"    Altura: {best['height']} | Difficulty: {best['difficulty']} | Tip: {best['tip']}...")
            if not args.no_update:
                if update_env(best["ip"], args.p2p_port):
                    print(f"    ✅ brn_network.env atualizado:")
                    print(f"       BRN_BOOTSTRAP_PEERS={best['ip']}:{args.p2p_port}")
                else:
                    print("    [!] Não consegui atualizar o .env")
        else:
            print("\n[!] Nenhum peer respondeu na porta", args.port)
            print("    Verifique se o nó Origem está rodando e o firewall liberou a porta 5000.")

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
