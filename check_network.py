#!/usr/bin/env python3
"""check_network.py — Diagnóstico completo de rede e consenso BRN (v2.0)
================================================================
v2.0 (2026-10-27):
  [FIX CRÍTICO] Detecção de fork agora compara CUMULATIVE_WORK antes
                de altura/hash (regra Bitcoin de "melhor cadeia").
                Classifica cada peer em same/behind/ahead/fork/unknown.
  [FIX] Tenta múltiplos endpoints HTTP no peer (/api/chain-info,
        /api/work, /api/status) com timeout curto. Antes só consultava
        PEER_API_PORT e dava "inconclusivo" quando o peer não expunha
        API HTTP naquele caminho.
  [FIX] Peer alcançável por TCP mas sem HTTP → 'unknown' com motivo
        explícito (em vez de fingir que comparou).
  [NEW] Reporta local e remoto lado a lado: height, tip_hash,
        cumulative_work.
  [NEW] final_summary diferencia:
          same     → OK
          behind   → info (peer atrasado, normal)
          ahead    → WARN (nós atrasados; sync deve resolver)
          fork     → ERR (exit 1)
          unknown  → warn (não foi possível comparar)
  [KEEP] Exit codes: 0=ok, 1=fork/sem-peer, 2=sem-secret, 3=offline.
  [KEEP] --json adiciona campos, não remove.
  [KEEP] --quiet funciona.
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
API = os.environ.get("BRN_API_URL", "http://127.0.0.1:5000").rstrip("/")
TIMEOUT = max(1, int(os.environ.get("BRN_HTTP_TIMEOUT", "5")))
PEER_API_PORT = int(os.environ.get("BRN_PEER_API_PORT", "5000"))
PEER_HTTP_TIMEOUT = max(1, int(os.environ.get("BRN_PEER_HTTP_TIMEOUT", "3")))

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
    """GET na API local configurada em BRN_API_URL + path."""
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
        result["network"] = "custom"
        ok(f"BRN_NETWORK_SECRET presente (len={len(secret)})")
        info(f"Fingerprint: {C.BOLD}{fp}{C.RESET}")
        out(f"     {C.YELLOW}compare manualmente nas máquinas; o script não verifica fingerprint remoto{C.RESET}")
    else:
        err("BRN_NETWORK_SECRET ausente (brn_network.env nem env var)")

    p2p_port = os.environ.get("BRN_P2P_PORT", "6001")
    result["p2p_port"] = p2p_port
    info(f"P2P porta: {p2p_port}")

    pwd = os.environ.get("BRN_NODE_PASSWORD", "")
    if pwd:
        result["node_password"] = True
        info(f"BRN_NODE_PASSWORD: definida (len={len(pwd)})")
    else:
        warn("BRN_NODE_PASSWORD não definida")

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

    # fallback: alguns nós incluem cumulative_work em /api/chain-info
    if cumulative_work == 0 and "cumulative_work" in ci:
        try:
            cumulative_work = int(ci["cumulative_work"])
        except (TypeError, ValueError):
            pass

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

    if "synced" in data:
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
            warn(f"Sincronizando: {local_h}/{target_h} ({progress:.1f}%) ETA={eta}")
        else:
            warn(f"Não sincronizado e não está sincronizando ({local_h}/{target_h})")

        if active_peer:
            info(f"Peer ativo: {active_peer}")
        if last_err:
            warn(f"Último erro: {last_err}")

        return {"ok": synced, "synced": synced, "syncing": syncing,
                "local": local_h, "target": target_h, "progress": progress}
    else:
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
# PARTE 8 — FORK DETECTION (v2.0: work-based)
# ============================================================
def _probe_peer_chain(ip: str, port_p2p: int) -> dict:
    """
    Consulta um peer via HTTP para obter height/tip_hash/work.

    Tenta, em ordem:
      1. PEER_API_PORT (default 5000) -> /api/chain-info, /api/work
      2. porta_p2p (anunciada)         -> /api/chain-info, /api/work
      3. porta_p2p                     -> /api/status

    Retorna:
      {ok, source, height, tip_hash, cumulative_work, error}

    ok=False + error != "" quando nada respondeu.
    """
    result = {
        "ok": False,
        "source": None,
        "height": None,
        "tip_hash": "",
        "cumulative_work": None,
        "error": "",
    }

    candidates = []
    if port_p2p != PEER_API_PORT:
        candidates.append(PEER_API_PORT)
    candidates.append(port_p2p)
    # dedupe preservando ordem
    seen = set()
    candidates = [p for p in candidates if not (p in seen or seen.add(p))]

    errors = []
    for port in candidates:
        base = f"http://{ip}:{port}"

        # tenta /api/chain-info
        st, ci = http_get_url(f"{base}/api/chain-info")
        if st == 200 and isinstance(ci, dict):
            try:
                result["height"] = int(ci.get("height", -1))
                result["tip_hash"] = str(ci.get("tip_hash", ""))
            except (TypeError, ValueError):
                pass
            result["source"] = f"{base}/api/chain-info"
            # tenta /api/work para work
            st_w, w = http_get_url(f"{base}/api/work")
            if st_w == 200 and isinstance(w, dict):
                try:
                    result["cumulative_work"] = int(w.get("cumulative_work", 0))
                except (TypeError, ValueError):
                    pass
            if result["cumulative_work"] is None and "cumulative_work" in ci:
                try:
                    result["cumulative_work"] = int(ci["cumulative_work"])
                except (TypeError, ValueError):
                    pass
            result["ok"] = True
            return result

        # tenta /api/status
        st_s, s = http_get_url(f"{base}/api/status")
        if st_s == 200 and isinstance(s, dict):
            try:
                result["height"] = int(s.get("height", -1))
                result["tip_hash"] = str(s.get("tip_hash", ""))
            except (TypeError, ValueError):
                pass
            if "cumulative_work" in s:
                try:
                    result["cumulative_work"] = int(s["cumulative_work"])
                except (TypeError, ValueError):
                    pass
            result["source"] = f"{base}/api/status"
            result["ok"] = True
            return result

        # registra erro agregado
        if st == -1:
            errors.append(f"{port}: conexão recusada")
        elif st == -2:
            errors.append(f"{port}: timeout")
        elif st == -3:
            errors.append(f"{port}: erro de rede")
        else:
            errors.append(f"{port}: HTTP {st}")

    result["error"] = "; ".join(errors) if errors else "sem HTTP"
    return result


def _classify_peer(local: dict, remote: dict) -> tuple:
    """
    Classifica um peer em relação à chain local.

    Retorna (verdict, motivo):
      same      — work, height e hash iguais
      behind    — peer tem menos work que nós
      ahead     — peer tem MAIS work que nós (sync pendente)
      fork      — MESMO work, MESMA altura, HASH DIFERENTE
      ambiguous — work igual mas altura diferente (raro)
      unknown   — dados insuficientes
    """
    if not remote.get("ok"):
        return "unknown", remote.get("error", "sem dados")

    lw = local.get("cumulative_work", 0)
    rw = remote.get("cumulative_work")
    lh = local.get("height", -1)
    rh = remote.get("height")
    lhash = local.get("tip_hash", "")
    rhash = remote.get("tip_hash", "")

    # Sem work de um dos lados: cai em height/hash (fallback honesto)
    if rw is None or lw in (0, None):
        if rh is None:
            return "unknown", "peer sem height"
        if rh > lh:
            return "ahead", f"peer height {rh} > local {lh} (sem work)"
        if rh < lh:
            return "behind", f"peer height {rh} < local {lh} (sem work)"
        if lhash and rhash and lhash == rhash:
            return "same", "height e hash iguais"
        if lhash and rhash and lhash != rhash:
            return "fork", f"mesma altura #{lh}, hash diferente"
        return "unknown", "height igual, hash ausente"

    # Work disponível dos dois lados (regra Bitcoin)
    if rw > lw:
        return "ahead", f"peer work {rw:,} > local {lw:,}"
    if rw < lw:
        return "behind", f"peer work {rw:,} < local {lw:,}"

    # Work igual: desempate por altura/hash
    if rh == lh:
        if lhash and rhash and lhash == rhash:
            return "same", "work, height e hash iguais"
        if lhash and rhash and lhash != rhash:
            return "fork", f"mesmo work, altura #{lh} igual, hash diferente"
        return "unknown", "work e height iguais, hash ausente"
    return "ambiguous", f"work igual mas height difere ({lh} vs {rh})"


def check_fork(local: dict, peers_data: dict) -> dict:
    header("8. Detecção de fork (work-based)")

    local_height = local.get("height", -1)
    local_hash = local.get("tip_hash", "")
    local_work = local.get("cumulative_work", 0)

    if local_height < 0:
        info("Sem cadeia local — fork detection não aplicável")
        return {"fork": False, "skipped": True, "checks": []}

    peers = peers_data.get("peers", [])
    if not peers:
        warn("Sem peers para comparar — não é possível detectar fork")
        return {"fork": False, "skipped": True, "checks": []}

    info(f"Local: height={local_height} · work={local_work:,} · "
         f"tip={local_hash[:16]}...")
    out()

    out(f"  {'PEER':<26} {'HEIGHT':>8} {'WORK':>18} {'VERDICT':<10} MOTIVO")
    out(f"  {'─' * 26} {'─' * 8} {'─' * 18} {'─' * 10} {'─' * 20}")

    checks = []
    n_ahead = n_behind = n_same = n_fork = n_unknown = n_ambiguous = 0

    for p in peers[:10]:
        if not isinstance(p, dict):
            continue
        addr = p.get("address", "")
        if ":" not in addr:
            continue
        ip, port_s = addr.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            continue

        # TCP rápido primeiro (evita HTTP longo em peer morto)
        if not tcp_check(ip, port, timeout=3):
            verdict = "unknown"
            motivo = "TCP timeout"
            remote = {"ok": False, "error": "TCP timeout"}
        else:
            remote = _probe_peer_chain(ip, port)
            verdict, motivo = _classify_peer(local, remote)

        # contadores
        if verdict == "same":
            n_same += 1
        elif verdict == "behind":
            n_behind += 1
        elif verdict == "ahead":
            n_ahead += 1
        elif verdict == "fork":
            n_fork += 1
        elif verdict == "ambiguous":
            n_ambiguous += 1
        else:
            n_unknown += 1

        # output
        rh = remote.get("height")
        rw = remote.get("cumulative_work")
        rh_str = str(rh) if rh is not None else "?"
        rw_str = f"{rw:,}" if rw is not None else "?"
        # motivos longos: trunca
        motivo_short = motivo[:24]
        out(f"  {addr:<26} {rh_str:>8} {rw_str:>18} {verdict:<10} {motivo_short}")

        checks.append({
            "peer": addr,
            "verdict": verdict,
            "motivo": motivo,
            "remote": {
                "ok": remote.get("ok", False),
                "height": rh,
                "tip_hash": remote.get("tip_hash", ""),
                "cumulative_work": rw,
                "source": remote.get("source"),
                "error": remote.get("error", ""),
            },
        })

    out()

    # Resumo visual
    if n_fork > 0:
        err(f"FORK DETECTADO com {n_fork} peer(s)")
        for c in checks:
            if c["verdict"] == "fork":
                err(f"  {c['peer']}: {c['motivo']}")
    if n_ahead > 0:
        warn(f"{n_ahead} peer(s) com MAIS work que nós — sincronização pendente")
    if n_same > 0:
        ok(f"{n_same} peer(s) com work/height/hash idênticos")
    if n_behind > 0:
        info(f"{n_behind} peer(s) atrás de nós (normal)")
    if n_ambiguous > 0:
        warn(f"{n_ambiguous} peer(s) ambíguo(s) — work igual, height difere")
    if n_unknown > 0:
        warn(f"{n_unknown} peer(s) sem dados suficientes (TCP ok, HTTP indisponível?)")

    return {
        "fork": n_fork > 0,
        "checks": checks,
        "summary": {
            "same": n_same,
            "behind": n_behind,
            "ahead": n_ahead,
            "fork": n_fork,
            "ambiguous": n_ambiguous,
            "unknown": n_unknown,
        },
    }


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

    # Fork (v2.0: veredito granular)
    fsum = fork.get("summary", {})
    if fork.get("fork"):
        issues.append(("ERR", f"FORK detectado com {fsum.get('fork', 0)} peer(s)!"))
        severity = max(severity, 2)
    if fsum.get("ahead", 0) > 0:
        issues.append(("WARN",
                       f"{fsum['ahead']} peer(s) com MAIS work — sync pendente"))
        severity = max(severity, 1)
    if fsum.get("unknown", 0) > 0 and fsum.get("same", 0) == 0 \
       and fsum.get("fork", 0) == 0:
        issues.append(("WARN",
                       f"{fsum['unknown']} peer(s) sem HTTP para comparar work"))
        severity = max(severity, 1)

    # Imprime
    if not issues:
        out()
        ok(f"{C.BOLD}Rede OK — sem problemas{C.RESET}")
        out(f"\n     {C.GREEN}Fingerprint: {config.get('fingerprint', '?')}{C.RESET}")
        out(f"     {C.GREEN}Altura: {chain.get('height', '?')}  |  "
              f"Peers: {peers.get('count', 0)}  |  "
              f"Miner: {'ON' if miner.get('running') else 'off'}{C.RESET}")
        return 0

    out()
    for level, msg in issues:
        if level == "ERR":
            err(msg)
        elif level == "WARN":
            warn(msg)
        else:
            info(msg)

    out()
    if fork.get("fork"):
        out(f"{C.RED}{C.BOLD}  ❌ FORK DETECTADO{C.RESET}")
        out(f"     Os peers têm cadeias diferentes com o MESMO cumulative_work.")
        out(f"     Causa mais comum: BRN_NETWORK_SECRET divergente")
        out(f"     ou um nó não conseguiu sincronizar antes de minerar.")
        out()
        out(f"     Como resolver:")
        out(f"     1. Confirme fingerprint IGUAL nas duas: "
              f"{C.CYAN}{config.get('fingerprint')}{C.RESET}")
        out(f"     2. Pare o miner nas duas (Ctrl+C)")
        out(f"     3. NÃO apague o banco de dados automaticamente.")
        out(f"     4. Faça backup do banco e valide os blocos com uma fonte confiável.")
        out(f"     5. Só retome a mineração depois de confirmar a cadeia correta.")
        return 1

    if not config.get("secret_ok"):
        out(f"{C.RED}{C.BOLD}  ❌ REDE INCORRETA{C.RESET}")
        out(f"     Defina BRN_NETWORK_SECRET em brn_network.env")
        return 2

    if peers.get("count", 0) == 0:
        out(f"{C.YELLOW}{C.BOLD}  ⚠️  NÓ ISOLADO{C.RESET}")
        out(f"     Nenhum peer conectado. Verifique:")
        out(f"     - Tailscale rodando nas duas (tailscale status)")
        out(f"     - IP correto em bootstrap_peers.json")
        out(f"     - Porta 6001 aberta (nc -zv <ip-peer> 6001)")
        out(f"     - BRN_NETWORK_SECRET IDÊNTICO nas duas")
        return 1

    if fsum.get("ahead", 0) > 0:
        out(f"{C.YELLOW}{C.BOLD}  ⚠️  SINCRONIZAÇÃO PENDENTE{C.RESET}")
        out(f"     {fsum['ahead']} peer(s) com MAIS cumulative_work.")
        out(f"     O nó deve sincronizar automaticamente; aguarde.")
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

    _QUIET = args.quiet or args.json

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

    # 8. Fork (work-based)
    fork = check_fork(chain, peers)

    # 9. Resumo
    exit_code = final_summary(config, chain, peers, sync, miner, fork)

    if args.json:
        report = {
            "version": "2.0",
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
