#!/usr/bin/env python3
"""check_network.py — Diagnóstico completo de rede e consenso BRN
================================================================
Verifica em uma única execução:

  1. Config local (brn_network.env + env vars)
  2. Fingerprint da rede (deve bater nas duas máquinas)
  3. Node identity (Ed25519)
  4. Blockchain local (altura, tip, cumulative work)
  5. Peers P2P conectados
  6. Fork detection:
     - Compara tip_hash local com cada peer
     - Compara cumulative_work (regra Bitcoin)
     - Detecta fork quando mesma altura tem hashes diferentes
  7. Sync status (progresso, ETA)
  8. Miner status
  9. Últimos blocos
 10. Resumo (veredito final)

Uso:
    python3 check_network.py
    python3 check_network.py --json    # saída estruturada
    python3 check_network.py --quiet   # só o veredito

Exit codes:
    0 = rede OK, sem fork
    1 = fork detectado
    2 = rede errada (fingerprint diverge de peers)
    3 = nó offline (HTTP não responde)
================================================================
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("[X] requests nao instalado: pip install requests")
    sys.exit(1)


# ============================================================
# CONFIG
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
API = os.environ.get("BRN_API_URL", "http://127.0.0.1:5000")
TIMEOUT = 5

# Cores
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    GRAY   = "\033[90m"

_QUIET = False


def out(msg=""):
    if not _QUIET:
        print(msg)


def header(title: str):
    out(f"\n{C.BOLD}{C.CYAN}▶ {title}{C.RESET}")
    out(f"{C.GRAY}{'─' * 64}{C.RESET}")


def ok(msg):
    out(f"  {C.GREEN}✅{C.RESET} {msg}")


def warn(msg):
    out(f"  {C.YELLOW}⚠️ {C.RESET} {msg}")


def err(msg):
    out(f"  {C.RED}❌{C.RESET} {msg}")


def info(msg):
    out(f"  {C.CYAN}ℹ{C.RESET}  {msg}")


# ============================================================
# HELPERS DE REDE
# ============================================================
def http_get(path: str, timeout: int = TIMEOUT):
    """GET em http://127.0.0.1:5000 + path."""
    try:
        r = requests.get(f"{API}{path}", timeout=timeout)
        if r.status_code == 200:
            return r.json()
        return {"_error": f"HTTP {r.status_code}"}
    except requests.exceptions.ConnectionError:
        return {"_error": "conexao recusada"}
    except requests.exceptions.Timeout:
        return {"_error": "timeout"}
    except Exception as e:
        return {"_error": str(e)[:100]}


