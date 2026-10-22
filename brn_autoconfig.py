"""brn_autoconfig.py — Auto-configuração do nó BRN (v2)
================================================================
v2 (2026-10-22):
  [FIX] CRÍTICO: NameError em `changes` quando não há peers detectados
        nem bootstrap existente. O `if` pulava o `else` que definia
        `changes`, mas a linha `needs_restart = (changes[...])` ainda
        era executada → crash.
  [FIX] CRÍTICO: `except urllib.error.HTTPError` quebrava com NameError
        porque `urllib.error` não estava importado explicitamente
        (dependia de import transitivo de `urllib.request`, que não
        é garantido pelo Python spec).
  [NEW] --version  (mostra versão e sai)
  [NEW] --peers IP:PORT[,IP:PORT]  (adiciona peer(s) manualmente)
  [NEW] --remove IP:PORT           (remove peer específico)
  [NEW] --reset                    (limpa TODOS os peers)
  [NEW] --port PORT                (override BRN_P2P_PORT para o probe)
  [NEW] --timeout SECONDS          (timeout global para testes)
  [NEW] --json                     (saída JSON para automação)
  [NEW] --probe                    (só testa, não altera config)
  [FIX] Filtra self IP da lista de peers detectados (antes poderia
        se auto-adicionar como peer se Tailscale retornasse o self
        por bug de parsing)
  [FIX] Filtra Docker IPs (172.17.0.0/16, 172.18.0.0/16, etc.)
  [FIX] Filtra loopback (127.0.0.0/8) e 0.0.0.0
  [FIX] Detecta se `brn_network.env` não existe e avisa (antes criava
        env file com apenas 1 chave, deletando o resto)
  [FIX] --auto/--yes: mensagem clara "não consigo reiniciar daqui"
        (antes fingia que reiniciava)
  [NEW] `tailscale ping` para cada peer (probe extra no relatório)
  [CLEAN] Removido parâmetro `env` morto de print_report()
v1:
  - Detecta Tailscale + LAN
  - Atualiza brn_network.env + bootstrap_peers.json
  - Consulta o nó via HTTP
  - Relatório com cores

Uso:
    python brn_autoconfig.py                # interativo
    python brn_autoconfig.py --auto         # sem perguntar (não reinicia)
    python brn_autoconfig.py --check        # dry-run
    python brn_autoconfig.py --probe        # só testa conectividade
    python brn_autoconfig.py --json         # saída para automação
    python brn_autoconfig.py --peers 1.2.3.4:6001,5.6.7.8:6001
    python brn_autoconfig.py --remove 1.2.3.4:6001
    python brn_autoconfig.py --reset
================================================================
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

__version__ = "2.0"


# ============================================================
# CONSTANTES
# ============================================================
BASE           = Path(__file__).resolve().parent
ENV_FILE       = BASE / "brn_network.env"
BOOTSTRAP_FILE = BASE / "bootstrap_peers.json"
PEERS_MANUAL   = BASE / "peers_manual.json"
TAILSCALE_PEER = BASE / "brn_tailscale_peer.txt"

API_BASE       = "http://127.0.0.1:5000"
EXPLORER_BASE  = "http://127.0.0.1:8080"

IS_WINDOWS = platform.system().lower().startswith("win")
IS_LINUX   = platform.system().lower() == "linux"

# IPs que nunca devem virar peer
_DOCKER_PREFIXES = ("172.17.", "172.18.", "172.19.", "172.20.",
                    "172.21.", "172.22.", "172.23.", "172.24.")


# ============================================================
# CORES ANSI (funciona no Linux e Windows 10+)
# ============================================================
class C:
    OK   = "\033[92m"
    WARN = "\033[93m"
    ERR  = "\033[91m"
    INFO = "\033[96m"
    BOLD = "\033[1m"
    RST  = "\033[0m"


def _try_enable_windows_ansi():
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


def ok(msg):    print(f"{C.OK}[ok]{C.RST} {msg}")
def warn(msg):  print(f"{C.WARN}[!]{C.RST} {msg}")
def err(msg):   print(f"{C.ERR}[X]{C.RST} {msg}")
def info(msg):  print(f"{C.INFO}[i]{C.RST} {msg}")
def title(msg): print(f"\n{C.BOLD}═══ {msg} ═══{C.RST}")


# ============================================================
# HELPERS
# ============================================================
def _is_invalid_peer_ip(ip: str, self_ip: str = "") -> bool:
    """Retorna True se o IP não deve ser adicionado como peer."""
    if not ip:
        return True
    if ip in ("0.0.0.0", "127.0.0.1", "localhost"):
        return True
    if ip.startswith("127."):
        return True
    if ip == self_ip:
        return True
    for prefix in _DOCKER_PREFIXES:
        if ip.startswith(prefix):
            return True
    return False


def run_cmd(cmd: list, timeout: float = 5.0) -> tuple:
    """Executa comando. Retorna (rc, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, shell=False,
        )
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except FileNotFoundError:
        return -1, "", "comando nao encontrado"
    except subprocess.TimeoutExpired:
        return -2, "", "timeout"
    except Exception as e:
        return -3, "", str(e)


