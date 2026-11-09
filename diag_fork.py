#!/usr/bin/env python3
"""
diag_fork.py — Diagnóstico de fork / rejeição de bloco (v2)
================================================================
v2 (2026-11-08) — PATCH DE AUDITORIA:
  [FIX CRÍTICO] origin_url agora é descoberto dinamicamente:
        1. --origin URL (se passado)
        2. BRN_POOL_URL do .env
        3. BRN_BOOTSTRAP_PEERS (testa HTTP 5000 de cada um)
        4. tailscale status --json + probe
        5. peers_discovered.json / bootstrap_peers.json
        Antes era "http://100.111.253.8:5000" hardcoded — quebrava
        em qualquer setup com IP diferente do Ubuntu do Bruno.
  [FIX] get_json com proxies={http:None, https:None} — se HTTP_PROXY
        estiver no ambiente, urllib injetava proxy e falhava com
        "connection refused" mesmo com o Origin online.
  [FIX] validate_block agora trata o caso "Origem não tem o bloco"
        (altura igual ou maior que a local). Antes, tentava validar
        mesmo quando o Origin não tinha o bloco.
  [FIX] Mensagens de erro reescritas — distinguem:
        prev_hash (fork real), altura (salto), pbft (prova ausente),
        sig (multi-sig), auth_tag (segredo errado), MTP (timestamp
        antigo), checkpoint (ancestral bloqueado).
  [NEW] --origin URL: sobrescreve o Origin descoberto.
  [NEW] --block N: diagnostica um bloco específico (default: local_h+1).
  [NEW] --all: testa os últimos 3 blocos (não só o imediato). Útil
        quando o bloco faltante é mais antigo.
  [KEEP] Compatível com setups que NÃO têm Origin HTTP (rede local).
        Se não conseguir descobrir, sugere comandos manuais.

Uso:
    python3 diag_fork.py
    python3 diag_fork.py --origin http://100.64.151.67:5000
    python3 diag_fork.py --block 1234
    python3 diag_fork.py --all
================================================================
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# ─── Carrega .env ───
if (BASE_DIR / "brn_network.env").exists():
    for line in (BASE_DIR / "brn_network.env").read_text(
            encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ============================================================
# HTTP helpers
# ============================================================
def get_json(url: str, timeout: float = 10.0) -> dict:
    """
    GET JSON com timeout e SEM proxy (evita HTTP_PROXY do ambiente
    injetar proxy e falhar com 'connection refused' em rede local).
    """
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "brn-diag-fork/2.0"}
        )
        # proxies={...} força conexão direta, ignorando HTTP_PROXY
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )
        with opener.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"_error": f"resposta não-JSON ({len(raw)} bytes)",
                        "_raw_prefix": raw[:120]}
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code} em {url}"}
    except urllib.error.URLError as e:
        return {"_error": f"URL error: {e.reason}"}
    except socket.timeout:
        return {"_error": f"timeout ({timeout}s)"}
    except Exception as e:
        return {"_error": str(e)}


def tcp_ok(ip: str, port: int, timeout: float = 2.0) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.close()
        return True
    except Exception:
        return False


# ============================================================
# Descoberta do Origin
# ============================================================
def _probe_http_origin(ip: str, port: int = 5000,
                       timeout: float = 3.0) -> str | None:
    """Testa se ip:port responde /api/chain-info. Retorna URL ou None."""
    base = f"http://{ip}:{port}"
    r = get_json(f"{base}/api/chain-info", timeout=timeout)
    if "_error" in r:
        return None
    if "height" not in r:
        return None
    return base


def _tailscale_ips() -> list[tuple[str, str]]:
    """
    Lista (ip, hostname) dos peers Tailscale online.
    Silencioso se tailscale não estiver instalado.
    """
    try:
        r = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode != 0:
            return []
        data = json.loads(r.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired,
            json.JSONDecodeError):
        return []
    except Exception:
        return []

    out: list[tuple[str, str]] = []
    # Self primeiro
    self_node = data.get("Self") or {}
    for ip in (self_node.get("TailscaleIPs") or []):
        if ip.startswith("100."):
            out.append((ip, self_node.get("HostName", "self")))
            break
    for pid, p in (data.get("Peer") or {}).items():
        if not p.get("Online"):
            continue
        for ip in (p.get("TailscaleIPs") or []):
            if ip.startswith("100."):
                out.append((ip, p.get("HostName", pid[:8])))
                break
    return out


def _peers_from_json_files() -> list[str]:
    """Lê IPs de peers_discovered.json / bootstrap_peers.json / peers.json."""
    ips: set[str] = set()
    for fname in ("peers_discovered.json", "bootstrap_peers.json", "peers.json"):
        fpath = BASE_DIR / fname
        if not fpath.exists():
            continue
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = list(data.keys())
        for item in items:
            addr = ""
            if isinstance(item, str):
                addr = item
            elif isinstance(item, dict):
                addr = item.get("address") or item.get("ip") or ""
            if not addr:
                continue
            ip = addr.rsplit(":", 1)[0] if ":" in addr else addr
            ip = ip.replace("http://", "").replace("https://", "").split(":")[0]
            if ip and not ip.startswith("127."):
                ips.add(ip)
    return sorted(ips)


def _env_bootstrap_ips() -> list[str]:
    ips: list[str] = []
    for p in os.environ.get("BRN_BOOTSTRAP_PEERS", "").split(","):
        p = p.strip()
        if not p:
            continue
        # Aceita "ip", "ip:porta", "ip:porta/scheme"
        s = p.split("/")[0]  # remove sufixo /scheme
        ip = s.rsplit(":", 1)[0] if ":" in s else s
        ip = ip.replace("http://", "").replace("https://", "")
        if ip and ip != "127.0.0.1":
            ips.append(ip)
    return ips


def _meu_ip() -> str:
    """IP do próprio nó (BRN_SEU_ENDERECO sem a porta)."""
    e = os.environ.get("BRN_SEU_ENDERECO", "").strip()
    if not e:
        return ""
    return e.rsplit(":", 1)[0] if ":" in e else e


def discover_origin(cli_origin: str | None) -> str | None:
    """
    Retorna a URL do Origin (ex.: http://100.64.151.67:5000) ou None.

    Ordem:
      1. --origin (CLI)
      2. BRN_POOL_URL
      3. BRN_BOOTSTRAP_PEERS (testa HTTP 5000)
      4. tailscale status + probe
      5. peers_*.json + probe
    """
    # 1. CLI
    if cli_origin:
        url = cli_origin.rstrip("/")
        if not url.startswith("http"):
            url = "http://" + url
        return url

    # 2. BRN_POOL_URL
    pool_url = os.environ.get("BRN_POOL_URL", "").strip().rstrip("/")
    if pool_url and pool_url.startswith("http"):
        return pool_url

    meu = _meu_ip()
    candidatos: list[str] = []

    # 3. bootstrap env
    for ip in _env_bootstrap_ips():
        if ip and ip != meu and ip not in candidatos:
            candidatos.append(ip)

    # 4. Tailscale
    for ip, _host in _tailscale_ips():
        if ip and ip != meu and ip not in candidatos:
            candidatos.append(ip)

    # 5. JSON files
    for ip in _peers_from_json_files():
        if ip and ip != meu and ip not in candidatos:
            candidatos.append(ip)

    if not candidatos:
        return None

    print(f"[*] Testando {len(candidatos)} candidato(s) para Origin "
          f"(porta HTTP 5000)...")
    for ip in candidatos:
        base = f"http://{ip}:5000"
        info = get_json(f"{base}/api/chain-info", timeout=2.5)
        if "_error" in info:
            continue
        if "height" not in info:
            continue
        print(f"    [ok] {base} → altura={info.get('height')}, "
              f"difficulty={info.get('difficulty', '?')}")
        return base

    return None


# ============================================================
# Classificação do motivo de rejeição
# ============================================================
def classify_rejection(msg: str) -> tuple[str, list[str]]:
    """
    Retorna (categoria, [linhas de diagnóstico e ação]).
    categoria ∈ {"fork_real", "salto", "pbft", "multisig",
                 "auth_tag", "mtp", "checkpoint", "ok", "desconhecido"}
    """
    m = (msg or "").lower()

    if m.startswith("ok") or m == "":
        return "ok", []

    if "prev_hash" in m or "prev hash" in m:
        return "fork_real", [
            "DIAGNÓSTICO: FORK REAL.",
            "  O bloco anterior do Origin tem hash diferente do seu.",
            "  Suas chains divergiram em algum ponto antes deste bloco.",
            "",
            "  AÇÕES (em ordem de preferência):",
            "    1. python3 sync_all_peers.py --strict",
            "       (deixa o SyncManager resolver o fork automaticamente)",
            "",
            "    2. Se o fork for profundo (mais de 500 blocos):",
            "       mv brn_v2_chain.db brn_v2_chain.fork.bak",
            "       python3 main.py --headless",
            "       (apaga e sincroniza do zero — perde só blocos locais)",
            "",
            "    3. Se você sabe que SUA chain está certa e o Origin errou:",
            "       NÃO sincronize. Investigue o Origin antes.",
        ]

    if "altura" in m or "height" in m or "salto" in m:
        return "salto", [
            "DIAGNÓSTICO: SALTO DE ALTURA.",
            "  Você está pulando blocos (gap na sequência).",
            "  O bloco anterior ainda não está no seu DB.",
            "",
            "  AÇÕES:",
            "    1. python3 sync_all_peers.py --strict",
            "    2. Se persistir, verifique qual bloco falta:",
            "       python3 diag_fork.py --all",
        ]

    if "pbft" in m:
        return "pbft", [
            "DIAGNÓSTICO: FALHA NO PBFT.",
            "  O bloco do Origin não tem commits válidos ou o quórum",
            "  PBFT não foi atingido.",
            "",
            "  AÇÕES:",
            "    1. Se você NÃO usa PBFT, garanta que BRN_PBFT_ENFORCE=0",
            "       no seu brn_network.env.",
            "    2. Se usa, verifique se o Origin tem o motor PBFT ativo:",
            "       curl -s http://<origin>:5000/health | python3 -m json.tool",
            "       (campo pbft_configured deve ser true)",
        ]

    if "sig" in m or "assinatura" in m or "multisig" in m:
        return "multisig", [
            "DIAGNÓSTICO: FALHA DE MULTI-SIG.",
            "  O bloco do Origin não tem assinaturas suficientes para o",
            "  seu validator set.",
            "",
            "  AÇÕES:",
            "    1. Veja o required no SEU nó vs. quantas sigs vieram:",
            "       python3 verify_cosign.py --blocks 5",
            "    2. Se o Origin tem menos assinaturas que o seu required:",
            "       - O Origin está sem brn_multisig ou com trust desatualizado.",
            "       - Rode no Origin: python3 validator_manager.py --check",
            "",
            "    3. Para bootstrap (DEV APENAS), você pode reduzir required:",
            "       BRN_VALIDATOR_QUORUM=0.5 no .env do SEU nó",
            "       (NUNCA em produção — quebra a segurança da rede)",
        ]

    if "auth_tag" in m:
        return "auth_tag", [
            "DIAGNÓSTICO: AUTH_TAG INVÁLIDO.",
            "  Seu BRN_NETWORK_SECRET é diferente do Origin.",
            "",
            "  AÇÕES:",
            "    1. Compare o BRN_NETWORK_SECRET no .env dos dois nós.",
            "    2. Se quiser desligar auth_tag (DEV), no SEU .env:",
            "       BRN_REQUIRE_AUTH_TAG=0",
            "    3. Se for o mesmo secret, o bloco do Origin foi forjado:",
            "       NÃO aceite, investigue.",
        ]

    if "timestamp" in m or "mtp" in m:
        return "mtp", [
            "DIAGNÓSTICO: TIMESTAMP ANTIGO (MTP).",
            "  O timestamp do bloco é anterior ao Median Time Past.",
            "",
            "  AÇÕES:",
            "    1. O Origin está com relógio errado?",
            "    2. Você minerou rápido demais (MTP = timestamp mediano dos",
            "       últimos 50 blocos)? Espere 1-2 blocos e tente de novo.",
        ]

    if "checkpoint" in m:
        return "checkpoint", [
            "DIAGNÓSTICO: CHECKPOINT VIOLADO.",
            "  O ancestral comum está abaixo de um checkpoint assinado.",
            "  Reorg bloqueado por segurança — o checkpoint é imutável.",
            "",
            "  AÇÕES:",
            "    1. Isto é proteção, não bug. NÃO force reorg.",
            "    2. Se você JÁ SABE que o checkpoint está errado (raro):",
            "       remova a entrada de checkpoints.json e reinicie.",
            "    3. Verifique: python3 checkpoints.py show",
        ]

    return "desconhecido", [
        "DIAGNÓSTICO: motivo não reconhecido pela classificação.",
        f"  Mensagem crua: {msg!r}",
        "",
        "  Procure o motivo no código:",
        "    grep -rn 'return False, \"' blockchain.py | head -30",
    ]


# ============================================================
# Diagnóstico de um bloco
# ============================================================
def diagnose_block(bc, origin_url: str, target_h: int,
                   local_h: int) -> bool:
    """
    Baixa o bloco #target_h do Origin e tenta validar.
    Retorna True se o bloco é válido, False se rejeitado, None se
    não foi possível baixar.
    """
    print(f"\n[*] Baixando bloco #{target_h} de {origin_url}...")
    remote = get_json(f"{origin_url}/api/block/{target_h}", timeout=10)

    if "_error" in remote:
        print(f"    [X] {remote['_error']}")
        return None
    if not isinstance(remote, dict) or "height" not in remote:
        print(f"    [X] resposta sem campo 'height': {remote}")
        return None
    if remote.get("height") != target_h:
        print(f"    [X] Origin devolveu bloco #{remote.get('height')} "
              f"(esperado #{target_h})")
        return None

    print(f"    Hash:        {remote.get('hash', '?')[:32]}...")
    print(f"    prev_hash:   {remote.get('prev_hash', '?')[:32]}...")
    print(f"    timestamp:   {remote.get('timestamp')}")
    print(f"    difficulty:  {remote.get('difficulty')}")
    print(f"    txs:         {len(remote.get('transactions', []))}")
    print(f"    sigs:        {len(remote.get('validators_sig', []) or [])}")
    print(f"    pbft_commits:{len(remote.get('pbft_commits', []) or [])}")

    # Compara com bloco local em local_h
    prev_local = bc.db.get_block(target_h - 1) if target_h > 0 else None
    if prev_local:
        expected_prev = prev_local.get("hash", "?")
        if remote.get("prev_hash") != expected_prev:
            print(f"\n    [!] prev_hash diverge:")
            print(f"        Origin: {remote.get('prev_hash', '?')}")
            print(f"        Local:  {expected_prev}")
    else:
        print(f"\n    [!] bloco #{target_h - 1} ausente no seu DB — "
              f"não posso comparar prev_hash")

    # Valida localmente
    print(f"\n[*] Tentando validate_block() local...")
    try:
        ok, msg = bc.validate_block(remote, prev_block=prev_local)
    except Exception as e:
        print(f"    [X] validate_block levantou exceção: {e}")
        return False

    cat, diag_lines = classify_rejection(msg if not ok else "ok")

    print()
    print("=" * 72)
    if ok:
        print(f" ✅ Bloco #{target_h} É VÁLIDO pelo seu consenso.")
        print()
        print(" Isto significa que o SyncManager deveria ter baixado.")
        print()
        print(" Verifique:")
        print("   1. sync_manager está ativo no main.py?")
        print("      grep 'ChainSyncManager iniciado' no log do main.py")
        print("   2. /api/sync/status mostra 'synced': true?")
        print("      curl -s http://127.0.0.1:5000/api/sync/status")
        print("   3. BRN_REORG_LOOKBACK é suficiente? (default 500)")
        print("      Se o fork for > 500, o SyncManager não resolve.")
        print("=" * 72)
        return True

    print(f" ❌ Bloco #{target_h} FOI REJEITADO PELO SEU CONSENSO!")
    print(f"    Categoria: {cat}")
    print(f"    Motivo:    {msg}")
    print()
    for line in diag_lines:
        print("    " + line)
    print("=" * 72)
    return False


# ============================================================
# Main
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(prog="diag_fork.py")
    ap.add_argument("--origin", default=None,
                    help="URL do Origin (ex: http://100.64.151.67:5000)")
    ap.add_argument("--block", type=int, default=None,
                    help="altura específica a diagnosticar (default: local_h+1)")
    ap.add_argument("--all", action="store_true",
                    help="diagnostica os últimos 3 blocos em vez do próximo")
    args = ap.parse_args()

    print("=" * 72)
    print(" 🩺 DIAGNÓSTICO DE FORK / REJEIÇÃO DE BLOCO — v2")
    print("=" * 72)

    # 1. Carrega chain local
    try:
        from blockchain import Blockchain
        from trust_network import TrustNetwork
    except ImportError as e:
        print(f"[X] Falha importando blockchain/trust: {e}")
        return 1

    try:
        bc = Blockchain("brn_v2_chain.db", auto_genesis=False)
        bc.set_trust_net(TrustNetwork("trust_network.json"))
        local_h = bc.db.height()
    except Exception as e:
        print(f"[X] Erro abrindo chain: {e}")
        return 1

    print(f"[*] Altura local: {local_h}")
    if local_h < 0:
        print("[X] Blockchain vazia — precisa de pelo menos genesis.")
        bc.db.close()
        return 1

    # 2. Descobre Origin
    origin_url = discover_origin(args.origin)
    if not origin_url:
        print("\n[X] Não consegui descobrir o Origin automaticamente.")
        print()
        print(" Opções:")
        print("   1. Defina BRN_POOL_URL no brn_network.env:")
        print("      BRN_POOL_URL=http://100.64.151.67:5000")
        print()
        print("   2. Passe explicitamente:")
        print("      python3 diag_fork.py --origin http://100.64.151.67:5000")
        print()
        print("   3. Se você não usa Origin HTTP (rede local pura),")
        print("      o diag_fork não se aplica — use diag_sign.py.")
        bc.db.close()
        return 1

    print(f"[*] Origin: {origin_url}")

    # Confirma que o Origin responde
    info = get_json(f"{origin_url}/api/chain-info", timeout=5)
    if "_error" in info:
        print(f"[X] Origin não responde: {info['_error']}")
        bc.db.close()
        return 1
    remote_h = int(info.get("height", -1))
    print(f"[*] Altura do Origin: {remote_h}")
    if remote_h <= local_h:
        print(f"[!] Origin não tem blocos novos (local={local_h}, "
              f"remoto={remote_h}). Nada a diagnosticar.")
        bc.db.close()
        return 0

    # 3. Decide quais blocos testar
    if args.block is not None:
        targets = [args.block]
    elif args.all:
        targets = list(range(max(1, local_h + 1),
                             min(remote_h, local_h + 3) + 1))
    else:
        targets = [local_h + 1]

    results = []
    for t in targets:
        if t > remote_h:
            print(f"\n[*] Bloco #{t} além da altura do Origin (#{remote_h}) "
                  f"— pulando.")
            continue
        r = diagnose_block(bc, origin_url, t, local_h)
        results.append((t, r))

    bc.db.close()

    # 4. Veredito final (só com --all)
    if args.all and len(results) > 1:
        print()
        print("=" * 72)
        print(" RESUMO")
        print("=" * 72)
        invalidos = [t for t, ok in results if ok is False]
        validos = [t for t, ok in results if ok is True]
        nao_baixados = [t for t, ok in results if ok is None]

        if validos:
            print(f"  ✅ Válidos:      {validos}")
        if invalidos:
            print(f"  ❌ Rejeitados:   {invalidos}")
        if nao_baixados:
            print(f"  ⚠  Não baixados: {nao_baixados}")

        if invalidos:
            print()
            print(f" O bloco mais baixo rejeitado é #{invalidos[0]}.")
            print(f" O fork começa por aí — não é no topo da chain.")
            print()
            print(f" Próximo passo:")
            print(f"   python3 diag_fork.py --block {invalidos[0]}")
            print(f"   (para ver o motivo detalhado do fork)")
        elif validos:
            print()
            print(" Todos os blocos testados são válidos. O problema")
            print(" NÃO é consenso — é o SyncManager não estar rodando.")
            print(" Verifique o log do main.py por 'ChainSyncManager'.")
        print("=" * 72)

    return 0 if all(ok is True for _, ok in results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] Cancelado.")
        sys.exit(130)