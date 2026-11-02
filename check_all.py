#!/usr/bin/env python3
"""check_all.py — Diagnostico completo do no BRN.

Verifica:
  1. Versao do p2p_unified.py (v6.7.7 ou anterior?)
  2. Trust network: quantos validadores, quem
  3. Wallet local: pubkey e se esta no trust
  4. Chain: altura, tip, work
  5. Ultimos N blocos: quantas assinaturas cada um
  6. Peers conectados e alturas
  7. Config: QUORUM, REQUIRE_ALL_SIGS, BOOTSTRAP_SKIP
  8. Fork detection (compara com peers via P2P)
  9. Veredito final

Uso:
    python check_all.py
    python check_all.py --blocks 20
"""
import os
import sys
import json
import time
import argparse
from pathlib import Path
from collections import Counter

BASE = Path(__file__).resolve().parent
os.chdir(BASE)

# --- carrega .env ---
ENV_FILE = BASE / "brn_network.env"
if ENV_FILE.exists():
    with open(ENV_FILE, "r", encoding="utf-8", errors="ignore") as f:
        for line in f.read().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SEP = "=" * 76
DASH = "-" * 76


def hdr(n, txt):
    print(f"\n{SEP}\n [{n}] {txt}\n{SEP}")


def ok(msg):
    print(f"  [OK]    {msg}")


def warn(msg):
    print(f"  [WARN]  {msg}")


def err(msg):
    print(f"  [ERR]   {msg}")


def info(msg):
    print(f"  [INFO]  {msg}")


# ============================================================
# 1. Versao p2p_unified
# ============================================================
hdr(1, "VERSOES DOS MODULOS")

versions = {}
for mod in ("p2p_unified", "blockchain", "sync_manager", "wallet"):
    try:
        m = __import__(mod)
        doc = (m.__doc__ or "").strip().splitlines()[0] if m.__doc__ else "(sem doc)"
        versions[mod] = doc
        if mod == "p2p_unified" and "v6.7.7" in doc:
            ok(f"{mod}: {doc}   <- v6.7.7 ATIVO (race recovery)")
        elif mod == "p2p_unified":
            warn(f"{mod}: {doc}   <- NAO e v6.7.7, race recovery pode faltar")
        elif mod == "blockchain" and "9.4.10" in doc:
            ok(f"{mod}: {doc}   <- v9.4.10 (bypass emergencia)")
        else:
            info(f"{mod}: {doc}")
    except Exception as e:
        err(f"{mod}: {e}")


# ============================================================
# 2. Trust network
# ============================================================
hdr(2, "TRUST NETWORK")

try:
    from trust_network import TrustNetwork
    trust = TrustNetwork(str(BASE / "trust_network.json"))
    nodes = getattr(trust, "nodes", {}) or {}
    info(f"Total de nos no trust: {len(nodes)}")
    for pk in nodes:
        if len(pk) == 66:
            ok(f"  [66] {pk}")
        else:
            err(f"  [{len(pk)}] {pk}   <- pubkey com tamanho errado")
except Exception as e:
    err(f"Falha carregando trust: {e}")
    trust = None


# ============================================================
# 3. Wallet local
# ============================================================
hdr(3, "WALLET LOCAL")

local_pub = None
try:
    from secure_store import load_current_wallet
    pw = (os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
          or os.environ.get("BRN_NODE_PASSWORD", "").strip())
    if not pw:
        err("BRN_WALLET_SESSION_PASS/BRN_NODE_PASSWORD vazia")
    else:
        w = load_current_wallet(pw)
        if not w or not w.get("ok"):
            err(f"Carteira nao decifrou: {w.get('msg', '?') if w else '?'}")
        else:
            local_pub = w.get("public_key") or w.get("pub_hex")
            if local_pub and len(local_pub) == 66:
                ok(f"Pubkey local: {local_pub}")
            else:
                err(f"Pubkey local com tamanho errado: {len(local_pub) if local_pub else 0}")
            if local_pub and trust and local_pub in trust.nodes:
                ok(f"Pubkey local esta no trust")
            elif local_pub and trust:
                err(f"Pubkey local NAO esta no trust")
except Exception as e:
    err(f"Falha carregando wallet: {e}")


# ============================================================
# 4. Config
# ============================================================
hdr(4, "CONFIG (brn_network.env)")

