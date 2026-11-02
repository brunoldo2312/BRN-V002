#!/usr/bin/env python3
"""test_signature.py - Verifica se o no vai assinar/aceitar o proximo bloco."""
import os, sys, time, logging

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
env_file = os.path.join(BASE, "brn_network.env")
if os.path.exists(env_file):
    for line in open(env_file, encoding="utf-8").read().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip(); v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

SEP = "=" * 70
print(SEP)
print(" TESTE DE ASSINATURA BRN")
print(" Diretorio:", BASE)
print(SEP)

try:
    from blockchain import (Blockchain, sign_block_as_validator,
                            verify_block_signature)
except Exception as e:
    print(f"[X] Falha importando blockchain: {e}"); sys.exit(1)

try:
    chain = Blockchain("brn_v2_chain.db", auto_genesis=False)
    h = chain.db.height(); tip = chain.db.tip_hash()
    print(f"\n[1] Blockchain OK")
    print(f"    Altura atual : {h}")
    print(f"    Tip hash     : {(tip or '(vazio)')[:32]}...")
except Exception as e:
    print(f"\n[X] Erro abrindo DB: {e}"); sys.exit(1)

try:
    from trust_network import TrustNetwork
    trust = TrustNetwork(os.path.join(BASE, "trust_network.json"))
    chain.set_trust_net(trust)
    print(f"\n[2] TrustNetwork OK")
    print(f"    Nos no arquivo : {len(trust.nodes)}")
except Exception as e:
    print(f"\n[X] Erro TrustNetwork: {e}"); sys.exit(1)

pw = (os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
      or os.environ.get("BRN_NODE_PASSWORD", "").strip())
if not pw:
    print("\n[X] Sem senha em BRN_WALLET_SESSION_PASS/BRN_NODE_PASSWORD"); sys.exit(1)

try:
    from secure_store import load_current_wallet
    from wallet import Wallet
    w = load_current_wallet(pw)
    if not w or not w.get("ok"):
        print(f"\n[X] Carteira nao decifrou: {w.get('msg','?')}"); sys.exit(1)
    sk_hex = w.get("private_key") or w.get("priv_hex")
    if not sk_hex:
        print("\n[X] Carteira sem private_key"); sys.exit(1)
    sk = Wallet(private_key_hex=sk_hex)
    chain.set_local_validator_sk(sk)
    print(f"\n[3] SK local OK")
    print(f"    Pubkey : {sk.pub_hex[:40]}...")
except Exception as e:
    print(f"\n[X] Erro SK local: {e}"); sys.exit(1)

validators = chain._get_active_validators()
required = chain._required_signatures(validators)
print(f"\n[4] Validators ATIVOS : {len(validators)}")
print(f"    Required (quorum): {required}")
for v in validators:
    marker = "  <- VOCE (local)" if v == sk.pub_hex else ""
    print(f"      {v[:44]}...{marker}")

test_block = {
    "height": h + 1, "hash": "a" * 64, "prev_hash": tip or "0" * 64,
    "timestamp": int(time.time()), "nonce": 0, "merkle": "b" * 64,
    "difficulty": chain.current_difficulty(), "transactions": [],
    "validators_sig": [],
}
print(f"\n[5] Bloco de teste: #{test_block['height']}")

entry = sign_block_as_validator(test_block, sk)
if not entry:
    print("\n[X] Assinatura local retornou vazio"); sys.exit(1)
print(f"\n[6] Assinatura local OK")
print(f"    Sig : {entry['signature'][:32]}...")

if not verify_block_signature(sk.pub_hex, test_block, entry["signature"]):
    print("\n[X] Verificacao criptografica FALHOU"); sys.exit(1)
print(f"\n[7] Verificacao criptografica OK")

test_block["validators_sig"] = [entry]
ok, msg = chain._validate_validators_sig(test_block, validators=validators)
print(f"\n[8] _validate_validators_sig")
print(f"    Resultado : {'ACEITO' if ok else 'REJEITADO'}")
if msg:
    print(f"    Motivo    : {msg}")

print("\n" + SEP)
if ok:
    print(" VAI ASSINAR E ACEITAR O PROXIMO BLOCO")
    print("   -> Suba: py main.py")
else:
    print(" NAO VAI ACEITAR - corrija o motivo acima")
print(SEP)

try:
    chain.db.close()
except Exception:
    pass