def find_tailscale() -> str | None:
    """Encontra o executável do Tailscale (PATH ou caminhos conhecidos)."""
    for name in ("tailscale", "tailscale.exe"):
        rc, _, _ = run_cmd([name, "version"], timeout=3)
        if rc == 0:
            return name

    if IS_WINDOWS:
        for p in (
            r"C:\Program Files\Tailscale\tailscale.exe",
            r"C:\Program Files (x86)\Tailscale\tailscale.exe",
        ):
            if Path(p).exists():
                return p

    if IS_LINUX:
        for p in ("/usr/bin/tailscale", "/usr/local/bin/tailscale"):
            if Path(p).exists():
                return p

    return None


def http_get_json(url: str, timeout: float = 5.0):
    """GET JSON. Retorna (ok, data_or_msg)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brn-autoconfig/2"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"url: {e.reason}"
    except Exception as e:
        return False, str(e)


def tcp_check(host: str, port: int, timeout: float = 3.0) -> bool:
    """Testa TCP. Retorna True/False."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, int(port)))
        s.close()
        return True
    except Exception:
        return False


def load_env_file() -> dict:
    """Lê brn_network.env como dict."""
    out = {}
    if not ENV_FILE.exists():
        return out
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k:
                out[k] = v
    except Exception:
        pass
    return out


def save_env_file(data: dict) -> bool:
    """
    Reescreve brn_network.env preservando comentários.
    Retorna False se arquivo não existir (evita criar env mínimo
    que deletaria todas as outras chaves críticas).
    """
    if not ENV_FILE.exists():
        warn(f"{ENV_FILE.name} nao existe — pulando atualizacao do env")
        return False

    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        warn(f"falha lendo {ENV_FILE.name}: {e}")
        return False

    seen = set()
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            out.append(line)
            continue
        if "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in data:
                out.append(f"{k}={data[k]}")
                seen.add(k)
            else:
                out.append(line)
        else:
            out.append(line)

    for k, v in data.items():
        if k not in seen:
            out.append(f"{k}={v}")

    try:
        ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
        return True
    except Exception as e:
        warn(f"falha gravando {ENV_FILE.name}: {e}")
        return False