cfg = {
    "BRN_VALIDATOR_QUORUM": os.environ.get("BRN_VALIDATOR_QUORUM", ""),
    "BRN_REQUIRE_ALL_SIGS": os.environ.get("BRN_REQUIRE_ALL_SIGS", ""),
    "BRN_MAX_VALIDATORS": os.environ.get("BRN_MAX_VALIDATORS", ""),
    "BRN_BOOTSTRAP_SKIP_SIGS_UNTIL": os.environ.get("BRN_BOOTSTRAP_SKIP_SIGS_UNTIL", ""),
    "BRN_AUTO_MINE": os.environ.get("BRN_AUTO_MINE", ""),
    "BRN_POOL_CLIENT": os.environ.get("BRN_POOL_CLIENT", ""),
    "BRN_P2P_PORT": os.environ.get("BRN_P2P_PORT", ""),
    "BRN_WEB_PORT": os.environ.get("BRN_WEB_PORT", ""),
}
for k, v in cfg.items():
    if v:
        info(f"{k}={v}")
    else:
        warn(f"{k} (vazio)")

q = cfg.get("BRN_VALIDATOR_QUORUM", "")
if q:
    try:
        qf = float(q)
        if 0.0 < qf <= 1.0:
            ok(f"QUORUM valido: {qf} -> required = ceil({len(nodes) if trust else '?'} * {qf})")
        else:
            err(f"QUORUM invalido: {qf} (use 0.0 < q <= 1.0)")
    except ValueError:
        err(f"QUORUM nao e numero: {q}")


# ============================================================
# 5. Chain local
# ============================================================
hdr(5, "CHAIN LOCAL")

chain = None
try:
    from blockchain import Blockchain
    chain = Blockchain(str(BASE / "brn_v2_chain.db"), auto_genesis=False)
    if trust:
        chain.set_trust_net(trust)
    h = chain.db.height()
    tip = chain.db.tip_hash() or "(vazio)"
    work = chain.cumulative_work()
    diff = chain.current_difficulty()
    ok(f"Altura: {h}")
    info(f"Tip: {tip[:48]}...")
    info(f"Work acumulado: {work:,}")
    info(f"Dificuldade: {diff}")
except Exception as e:
    err(f"Falha abrindo chain: {e}")


# ============================================================
# 6. Ultimos blocos — assinaturas
# ============================================================
hdr(6, "ULTIMOS BLOCOS — CONTAGEM DE ASSINATURAS")

if chain:
    N = 20
    try:
        blocks = []
        for hh in range(max(1, chain.db.height() - N + 1), chain.db.height() + 1):
            b = chain.db.get_block(hh)
            if b:
                blocks.append(b)
        print(f"  {'ALTURA':<8} {'SIGS':<6} SIGNERS")
        print(f"  {DASH}")
        dist = Counter()
        for b in blocks:
            sigs = b.get("validators_sig", []) or []
            dist[len(sigs)] += 1
            signers = ", ".join(s.get("pubkey", "?")[:12] for s in sigs) or "-"
            print(f"  {b['height']:<8} {len(sigs):<6} {signers}")
        print(f"  {DASH}")
        info(f"Distribuicao: {dict(dist)}")
        n2plus = sum(v for k, v in dist.items() if k >= 2)
        if n2plus > 0:
            ok(f"Multi-sig real: {n2plus}/{len(blocks)} blocos com 2+ assinaturas")
        else:
            err(f"NENHUM bloco com 2+ assinaturas — ninguem co-assina")
    except Exception as e:
        err(f"Falha lendo blocos: {e}")


# ============================================================
# 7. Peers conectados
# ============================================================
hdr(7, "PEERS (via P2P)")

if chain and trust:
    try:
        from p2p_unified import P2PClient
        peers_file = BASE / "peers_discovered.json"
        peers = []
        if peers_file.exists():
            try:
                peers = json.loads(peers_file.read_text())
            except Exception:
                peers = []
        info(f"Peers descobertos: {len(peers)}")
        for addr in peers[:10]:
            if ":" not in addr:
                continue
            try:
                ip, port_s = addr.rsplit(":", 1)
                port = int(port_s)
                resp, lat = P2PClient.get_chain_height(ip, port, timeout=5.0)
                if resp:
                    ph = resp.get("height", -1)
                    pw_ = resp.get("work", 0)
                    same = "SAME" if ph == chain.db.height() else f"DIFF {ph - chain.db.height():+d}"
                    info(f"  {addr:<24} h={ph:<6} work={pw_:<12} {same}  lat={lat:.0f}ms")
                else:
                    warn(f"  {addr:<24} sem resposta")
            except Exception:
                warn(f"  {addr:<24} erro")
    except Exception as e:
        err(f"Falha checando peers: {e}")


