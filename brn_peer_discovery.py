"""
brn_peer_discovery.py — Descoberta automática de peers via Tailscale
================================================================
Lê `tailscale status` e atualiza bootstrap_peers.json com TODOS
os peers Linux (Ubuntu origin) encontrados.

Chamado no boot do main.py. Também pode rodar em loop (refresh).

Fallback em camadas:
  1. tailscale status --json   (preferido — parser robusto)
  2. tailscale status          (texto puro)
  3. arquivo brn_tailscale_peer.txt  (fallback manual)

Uso:
    from brn_peer_discovery import refresh_peers_from_tailscale
    peers = refresh_peers_from_tailscale()
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("peer_discovery")

BASE_DIR = Path(__file__).resolve().parent
BOOTSTRAP_FILE = BASE_DIR / "bootstrap_peers.json"
PEER_FILE = BASE_DIR / "brn_tailscale_peer.txt"
P2P_PORT = int(os.environ.get("BRN_P2P_PORT", "6001"))


# ============================================================
# HELPERS DE SISTEMA
# ============================================================
def _find_tailscale() -> Optional[str]:
    """Retorna o caminho/alias do tailscale ou None."""
    # 1) Tenta no PATH
    for name in ("tailscale", "tailscale.exe"):
        try:
            r = subprocess.run([name, "version"],
                               capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                return name
        except Exception:
            pass

    # 2) Caminhos Windows default
    if sys.platform.startswith("win"):
        for p in (r"C:\Program Files\Tailscale\tailscale.exe",
                  r"C:\Program Files (x86)\Tailscale\tailscale.exe"):
            if os.path.exists(p):
                return p

    # 3) Caminhos Linux default
    for p in ("/usr/bin/tailscale", "/usr/local/bin/tailscale"):
        if os.path.exists(p):
            return p

    return None


def _run_ts(args: List[str], timeout: int = 5) -> tuple:
    """Executa tailscale <args>. Retorna (rc, stdout, stderr)."""
    ts = _find_tailscale()
    if not ts:
        return -1, "", "tailscale nao encontrado"

    try:
        r = subprocess.run(
            [ts] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return -2, "", "timeout"
    except Exception as e:
        return -3, "", str(e)


# ============================================================
# PARSERS
# ============================================================
def _get_my_ips() -> List[str]:
    """Retorna a lista de IPs Tailscale desta máquina."""
    rc, out, _ = _run_ts(["ip", "-4"])
    if rc != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip().startswith("100.")]


def _parse_status_json(data: dict) -> List[dict]:
    """Extrai peers do JSON de `tailscale status --json`."""
    peers = []
    self_info = data.get("Self") or {}
    self_ips = set(self_info.get("TailscaleIPs") or [])

    for _, peer in (data.get("Peer") or {}).items():
        if peer.get("Online") is False:
            continue

        ips = peer.get("TailscaleIPs") or []
        if not ips:
            continue

        # Pega IP principal (primeiro 100.x)
        ip = next((i for i in ips if i.startswith("100.")), ips[0])
        if ip in self_ips:
            continue

        os_name = (peer.get("OS") or "").lower()
        hostname = peer.get("HostName") or ip

        peers.append({
            "ip": ip,
            "hostname": hostname,
            "os": os_name,
            "online": True,
        })
    return peers


def _parse_status_text(output: str, self_ips: List[str]) -> List[dict]:
    """Parse da saída de `tailscale status` em texto."""
    peers = []
    self_set = set(self_ips)

    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()
        if len(parts) < 4:
            continue

        ip = parts[0]
        if not ip.startswith("100."):
            continue
        if ip in self_set:
            continue

        hostname = parts[1]
        # OS geralmente está na coluna 3 ou 4
        os_name = ""
        for p in parts[2:]:
            pl = p.lower()
            if pl in ("linux", "windows", "macos", "ios", "android"):
                os_name = pl
                break

        peers.append({
            "ip": ip,
            "hostname": hostname,
            "os": os_name,
            "online": True,
        })
    return peers


# ============================================================
# DESCOBERTA PRINCIPAL
# ============================================================
def discover_tailscale_peers() -> List[dict]:
    """
    Descobre peers via Tailscale. Tenta JSON primeiro, texto depois.
    Retorna lista de dicts {ip, hostname, os, online}.
    """
    self_ips = _get_my_ips()

    # --- 1) Tenta --json ---
    rc, out, _ = _run_ts(["status", "--json"])
    if rc == 0 and out.startswith("{"):
        try:
            data = json.loads(out)
            peers = _parse_status_json(data)
            if peers:
                log.info("discovery: %d peer(s) encontrados via JSON", len(peers))
                return peers
        except Exception as e:
            log.debug("parse JSON falhou: %s", e)

    # --- 2) Tenta texto puro ---
    rc, out, _ = _run_ts(["status"])
    if rc == 0:
        peers = _parse_status_text(out, self_ips)
        if peers:
            log.info("discovery: %d peer(s) encontrados via texto", len(peers))
            return peers

    log.warning("discovery: nenhum peer encontrado via Tailscale")
    return []


def refresh_peers_from_tailscale(
    prefer_os: str = "linux",
    write_file: bool = True,
) -> List[str]:
    """
    Descobre peers e atualiza bootstrap_peers.json.

    Args:
        prefer_os: tipo de OS preferido (peers desse tipo vêm primeiro)
        write_file: se True, salva em bootstrap_peers.json

    Returns:
        Lista de strings "ip:porta" prontas para o P2P.
    """
    peers_info = discover_tailscale_peers()
    if not peers_info:
        # Fallback: lê arquivo manual
        return _read_peer_file()

    # Ordena: prefer_os primeiro
    def sort_key(p):
        return 0 if p["os"] == prefer_os else 1

    peers_info.sort(key=sort_key)

    entries = []
    for p in peers_info:
        entry = f"{p['ip']}:{P2P_PORT}"
        if entry not in entries:
            entries.append(entry)
            log.info("discovery: peer %s (%s, %s)",
                     entry, p["hostname"], p["os"] or "?")

    if write_file and entries:
        try:
            BOOTSTRAP_FILE.write_text(
                json.dumps(entries, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info("discovery: bootstrap_peers.json atualizado (%d peer(s))",
                     len(entries))
        except Exception as e:
            log.warning("discovery: falha salvando bootstrap: %s", e)

    return entries


def _read_peer_file() -> List[str]:
    """Fallback: lê brn_tailscale_peer.txt."""
    if not PEER_FILE.exists():
        return []
    try:
        content = PEER_FILE.read_text(encoding="utf-8")
        entries = []
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                if ":" not in line:
                    line = line + ":" + str(P2P_PORT)
                entries.append(line)
        if entries:
            log.info("discovery: %d peer(s) do arquivo %s",
                     len(entries), PEER_FILE.name)
        return entries
    except Exception as e:
        log.warning("discovery: falha lendo %s: %s", PEER_FILE.name, e)
        return []


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 60)
    print(" BRN Peer Discovery — Auto-teste")
    print("=" * 60)

    my_ips = _get_my_ips()
    print(f"\nMeus IPs Tailscale: {my_ips or '(nenhum)'}")

    peers = discover_tailscale_peers()
    print(f"\nPeers detectados ({len(peers)}):")
    for p in peers:
        print(f"  {p['ip']:<18} {p['hostname']:<32} OS={p['os'] or '?'}")

    entries = refresh_peers_from_tailscale()
    print(f"\nBootstrap final ({len(entries)}):")
    for e in entries:
        print(f"  {e}")