#!/usr/bin/env python3
"""validator_manager.py — Gerencia validadores e verifica assinatura de blocos.

Modos:
    --check            Lista validadores, top 5, ativos, e quem qualifica
    --promote          Promove qualificados a trusted (score 1000)
    --verify-signing   Verifica se últimos blocos têm 2+ assinaturas
    --test-peers       Testa cada peer: "você assina este bloco?"
    --test-local       Testa assinatura local (você assina?)
    --daemon           Loop a cada 30s (Ctrl+C para parar)
    --all              Roda check + promote + verify-signing + test-peers

Critérios para promoção (--promote):
    - last_seen < 300s (BRN_TRUST_ACTIVE_TTL)
    - pubkey com 66 chars hex (secp256k1 comprimida)
    - não está banned
    - status != "trusted" (senão já está)

A assinatura de blocos em si é AUTOMÁTICA via _handle_sign_block
(p2p_unified.py). Este programa VERIFICA e DIAGNOSTICA por que ela
pode estar falhando.
"""
import os
import sys
import json
import time
import argparse
from pathlib import Path

BASE = Path(__file__).resolve().parent
os.chdir(BASE)

# Carrega .env (UTF-8 tolerante a bytes inválidos)
ENV_FILE = BASE / "brn_network.env"
if ENV_FILE.exists():
    try:
        with open(ENV_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for line in f.read().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception as e:
        print(f"[env] aviso: {e}")

TRUST_FILE = BASE / "trust_network.json"
DB_FILE = BASE / "brn_v2_chain.db"
PEERS_FILE = BASE / "peers_discovered.json"

ACTIVE_TTL = int(os.environ.get("BRN_TRUST_ACTIVE_TTL", "300"))
MIN_SCORE_VALIDATOR = 500.0
PUBKEY_LEN = 66

SEP = "=" * 76
DASH = "-" * 76


def hdr(txt):
    print(f"\n{SEP}\n {txt}\n{SEP}")


def ok(msg):
    print(f"  [OK]    {msg}")


def warn(msg):
    print(f"  [WARN]  {msg}")


def err(msg):
    print(f"  [ERR]   {msg}")


def info(msg):
    print(f"  [INFO]  {msg}")


def load_trust():
    if not TRUST_FILE.exists():
        return {"nodes": {}}
    try:
        with open(TRUST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        err(f"Erro lendo trust_network.json: {e}")
        return {"nodes": {}}


def save_trust(data):
    tmp = TRUST_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, TRUST_FILE)


def valid_pubkey(pk):
    if not pk or not isinstance(pk, str):
        return False
    if len(pk) != PUBKEY_LEN:
        return False
    try:
        int(pk, 16)
        return True
    except ValueError:
        return False


def classify(pk, node):
    now = time.time()
    score = float(node.get("trust_score", 0) or 0)
    status = node.get("status", "new")
    last_seen = float(node.get("last_seen", 0) or 0)
    age = now - last_seen if last_seen else 999999
    banned = bool(node.get("banned", False))

    is_active = (age < ACTIVE_TTL) and not banned
    is_valid = valid_pubkey(pk)
    is_trusted = (status == "trusted") or (score >= MIN_SCORE_VALIDATOR)

    return {
        "pubkey": pk,
        "short": pk[:16] + "...",
        "score": score,
        "status": status,
        "age_s": int(age),
        "banned": banned,
        "is_active": is_active,
        "is_valid_pubkey": is_valid,
        "is_trusted": is_trusted,
        "qualifies": is_active and is_valid and not banned,
    }


# ============================================================
# --check
# ============================================================
def mode_check():
    hdr("VALIDACOES ATUAIS")

    trust = load_trust()
    nodes = trust.get("nodes", {})

    if not nodes:
        warn("Nenhum no no trust_network.json")
        return

    print(f"  {'PUBKEY':<24} {'SCORE':>8} {'STATUS':<12} {'IDADE':>10} {'ATIVO':<6} {'OK':<6} {'QUALIF':<6}")
    print(f"  {DASH}")

    qualificados = 0
    for pk, node in nodes.items():
        i = classify(pk, node)
        if i["qualifies"]:
            qualificados += 1
        print(f"  {i['short']:<24} {i['score']:>8.1f} {i['status']:<12} "
              f"{i['age_s']:>8}s {'SIM' if i['is_active'] else 'NAO':<6} "
              f"{'OK' if i['is_valid_pubkey'] else 'TRUNC':<6} "
              f"{'SIM' if i['qualifies'] else 'NAO':<6}")

    print()
    info(f"Total no trust       : {len(nodes)}")
    info(f"Qualificados p/ val. : {qualificados}")

    # top5 do trust (inclui offline)
    print()
    print(f"  TOP 5 (get_top_validators — inclui offline):")
    try:
        from trust_network import TrustNetwork
        tn = TrustNetwork(str(TRUST_FILE))
        top5 = tn.get_top_validators(count=5)
        for i, pk in enumerate(top5, 1):
            print(f"    [{i}] {pk[:44]}...")
    except Exception as e:
        err(f"get_top_validators falhou: {e}")

    print()
    print(f"  ATIVOS (get_active_validators — last_seen < {ACTIVE_TTL}s + Origin):")
    try:
        from trust_network import TrustNetwork
        tn = TrustNetwork(str(TRUST_FILE))
        active = tn.get_active_validators(count=5)
        for i, pk in enumerate(active, 1):
            print(f"    [{i}] {pk[:44]}...")

        q = float(os.environ.get("BRN_VALIDATOR_QUORUM", "0.5"))
        import math
        req = max(1, math.ceil(len(active) * q)) if active else 0
        print()
        info(f"QUORUM atual: {q}")
        info(f"required: {req}/{len(active)}  "
             f"(com {len(active)} validadores ativos)")
    except Exception as e:
        err(f"get_active_validators falhou: {e}")


# ============================================================
# --promote
# ============================================================
def mode_promote():
    hdr("PROMOVER VALIDADORES")

    trust = load_trust()
    nodes = trust.get("nodes", {})

    if not nodes:
        warn("Nenhum no no trust")
        return

    promovidos = 0
    for pk, node in nodes.items():
        i = classify(pk, node)

        if not i["is_valid_pubkey"]:
            warn(f"{i['short']} — pubkey invalida (len={len(pk)}, esperado {PUBKEY_LEN})")
            continue
        if i["banned"]:
            warn(f"{i['short']} — banned")
            continue
        if not i["is_active"]:
            warn(f"{i['short']} — inativo (last_seen {i['age_s']}s > {ACTIVE_TTL}s)")
            continue
        if i["is_trusted"]:
            info(f"{i['short']} — ja e trusted (score={i['score']:.1f})")
            continue

        # Promove
        old_score = i["score"]
        node["trust_score"] = 1000.0
        node["status"] = "trusted"
        node["promoted_at"] = time.time()
        node["last_seen"] = time.time()
        promovidos += 1
        ok(f"{i['short']} — PROMOVIDO (score {old_score:.1f} → 1000.0)")

    if promovidos > 0:
        save_trust(trust)
        print()
        ok(f"{promovidos} no(s) promovido(s). trust_network.json atualizado.")
    else:
        print()
        info("Nenhum no precisava de promocao.")


# ============================================================
# --verify-signing
# ============================================================
def mode_verify_signing(n=20):
    hdr(f"VERIFICAR ASSINATURA — ultimos {n} blocos")

    try:
        from blockchain import Blockchain
        from trust_network import TrustNetwork
    except ImportError as e:
        err(f"Falha importando: {e}")
        return

    try:
        bc = Blockchain(str(DB_FILE), auto_genesis=False)
        tn = TrustNetwork(str(TRUST_FILE))
        bc.set_trust_net(tn)
    except Exception as e:
        err(f"Falha abrindo chain/trust: {e}")
        return

    try:
        h = bc.db.height()
        validators = bc._get_active_validators()
        required = bc._required_signatures(validators)

        info(f"Altura atual     : {h}")
        info(f"Validators ativos: {len(validators)}")
        info(f"Required         : {required}/{len(validators)}")

        if h < 0:
            warn("Sem blocos")
            return

        print()
        print(f"  {'ALTURA':<8} {'SIGS':<6} SIGNERS")
        print(f"  {DASH}")

        total_2plus = 0
        total_blocks = 0
        for hh in range(max(1, h - n + 1), h + 1):
            b = bc.db.get_block(hh)
            if not b:
                continue
            total_blocks += 1
            sigs = b.get("validators_sig", []) or []
            if len(sigs) >= 2:
                total_2plus += 1
            signers = ", ".join(s.get("pubkey", "?")[:12] for s in sigs) or "-"
            print(f"  {hh:<8} {len(sigs):<6} {signers}")

        print()
        info(f"Blocos com 2+ assinaturas: {total_2plus}/{total_blocks}")

        if total_2plus == 0 and total_blocks > 0:
            print()
            print(f"  {SEP}")
            print("  DIAGNOSTICO — por que peers nao assinam?")
            print(f"  {SEP}")

            # [1] p2p_unified version
            print()
            print("  [1] p2p_unified.py e v6.7.7 (race recovery)?")
            try:
                import p2p_unified
                doc = p2p_unified.__doc__ or ""
                if "v6.7.7" in doc:
                    ok("v6.7.7 ativo (race recovery)")
                else:
                    err("NAO e v6.7.7 — sem race recovery, peers recusam quando bloco ja e tip")
                    print("        Fix: substituir p2p_unified.py pelo v6.7.7")
            except Exception as e:
                warn(f"Erro: {e}")

            # [2] sign collector
            print()
            print("  [2] Coletor de assinaturas configurado?")
            collector = getattr(bc, "_sign_collector", None)
            if collector is not None:
                ok("_sign_collector ativo")
            else:
                err("_sign_collector NAO configurado")
                print("        Fix: main.py precisa chamar chain.set_sign_collector()")
                print("        (acontece automaticamente se P2P + trust_net estao ativos)")

            # [3] timeout
            print()
            print("  [3] SIG_COLLECT_TIMEOUT suficiente?")
            try:
                timeout = float(os.environ.get("BRN_SIG_COLLECT_TIMEOUT", "10"))
                if timeout < 10:
                    warn(f"{timeout}s — pode ser curto com 3+ peers")
                    print("        Fix: echo BRN_SIG_COLLECT_TIMEOUT=15 >> brn_network.env")
                else:
                    ok(f"{timeout}s")
            except ValueError:
                warn("Valor invalido")

            # [4] peers online
            print()
            print("  [4] Peers online?")
            if PEERS_FILE.exists():
                try:
                    from p2p_unified import P2PClient
                    peers = json.loads(PEERS_FILE.read_text())
                    for addr in peers[:5]:
                        if ":" not in addr:
                            continue
                        try:
                            ip, port_s = addr.rsplit(":", 1)
                            resp, _ = P2PClient.get_chain_height(ip, int(port_s), timeout=3.0)
                            if resp:
                                ok(f"{addr} — online (h={resp.get('height')})")
                            else:
                                warn(f"{addr} — sem resposta")
                        except Exception:
                            warn(f"{addr} — erro")
                except Exception:
                    pass

            print()
            print(f"  {SEP}")
            print("  PROXIMO PASSO: rode --test-peers para ver o motivo exato")
            print(f"  {SEP}")
    finally:
        try:
            bc.db.close()
        except Exception:
            pass


# ============================================================
# --test-peers
# ============================================================
def mode_test_peers():
    hdr("TESTAR PEERS — eles assinam blocos?")

    if not PEERS_FILE.exists():
        warn("peers_discovered.json nao existe")
        return

    try:
        peers = json.loads(PEERS_FILE.read_text())
    except Exception as e:
        err(f"Erro lendo peers: {e}")
        return

    if not peers:
        warn("Nenhum peer descoberto")
        return

    try:
        from p2p_unified import P2PClient
    except ImportError as e:
        err(f"p2p_unified indisponivel: {e}")
        return

    info(f"Testando {len(peers)} peer(s)")
    print()

    for addr in peers:
        if ":" not in addr:
            continue
        try:
            ip, port_s = addr.rsplit(":", 1)
            port = int(port_s)
        except Exception:
            continue

        print(f"  ── {addr} ──")

        # 1. Pegar altura
        try:
            resp, lat = P2PClient.get_chain_height(ip, port, timeout=5.0)
        except Exception as e:
            err(f"  get_chain_height falhou: {e}")
            continue

        if not resp:
            err(f"  sem resposta (peer offline ou porta fechada)")
            continue

        peer_h = resp.get("height", -1)
        info(f"  altura={peer_h}  latencia={lat:.0f}ms")

        # 2. Baixar o tip block do peer
        try:
            blk_resp, _ = P2PClient.send_message(
                ip, port, {"type": "get_block", "height": peer_h}, timeout=5.0
            )
        except Exception as e:
            err(f"  get_block falhou: {e}")
            continue

        if not blk_resp or not blk_resp.get("block"):
            err(f"  peer nao devolveu bloco #{peer_h}")
            continue

        tip_block = blk_resp["block"]
        info(f"  bloco #{peer_h} hash={tip_block.get('hash','?')[:16]}...")

        # 3. Enviar sign_block com o proprio tip do peer
        try:
            sign_resp, _ = P2PClient.send_message(
                ip, port,
                {"type": "sign_block", "block": tip_block},
                timeout=8.0,
            )
        except Exception as e:
            err(f"  sign_block falhou: {e}")
            continue

        if not sign_resp:
            err("  sem resposta ao sign_block")
            continue

        pk = (sign_resp.get("pubkey") or "").strip()
        sig = (sign_resp.get("signature") or "").strip()
        error = (sign_resp.get("error") or "").strip()

        if pk and sig:
            ok(f"  ASSINA (pubkey={pk[:16]}...)")
            ok(f"  v6.7.7 ativo (race recovery funcionou)")
        elif error:
            err(f"  RECUSA: {error}")
            if "altura_fora" in error:
                print("        → peer tem v6.7.6 (sem race recovery)")
            elif "nao_no_top5" in error:
                print("        → peer nao tem a propria pubkey no trust")
            elif "sem_config" in error:
                print("        → peer sem SK local (current_wallet.enc)")
            elif "prev_hash_fork" in error or "hash_fork" in error:
                print("        → fork local — sincronizar")
            elif "validate_block" in error:
                print("        → bloco falhou validacao (normal se peer avancou)")
        else:
            err("  RECUSA sem motivo (peer tem v6.7.6)")
            print("        → atualizar p2p_unified.py para v6.7.7")

        print()


# ============================================================
# --test-local
# ============================================================
def mode_test_local():
    hdr("TESTAR ASSINATURA LOCAL — voce assinaria?")

    try:
        from blockchain import Blockchain, sign_block_as_validator
        from trust_network import TrustNetwork
        from secure_store import load_current_wallet
        from wallet import Wallet
        from p2p_unified import P2PServer, Metrics
    except ImportError as e:
        err(f"Falha importando: {e}")
        return

    try:
        bc = Blockchain(str(DB_FILE), auto_genesis=False)
        tn = TrustNetwork(str(TRUST_FILE))
        bc.set_trust_net(tn)
    except Exception as e:
        err(f"Falha abrindo chain/trust: {e}")
        return

    # Load wallet
    pw = (os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
          or os.environ.get("BRN_NODE_PASSWORD", "").strip())
    if not pw:
        err("Sem BRN_WALLET_SESSION_PASS/BRN_NODE_PASSWORD no .env")
        bc.db.close()
        return

    try:
        w = load_current_wallet(pw)
        if not w or not w.get("ok"):
            err(f"Carteira nao decifrou: {w.get('msg','?')}")
            bc.db.close()
            return
        sk = Wallet(private_key_hex=w["private_key"])
        bc.set_local_validator_sk(sk)
    except Exception as e:
        err(f"Erro carregando wallet: {e}")
        bc.db.close()
        return

    h = bc.db.height()
    tip = bc.db.tip_hash() or ("0" * 64)

    info(f"Altura local: {h}")
    info(f"Pubkey local: {sk.pub_hex[:40]}...")
    validators = bc._get_active_validators()
    required = bc._required_signatures(validators)
    info(f"Validators ativos: {len(validators)} | required: {required}")

    # Bloco fake (vai falhar em header_hash, mas mostra ate onde chega)
    fake_block = {
        "height": h + 1,
        "hash": "a" * 64,
        "prev_hash": tip,
        "timestamp": int(time.time()),
        "nonce": 0,
        "merkle": "b" * 64,
        "difficulty": bc.current_difficulty(),
        "transactions": [],
        "validators_sig": [],
    }

    print()
    print("  Testando com bloco FICTICIO (hash='aaa...'):")
    print("  (vai falhar em header_hash_diverge — e o esperado)")

    metrics = Metrics()
    server = P2PServer(bc, 6001, metrics, on_new_block=None, on_new_tx=None)
    server.node_uuid = "test_local"

    try:
        resp = server._handle_sign_block(
            {"block": fake_block},
            addr=("127.0.0.1", 12345),
            peer_id="test",
        )
    except Exception as e:
        err(f"Excecao: {e}")
        bc.db.close()
        return

    error = resp.get("error", "(sem campo error - v6.7.6)")
    print()
    if resp.get("pubkey") and resp.get("signature"):
        ok("VOCE ASSINOU (bloco ficticio)")
    elif "header_hash_diverge" in str(error):
        ok("Validacao chegou ate o hash — o RESTO esta funcionando")
        info(f"Motivo: {error}")
        info("Se um bloco REAL chegar, vai assinar (o hash ficticio e que falha)")
    elif error:
        err(f"Motivo: {error}")
        if "nao_no_top5" in str(error):
            print("        → sua pubkey nao esta em get_top_validators()")
            print("        → rode --promote ou reinicie o main.py")
        elif "altura_fora" in str(error):
            print("        → race recovery NAO esta ativo (v6.7.6)")
            print("        → atualizar p2p_unified.py v6.7.7")
        elif "sem_config" in str(error):
            print("        → SK local nao chegou em chain._local_validator_sk")
    else:
        err("RECUSA sem motivo (v6.7.6)")

    bc.db.close()


# ============================================================
# --daemon
# ============================================================
def mode_daemon(interval=30):
    hdr(f"DAEMON — loop a cada {interval}s (Ctrl+C para parar)")
    try:
        while True:
            print(f"\n[{time.strftime('%H:%M:%S')}] ──────────────────────")
            mode_promote()
            mode_check()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[daemon] encerrado")


# ============================================================
# main
# ============================================================
def main():
    p = argparse.ArgumentParser(
        prog="validator_manager.py",
        description="Gerencia validadores BRN e verifica assinatura de blocos",
    )
    p.add_argument("--check", action="store_true",
                   help="Lista validadores e status (default)")
    p.add_argument("--promote", action="store_true",
                   help="Promove qualificados a trusted (score 1000)")
    p.add_argument("--verify-signing", action="store_true",
                   help="Verifica se ultimos N blocos tem 2+ assinaturas")
    p.add_argument("--test-peers", action="store_true",
                   help="Testa cada peer: eles assinam?")
    p.add_argument("--test-local", action="store_true",
                   help="Testa assinatura local")
    p.add_argument("--daemon", action="store_true",
                   help="Loop continuo (Ctrl+C para parar)")
    p.add_argument("--all", action="store_true",
                   help="Executa check + promote + verify-signing + test-peers")
    p.add_argument("--blocks", type=int, default=20,
                   help="N blocos para --verify-signing (default 20)")
    p.add_argument("--interval", type=int, default=30,
                   help="Intervalo do daemon em segundos (default 30)")

    args = p.parse_args()

    if args.all:
        mode_check()
        mode_promote()
        mode_verify_signing(args.blocks)
        mode_test_peers()
        return 0

    if args.daemon:
        return mode_daemon(args.interval) or 0
    if args.promote:
        mode_promote()
        return 0
    if args.verify_signing:
        mode_verify_signing(args.blocks)
        return 0
    if args.test_peers:
        mode_test_peers()
        return 0
    if args.test_local:
        mode_test_local()
        return 0

    # Default
    mode_check()
    return 0


if __name__ == "__main__":
    sys.exit(main())