def tcp_check(ip: str, port: int, timeout: float = 3.0) -> bool:
    """Testa TCP cru."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.close()
        return True
    except Exception:
        return False


# ============================================================
# PARTE 1 — CONFIG LOCAL
# ============================================================
def check_config() -> dict:
    """Lê config local e calcula fingerprint da rede."""
    header("1. Configuração local")

    result = {
        "secret_ok":     False,
        "fingerprint":   "",
        "network":       "",
        "p2p_port":      "6001",
        "bootstrap":     [],
        "node_password": False,
    }

    # brn_network.env
    env_file = BASE_DIR / "brn_network.env"
    secret = ""
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("BRN_NETWORK_SECRET=") and "=" in line:
                secret = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

    # env var sobrescreve
    secret = os.environ.get("BRN_NETWORK_SECRET", secret)

    if secret:
        fp = hashlib.sha256(secret.encode()).hexdigest()[:16]
        result["secret_ok"] = True
        result["fingerprint"] = fp
        result["network"] = "custom"
        ok(f"BRN_NETWORK_SECRET presente (len={len(secret)})")
        info(f"Fingerprint: {C.BOLD}{fp}{C.RESET}")
        out(f"     {C.YELLOW}↑ deve ser IDÊNTICO nas duas máquinas{C.RESET}")
    else:
        err("BRN_NETWORK_SECRET ausente (brn_network.env nem env var)")

    # p2p port
    p2p_port = os.environ.get("BRN_P2P_PORT", "6001")
    result["p2p_port"] = p2p_port
    info(f"P2P porta: {p2p_port}")

    # node password
    pwd = os.environ.get("BRN_NODE_PASSWORD", "")
    if pwd:
        result["node_password"] = True
        info(f"BRN_NODE_PASSWORD: definida (len={len(pwd)})")
    else:
        warn("BRN_NODE_PASSWORD não definida")

    # bootstrap peers
    bootstrap = []
    bp_json = BASE_DIR / "bootstrap_peers.json"
    if bp_json.exists():
        try:
            data = json.loads(bp_json.read_text(encoding="utf-8"))
            if isinstance(data, list):
                bootstrap = [str(x).strip() for x in data if str(x).strip()]
        except Exception as e:
            warn(f"bootstrap_peers.json inválido: {e}")

    env_bp = os.environ.get("BRN_BOOTSTRAP_PEERS", "").strip()
    if env_bp:
        bootstrap.extend(p.strip() for p in env_bp.split(",") if p.strip())

    bootstrap = list(dict.fromkeys(bootstrap))
    result["bootstrap"] = bootstrap

    if bootstrap:
        ok(f"Bootstrap: {len(bootstrap)} peer(s) configurado(s)")
        for b in bootstrap:
            info(f"  - {b}")
    else:
        warn("Nenhum bootstrap configurado — nó só vai descobrir via LAN")

    return result


# ============================================================
# PARTE 2 — HTTP STATUS
# ============================================================
def check_node_alive() -> bool:
    header("2. Nó HTTP respondendo")
    try:
        r = requests.get(f"{API}/health", timeout=3)
        if r.status_code == 200:
            ok(f"HTTP {API} OK")
            return True
    except Exception:
        pass

    # tenta /api/status como alternativa
    try:
        r = requests.get(f"{API}/api/status", timeout=3)
        if r.status_code == 200:
            ok(f"HTTP {API} OK (via /api/status)")
            return True
    except Exception:
        pass

    err(f"HTTP {API} NÃO respondeu")
    err("Verifique: o nó está rodando? (ps aux | grep main.py)")
    return False


# ============================================================
# PARTE 3 — CHAIN LOCAL
# ============================================================
def get_chain_info() -> dict:
    return http_get("/api/chain-info")


def get_work() -> dict:
    return http_get("/api/work")


def get_status() -> dict:
    return http_get("/api/status")


def check_chain() -> dict:
    header("3. Blockchain local")

    ci = get_chain_info()
    if "_error" in ci:
        err(f"chain-info falhou: {ci['_error']}")
        return {"ok": False}

    height = int(ci.get("height", -1))
    tip_hash = ci.get("tip_hash", "")
    difficulty = ci.get("difficulty", 0)
    reward = ci.get("reward", 0)

    work = get_work()
    cumulative_work = int(work.get("cumulative_work", 0)) if isinstance(work, dict) else 0

    ok(f"Altura: {C.BOLD}{height}{C.RESET}")
    info(f"Tip hash: {tip_hash[:32]}...")
    info(f"Difficulty: {difficulty}  |  Reward: {reward / 1e8:.8f} BRN")
    info(f"Cumulative work: {cumulative_work:,}")

    if height < 0:
        warn("Cadeia vazia (sem genesis) — modo cliente?")
    elif height == 0:
        info("Apenas genesis — miner ainda não encontrou bloco 1")
    else:
        ok(f"Cadeia com {height + 1} bloco(s)")

    return {
        "ok": True,
        "height": height,
        "tip_hash": tip_hash,
        "cumulative_work": cumulative_work,
        "difficulty": difficulty,
    }


# ============================================================
# PARTE 4 — PEERS P2P
# ============================================================
def check_peers() -> dict:
    header("4. Peers P2P conectados")

    peers_data = http_get("/api/peers")
    if "_error" in peers_data:
        err(f"/api/peers falhou: {peers_data['_error']}")
        return {"ok": False, "count": 0, "peers": []}

    peers = peers_data.get("peers", []) or []
    count = len(peers)
    active = peers_data.get("active", count)

    if count == 0:
        warn(f"Nenhum peer conectado (active={active})")
        return {"ok": False, "count": 0, "peers": []}

    ok(f"{count} peer(s) conhecido(s), {active} ativo(s)")
    out()

    # Tabela de peers
    out(f"  {'ENDEREÇO':<26} {'ALTURA':>8} {'SCORE':>7} {'LAST SEEN':>12}")
    out(f"  {'─' * 26} {'─' * 8} {'─' * 7} {'─' * 12}")

    now = int(time.time())
    parsed_peers = []
    for p in peers[:20]:
        if isinstance(p, dict):
            addr = p.get("address", "?")
            h = p.get("height", 0) or 0
            score = p.get("score", 0) or 0
            last = int(p.get("last_seen", 0) or 0)
            age = now - last if last > 0 else -1
            age_s = f"{age}s" if 0 <= age < 3600 else "?"
            out(f"  {addr:<26} {h:>8} {score:>7} {age_s:>12}")
            parsed_peers.append({"address": addr, "height": h,
                                  "score": score, "last_seen": last})
        else:
            out(f"  {str(p)[:26]}")

    return {"ok": count > 0, "count": count, "peers": parsed_peers}


# ============================================================
# PARTE 5 — SYNC
# ============================================================
def check_sync() -> dict:
    header("5. Sincronização")

    for path in ("/api/sync/status", "/api/sync-info"):
        data = http_get(path)
        if "_error" not in data:
            break
    else:
        err("Nem /api/sync/status nem /api/sync-info responderam")
        return {"ok": False}

    if "synced" in data:  # formato v7 (sync_manager)
        synced = bool(data.get("synced", False))
        syncing = bool(data.get("syncing", False))
        progress = float(data.get("progress", 0))
        local_h = int(data.get("local_height", 0))
        target_h = int(data.get("target_height", 0))
        eta = data.get("eta_legivel", "")
        active_peer = data.get("active_peer")
        last_err = data.get("last_error", "")

        if synced:
            ok(f"Sincronizado · altura {local_h}/{target_h} · 100%")
        elif syncing:
            warn(f"Sincronizando: {local_h}/{target_h} ({progress:.1f}%) "
                 f"ETA={eta}")
        else:
            warn(f"Não sincronizado e não está sincronizando "
                 f"({local_h}/{target_h})")

        if active_peer:
            info(f"Peer ativo: {active_peer}")
        if last_err:
            warn(f"Último erro: {last_err}")

        return {"ok": synced, "synced": synced, "local": local_h,
                "target": target_h, "progress": progress}

    else:  # formato v9 (sync-info dict)
        s = data.get("sync", {})
        return {"ok": s.get("percent", 0) >= 100,
                "local": s.get("height", 0),
                "target": s.get("target", 0),
                "progress": s.get("percent", 0)}


# ============================================================
# PARTE 6 — MINER
# ============================================================
def check_miner() -> dict:
    header("6. Miner")

    data = http_get("/api/miner/status")
    if "_error" in data:
        warn(f"/api/miner/status indisponível: {data['_error']}")
        return {"ok": False}

    running = bool(data.get("running", False))
    blocks = int(data.get("blocks_mined", 0) or 0)
    addr = data.get("address", "")
    last_h = data.get("last_height")
    last_err = data.get("last_error", "")

    if running:
        ok(f"Miner ATIVO · {blocks} bloco(s) minerado(s)")
        if addr:
            info(f"Address: {addr[:32]}...")
        if last_h is not None:
            info(f"Último bloco: #{last_h}")
    else:
        warn(f"Miner parado ({blocks} bloco(s) no total)")

    if last_err:
        warn(f"Último erro: {last_err}")

    return {"ok": running, "running": running, "blocks_mined": blocks}


# ============================================================
# PARTE 7 — ÚLTIMOS BLOCOS
# ============================================================
def check_latest_blocks() -> dict:
    header("7. Últimos blocos")

    data = http_get("/api/latest")
    if "_error" in data or not isinstance(data, list):
        warn(f"/api/latest indisponível")
        return {"ok": False, "blocks": []}

    blocks = sorted(data, key=lambda b: b.get("height", 0), reverse=True)[:5]

    out(f"  {'#':>7}  {'HASH':<22} {'TIME':>8}  {'TXs':>4}  {'DIFF':>4}")
    out(f"  {'─' * 7}  {'─' * 22} {'─' * 8}  {'─' * 4}  {'─' * 4}")

    parsed = []
    for b in blocks:
        h = b.get("height", "?")
        hs = str(b.get("hash", ""))[:20]
        ts = int(b.get("timestamp", 0) or 0)
        txs = len(b.get("transactions", []) or [])
        diff = b.get("difficulty", "?")

        from datetime import datetime
        try:
            dt = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        except Exception:
            dt = "—"

        out(f"  {h:>7}  {hs}... {dt:>8}  {txs:>4}  {diff:>4}")
        parsed.append({"height": h, "hash": b.get("hash", ""),
                       "timestamp": ts, "txs": txs, "difficulty": diff})

    return {"ok": True, "blocks": parsed}


# ============================================================
# PARTE 8 — FORK DETECTION
# ============================================================
def check_fork(local: dict, peers_data: dict) -> dict:
    header("8. Detecção de fork")

    local_height = local.get("height", -1)
    local_hash = local.get("tip_hash", "")
    local_work = local.get("cumulative_work", 0)

    if local_height < 0:
        info("Sem cadeia local — fork detection não aplicável")
        return {"fork": False, "skipped": True}

    peers = peers_data.get("peers", [])
    if not peers:
        warn("Sem peers para comparar — não é possível detectar fork")
        return {"fork": False, "skipped": True, "reason": "sem peers"}

    forks = []
    for p in peers:
        if not isinstance(p, dict):
            continue
        addr = p.get("address", "?")
        # O DB guarda height=0 na maioria dos casos (não atualiza dinamicamente)
        # Então só comparamos o que temos

    # Não temos altura dos peers via /api/peers de forma confiável.
    # Tentamos conectar direto em cada peer e pedir a chain-info dele.
    checked = 0
    for p in peers[:5]:
        if not isinstance(p, dict):
            continue
        addr = p.get("address", "")
        if ":" not in addr:
            continue
        ip, port = addr.rsplit(":", 1)
        try:
            port = int(port)
        except ValueError:
            continue

        # Testa TCP cru
        if not tcp_check(ip, port, timeout=3):
            info(f"{addr}  ⚠️  TCP timeout")
            continue

        checked += 1
        # Tenta pedir /api/chain-info do peer (se ele tiver HTTP exposto)
        try:
            r = requests.get(f"http://{ip}:5000/api/chain-info", timeout=5)
            if r.status_code == 200:
                remote = r.json()
                r_height = int(remote.get("height", -1))
                r_hash = remote.get("tip_hash", "")
                r_diff = int(remote.get("height", 0))  # placeholder

                fork = False
                if r_height == local_height and r_hash != local_hash:
                    fork = True
                    err(f"FORK com {addr}!")
                    info(f"  altura: ambos #{local_height}")
                    info(f"  nosso: {local_hash[:24]}...")
                    info(f"  peer : {r_hash[:24]}...")
                elif r_height > local_height:
                    warn(f"{addr} mais alto ({r_height} > {local_height}) "
                         f"— sincronização pendente")
                elif r_height < local_height:
                    info(f"{addr} atrás ({r_height} < {local_height})")
                else:
                    ok(f"{addr}: mesma altura e mesmo hash ✓")

                if fork:
                    forks.append({
                        "peer": addr,
                        "height": r_height,
                        "our_hash": local_hash,
                        "peer_hash": r_hash,
                    })
        except Exception as e:
            info(f"{addr}: HTTP do peer indisponível ({str(e)[:40]})")

    if not forks and checked == 0:
        warn("Não consegui comparar com nenhum peer")
        return {"fork": False, "checked": 0}

    if forks:
        return {"fork": True, "forks": forks, "checked": checked}

    ok(f"Sem fork detectado (comparado com {checked} peer(s))")
    return {"fork": False, "checked": checked}


# ============================================================
# PARTE 9 — RESUMO
# ============================================================
def final_summary(config: dict, chain: dict, peers: dict,
                   sync: dict, miner: dict, fork: dict) -> int:
    header("VEREDITO FINAL")

    issues = []
    severity = 0  # 0 = OK, 1 = warn, 2 = err, 3 = fatal

    # Config
    if not config.get("secret_ok"):
        issues.append(("ERR", "BRN_NETWORK_SECRET ausente"))
        severity = max(severity, 3)

    # Cadeia
    if not chain.get("ok"):
        issues.append(("ERR", "Blockchain não respondeu"))
        severity = max(severity, 3)
    elif chain.get("height", -1) < 0:
        issues.append(("WARN", "Cadeia vazia (sem genesis)"))
        severity = max(severity, 1)

    # Peers
    if peers.get("count", 0) == 0:
        issues.append(("WARN", "Sem peers conectados"))
        severity = max(severity, 1)

    # Sync
    if not sync.get("synced") and not sync.get("skipped"):
        issues.append(("WARN", "Não sincronizado"))
        severity = max(severity, 1)

    # Miner
    if not miner.get("running"):
        issues.append(("INFO", "Miner não está rodando"))
        severity = max(severity, 0)

    # Fork
    if fork.get("fork"):
        issues.append(("ERR", "FORK detectado com peer!"))
        severity = max(severity, 2)

    # Imprime
    if not issues:
        print()
        ok(f"{C.BOLD}Rede OK — sem problemas{C.RESET}")
        print(f"\n     {C.GREEN}Fingerprint: {config.get('fingerprint', '?')}{C.RESET}")
        print(f"     {C.GREEN}Altura: {chain.get('height', '?')}  |  "
              f"Peers: {peers.get('count', 0)}  |  "
              f"Miner: {'ON' if miner.get('running') else 'off'}{C.RESET}")
        return 0

    print()
    for level, msg in issues:
        if level == "ERR":
            err(msg)
        elif level == "WARN":
            warn(msg)
        else:
            info(msg)

    # Mensagem contextual
    print()
    if fork.get("fork"):
        print(f"{C.RED}{C.BOLD}  ❌ FORK DETECTADO{C.RESET}")
        print(f"     Os dois nós estão minerando cadeias diferentes.")
        print(f"     Causa mais comum: BRN_NETWORK_SECRET divergente")
        print(f"     ou um nó não conseguiu sincronizar antes de minerar.")
        print()
        print(f"     Como resolver:")
        print(f"     1. Confirme fingerprint IGUAL nas duas: "
              f"{C.CYAN}{config.get('fingerprint')}{C.RESET}")
        print(f"     2. Pare o miner nas duas (Ctrl+C)")
        print(f"     3. Apague brn_v2_chain.db da máquina com cadeia MENOR")
        print(f"     4. Suba ela com --client-mode, deixe sincronizar")
        print(f"     5. Só depois, suba com --mine")
        return 1

    if not config.get("secret_ok"):
        print(f"{C.RED}{C.BOLD}  ❌ REDE INCORRETA{C.RESET}")
        print(f"     Defina BRN_NETWORK_SECRET em brn_network.env")
        return 2

    if peers.get("count", 0) == 0:
        print(f"{C.YELLOW}{C.BOLD}  ⚠️  NÓ ISOLADO{C.RESET}")
        print(f"     Nenhum peer conectado. Verifique:")
        print(f"     - Tailscale rodando nas duas (tailscale status)")
        print(f"     - IP correto em bootstrap_peers.json")
        print(f"     - Porta 6001 aberta (nc -zv <ip-peer> 6001)")
        print(f"     - BRN_NETWORK_SECRET IDÊNTICO nas duas")
        return 1

    return 0


# ============================================================
# MAIN
# ============================================================
def main() -> int:
    global _QUIET

    p = argparse.ArgumentParser(prog="check_network.py")
    p.add_argument("--json", action="store_true",
                   help="Saída estruturada em JSON")
    p.add_argument("--quiet", action="store_true",
                   help="Só o veredito final")
    args = p.parse_args()

    _QUIET = args.quiet

    # Banner
    if not _QUIET:
        print()
        print(f"{C.BOLD}{'═' * 64}{C.RESET}")
        print(f"{C.BOLD}  BRN Node — Verificação de Rede e Consenso{C.RESET}")
        print(f"  {C.GRAY}{time.strftime('%Y-%m-%d %H:%M:%S')}  |  "
              f"Host: {socket.gethostname()}{C.RESET}")
        print(f"{C.BOLD}{'═' * 64}{C.RESET}")

    # 1. Config
    config = check_config()

    # 2. Nó vivo?
    if not check_node_alive():
        if args.json:
            print(json.dumps({"ok": False, "error": "node_offline",
                              "config": config}, indent=2))
        return 3

    # 3. Cadeia
    chain = check_chain()

    # 4. Peers
    peers = check_peers()

    # 5. Sync
    sync = check_sync()

    # 6. Miner
    miner = check_miner()

    # 7. Últimos blocos
    blocks = check_latest_blocks()

    # 8. Fork
    fork = check_fork(chain, peers)

    # 9. Resumo
    exit_code = final_summary(config, chain, peers, sync, miner, fork)

    if args.json:
        report = {
            "timestamp": time.time(),
            "hostname": socket.gethostname(),
            "config": config,
            "chain": chain,
            "peers": peers,
            "sync": sync,
            "miner": miner,
            "blocks": blocks,
            "fork": fork,
            "exit_code": exit_code,
        }
        print(json.dumps(report, indent=2, default=str))

    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(130)