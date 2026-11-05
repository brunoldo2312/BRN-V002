#!/usr/bin/env python3
"""check_network.py — Diagnóstico completo de rede e consenso BRN (v3.1)
================================================================
v3.1 (2026-11-05):
  [FIX CRÍTICO] Carrega brn_network.env ANTES de ler os.environ.
        Antes, IS_ORIGIN/API/TIMEOUT/PEER_* eram lidos no nível do
        módulo sem o .env carregado. Resultado: rodar o script num
        shell sem BRN_IS_ORIGIN exportado fazia o Ubuntu (origem)
        reportar como "cliente", escondendo alertas de PoolManager
        vazio e de quórum de validadores. Também falhava se a API
        fosse em porta diferente de 5000.
v3.0 (2026-11-04):
  [NEW] PoolManager (mineradores, shares, blocos).
  [NEW] PBFT (status e commits no último bloco).
  [NEW] Trust Network (altura real dos validadores).
  [KEEP] Detecção de fork baseada em cumulative_work.
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
# [v3.1] CARREGA .env ANTES DE LER os.environ
# ============================================================
BASE_DIR = Path(__file__).resolve().parent

_env_file = BASE_DIR / "brn_network.env"
if _env_file.exists():
    try:
        _text = _env_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        _text = ""
    for _line in _text.splitlines():
        _s = _line.strip()
        if not _s or _s.startswith("#") or "=" not in _s:
            continue
        _k, _, _v = _s.partition("=")
        _k = _k.strip()
        _v = _v.strip().strip('"').strip("'")
        if _k and _k not in os.environ:
            os.environ[_k] = _v


# ============================================================
# CONFIG (agora reflete o .env real)
# ============================================================
API = os.environ.get("BRN_API_URL", "http://127.0.0.1:5000").rstrip("/")

# Se BRN_API_URL não veio do .env, monta a partir de BRN_WEB_PORT/BRN_WEB_HOST
if API == "http://127.0.0.1:5000" and os.environ.get("BRN_WEB_PORT"):
    _wp = os.environ.get("BRN_WEB_PORT", "5000").strip()
    _wh = os.environ.get("BRN_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if _wh in ("0.0.0.0", ""):
        _wh = "127.0.0.1"
    if _wp and _wp != "5000":
        API = f"http://{_wh}:{_wp}"

TIMEOUT = max(1, int(os.environ.get("BRN_HTTP_TIMEOUT", "5")))
PEER_API_PORT = int(os.environ.get("BRN_PEER_API_PORT", "5000"))
PEER_HTTP_TIMEOUT = max(1, int(os.environ.get("BRN_PEER_HTTP_TIMEOUT", "3")))
IS_ORIGIN = os.environ.get("BRN_IS_ORIGIN", "0") == "1"


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
    """GET na API local."""
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


def http_get_url(url: str, timeout: int = PEER_HTTP_TIMEOUT):
    """GET em URL arbitrária (peer). Retorna (status, payload)."""
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            try:
                return 200, r.json()
            except ValueError:
                return 200, None
        return r.status_code, None
    except requests.exceptions.ConnectionError:
        return -1, None
    except requests.exceptions.Timeout:
        return -2, None
    except Exception:
        return -3, None


def tcp_check(ip: str, port: int, timeout: float = 3.0) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.close()
        return True
    except Exception:
        return False


# ============================================================
# 1. CONFIG LOCAL
# ============================================================
def check_config() -> dict:
    header("1. Configuração local")

    result = {"secret_ok": False, "fingerprint": "", "p2p_port": "6001",
              "is_origin": IS_ORIGIN, "api": API}

    env_file = BASE_DIR / "brn_network.env"
    secret = ""
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("BRN_NETWORK_SECRET=") and "=" in line:
                secret = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

    secret = os.environ.get("BRN_NETWORK_SECRET", secret)

    if secret:
        fp = hashlib.sha256(secret.encode()).hexdigest()[:16]
        result["secret_ok"] = True
        result["fingerprint"] = fp
        ok(f"BRN_NETWORK_SECRET presente (len={len(secret)})")
        info(f"Fingerprint: {C.BOLD}{fp}{C.RESET}")
    else:
        err("BRN_NETWORK_SECRET ausente")

    p2p_port = os.environ.get("BRN_P2P_PORT", "6001")
    result["p2p_port"] = p2p_port
    info(f"P2P porta: {p2p_port}")
    info(f"API: {API}")
    info(f"Modo: {'ORIGEM' if IS_ORIGIN else 'CLIENTE'}")

    return result


# ============================================================
# 2. NÓ VIVO
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
    try:
        r = requests.get(f"{API}/api/status", timeout=3)
        if r.status_code == 200:
            ok(f"HTTP {API} OK (via /api/status)")
            return True
    except Exception:
        pass
    err(f"HTTP {API} NÃO respondeu")
    return False


# ============================================================
# 3. CHAIN LOCAL
# ============================================================
def check_chain() -> dict:
    header("3. Blockchain local")

    ci = http_get("/api/chain-info")
    if "_error" in ci:
        err(f"chain-info falhou: {ci['_error']}")
        return {"ok": False}

    height = int(ci.get("height", -1))
    tip_hash = ci.get("tip_hash", "")
    difficulty = ci.get("difficulty", 0)

    work = http_get("/api/work")
    cumulative_work = int(work.get("cumulative_work", 0)) if isinstance(work, dict) else 0

    ok(f"Altura: {C.BOLD}{height}{C.RESET}")
    info(f"Tip hash: {tip_hash[:32]}...")
    info(f"Dificuldade: {difficulty}")
    info(f"Cumulative work: {cumulative_work:,}")

    return {"ok": True, "height": height, "tip_hash": tip_hash,
            "cumulative_work": cumulative_work, "difficulty": difficulty}


# ============================================================
# 4. POOL
# ============================================================
def check_pool() -> dict:
    header("4. Pool (Mineração Compartilhada)")

    # 4a. PoolManager (se for origem)
    manager = http_get("/api/pool/stats")
    if "_error" not in manager:
        total = manager.get("total", manager.get("active_miners", 0))
        shares = manager.get("shares", manager.get("total_shares", 0))
        blocks = manager.get("blocks", manager.get("blocks_found", 0))
        ok(f"PoolManager ATIVO")
        info(f"Mineradores no pool: {total}")
        info(f"Shares processados: {shares}")
        info(f"Blocos encontrados pelo pool: {blocks}")
    elif IS_ORIGIN:
        warn("PoolManager não respondeu (deveria estar ativo no Origin)")
    else:
        info("PoolManager desligado (normal em clientes)")

    # 4b. PoolClient (se for cliente e estiver minerando)
    local = http_get("/api/pool/local_status")
    if "_error" not in local:
        running = local.get("running", False)
        if running:
            ok(f"PoolClient MINERANDO")
            hr = local.get("hashrate_khs", 0)
            acc = local.get("shares_accepted", 0)
            sub = local.get("shares_submitted", 0)
            info(f"Hashrate: {hr:.2f} kH/s")
            info(f"Shares: {acc} aceitos de {sub} enviados")
            info(f"Job atual: #{local.get('job_height', '?')}")
        else:
            warn("PoolClient parado (clique em 'Minerar Pool' na GUI)")

    return {"ok": True, "manager": manager if "_error" not in manager else None,
            "local": local if "_error" not in local else None}


# ============================================================
# 5. TRUST NETWORK
# ============================================================
def check_trust_network() -> dict:
    header("5. Trust Network (Validadores)")

    nodes_resp = http_get("/api/trust/nodes?limit=20")
    if "_error" in nodes_resp:
        warn(f"Trust Network indisponível: {nodes_resp['_error']}")
        return {"ok": False, "nodes": []}

    nodes = nodes_resp.get("nodes", [])
    if not nodes:
        warn("Nenhum nó no Trust Network")
        return {"ok": False, "nodes": []}

    ok(f"{len(nodes)} nó(s) registrado(s)")

    out(f"  {'ENDEREÇO':<26} {'ALTURA':>8} {'SCORE':>8} {'STATUS':<12}")
    out(f"  {'─' * 26} {'─' * 8} {'─' * 8} {'─' * 12}")

    for n in nodes[:10]:
        addr = (n.get("address") or "?")[:26]
        h = int(n.get("height", 0) or 0)
        score = float(n.get("trust_score", 0) or 0)
        stat = (n.get("status") or "new").upper()
        h_color = C.GREEN if h > 0 else C.YELLOW
        out(f"  {addr:<26} {h_color}{h:>8}{C.RESET} {score:>8.1f} {stat:<12}")

    return {"ok": True, "nodes": nodes}


# ============================================================
# 6. PEERS P2P
# ============================================================
def check_peers() -> dict:
    header("6. Peers P2P conectados")

    peers_data = http_get("/api/peers")
    if "_error" in peers_data:
        err(f"/api/peers falhou: {peers_data['_error']}")
        return {"ok": False, "count": 0, "peers": []}

    peers = peers_data.get("peers", []) or []
    count = len(peers)

    if count == 0:
        warn(f"Nenhum peer P2P conectado")
        return {"ok": False, "count": 0, "peers": []}

    ok(f"{count} peer(s) P2P conectado(s)")

    parsed_peers = []
    for p in peers[:15]:
        if isinstance(p, dict):
            addr = p.get("address", "?")
            h = p.get("height", 0) or 0
            last = int(p.get("last_seen", 0) or 0)
            age = int(time.time()) - last if last > 0 else -1
            if age >= 0:
                info(f"  {addr:<24} altura={h:<6} visto há {age}s")
            else:
                info(f"  {addr:<24} altura={h}")
            parsed_peers.append({"address": addr, "height": h, "last_seen": last})

    return {"ok": count > 0, "count": count, "peers": parsed_peers}


# ============================================================
# 7. SYNC
# ============================================================
def check_sync() -> dict:
    header("7. Sincronização")

    for path in ("/api/sync/status", "/api/sync-info"):
        data = http_get(path)
        if "_error" not in data:
            break
    else:
        err("Sync status não respondeu")
        return {"ok": False}

    if "synced" in data:
        synced = bool(data.get("synced", False))
        syncing = bool(data.get("syncing", False))
        progress = float(data.get("progress", 0))
        local_h = int(data.get("local_height", 0))
        target_h = int(data.get("target_height", 0))

        if synced:
            ok(f"Sincronizado · altura {local_h}/{target_h} · 100%")
        elif syncing:
            warn(f"Sincronizando: {local_h}/{target_h} ({progress:.1f}%)")
        else:
            warn(f"Não sincronizado ({local_h}/{target_h})")

        return {"ok": synced, "synced": synced, "syncing": syncing,
                "local": local_h, "target": target_h}
    return {"ok": False}


# ============================================================
# 8. MULTI-SIG E PBFT
# ============================================================
def check_consensus() -> dict:
    header("8. Multi-sig e PBFT")

    val_info = http_get("/api/consensus/validators")
    required_sigs = 0
    validators = []
    if "_error" not in val_info:
        required_sigs = int(val_info.get("required_sigs", 0) or 0)
        validators = val_info.get("validators", [])
        ok(f"Validadores ativos: {len(validators)}")
        info(f"Quórum exigido: {required_sigs}")

    # Último bloco
    latest = http_get("/api/latest")
    if "_error" in latest or not isinstance(latest, list) or not latest:
        warn("Não foi possível obter o último bloco")
        return {"ok": False}

    blk = latest[0]
    sigs = blk.get("validators_sig", []) or []
    pbft_commits = blk.get("pbft_commits", []) or []
    height = blk.get("height", 0)

    info(f"Último bloco: #{height}")
    info(f"Assinaturas (validators_sig): {len(sigs)}")
    info(f"Commits (pbft_commits): {len(pbft_commits)}")

    if required_sigs > 0 and len(sigs) >= required_sigs:
        ok(f"Multi-sig OK ({len(sigs)}/{required_sigs})")
    elif required_sigs > 0:
        warn(f"Multi-sig insuficiente ({len(sigs)}/{required_sigs})")

    if len(pbft_commits) > 0:
        ok(f"PBFT ATIVO ({len(pbft_commits)} commits)")
    else:
        warn("PBFT sem commits no último bloco")

    return {"ok": len(sigs) >= required_sigs and required_sigs > 0,
            "required_sigs": required_sigs, "sigs": len(sigs),
            "pbft_commits": len(pbft_commits)}


# ============================================================
# 9. MINER
# ============================================================
def check_miner() -> dict:
    header("9. Miner (local)")

    data = http_get("/api/miner/status")
    if "_error" in data:
        warn(f"/api/miner/status indisponível: {data['_error']}")
        return {"ok": False}

    running = bool(data.get("running", False))
    blocks = int(data.get("blocks_mined", 0) or 0)

    if running:
        ok(f"Miner ATIVO · {blocks} bloco(s) minerado(s)")
    else:
        info(f"Miner local parado ({blocks} blocos no total)")

    return {"ok": running, "running": running, "blocks_mined": blocks}


# ============================================================
# 10. RESUMO FINAL
# ============================================================
def final_summary(config: dict, chain: dict, pool: dict,
                  trust: dict, peers: dict, sync: dict,
                  consensus: dict, miner: dict) -> int:
    header("VEREDITO FINAL")

    issues = []
    severity = 0

    if not config.get("secret_ok"):
        issues.append(("ERR", "BRN_NETWORK_SECRET ausente"))
        severity = max(severity, 3)

    if not chain.get("ok"):
        issues.append(("ERR", "Blockchain não respondeu"))
        severity = max(severity, 3)

    if peers.get("count", 0) == 0:
        issues.append(("WARN", "Sem peers P2P conectados"))
        severity = max(severity, 1)

    if not sync.get("synced") and sync.get("syncing") is not None:
        issues.append(("WARN", "Não sincronizado"))
        severity = max(severity, 1)

    # Pool (só avisa se for origem)
    if IS_ORIGIN:
        pm = pool.get("manager")
        if not pm:
            issues.append(("WARN", "PoolManager não respondeu (você é Origin)"))
            severity = max(severity, 1)
        elif not pm.get("total", pm.get("active_miners", 0)):
            issues.append(("WARN", "PoolManager sem mineradores (você é Origin)"))
            severity = max(severity, 1)

    # Trust
    if trust.get("ok") and len(trust.get("nodes", [])) == 0:
        issues.append(("WARN", "Trust Network vazia"))
        severity = max(severity, 1)

    # Consensus
    if not consensus.get("ok"):
        issues.append(("WARN",
                       f"Multi-sig: {consensus.get('sigs', 0)}/"
                       f"{consensus.get('required_sigs', 0)} assinaturas"))
        severity = max(severity, 1)

    if consensus.get("pbft_commits", 0) == 0:
        issues.append(("INFO", "PBFT sem commits no último bloco"))

    if not issues:
        out()
        ok(f"{C.BOLD}TUDO OK — Rede e Consenso funcionando{C.RESET}")
        out(f"\n     {C.GREEN}Fingerprint: {config.get('fingerprint', '?')}{C.RESET}")
        out(f"     {C.GREEN}Altura: {chain.get('height', '?')}{C.RESET}")
        out(f"     {C.GREEN}Peers: {peers.get('count', 0)}{C.RESET}")
        return 0

    out()
    for level, msg in issues:
        if level == "ERR":
            err(msg)
        elif level == "WARN":
            warn(msg)
        else:
            info(msg)

    if severity >= 3:
        return 2
    if severity >= 1:
        return 1
    return 0


# ============================================================
# MAIN
# ============================================================
def main() -> int:
    global _QUIET

    p = argparse.ArgumentParser(prog="check_network.py")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    _QUIET = args.quiet or args.json

    if not _QUIET:
        print()
        print(f"{C.BOLD}{'═' * 64}{C.RESET}")
        print(f"{C.BOLD}  BRN Node — Verificação de Rede e Consenso (v3.1){C.RESET}")
        print(f"  {C.GRAY}{time.strftime('%Y-%m-%d %H:%M:%S')}  |  "
              f"Host: {socket.gethostname()}{C.RESET}")
        print(f"  {C.GRAY}API: {API}  |  Modo: "
              f"{'ORIGEM' if IS_ORIGIN else 'CLIENTE'}{C.RESET}")
        print(f"{C.BOLD}{'═' * 64}{C.RESET}")

    config = check_config()
    if not check_node_alive():
        return 3

    chain = check_chain()
    pool = check_pool()
    trust = check_trust_network()
    peers = check_peers()
    sync = check_sync()
    consensus = check_consensus()
    miner = check_miner()

    exit_code = final_summary(config, chain, pool, trust, peers, sync, consensus, miner)

    if args.json:
        print(json.dumps({
            "version": "3.1",
            "timestamp": time.time(),
            "hostname": socket.gethostname(),
            "api": API,
            "is_origin": IS_ORIGIN,
            "config": config,
            "chain": chain,
            "pool": pool,
            "trust": trust,
            "peers": peers,
            "sync": sync,
            "consensus": consensus,
            "miner": miner,
            "exit_code": exit_code,
        }, indent=2, default=str))

    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(130)