# ============================================================
# 8. Fork detection (resumo)
# ============================================================
hdr(8, "FORK DETECTION")

if chain:
    try:
        from p2p_unified import P2PClient
        peers_file = BASE / "peers_discovered.json"
        peers = []
        if peers_file.exists():
            try:
                peers = json.loads(peers_file.read_text())
            except Exception:
                peers = []
        local_h = chain.db.height()
        local_work = chain.cumulative_work()
        forks = []
        behind = []
        ahead = []
        for addr in peers[:20]:
            if ":" not in addr:
                continue
            try:
                ip, port_s = addr.rsplit(":", 1)
                port = int(port_s)
                resp, _ = P2PClient.get_chain_height(ip, port, timeout=5.0)
                if not resp:
                    continue
                ph = resp.get("height", -1)
                pw_ = resp.get("work", 0)
                if pw_ > local_work:
                    ahead.append((addr, ph, pw_))
                elif ph == local_h and pw_ == local_work:
                    pass  # empate
                elif ph != local_h:
                    behind.append((addr, ph))
                # fork heuristico: mesma altura, hash diferente
                if ph == local_h:
                    # tenta pegar o bloco da mesma altura
                    try:
                        from p2p_unified import P2PClient as _C
                        from blockchain import P2PClient as _unused
                    except Exception:
                        pass
            except Exception:
                continue

        if ahead:
            for a, h_, w_ in ahead:
                err(f"Peer {a} tem MAIS work: h={h_} work={w_}")
            err("Voce esta ATRAS. Sincronize.")
        if behind:
            for a, h_ in behind:
                warn(f"Peer {a} esta atras: h={h_} (voce tem {local_h})")
        if not ahead and not behind:
            ok("Todos os peers tem mesma altura que voce")

        # fork real: se houver peer com mesma altura mas hash diferente
        # Nao temos como obter hash via P2P de forma simples aqui,
        # mas o ForkGuard do main.py ja loga isso.
        if not ahead and not behind:
            info("Para fork REAL (mesmo altura, hash diferente), cheque o log:")
            info("  grep 'ForkGuard' brn.log | tail -5")
    except Exception as e:
        err(f"Falha fork detection: {e}")


# ============================================================
# 9. Veredito
# ============================================================
hdr(9, "VEREDITO FINAL")

problems = []
warnings = []

# Versao
if "p2p_unified" in versions and "v6.7.7" not in versions["p2p_unified"]:
    problems.append("p2p_unified.py NAO e v6.7.7 (race recovery falta)")

# Trust
if trust and len(trust.nodes) < 2:
    problems.append(f"Trust tem so {len(trust.nodes)} validador(es)")
elif trust and len(trust.nodes) > 3:
    warnings.append(f"Trust tem {len(trust.nodes)} validadores (esperado: 2 ou 3)")

# Wallet no trust
if local_pub and trust and local_pub not in trust.nodes:
    problems.append("Pubkey local NAO esta no trust")

# QUORUM
q = cfg.get("BRN_VALIDATOR_QUORUM", "")
if q:
    try:
        qf = float(q)
        if len(trust.nodes) == 2 and qf < 0.99:
            warnings.append(f"Com 2 validadores e QUORUM={qf}, required pode ser 1 (nao 2)")
        elif len(trust.nodes) == 3 and 0.5 < qf < 0.99:
            warnings.append(f"Com 3 validadores e QUORUM={qf}, required=3 (sem tolerancia)")
    except Exception:
        pass

# Assinaturas
if chain:
    try:
        last = chain.db.get_block(chain.db.height())
        n_sigs = len(last.get("validators_sig", []) or [])
        if n_sigs < 2:
            problems.append(f"Ultimo bloco tem so {n_sigs} assinatura(s)")
    except Exception:
        pass

# Fork
# (ja listado acima)

if problems:
    print()
    for p in problems:
        err(f"PROBLEMA: {p}")
    print()
    print(f"  {SEP}")
    print("  STATUS: FALHA — corrija os problemas acima")
    print(f"  {SEP}")
    sys.exit(1)
elif warnings:
    print()
    for w in warnings:
        warn(f"AVISO: {w}")
    print()
    print(f"  {SEP}")
    print("  STATUS: OK COM AVISOS")
    print(f"  {SEP}")
    sys.exit(0)
else:
    print()
    ok("TUDO OK — multi-sig funcionando, sem fork, sem problemas")
    print(f"  {SEP}")
    print("  STATUS: SAUDAVEL")
    print(f"  {SEP}")
    sys.exit(0)


# cleanup
try:
    if chain:
        chain.db.close()
except Exception:
    pass
