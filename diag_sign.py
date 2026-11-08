#!/usr/bin/env python3
"""
diag_sign.py — Diagnóstico de assinatura remota (v2)
================================================================
v2 (2026-11-08):
  [FIX] Interpreta corretamente os motivos de recusa:
        - "altura_fora"     → ESPERADO (peer avançou, race recovery ok)
        - "header_hash_diverge" → ESPERADO (bloco fictício)
        - "sem_config"      → PROBLEMA (peer não tem SK)
        - "nao_no_top5"     → PROBLEMA (peer fora do trust)
        - "prev_hash_fork"  → PROBLEMA (fork local)
  [FIX] Envia DOIS testes por peer: (a) bloco real (= tip) e (b) bloco
        fictício (h+1). O primeiro mede race recovery; o segundo mede
        validação de header.
  [NEW] --skip-fake: só testa o bloco real (mais rápido).
  [NEW] Resumo agregado: quantos peers tem SK, quantos estão em fork,
        quantos estão em altura diferente.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
os.chdir(BASE)

if (BASE / "brn_network.env").exists():
    for line in (BASE / "brn_network.env").read_text(
            encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _classify_error(err: str) -> tuple[str, str]:
    """
    Retorna (categoria, mensagem_humana).
    categoria ∈ {ok_esperado, problema, neutro}
    """
    e = (err or "").lower()
    if not e:
        return "neutro", "sem erro explícito (versão antiga?)"
    if "altura_fora" in e:
        return "ok_esperado", \
            "peer já passou desse bloco (race recovery OK)"
    if "header_hash_diverge" in e:
        return "ok_esperado", \
            "bloco fictício tem hash inválido — esperado"
    if "sem_config" in e:
        return "problema", \
            "peer sem SK local (current_wallet.enc não carregou)"
    if "nao_no_top5" in e:
        return "problema", \
            "pubkey do peer fora do trust (--promote no peer)"
    if "prev_hash_fork" in e or "hash_fork" in e:
        return "problema", \
            "peer em fork — sincronizar"
    if "validate_block" in e:
        return "ok_esperado", \
            "bloco não passa validação (normal para bloco fictício)"
    return "neutro", err[:80]


def main() -> int:
    ap = argparse.ArgumentParser(prog="diag_sign.py")
    ap.add_argument("--skip-fake", action="store_true",
                    help="não testa o bloco fictício (h+1)")
    args = ap.parse_args()

    from blockchain import Blockchain
    from trust_network import TrustNetwork
    from secure_store import load_current_wallet
    from wallet import Wallet

    try:
        from p2p_unified import P2PClient, PeerDiscovery
    except ImportError:
        print("[X] p2p_unified.py ausente.")
        return 1

    print("=" * 72)
    print(" DIAGNÓSTICO DE ASSINATURA (SIGN_BLOCK) BRN — v2")
    print("=" * 72)

    chain = Blockchain("brn_v2_chain.db", auto_genesis=False)
    chain.set_trust_net(TrustNetwork("trust_network.json"))

    pw = (os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
          or os.environ.get("BRN_NODE_PASSWORD", "").strip())
    if pw:
        w = load_current_wallet(pw)
        if w and w.get("ok"):
            sk = Wallet(private_key_hex=w["private_key"])
            chain.set_local_validator_sk(sk)
            print(f"SK local: {sk.pub_hex[:16]}…")

    h = chain.db.height()
    tip = chain.db.tip_hash()
    if h < 0:
        print("[X] Blockchain vazia.")
        return 1

    real_block = chain.db.get_block(h)
    if not real_block:
        print(f"[X] Bloco #{h} ausente no DB.")
        return 1

    fake_block = {
        "height": h + 1,
        "hash": "a" * 64,
        "prev_hash": tip or ("0" * 64),
        "timestamp": int(time.time()),
        "nonce": 0,
        "merkle": "b" * 64,
        "difficulty": chain.current_difficulty(),
        "transactions": [],
        "validators_sig": [],
    }

    validators = chain._get_active_validators()
    required = chain._required_signatures(validators)
    print(f"Altura local   : {h}")
    print(f"Tip hash       : {tip[:32]}…")
    print(f"Validadores    : {len(validators)}")
    print(f"Required       : {required}")

    try:
        disc = PeerDiscovery(6001, "diag")
        peers = disc.listar_peers()
    except Exception as e:
        print(f"[X] PeerDiscovery falhou: {e}")
        return 1

    if not peers:
        print("\n[X] Nenhum peer P2P descoberto.")
        print("    Verifique se o main.py está rodando nos outros nós.")
        return 1

    # Normaliza peers (string "ip:port" ou dict)
    endpoints: list[tuple[str, int]] = []
    for p in peers:
        if isinstance(p, str) and ":" in p:
            ip, port_s = p.rsplit(":", 1)
            try:
                endpoints.append((ip, int(port_s)))
            except ValueError:
                continue
        elif isinstance(p, dict):
            ip = p.get("ip") or p.get("address", "")
            port = p.get("port") or p.get("tcp_port") or 6001
            if ip and ":" not in ip:
                endpoints.append((ip, int(port)))
            elif ip and ":" in ip:
                ip2, port_s = ip.rsplit(":", 1)
                try:
                    endpoints.append((ip2, int(port_s)))
                except ValueError:
                    pass

    print(f"\nTestando {len(endpoints)} peer(s) P2P:")
    print("-" * 72)

    stats = {
        "ok_real":      0,
        "ok_fake":      0,
        "altura_fora":  0,
        "sem_config":   0,
        "nao_no_top5":  0,
        "fork":         0,
        "offline":      0,
        "recusa_sem_motivo": 0,
    }

    for ip, port in endpoints:
        print(f"\n→ {ip}:{port}")

        # ─── Teste A: bloco real (= tip) ───
        try:
            resp, _ = P2PClient.send_message(
                ip, port,
                {"type": "sign_block", "block": real_block},
                timeout=5,
            )
        except Exception as e:
            print(f"   [X] rede: {e}")
            stats["offline"] += 1
            continue

        if resp is None:
            print("   [X] timeout (peer offline)")
            stats["offline"] += 1
            continue

        pk = (resp.get("pubkey") or "").strip()
        sg = (resp.get("signature") or "").strip()
        err = (resp.get("error") or "").strip()

        if pk and sg:
            print(f"   [A] ASSINOU (bloco real) pk={pk[:16]}…")
            stats["ok_real"] += 1
        elif err:
            cat, human = _classify_error(err)
            icon = {"ok_esperado": "✓", "problema": "✗", "neutro": "?"}[cat]
            print(f"   [A] {icon} {err} — {human}")
            if "altura_fora" in err.lower():
                stats["altura_fora"] += 1
            elif "sem_config" in err.lower():
                stats["sem_config"] += 1
            elif "nao_no_top5" in err.lower():
                stats["nao_no_top5"] += 1
            elif "fork" in err.lower():
                stats["fork"] += 1
        else:
            print("   [A] [X] RECUSOU sem motivo (v<v6.7.7)")
            stats["recusa_sem_motivo"] += 1

        if args.skip_fake:
            continue

        # ─── Teste B: bloco fictício (h+1) ───
        try:
            resp2, _ = P2PClient.send_message(
                ip, port,
                {"type": "sign_block", "block": fake_block},
                timeout=5,
            )
        except Exception:
            resp2 = None

        if resp2 is None:
            print("   [B] timeout no bloco fictício")
            continue

        pk2 = (resp2.get("pubkey") or "").strip()
        err2 = (resp2.get("error") or "").strip()

        if pk2:
            print(f"   [B] ✓ ASSINOU bloco fictício — peer passou "
                  f"por TODA validação e só falhou no hash")
            stats["ok_fake"] += 1
        elif "header_hash_diverge" in err2.lower():
            print("   [B] ✓ rejeitou no header_hash_diverge — peer "
                  "passou por validação e assinou corretamente antes "
                  "de detectar o hash inválido")
            stats["ok_fake"] += 1
        elif err2:
            cat, human = _classify_error(err2)
            icon = {"ok_esperado": "✓", "problema": "✗", "neutro": "?"}[cat]
            print(f"   [B] {icon} {err2} — {human}")
        else:
            print("   [B] [X] RECUSOU sem motivo (v<v6.7.7)")

    print()
    print("=" * 72)
    print(" RESUMO")
    print("=" * 72)
    print(f"  Assinou bloco real        : {stats['ok_real']}")
    print(f"  Validou bloco fictício    : {stats['ok_fake']}")
    print(f"  Rejeitou por altura_fora  : {stats['altura_fora']}  "
          f"(✓ normal se peer avançou)")
    print(f"  Sem SK (sem_config)       : {stats['sem_config']}  "
          f"(✗ peer não carregou carteira)")
    print(f"  Fora do trust (nao_top5)  : {stats['nao_no_top5']}  "
          f"(✗ rode --promote no peer)")
    print(f"  Em fork                   : {stats['fork']}  "
          f"(✗ sincronizar)")
    print(f"  Offline/timeout           : {stats['offline']}")
    print(f"  Recusa sem motivo         : {stats['recusa_sem_motivo']}  "
          f"(✗ peer com p2p_unified < v6.7.7)")
    print("=" * 72)

    problemas = (stats["sem_config"] + stats["nao_no_top5"]
                 + stats["fork"] + stats["recusa_sem_motivo"])
    if problemas == 0 and stats["ok_real"] + stats["ok_fake"] > 0:
        print("\n ✅ Peers estão saudáveis (motivos de recusa são todos esperados).")
        return 0
    elif problemas > 0:
        print(f"\n ✗ {problemas} problema(s) real(is) — ver ✗ acima.")
        return 1
    else:
        print("\n ⚠ Nenhum peer respondeu — verifique a rede.")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
