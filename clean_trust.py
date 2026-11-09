#!/usr/bin/env python3
"""
clean_trust.py — Remove nós fantasmas do trust_network.json (v2)
================================================================
v2 (2026-11-08) — REESCRITO:
  [FIX CRÍTICO] Agora usa TrustNetwork.remove_ghosts() em vez de
        reescrever o JSON na mão. A versão antiga:
          1. Preservava "signature" e "origin_pubkey" mas os deixava
             INVÁLIDOS (a assinatura era sobre o conteúdo, que foi
             alterado). Próximo boot do Origin → "ASSINATURA INVÁLIDA";
             próximo sync de cliente → payload rejeitado (v9.3 do
             sync_manager.py).
          2. Lia last_seen do ARQUIVO, não do estado em memória.
             O arquivo pode ter last_seen de até 10s atrás (flush
             throttled) ou horas (nó recém-bootado). Removia nós
             que o processo considerava ativos.
          3. Não protegia score >= 500 (o _prune_inactive protege).
          4. Não protegia o próprio Origin (o remove_ghosts protege).
          5. Não considerava trust_network.local.json dos clientes.
  [NEW] --dry-run: mostra o que seria removido sem alterar.
  [NEW] --max-age: segundos de inatividade (default: LÊ
        BRN_TRUST_ACTIVE_TTL do .env, fallback 300).
  [NEW] --force: permite remover nós com trust_score >= 500.
  [NEW] Categoriza o que foi mantido/removido/protegido.

Uso:
    python3 clean_trust.py --dry-run    # só mostra
    python3 clean_trust.py              # aplica (max_age do .env)
    python3 clean_trust.py --max-age 600
    python3 clean_trust.py --force      # remove mesmo score >= 500

Recomendado: prefira o TrustDaemon do main.py (roda a cada 60s
automaticamente). Este script é para uso manual quando o nó está
parado ou você quer forçar uma limpeza imediata.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
os.chdir(BASE)

# Carrega .env
env_file = BASE / "brn_network.env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("clean_trust")


def _load_node_sk_if_origin():
    """Carrega a node_sk do Origin para o flush() re-assinar."""
    try:
        from secure_store import load_node_identity
        from crypto import Ed25519PrivateKey
    except Exception as e:
        log.warning("crypto/secure_store indisponíveis: %s", e)
        return None
    pw = os.environ.get("BRN_NODE_PASSWORD", "").strip()
    if not pw:
        log.warning("BRN_NODE_PASSWORD vazio — sem node_sk para re-assinar")
        return None
    try:
        data = load_node_identity(pw, "node_identity.enc")
        if data and data.get("sk"):
            return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
    except Exception as e:
        log.warning("não consegui carregar node_sk: %s", e)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(prog="clean_trust.py")
    ap.add_argument("--dry-run", action="store_true",
                    help="só mostra o que seria removido")
    ap.add_argument("--max-age", type=int, default=None,
                    help="segundos de inatividade "
                         "(default: BRN_TRUST_ACTIVE_TTL do .env, "
                         "fallback 300)")
    ap.add_argument("--force", action="store_true",
                    help="remove nós com score >= 500 (protegidos)")
    args = ap.parse_args()

    try:
        from trust_network import TrustNetwork
    except ImportError as e:
        log.error("Falha importando trust_network: %s", e)
        return 1

    is_origin = os.environ.get("BRN_IS_ORIGIN", "0") == "1"

    # [FIX] Se is_origin, precisamos da node_sk para o flush() re-assinar.
    # Sem isso, o flush() salva SEM assinatura e o payload é rejeitado
    # por clientes (v9.3 do sync_manager.py).
    node_sk = _load_node_sk_if_origin() if is_origin else None
    if is_origin and node_sk is None:
        log.error(
            "Este nó é Origin mas não consegui carregar a node_sk. "
            "Sem a node_sk, o flush() salvaria o trust_network.json "
            "SEM assinatura e clientes rejeitariam o payload "
            "(v9.3 do sync_manager.py). Abortando para não quebrar a rede."
        )
        return 1

    trust = TrustNetwork(
        str(BASE / "trust_network.json"),
        is_origin=is_origin,
        node_sk=node_sk,
    )

    # [FIX] Respeita env var (era 300 hardcoded)
    if args.max_age is not None:
        max_age = args.max_age
    else:
        try:
            max_age = int(os.environ.get("BRN_TRUST_ACTIVE_TTL", "300"))
        except ValueError:
            max_age = 300

    now = time.time()
    total = len(trust.nodes)

    keep: list[tuple] = []
    remove: list[tuple] = []
    protected: list[tuple] = []

    for nid, n in trust.nodes.items():
        age = int(now - getattr(n, "last_seen", 0))
        score = getattr(n, "trust_score", 0)
        pk = (getattr(n, "public_key", "") or nid).lower()

        # [FIX] Preserva o próprio Origin
        if trust._origin_pubkey and pk == trust._origin_pubkey:
            keep.append((nid, age, score, "origin protegido"))
            continue

        # [FIX] Preserva score >= 500 (SCORE_PROTECTED em trust_network.py)
        if score >= 500 and not args.force:
            protected.append((nid, age, score))
            keep.append((nid, age, score, "score>=500"))
            continue

        if age < max_age:
            keep.append((nid, age, score, "ativo"))
        else:
            remove.append((nid, age, score))

    print("=" * 72)
    print(f" CLEAN TRUST — is_origin={is_origin} max_age={max_age}s"
          f"{' (dry-run)' if args.dry_run else ''}")
    print("=" * 72)
    print(f" Total: {total} | Mantidos: {len(keep)} | "
          f"Remover: {len(remove)} | Protegidos: {len(protected)}")
    print("-" * 72)

    for nid, age, score, why in keep:
        marker = "🔒" if why in ("origin protegido", "score>=500") else "✅"
        print(f"  {marker} KEEP   {nid[:24]}... "
              f"age={age}s score={score} ({why})")

    for nid, age, score in remove:
        print(f"  ❌ REMOVE {nid[:24]}... age={age}s score={score}")

    if protected:
        print()
        print(f" [!] {len(protected)} nó(s) protegido(s) por score>=500.")
        print("     Use --force se realmente quer removê-los.")

    if args.dry_run:
        print()
        print("[dry-run] Nenhum arquivo alterado.")
        return 0

    if not remove:
        print()
        print(" Nada a remover.")
        return 0

    # [FIX CRÍTICO] Usa a API oficial — preserva assinatura, respeita
    # origin, atualiza .json E .local.json, grita se flush falhar.
    removed = trust.remove_ghosts(max_age_s=max_age)

    print()
    if is_origin:
        print(f" [ok] {removed} nó(s) removido(s). trust_network.json "
              f"re-assinado pelo Origin.")
    else:
        print(f" [ok] {removed} nó(s) removido(s). Salvo em "
              f"trust_network.local.json (cliente, sem assinatura).")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)