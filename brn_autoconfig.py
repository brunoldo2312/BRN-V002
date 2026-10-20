"""brn_autoconfig.py — Auto-configuração do nó BRN
================================================================
Detecta peers, corrige bootstrap, testa conectividade e sincroniza.

Uso:
    python brn_autoconfig.py            # modo interativo
    python brn_autoconfig.py --auto     # aplica sem perguntar
    python brn_autoconfig.py --check    # só mostra, não altera

Funciona em Linux e Windows (apenas stdlib).
Não requer dependências externas.
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
import urllib.request
from pathlib import Path


# ============================================================
# CONSTANTES
# ============================================================
BASE = Path(__file__).resolve().parent
ENV_FILE       = BASE / "brn_network.env"
BOOTSTRAP_FILE = BASE / "bootstrap_peers.json"
PEERS_MANUAL   = BASE / "peers_manual.json"
TAILSCALE_PEER = BASE / "brn_tailscale_peer.txt"

API_BASE       = "http://127.0.0.1:5000"
EXPLORER_BASE  = "http://127.0.0.1:8080"

IS_WINDOWS = platform.system().lower().startswith("win")
IS_LINUX   = platform.system().lower() == "linux"


# ============================================================
# CORES (ANSI) — funciona no Linux e Windows 10+
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
# UTILITÁRIOS
# ============================================================
def run_cmd(cmd: list, timeout: int = 5) -> tuple:
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
    """Encontra o executável do Tailscale."""
    # tenta PATH primeiro
    for name in ("tailscale", "tailscale.exe"):
        rc, _, _ = run_cmd([name, "version"])
        if rc == 0:
            return name

    # caminhos conhecidos no Windows
    if IS_WINDOWS:
        for p in (
            r"C:\Program Files\Tailscale\tailscale.exe",
            r"C:\Program Files (x86)\Tailscale\tailscale.exe",
        ):
            if Path(p).exists():
                return p

    # caminhos conhecidos no Linux
    if IS_LINUX:
        for p in ("/usr/bin/tailscale", "/usr/local/bin/tailscale"):
            if Path(p).exists():
                return p

    return None


def http_get_json(url: str, timeout: float = 5.0):
    """GET JSON. Retorna (ok, data_or_msg)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brn-autoconfig"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
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


