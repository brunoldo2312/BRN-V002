"""
brn_peer_discovery.py — Descoberta automática de peers via Tailscale (v2)
================================================================
v2 (2026-10-22):
  [NEW] __version__, __all__
  [NEW] Timeout configurável via BRN_TS_TIMEOUT (default 5s)
  [NEW] Filtros: self IP, Docker (172.x), loopback (127.x), 0.0.0.0
  [NEW] Hostname exclusion via BRN_PEER_EXCLUDE_HOSTS="host1,host2"
  [NEW] Cache de self IPs (chamado 1× por execução)
  [NEW] refresh_peers_from_tailscale(detailed=True) retorna dict
        com peers_info, self_ips, reason. Mantém compat com v1
        (default detailed=False retorna list[str])
  [NEW] CLI: --once (default), --loop N, --probe, --check, --json,
        --clear, --prefer-os OS, --version
  [NEW] --probe faz TCP check em cada peer antes de salvar
  [NEW] --clear limpa bootstrap se nenhum peer detectado
        (evita bootstrap stale quebrado)
  [NEW] Validação de brn_tailscale_peer.txt (rejeita IP inválido)
  [NEW] Detecção de "Tailscale deslogado" com mensagem clara
  [NEW] Sort: prefer_os > online > hostname (antes só prefer_os)
  [NEW] discover_tailscale_peers_detailed() retorna (peers, self, reason)
v1:
  - JSON primeiro, texto depois, arquivo como fallback
  - Atualiza bootstrap_peers.json
  - Chamado no boot do main.py

Uso (compat v1):
    from brn_peer_discovery import refresh_peers_from_tailscale
    peers = refresh_peers_from_tailscale()

Uso (CLI):
    python brn_peer_discovery.py              # 1 refresh + relatório
    python brn_peer_discovery.py --json       # saída JSON
    python brn_peer_discovery.py --loop 60    # refresh a cada 60s
    python brn_peer_discovery.py --probe      # testa TCP de cada peer
    python brn_peer_discovery.py --check      # não salva
    python brn_peer_discovery.py --clear      # limpa se vazio
================================================================
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

__version__ = "2.0"

__all__ = [
    "refresh_peers_from_tailscale",
    "discover_tailscale_peers",
    "discover_tailscale_peers_detailed",
    "read_peer_file",
    "P2P_PORT",
    "BOOTSTRAP_FILE",
    "PEER_FILE",
]


# ============================================================
# CONFIG
# ============================================================
BASE_DIR       = Path(__file__).resolve().parent
BOOTSTRAP_FILE = BASE_DIR / "bootstrap_peers.json"
PEER_FILE      = BASE_DIR / "brn_tailscale_peer.txt"

try:
    P2P_PORT = int(os.environ.get("BRN_P2P_PORT", "6001"))
except (ValueError, TypeError):
    P2P_PORT = 6001

try:
    TS_TIMEOUT = float(os.environ.get("BRN_TS_TIMEOUT", "5.0"))
except (ValueError, TypeError):
    TS_TIMEOUT = 5.0

try:
    PROBE_TIMEOUT = float(os.environ.get("BRN_TS_PROBE_TIMEOUT", "2.0"))
except (ValueError, TypeError):
    PROBE_TIMEOUT = 2.0

_EXCLUDE_HOSTS = set()
for _h in os.environ.get("BRN_PEER_EXCLUDE_HOSTS", "").split(","):
    _h = _h.strip().lower()
    if _h:
        _EXCLUDE_HOSTS.add(_h)

_DOCKER_PREFIXES = ("172.17.", "172.18.", "172.19.", "172.20.",
                    "172.21.", "172.22.", "172.23.", "172.24.")

log = logging.getLogger("peer_discovery")


# ============================================================
# HELPERS
# ============================================================
def _is_invalid_peer_ip(ip: str, self_ips: set) -> bool:
    """Retorna True se o IP não deve ser peer."""
    if not ip:
        return True
    if ip in ("0.0.0.0", "127.0.0.1", "localhost"):
        return True
    if ip.startswith("127."):
        return True
    if ip in self_ips:
        return True
    for prefix in _DOCKER_PREFIXES:
        if ip.startswith(prefix):
            return True
    # IPv4 validation
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return True
    return False


def _is_excluded_host(hostname: str) -> bool:
    """True se hostname está em BRN_PEER_EXCLUDE_HOSTS."""
    if not hostname or not _EXCLUDE_HOSTS:
        return False
    return hostname.strip().lower() in _EXCLUDE_HOSTS


def _tcp_check(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """TCP probe rápido (não envia dados, só connect)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, int(port)))
        s.close()
        return True
    except Exception:
        return False