def save_bootstrap(peers: list) -> bool:
    """Escreve bootstrap_peers.json."""
    try:
        BOOTSTRAP_FILE.write_text(
            json.dumps(peers, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        warn(f"falha gravando {BOOTSTRAP_FILE.name}: {e}")
        return False


# ============================================================
# DETECÇÃO
# ============================================================
def detect_tailscale_ips() -> dict:
    """
    Retorna {"self": "100.x.x.x", "peers": [...], "names": {...},
             "available": bool, "ping_ok": set()}.
    """
    out = {"self": "", "peers": [], "names": {}, "available": False,
           "ping_ok": set()}

    ts = find_tailscale()
    if not ts:
        return out

    rc, stdout, _ = run_cmd([ts, "status", "--json"], timeout=5)
    if rc != 0:
        rc, stdout, _ = run_cmd([ts, "status"], timeout=5)
        if rc != 0:
            return out

    out["available"] = True

    if stdout.startswith("{"):
        try:
            data = json.loads(stdout)
            self_info = data.get("Self") or {}
            self_ips = self_info.get("TailscaleIPs") or []
            if self_ips:
                out["self"] = self_ips[0]
            for _, peer in (data.get("Peer") or {}).items():
                if peer.get("Online", False) is False:
                    continue
                ips = peer.get("TailscaleIPs") or []
                if not ips:
                    continue
                ip = ips[0]
                if ip and ip != out["self"] and not _is_invalid_peer_ip(ip, out["self"]):
                    out["peers"].append(ip)
                    out["names"][ip] = peer.get("HostName", ip)
            return out
        except Exception:
            pass

    for line in stdout.splitlines():
        m = re.match(r"^(100\.\d+\.\d+\.\d+)\s+(\S+)\s+", line)
        if m:
            ip, name = m.group(1), m.group(2)
            if "self" in line.lower() or ip == out["self"]:
                out["self"] = ip
                continue
            if ip not in out["peers"] and not _is_invalid_peer_ip(ip, out["self"]):
                out["peers"].append(ip)
                out["names"][ip] = name

    if not out["self"]:
        rc, ip_stdout, _ = run_cmd([ts, "ip", "-4"], timeout=5)
        if rc == 0:
            for line in ip_stdout.splitlines():
                line = line.strip()
                if line.startswith("100."):
                    out["self"] = line
                    break

    return out


def tailscale_ping(ip: str, timeout: float = 3.0) -> bool:
    """Testa `tailscale ping <ip>`. Retorna True se responder."""
    ts = find_tailscale()
    if not ts:
        return False
    rc, out, _ = run_cmd([ts, "ping", "-c", "1", "--timeout", f"{int(timeout)}s", ip],
                         timeout=timeout + 2)
    return rc == 0 and "pong" in out.lower()


def detect_lan_ips() -> list:
    """IPs da LAN local (filtra docker/loopback)."""
    ips = set()
    try:
        for probe in ("10.255.255.255", "192.168.255.255", "172.16.255.255"):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.settimeout(0.2)
                s.connect((probe, 1))
                ips.add(s.getsockname()[0])
            except Exception:
                pass
            finally:
                s.close()
    except Exception:
        pass
    try:
        for info_ in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info_[4][0]
            if not _is_invalid_peer_ip(ip):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


def same_subnet(ip1: str, ip2: str) -> bool:
    try:
        return ip1.rsplit(".", 1)[0] == ip2.rsplit(".", 1)[0]
    except Exception:
        return False


# ============================================================
# CONSULTAS AO NÓ
# ============================================================
def node_running() -> bool:
    ok_, _ = http_get_json(f"{API_BASE}/api/status")
    return ok_


def get_chain_info():
    return http_get_json(f"{API_BASE}/api/chain-info")


def get_sync_status():
    return http_get_json(f"{API_BASE}/api/sync/status")


def get_peers_score():
    return http_get_json(f"{API_BASE}/api/peers/score")


# ============================================================
# CONFIG HELPERS
# ============================================================
def _normalize_peer(entry: str, default_port: str = "6001") -> str:
    """Normaliza 'ip' ou 'ip:port' para 'ip:port'."""
    entry = entry.strip()
    if not entry:
        return ""
    if ":" in entry:
        return entry
    return f"{entry}:{default_port}"


def update_configs(peer_ips: list, dry: bool = False,
                   port_override: str | None = None) -> dict:
    """
    Atualiza brn_network.env e bootstrap_peers.json com os peers.
    Retorna {"env_changed": bool, "bootstrap_changed": bool,
             "env_skipped": bool}.
    """
    result = {
        "env_changed": False,
        "bootstrap_changed": False,
        "env_skipped": not ENV_FILE.exists(),
    }

    env = load_env_file()
    cur_peers_str = env.get("BRN_BOOTSTRAP_PEERS", "")
    cur_peers = [p.strip() for p in cur_peers_str.split(",") if p.strip()]

    p2p_port = port_override or env.get("BRN_P2P_PORT", "6001")

    new_peers = []
    for ip in peer_ips:
        entry = _normalize_peer(ip, p2p_port)
        if entry and entry not in new_peers:
            new_peers.append(entry)

    for old in cur_peers:
        if old not in new_peers:
            new_peers.append(old)

    new_peers_str = ",".join(new_peers)

    if new_peers_str != cur_peers_str:
        result["env_changed"] = True
        if not dry and ENV_FILE.exists():
            env["BRN_BOOTSTRAP_PEERS"] = new_peers_str
            if not save_env_file(env):
                result["env_changed"] = False
                result["env_skipped"] = True

    cur_json = []
    if BOOTSTRAP_FILE.exists():
        try:
            cur_json = json.loads(BOOTSTRAP_FILE.read_text(encoding="utf-8"))
            if not isinstance(cur_json, list):
                cur_json = []
        except Exception:
            cur_json = []

    merged = list(new_peers)
    for old in cur_json:
        if old not in merged:
            merged.append(old)

    if merged != cur_json:
        result["bootstrap_changed"] = True
        if not dry:
            if not save_bootstrap(merged):
                result["bootstrap_changed"] = False

    return result


# ============================================================
# RELATÓRIO
# ============================================================
def print_report(ts: dict, lan: list, peers_before: list,
                 peers_after: list, node_ok: bool, chain_ok: bool,
                 chain: dict, sync: dict, peers_db: dict,
                 timeout: float):
    print()
    print(f"{C.BOLD}{'=' * 64}{C.RST}")
    print(f"{C.BOLD}  RELATÓRIO FINAL{C.RST}")
    print(f"{C.BOLD}{'=' * 64}{C.RST}")

    title("Sistema")
    info(f"SO: {platform.system()} {platform.release()}")
    info(f"Hostname: {socket.gethostname()}")

    title("Tailscale")
    if not ts.get("available"):
        warn("Tailscale não disponível ou não instalado")
    else:
        ok(f"Meu IP: {ts['self'] or '?'}")
        if ts["peers"]:
            for ip in ts["peers"]:
                name = ts["names"].get(ip, "?")
                # Probe: tailscale ping
                ping_ok = tailscale_ping(ip, timeout=timeout)
                marker = "✓" if ping_ok else "✗"
                print(f"  {marker} {ip} ({name})")
        else:
            warn("Nenhum peer Tailscale encontrado")

    title("IPs locais (LAN)")
    for ip in lan:
        info(f"  {ip}")

    title("Configuração de peers")
    info(f"Antes:  {peers_before or '(vazio)'}")
    info(f"Depois: {peers_after or '(vazio)'}")
    if peers_before != peers_after:
        ok("Bootstrap atualizado")
    else:
        info("Bootstrap já estava correto")

    title("Nó")
    if not node_ok:
        err("HTTP :5000 não respondeu — o nó não está rodando?")
    else:
        ok("HTTP :5000 respondendo")
        if chain_ok:
            info(f"Altura:      {chain.get('height')}")
            info(f"Tip hash:    {str(chain.get('tip_hash', ''))[:32]}...")
            info(f"Dificuldade: {chain.get('difficulty')}")

        if sync.get("ok"):
            synced = sync.get("synced")
            prog = sync.get("progress", 0)
            if synced:
                ok(f"Sync: sincronizado ({prog}%)")
            else:
                warn(f"Sync: em andamento ({prog}%)")
            info(f"Local:  {sync.get('local_height')}")
            info(f"Target: {sync.get('target_height')}")
            info(f"Peers conhecidos: {sync.get('peers_known')}")

        peers_list = peers_db.get("peers", [])
        if peers_list:
            ok(f"Peers no DB: {len(peers_list)}")
            for p in peers_list:
                addr = p.get("address", "?")
                score = p.get("score", 0)
                last = p.get("last_seen", 0)
                age = int(time.time() - last) if last else 0
                info(f"  {addr:<24} score={score} last_seen={age}s atrás")
        else:
            warn("Nenhum peer no DB")

    title("Testes TCP")
    if peers_after:
        for p in peers_after:
            if ":" in p:
                ip, port = p.rsplit(":", 1)
                if tcp_check(ip, int(port), timeout=timeout):
                    ok(f"TCP {p} — alcançável")
                else:
                    warn(f"TCP {p} — inalcançável")
    else:
        info("(nenhum peer para testar)")

    print()
    print(f"{C.BOLD}{'=' * 64}{C.RST}")
    if node_ok and chain_ok and sync.get("synced"):
        print(f"{C.OK}{C.BOLD}  ✓ Nó sincronizado e operacional{C.RST}")
    elif node_ok and chain_ok:
        print(f"{C.WARN}{C.BOLD}  ⏳ Nó rodando, aguardando sync{C.RST}")
    elif node_ok:
        print(f"{C.WARN}{C.BOLD}  ⚠ Nó rodando mas chain vazia{C.RST}")
    else:
        print(f"{C.ERR}{C.BOLD}  ✗ Nó não está respondendo{C.RST}")
    print(f"{C.BOLD}{'=' * 64}{C.RST}")


def build_json_output(ts, lan, peers_before, peers_after,
                      node_ok, chain_ok, chain, sync, peers_db) -> dict:
    """Monta o objeto JSON de saída."""
    return {
        "version": __version__,
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "tailscale": {
            "available": ts.get("available", False),
            "self_ip": ts.get("self", ""),
            "peers": ts.get("peers", []),
            "names": ts.get("names", {}),
        },
        "lan_ips": lan,
        "peers_before": peers_before,
        "peers_after": peers_after,
        "node": {
            "ok": node_ok,
            "chain_ok": chain_ok,
            "height": chain.get("height") if chain_ok else None,
            "tip_hash": chain.get("tip_hash") if chain_ok else None,
            "difficulty": chain.get("difficulty") if chain_ok else None,
        },
        "sync": {
            "ok": sync.get("ok", False),
            "synced": sync.get("synced", False),
            "progress": sync.get("progress", 0),
            "local_height": sync.get("local_height"),
            "target_height": sync.get("target_height"),
            "peers_known": sync.get("peers_known", 0),
        },
        "peers_db": peers_db.get("peers", []),
        "timestamp": time.time(),
    }


# ============================================================
# CLI: --peers / --remove / --reset
# ============================================================
def _add_manual_peers(peer_args: str, port_override: str | None,
                      dry: bool) -> int:
    """--peers IP:PORT[,IP:PORT]"""
    if not peer_args:
        return 0
    p2p_port = port_override or load_env_file().get("BRN_P2P_PORT", "6001")
    entries = []
    for item in peer_args.split(","):
        entry = _normalize_peer(item, p2p_port)
        if entry:
            entries.append(entry)
    if not entries:
        err("--peers: nenhum endereço válido")
        return 1

    changes = update_configs(entries, dry=dry, port_override=port_override)
    if dry:
        info(f"(dry-run) adicionaria: {entries}")
    else:
        if changes["env_changed"]:
            ok("brn_network.env atualizado")
        if changes["bootstrap_changed"]:
            ok("bootstrap_peers.json atualizado")
        if changes["env_skipped"]:
            warn("brn_network.env nao existe — só bootstrap_peers.json foi alterado")
    return 0


def _remove_peer(peer_arg: str, dry: bool) -> int:
    """--remove IP:PORT"""
    if not peer_arg:
        return 1
    env = load_env_file()
    cur = [p.strip() for p in env.get("BRN_BOOTSTRAP_PEERS", "").split(",")
           if p.strip()]
    if peer_arg not in cur:
        warn(f"{peer_arg} não está no bootstrap")
        return 1
    new = [p for p in cur if p != peer_arg]
    if dry:
        info(f"(dry-run) removeria {peer_arg}, sobrariam {new}")
        return 0
    env["BRN_BOOTSTRAP_PEERS"] = ",".join(new)
    save_env_file(env)
    # bootstrap json
    if BOOTSTRAP_FILE.exists():
        try:
            cur_json = json.loads(BOOTSTRAP_FILE.read_text(encoding="utf-8"))
            if isinstance(cur_json, list):
                save_bootstrap([p for p in cur_json if p != peer_arg])
        except Exception:
            pass
    ok(f"removido: {peer_arg}")
    return 0


def _reset_peers(dry: bool) -> int:
    """--reset"""
    if dry:
        info("(dry-run) limparia TODOS os peers")
        return 0
    env = load_env_file()
    env["BRN_BOOTSTRAP_PEERS"] = ""
    save_env_file(env)
    save_bootstrap([])
    ok("bootstrap limpo (env + json)")
    return 0


# ============================================================
# MAIN
# ============================================================
def main() -> int:
    _try_enable_windows_ansi()

    p = argparse.ArgumentParser(prog="brn_autoconfig.py")
    p.add_argument("--version", action="store_true",
                   help="Mostra versão e sai")
    p.add_argument("--auto", action="store_true",
                   help="Aplica sem perguntar (NÃO reinicia automaticamente)")
    p.add_argument("--check", action="store_true",
                   help="Dry-run: só mostra, não altera")
    p.add_argument("--yes", action="store_true",
                   help="Equivale a --auto")
    p.add_argument("--probe", action="store_true",
                   help="Só testa conectividade, não altera nada")
    p.add_argument("--json", action="store_true",
                   help="Saída JSON (para automação)")
    p.add_argument("--peers", metavar="LIST", default=None,
                   help="Adiciona peers: '1.2.3.4:6001,5.6.7.8:6001'")
    p.add_argument("--remove", metavar="ADDR", default=None,
                   help="Remove peer específico: '1.2.3.4:6001'")
    p.add_argument("--reset", action="store_true",
                   help="Limpa TODOS os peers (env + json)")
    p.add_argument("--port", metavar="PORT", default=None,
                   help="Override BRN_P2P_PORT para probes")
    p.add_argument("--timeout", metavar="SECONDS", type=float, default=5.0,
                   help="Timeout de testes de rede (default 5s)")
    args = p.parse_args()

    if args.version:
        print(f"brn_autoconfig.py v{__version__}")
        return 0

    dry = args.check or args.probe

    # ---------- --peers / --remove / --reset (modo direto) ----------
    if args.peers is not None:
        return _add_manual_peers(args.peers, args.port, dry)
    if args.remove is not None:
        return _remove_peer(args.remove, dry)
    if args.reset:
        return _reset_peers(dry)

    # ---------- modo padrão ----------
    if not args.json:
        print()
        print(f"{C.BOLD}{'=' * 64}{C.RST}")
        print(f"{C.BOLD}  BRN Auto-Config v{__version__}{C.RST}")
        print(f"{C.BOLD}  {socket.gethostname()} — {platform.system()}{C.RST}")
        print(f"{C.BOLD}{'=' * 64}{C.RST}")

    # 1. Tailscale
    if not args.json:
        title("1. Detectando Tailscale")
    ts = detect_tailscale_ips()
    if not args.json:
        if not ts.get("available"):
            warn("Tailscale não encontrado")
        else:
            ok(f"Meu IP: {ts['self'] or '?'}")
            for ip in ts["peers"]:
                info(f"  peer: {ip} ({ts['names'].get(ip, '?')})")

    # 2. LAN
    if not args.json:
        title("2. Detectando IPs LAN")
    lan = detect_lan_ips()
    if not args.json:
        for ip in lan:
            info(f"  {ip}")

    # 3. Config atual
    env = load_env_file()
    cur_str = env.get("BRN_BOOTSTRAP_PEERS", "")
    cur_peers = [p.strip() for p in cur_str.split(",") if p.strip()]

    if not args.json:
        title("3. Configuração atual")
        info(f"BRN_BOOTSTRAP_PEERS = {cur_str or '(vazio)'}")
        if BOOTSTRAP_FILE.exists():
            try:
                cb = json.loads(BOOTSTRAP_FILE.read_text(encoding="utf-8"))
                info(f"bootstrap_peers.json = {cb}")
            except Exception:
                pass

    # 4. Determina peers
    if not args.json:
        title("4. Determinando peers corretos")

    candidate_ips = []
    for ip in ts.get("peers", []):
        if ip != ts.get("self") and ip not in candidate_ips:
            if not _is_invalid_peer_ip(ip, ts.get("self", "")):
                candidate_ips.append(ip)

    if not candidate_ips and lan:
        primary = lan[0] if lan else ""
        for ip in lan[1:]:
            if same_subnet(primary, ip) and ip not in candidate_ips:
                if not _is_invalid_peer_ip(ip):
                    candidate_ips.append(ip)

    if candidate_ips and not args.json:
        ok(f"Peers detectados: {candidate_ips}")
    elif not candidate_ips and not args.json:
        warn("Nenhum peer detectado automaticamente")

    if not candidate_ips and TAILSCALE_PEER.exists():
        try:
            content = TAILSCALE_PEER.read_text(encoding="utf-8").strip()
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    ip = line.split(":")[0]
                    if ip not in candidate_ips and not _is_invalid_peer_ip(ip):
                        candidate_ips.append(ip)
            if candidate_ips and not args.json:
                ok(f"Peers do brn_tailscale_peer.txt: {candidate_ips}")
        except Exception:
            pass

    # 5. Aplica configs
    if not args.json:
        title("5. Aplicando configuração")

    changes = {
        "env_changed": False,
        "bootstrap_changed": False,
        "env_skipped": not ENV_FILE.exists(),
    }
    peers_after = cur_peers

    if args.probe:
        if not args.json:
            info("(modo --probe) NÃO altera configs")
    elif not candidate_ips and not cur_peers:
        if not args.json:
            warn("Sem peers e sem bootstrap — pulando atualização")
    else:
        changes = update_configs(candidate_ips, dry=dry,
                                 port_override=args.port)

        if not args.json:
            if changes["env_changed"]:
                if dry:
                    info("(dry-run) brn_network.env SERIA alterado")
                else:
                    ok("brn_network.env atualizado")
            else:
                info("brn_network.env não precisou mudar")

            if changes["bootstrap_changed"]:
                if dry:
                    info("(dry-run) bootstrap_peers.json SERIA alterado")
                else:
                    ok("bootstrap_peers.json atualizado")
            else:
                info("bootstrap_peers.json não precisou mudar")

            if changes["env_skipped"]:
                warn("brn_network.env não existe — só bootstrap_peers.json foi alterado")

        # relê para o relatório
        if not dry:
            env = load_env_file()
            peers_after = [p.strip() for p in
                           env.get("BRN_BOOTSTRAP_PEERS", "").split(",")
                           if p.strip()]
        else:
            # em dry-run, mostra o que SERIA
            p2p_port = args.port or env.get("BRN_P2P_PORT", "6001")
            peers_after = list(cur_peers)
            for ip in candidate_ips:
                entry = _normalize_peer(ip, p2p_port)
                if entry and entry not in peers_after:
                    peers_after.append(entry)

    # 6. Consulta nó
    if not args.json:
        title("6. Consultando o nó")

    node_ok = node_running()
    chain_ok = False
    chain = {}
    sync = {}
    peers_db = {}

    if node_ok:
        if not args.json:
            ok("HTTP :5000 respondendo")
        ch_ok, ch = get_chain_info()
        if ch_ok and isinstance(ch, dict):
            chain_ok = True
            chain = ch

        s_ok, s = get_sync_status()
        if s_ok and isinstance(s, dict):
            sync = s

        p_ok, p_res = get_peers_score()
        if p_ok and isinstance(p_res, dict):
            peers_db = p_res
    elif not args.json:
        err("HTTP :5000 não respondeu")

    # 7. Relatório
    if args.json:
        output = build_json_output(ts, lan, cur_peers, peers_after,
                                   node_ok, chain_ok, chain, sync, peers_db)
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print_report(ts, lan, cur_peers, peers_after,
                     node_ok, chain_ok, chain, sync, peers_db,
                     timeout=args.timeout)

    # 8. Aviso de reinício
    needs_restart = (
        not dry
        and not args.probe
        and (changes["env_changed"] or changes["bootstrap_changed"])
        and node_ok
    )

    if needs_restart and not args.json:
        print()
        warn("Configuração alterada — reinicie o nó para aplicar.")
        print(f"  1. Vá na janela onde o nó está rodando")
        print(f"  2. Aperte Ctrl+C")
        print(f"  3. Rode novamente: python{'3' if IS_LINUX else ''} main.py --pool-client")
        if not (args.auto or args.yes):
            try:
                resp = input(f"\n{C.BOLD}Ver lembrete acima? (s/N): {C.RST}").strip().lower()
                if resp not in ("s", "sim", "y", "yes"):
                    info("Ok, apenas anote e reinicie depois.")
            except (EOFError, KeyboardInterrupt):
                pass

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelado pelo usuário.")
        sys.exit(130)