def save_env_file(data: dict) -> None:
    """Reescreve o brn_network.env preservando comentários."""
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

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

    # adiciona chaves novas
    for k, v in data.items():
        if k not in seen:
            out.append(f"{k}={v}")

    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def save_bootstrap(peers: list) -> None:
    """Escreve bootstrap_peers.json."""
    BOOTSTRAP_FILE.write_text(
        json.dumps(peers, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ============================================================
# DETECÇÃO DE REDE
# ============================================================
def detect_tailscale_ips() -> dict:
    """
    Retorna {"self": "100.x.x.x", "peers": ["100.y.y.y", ...]}.
    Lista vazia se Tailscale não estiver disponível.
    """
    out = {"self": "", "peers": [], "names": {}, "available": False}

    ts = find_tailscale()
    if not ts:
        return out

    rc, stdout, _ = run_cmd([ts, "status", "--json"], timeout=5)
    if rc != 0:
        # fallback para texto
        rc, stdout, _ = run_cmd([ts, "status"], timeout=5)
        if rc != 0:
            return out

    out["available"] = True

    # ---- versão JSON ----
    if stdout.startswith("{"):
        try:
            data = json.loads(stdout)
            self_info = data.get("Self") or {}
            out["self"] = (self_info.get("TailscaleIPs") or [""])[0]
            for _, peer in (data.get("Peer") or {}).items():
                if peer.get("Online", False) is False:
                    continue
                ips = peer.get("TailscaleIPs") or []
                if not ips:
                    continue
                ip = ips[0]
                if ip and ip != out["self"]:
                    out["peers"].append(ip)
                    out["names"][ip] = peer.get("HostName", ip)
            return out
        except Exception:
            pass

    # ---- versão texto ----
    for line in stdout.splitlines():
        # formato: "100.x.x.x   hostname    user@    os   ..."
        m = re.match(r"^(100\.\d+\.\d+\.\d+)\s+(\S+)\s+", line)
        if m:
            ip, name = m.group(1), m.group(2)
            if "self" in line.lower() or ip == out["self"]:
                out["self"] = ip
                continue
            if ip not in out["peers"]:
                out["peers"].append(ip)
                out["names"][ip] = name

    # tenta também "tailscale ip -4"
    if not out["self"]:
        rc, ip_stdout, _ = run_cmd([ts, "ip", "-4"], timeout=5)
        if rc == 0:
            for line in ip_stdout.splitlines():
                line = line.strip()
                if line.startswith("100."):
                    out["self"] = line
                    break

    return out


def detect_lan_ips() -> list:
    """IPs da LAN local."""
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
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
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
    ok, _ = http_get_json(f"{API_BASE}/api/status")
    return ok


def get_chain_info():
    return http_get_json(f"{API_BASE}/api/chain-info")


def get_sync_status():
    return http_get_json(f"{API_BASE}/api/sync/status")


def get_peers_score():
    return http_get_json(f"{API_BASE}/api/peers/score")


# ============================================================
# AÇÃO: reescrever configs
# ============================================================
def update_configs(peer_ips: list, dry: bool = False) -> dict:
    """
    Atualiza brn_network.env e bootstrap_peers.json com os peers.
    Retorna {"env_changed": bool, "bootstrap_changed": bool}.
    """
    result = {"env_changed": False, "bootstrap_changed": False}

    # ---------- brn_network.env ----------
    env = load_env_file()
    cur_peers_str = env.get("BRN_BOOTSTRAP_PEERS", "")
    cur_peers = [p.strip() for p in cur_peers_str.split(",") if p.strip()]

    # normaliza para "ip:6001"
    new_peers = []
    p2p_port = env.get("BRN_P2P_PORT", "6001")
    for ip in peer_ips:
        if ":" in ip:
            entry = ip
        else:
            entry = f"{ip}:{p2p_port}"
        if entry not in new_peers:
            new_peers.append(entry)

    # conserva peers manuais já existentes que não conflitam
    for old in cur_peers:
        if old not in new_peers:
            new_peers.append(old)

    new_peers_str = ",".join(new_peers)

    if new_peers_str != cur_peers_str:
        result["env_changed"] = True
        if not dry:
            env["BRN_BOOTSTRAP_PEERS"] = new_peers_str
            save_env_file(env)

    # ---------- bootstrap_peers.json ----------
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
            save_bootstrap(merged)

    return result


# ============================================================
# AÇÃO: reiniciar o nó
# ============================================================
def restart_node_confirmation() -> bool:
    print()
    warn("Para aplicar a nova configuração, o nó precisa ser reiniciado.")
    warn("Isso vai interromper a mineração por ~10s.")
    print()
    try:
        resp = input(f"{C.BOLD}Reiniciar agora? (s/n): {C.RST}").strip().lower()
        return resp in ("s", "sim", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


# ============================================================
# RELATÓRIO FINAL
# ============================================================
def print_report(ts: dict, lan: list, env: dict, peers_before: list,
                 peers_after: list, node_ok: bool, chain_ok: bool,
                 chain: dict, sync: dict, peers_db: dict):
    print()
    print(f"{C.BOLD}{'=' * 64}{C.RST}")
    print(f"{C.BOLD}  RELATÓRIO FINAL{C.RST}")
    print(f"{C.BOLD}{'=' * 64}{C.RST}")

    # ----- Sistema -----
    title("Sistema")
    info(f"SO: {platform.system()} {platform.release()}")
    info(f"Hostname: {socket.gethostname()}")

    # ----- Tailscale -----
    title("Tailscale")
    if not ts.get("available"):
        warn("Tailscale não disponível ou não instalado")
    else:
        ok(f"Meu IP: {ts['self'] or '?'}")
        if ts["peers"]:
            for ip in ts["peers"]:
                name = ts["names"].get(ip, "?")
                ok(f"Peer: {ip} ({name})")
        else:
            warn("Nenhum peer Tailscale encontrado")

    # ----- LAN -----
    title("IPs locais (LAN)")
    for ip in lan:
        info(f"  {ip}")

    # ----- Config -----
    title("Configuração de peers")
    info(f"Antes:  {peers_before or '(vazio)'}")
    info(f"Depois: {peers_after or '(vazio)'}")
    if peers_before != peers_after:
        ok("Bootstrap atualizado")
    else:
        info("Bootstrap já estava correto")

    # ----- Nó -----
    title("Nó")
    if not node_ok:
        err("HTTP :5000 não respondeu — o nó não está rodando?")
    else:
        ok("HTTP :5000 respondendo")
        if chain_ok:
            info(f"Altura:    {chain.get('height')}")
            info(f"Tip hash:  {str(chain.get('tip_hash', ''))[:32]}...")
            info(f"Dificuldade: {chain.get('difficulty')}")

        # Sync
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

        # Peers DB
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

    # ----- Testes TCP -----
    title("Testes TCP")
    if peers_after:
        for p in peers_after:
            if ":" in p:
                ip, port = p.rsplit(":", 1)
                if tcp_check(ip, int(port)):
                    ok(f"TCP {p} — alcançável")
                else:
                    warn(f"TCP {p} — inalcançável")
    else:
        info("(nenhum peer para testar)")

    # ----- Conclusão -----
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


# ============================================================
# MAIN
# ============================================================
def main() -> int:
    _try_enable_windows_ansi()

    parser = argparse.ArgumentParser(prog="brn_autoconfig.py")
    parser.add_argument("--auto", action="store_true",
                        help="Aplica mudanças sem perguntar")
    parser.add_argument("--check", action="store_true",
                        help="Só mostra, não altera nada")
    parser.add_argument("--yes", action="store_true",
                        help="Reinicia o nó automaticamente se precisar")
    args = parser.parse_args()

    print()
    print(f"{C.BOLD}{'=' * 64}{C.RST}")
    print(f"{C.BOLD}  BRN Auto-Config{C.RST}")
    print(f"{C.BOLD}  {socket.gethostname()} — {platform.system()}{C.RST}")
    print(f"{C.BOLD}{'=' * 64}{C.RST}")

    # ---------- 1. Tailscale ----------
    title("1. Detectando Tailscale")
    ts = detect_tailscale_ips()
    if not ts.get("available"):
        warn("Tailscale não encontrado no PATH")
        warn("Instale ou continue apenas com LAN")
    else:
        ok(f"Tailscale instalado — meu IP: {ts['self'] or '?'}")
        for ip in ts["peers"]:
            info(f"  peer: {ip} ({ts['names'].get(ip, '?')})")

    # ---------- 2. LAN ----------
    title("2. Detectando IPs LAN")
    lan = detect_lan_ips()
    for ip in lan:
        info(f"  {ip}")

    # ---------- 3. Config atual ----------
    title("3. Configuração atual")
    env = load_env_file()
    cur_str = env.get("BRN_BOOTSTRAP_PEERS", "")
    cur_peers = [p.strip() for p in cur_str.split(",") if p.strip()]
    info(f"BRN_BOOTSTRAP_PEERS = {cur_str or '(vazio)'}")

    cur_bootstrap = []
    if BOOTSTRAP_FILE.exists():
        try:
            cur_bootstrap = json.loads(BOOTSTRAP_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    info(f"bootstrap_peers.json = {cur_bootstrap or '(vazio)'}")

    # ---------- 4. Determina peers ----------
    title("4. Determinando peers corretos")

    # Preferência: IPs Tailscale dos peers (exceto self)
    candidate_ips = []
    for ip in ts.get("peers", []):
        if ip != ts.get("self") and ip not in candidate_ips:
            candidate_ips.append(ip)

    # Fallback: IPs LAN na mesma sub-rede
    if not candidate_ips and lan:
        primary = lan[0]
        for ip in lan[1:]:
            if same_subnet(primary, ip) and ip not in candidate_ips:
                candidate_ips.append(ip)

    if candidate_ips:
        ok(f"Peers detectados: {candidate_ips}")
    else:
        warn("Nenhum peer detectado automaticamente")
        # tenta ler do arquivo brn_tailscale_peer.txt
        if TAILSCALE_PEER.exists():
            content = TAILSCALE_PEER.read_text(encoding="utf-8").strip()
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    ip = line.split(":")[0]
                    if ip not in candidate_ips:
                        candidate_ips.append(ip)
            if candidate_ips:
                ok(f"Peers do brn_tailscale_peer.txt: {candidate_ips}")

    # ---------- 5. Aplica configs ----------
    title("5. Aplicando configuração")
    if not candidate_ips and not cur_peers:
        warn("Sem peers e sem bootstrap — pulando atualização")
        peers_after = cur_peers
    else:
        changes = update_configs(
            candidate_ips, dry=args.check
        )
        if changes["env_changed"]:
            if args.check:
                info("(modo --check) brn_network.env SERIA alterado")
            else:
                ok("brn_network.env atualizado")
        else:
            info("brn_network.env não precisou mudar")

        if changes["bootstrap_changed"]:
            if args.check:
                info("(modo --check) bootstrap_peers.json SERIA alterado")
            else:
                ok("bootstrap_peers.json atualizado")
        else:
            info("bootstrap_peers.json não precisou mudar")

        # relê para o relatório
        env = load_env_file()
        peers_after = [p.strip() for p in
                       env.get("BRN_BOOTSTRAP_PEERS", "").split(",")
                       if p.strip()]

    # ---------- 6. Consulta o nó ----------
    title("6. Consultando o nó")
    node_ok = node_running()
    chain_ok = False
    chain = {}
    sync = {}
    peers_db = {}

    if node_ok:
        ok("HTTP :5000 respondendo")
        ch_ok, ch = get_chain_info()
        if ch_ok and isinstance(ch, dict):
            chain_ok = True
            chain = ch

        s_ok, s = get_sync_status()
        if s_ok and isinstance(s, dict):
            sync = s

        p_ok, p = get_peers_score()
        if p_ok and isinstance(p, dict):
            peers_db = p
    else:
        err("HTTP :5000 não respondeu")

    # ---------- 7. Relatório ----------
    print_report(ts, lan, env, cur_peers, peers_after,
                 node_ok, chain_ok, chain, sync, peers_db)

    # ---------- 8. Reiniciar se necessário ----------
    needs_restart = (
        not args.check
        and (changes["env_changed"] or changes["bootstrap_changed"])
        and node_ok
    )

    if needs_restart:
        if args.auto or args.yes:
            info("Modo --auto/--yes: reiniciando o nó...")
            warn("Reinicie manualmente: Ctrl+C na janela do nó e suba de novo")
            info("(Não consigo reiniciar o processo do nó daqui)")
        else:
            if restart_node_confirmation():
                print()
                warn("Faça manualmente:")
                print(f"  1. Vá na janela onde o nó está rodando")
                print(f"  2. Aperte Ctrl+C")
                print(f"  3. Rode: python{'3' if IS_LINUX else ''} main.py --client-mode --no-tailscale --headless")
            else:
                info("Reinício adiado — você pode fazer quando quiser")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelado pelo usuário.")
        sys.exit(130)