def _find_tailscale() -> Optional[str]:
    """Retorna o caminho/alias do tailscale ou None."""
    # 1) PATH
    for name in ("tailscale", "tailscale.exe"):
        try:
            r = subprocess.run(
                [name, "version"],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0:
                return name
        except Exception:
            pass

    # 2) Windows default
    if sys.platform.startswith("win"):
        for p in (
            r"C:\Program Files\Tailscale\tailscale.exe",
            r"C:\Program Files (x86)\Tailscale\tailscale.exe",
        ):
            if os.path.exists(p):
                return p

    # 3) Linux/macOS default
    for p in (
        "/usr/bin/tailscale",
        "/usr/local/bin/tailscale",
        "/opt/homebrew/bin/tailscale",
    ):
        if os.path.exists(p):
            return p

    return None


def _run_ts(args: List[str], timeout: float = TS_TIMEOUT) -> Tuple[int, str, str]:
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
    return [
        line.strip() for line in out.splitlines()
        if line.strip().startswith("100.")
    ]


def _parse_status_json(data: dict, self_ips: set) -> List[dict]:
    """Extrai peers do JSON de `tailscale status --json`."""
    peers = []

    for _, peer in (data.get("Peer") or {}).items():
        # Online (False explícito é diferente de "não tem campo")
        if peer.get("Online") is False:
            continue

        ips = peer.get("TailscaleIPs") or []
        if not ips:
            continue

        ip = next((i for i in ips if i.startswith("100.")), ips[0])
        if _is_invalid_peer_ip(ip, self_ips):
            continue

        hostname = peer.get("HostName") or ip
        if _is_excluded_host(hostname):
            log.debug("excluindo hostname: %s", hostname)
            continue

        os_name = (peer.get("OS") or "").lower()
        online = peer.get("Online", True)

        peers.append({
            "ip":       ip,
            "hostname": hostname,
            "os":       os_name,
            "online":   bool(online),
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
        if len(parts) < 2:
            continue

        ip = parts[0]
        if not ip.startswith("100."):
            continue
        if _is_invalid_peer_ip(ip, self_set):
            continue

        hostname = parts[1] if len(parts) > 1 else ip
        if _is_excluded_host(hostname):
            continue

        os_name = ""
        for p in parts[2:]:
            pl = p.lower()
            if pl in ("linux", "windows", "macos", "ios", "android"):
                os_name = pl
                break

        # tailscale status mostra "offline" em algum lugar da linha
        online = "offline" not in line.lower()

        peers.append({
            "ip":       ip,
            "hostname": hostname,
            "os":       os_name,
            "online":   online,
        })
    return peers


# ============================================================
# DESCOBERTA
# ============================================================
def discover_tailscale_peers_detailed() -> Tuple[List[dict], List[str], str]:
    """
    [NEW v2] Retorna (peers, self_ips, reason).

    reason explica por que não achou peers:
      "ok"                     — peers encontrados
      "tailscale_nao_encontrado"
      "tailscale_deslogado"
      "sem_peers"
      "timeout"
    """
    self_ips = _get_my_ips()

    # --- 1) Tenta --json ---
    rc, out, err = _run_ts(["status", "--json"])
    if rc == -2:
        return [], self_ips, "timeout"
    if rc == 0 and out.startswith("{"):
        try:
            data = json.loads(out)
            peers = _parse_status_json(data, set(self_ips))
            if peers:
                log.info("discovery: %d peer(s) via JSON", len(peers))
                return peers, self_ips, "ok"
            # Sem peers mas JSON OK → pode estar deslogado ou sozinho
            self_info = data.get("Self") or {}
            if not self_info.get("TailscaleIPs"):
                return [], self_ips, "tailscale_deslogado"
            return [], self_ips, "sem_peers"
        except Exception as e:
            log.debug("parse JSON falhou: %s", e)

    # --- 2) Tenta texto puro ---
    rc, out, err = _run_ts(["status"])
    if rc == 0:
        if "Logged out" in out or "not logged in" in out.lower():
            return [], self_ips, "tailscale_deslogado"
        peers = _parse_status_text(out, self_ips)
        if peers:
            log.info("discovery: %d peer(s) via texto", len(peers))
            return peers, self_ips, "ok"
        return [], self_ips, "sem_peers"

    # --- 3) Fallback ---
    if not _find_tailscale():
        return [], self_ips, "tailscale_nao_encontrado"
    return [], self_ips, "sem_peers"


def discover_tailscale_peers() -> List[dict]:
    """
    Compat v1: retorna só a lista de peers (sem self_ips/reason).
    """
    peers, _, _ = discover_tailscale_peers_detailed()
    return peers


def read_peer_file() -> List[str]:
    """
    Fallback: lê brn_tailscale_peer.txt.
    Valida cada entrada (rejeita IP inválido).
    """
    if not PEER_FILE.exists():
        return []
    try:
        content = PEER_FILE.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("discovery: falha lendo %s: %s", PEER_FILE.name, e)
        return []

    entries = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Remove comentário inline
        if "#" in line:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue

        # Parse "ip:porta" ou só "ip"
        if ":" in line:
            ip, port_s = line.rsplit(":", 1)
            try:
                port = int(port_s)
                if not (1 <= port <= 65535):
                    log.debug("porta inválida em %r", raw_line)
                    continue
            except ValueError:
                log.debug("porta não numérica em %r", raw_line)
                continue
            entry = f"{ip}:{port}"
        else:
            ip = line
            port = P2P_PORT
            entry = f"{ip}:{port}"

        # Valida IP (aceita 100.x Tailscale + IPs públicos; rejeita self?
        # não sabemos self aqui, deixa pro caller filtrar)
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            log.debug("IP inválido em %r", raw_line)
            continue

        if ip in ("0.0.0.0", "127.0.0.1", "localhost"):
            continue

        if entry not in entries:
            entries.append(entry)

    if entries:
        log.info("discovery: %d peer(s) do arquivo %s", len(entries), PEER_FILE.name)
    return entries


# ============================================================
# REFRESH
# ============================================================
def refresh_peers_from_tailscale(
    prefer_os: str = "linux",
    write_file: bool = True,
    clear_if_empty: bool = False,
    probe: bool = False,
    detailed: bool = False,
) -> object:
    """
    Descobre peers e atualiza bootstrap_peers.json.

    Args:
        prefer_os:      OS preferido (peers desse tipo vêm primeiro)
        write_file:     salva em bootstrap_peers.json
        clear_if_empty: [NEW v2] se nenhum peer encontrado, LIMPA o bootstrap
                        (evita stale quebrado)
        probe:          [NEW v2] TCP check antes de adicionar
        detailed:       [NEW v2] retorna dict completo

    Returns:
        detailed=False (default, compat v1): List[str] "ip:porta"
        detailed=True: dict {
            "peers":      list[str],
            "peers_info": list[dict],
            "self_ips":   list[str],
            "reason":     str,
            "probed_ok":  int,
            "probed_fail": int,
        }
    """
    peers_info, self_ips, reason = discover_tailscale_peers_detailed()

    if not peers_info:
        # Fallback: arquivo manual
        file_peers = read_peer_file()
        if file_peers:
            peers_info = [
                {"ip": e.rsplit(":", 1)[0], "hostname": "?",
                 "os": "?", "online": True}
                for e in file_peers
            ]
            reason = "arquivo_fallback"

    # Ordena: prefer_os primeiro, online antes de offline, hostname como desempate
    def sort_key(p):
        return (
            0 if p["os"] == prefer_os else 1,
            0 if p.get("online", True) else 1,
            p.get("hostname", "").lower(),
        )

    peers_info.sort(key=sort_key)

    entries = []
    probed_ok = 0
    probed_fail = 0

    for p in peers_info:
        ip = p["ip"]
        entry = f"{ip}:{P2P_PORT}"
        if entry in entries:
            continue

        if probe:
            if _tcp_check(ip, P2P_PORT):
                probed_ok += 1
                log.info("probe OK: %s (%s)", entry, p.get("hostname", "?"))
                entries.append(entry)
            else:
                probed_fail += 1
                log.warning("probe FALHOU: %s (%s) — pulando",
                            entry, p.get("hostname", "?"))
        else:
            entries.append(entry)
            log.info("discovery: peer %s (%s, %s)",
                     entry, p.get("hostname", "?"), p.get("os") or "?")

    # Persiste no arquivo
    if write_file:
        if entries:
            try:
                BOOTSTRAP_FILE.write_text(
                    json.dumps(entries, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                log.info("discovery: %s atualizado (%d peer(s))",
                         BOOTSTRAP_FILE.name, len(entries))
            except Exception as e:
                log.warning("discovery: falha salvando bootstrap: %s", e)
        elif clear_if_empty:
            try:
                BOOTSTRAP_FILE.write_text("[]\n", encoding="utf-8")
                log.warning("discovery: bootstrap_peers.json LIMPO "
                            "(nenhum peer detectado)")
            except Exception as e:
                log.warning("discovery: falha limpando bootstrap: %s", e)

    if not detailed:
        return entries

    return {
        "peers":       entries,
        "peers_info":  peers_info,
        "self_ips":    self_ips,
        "reason":      reason,
        "probed_ok":   probed_ok,
        "probed_fail": probed_fail,
    }


# ============================================================
# CLI
# ============================================================
def _print_report(result: dict) -> None:
    peers = result["peers"]
    info = result["peers_info"]
    self_ips = result["self_ips"]
    reason = result["reason"]

    print()
    print("=" * 64)
    print(f" BRN Peer Discovery v{__version__}")
    print("=" * 64)

    print(f"\nMeus IPs Tailscale: {self_ips or '(nenhum)'}")
    print(f"Motivo: {reason}")

    if info:
        print(f"\nPeers detectados ({len(info)}):")
        print(f"  {'IP':<18} {'HOSTNAME':<32} {'OS':<10} ONLINE")
        print("  " + "-" * 74)
        for p in info:
            online = "✓" if p.get("online", True) else "✗"
            print(f"  {p['ip']:<18} {p['hostname']:<32} "
                  f"{p['os'] or '?':<10} {online}")
    else:
        print("\n( nenhum peer detectado )")

    if peers:
        print(f"\nBootstrap final ({len(peers)}):")
        for e in peers:
            print(f"  {e}")
    else:
        print(f"\n( bootstrap vazio )")

    if result.get("probed_ok") or result.get("probed_fail"):
        print(f"\nProbe TCP: {result['probed_ok']} OK, "
              f"{result['probed_fail']} falharam")


def _main() -> int:
    p = argparse.ArgumentParser(
        prog="brn_peer_discovery.py",
        description="Descobre peers BRN via Tailscale",
    )
    p.add_argument("--version", action="store_true",
                   help="mostra versão e sai")
    p.add_argument("--loop", metavar="SECONDS", type=float, default=0,
                   help="refresh contínuo a cada N segundos (0 = só 1×)")
    p.add_argument("--probe", action="store_true",
                   help="TCP check em cada peer antes de salvar")
    p.add_argument("--check", action="store_true",
                   help="NÃO salva em bootstrap_peers.json")
    p.add_argument("--clear", action="store_true",
                   help="limpa bootstrap se nenhum peer detectado")
    p.add_argument("--json", action="store_true",
                   help="saída JSON")
    p.add_argument("--prefer-os", default="linux",
                   help="OS preferido (default: linux)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="logging DEBUG")
    args = p.parse_args()

    if args.version:
        print(f"brn_peer_discovery.py v{__version__}")
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    def _once():
        t0 = time.time()
        result = refresh_peers_from_tailscale(
            prefer_os=args.prefer_os,
            write_file=not args.check,
            clear_if_empty=args.clear,
            probe=args.probe,
            detailed=True,
        )
        result["duration_s"] = round(time.time() - t0, 3)

        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            _print_report(result)
        return result

    if args.loop <= 0:
        _once()
        return 0

    # Loop mode
    print(f"Refresh loop a cada {args.loop:.0f}s "
          f"(Ctrl+C para sair)")
    try:
        while True:
            _once()
            time.sleep(args.loop)
    except KeyboardInterrupt:
        print("\nEncerrado.")
        return 0


if __name__ == "__main__":
    sys.exit(_main())
