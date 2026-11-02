#!/usr/bin/env python3
"""test_sign_local.py - Testa se a Mayra assinaria um bloco pedido por um peer.

Carrega o .env manualmente (com encoding UTF-8 tolerante a erros), prepara
o Blockchain, TrustNetwork e SK local, e chama _handle_sign_block() como se
o Bruno estivesse pedindo assinatura.

Windows: o arquivo .env pode ter acentos em comentários. Por isso o
open() usa encoding='utf-8', errors='ignore' - se não fizer isso, o cp1252
padrão do Windows pode falhar com UnicodeDecodeError.
"""
import os
import sys
import time
import json

# ------------------------------------------------------------------
# 1) Carrega .env com encoding tolerante (UTF-8 + ignore de bytes ruins)
# ------------------------------------------------------------------
BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)

ENV_FILE = os.path.join(BASE, "brn_network.env")
if os.path.exists(ENV_FILE):
    try:
        with open(ENV_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for line in f.read().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    # setdefault: não sobrescreve o que já está no ambiente
                    os.environ.setdefault(k, v)
        print("[env] brn_network.env carregado (%d linhas lidas)" % len(open(ENV_FILE, encoding="utf-8", errors="ignore").read().splitlines()))
    except Exception as e:
        print("[env] aviso: falha lendo brn_network.env: %s" % e)
else:
    print("[env] aviso: brn_network.env não encontrado — continuando só com o ambiente")

# ------------------------------------------------------------------
# 2) Imports (o .env já está no ambiente agora, então discovery_v2
#    não vai reclamar de BRN_NETWORK_SECRET ausente)
# ------------------------------------------------------------------
try:
    from blockchain import Blockchain, sign_block_as_validator, verify_block_signature
    from trust_network import TrustNetwork
    from secure_store import load_current_wallet
    from wallet import Wallet
    from p2p_unified import P2PServer, Metrics
except Exception as e:
    print("[X] Falha importando módulos: %s" % e)
    import traceback
    traceback.print_exc()
    sys.exit(1)

# ------------------------------------------------------------------
# 3) Carrega chain + trust + SK
# ------------------------------------------------------------------
try:
    chain = Blockchain("brn_v2_chain.db", auto_genesis=False)
except Exception as e:
    print("[X] Falha abrindo chain: %s" % e)
    sys.exit(1)

try:
    trust = TrustNetwork("trust_network.json")
    chain.set_trust_net(trust)
except Exception as e:
    print("[X] Falha carregando trust_network.json: %s" % e)
    chain.db.close()
    sys.exit(1)

pw = (os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
      or os.environ.get("BRN_NODE_PASSWORD", "").strip())
if not pw:
    print("[X] Senha não configurada (BRN_WALLET_SESSION_PASS ou BRN_NODE_PASSWORD)")
    chain.db.close()
    sys.exit(1)

try:
    w = load_current_wallet(pw)
    if not w or not w.get("ok"):
        print("[X] Carteira não decifrou: %s" % (w.get("msg", "?") if w else "?"))
        chain.db.close()
        sys.exit(1)
    sk_hex = w.get("private_key") or w.get("priv_hex")
    if not sk_hex:
        print("[X] Carteira sem private_key")
        chain.db.close()
        sys.exit(1)
    sk = Wallet(private_key_hex=sk_hex)
    chain.set_local_validator_sk(sk)
except Exception as e:
    print("[X] Falha carregando SK: %s" % e)
    chain.db.close()
    sys.exit(1)

# ------------------------------------------------------------------
# 4) Estado atual
# ------------------------------------------------------------------
h = chain.db.height()
tip = chain.db.tip_hash() or ("0" * 64)
diff = chain.current_difficulty()
validators = chain._get_active_validators()
required = chain._required_signatures(validators)

print("=" * 70)
print(" TESTE: peer pede assinatura PRA MAYRA (via _handle_sign_block)")
print("=" * 70)
print(" Altura local : %d" % h)
print(" Tip hash     : %s..." % (tip[:32] if tip else "(vazio)"))
print(" Dificuldade  : %d" % diff)
print(" SK da Mayra  : %s..." % sk.pub_hex[:40])
print(" SK address   : %s" % sk.address)
print()
print(" Validators ATIVOS (%d):" % len(validators))
for v in validators:
    marker = "  ← VOCÊ" if v == sk.pub_hex else ""
    print("   %s...%s" % (v[:40], marker))
print(" Required     : %d/%d" % (required, len(validators)))
print()
print("─" * 70)

# ------------------------------------------------------------------
# 5) Constrói um bloco #h+1 fictício (só para o _handle_sign_block)
# ------------------------------------------------------------------
fake_block = {
    "height": h + 1,
    "hash": "a" * 64,
    "prev_hash": tip,
    "timestamp": int(time.time()),
    "nonce": 0,
    "merkle": "b" * 64,
    "difficulty": diff,
    "transactions": [],
    "validators_sig": [],
}

# ------------------------------------------------------------------
# 6) Chama _handle_sign_block como se fosse o P2PServer recebendo
#    {"type":"sign_block","block":fake_block} do Bruno
# ------------------------------------------------------------------
metrics = Metrics()
server = P2PServer(chain, 6001, metrics, on_new_block=None, on_new_tx=None)
server.node_uuid = "test_local"

try:
    resp = server._handle_sign_block(
        {"block": fake_block},
        addr=("127.0.0.1", 12345),
        peer_id="test",
    )
except Exception as e:
    print("[X] Exceção em _handle_sign_block: %s" % e)
    import traceback
    traceback.print_exc()
    chain.db.close()
    sys.exit(1)

# ------------------------------------------------------------------
# 7) Mostra resultado
# ------------------------------------------------------------------
print(" RESPOSTA:")
print("   type      : %s" % resp.get("type", "?"))
print("   pubkey    : %s" % (resp.get("pubkey") or "(vazio)"))
print("   signature : %s" % ((resp.get("signature") or "(vazia)")[:64]))
print("   error     : %s" % resp.get("error", "(sem motivo)"))
print("─" * 70)

# ------------------------------------------------------------------
# 8) Veredito
# ------------------------------------------------------------------
signed = bool(resp.get("pubkey") and resp.get("signature"))

if signed:
    # Verifica criptograficamente
    ok_sig = verify_block_signature(resp["pubkey"], fake_block, resp["signature"])
    if ok_sig:
        print(" ✓ MAYRA VAI ASSINAR")
        print("   → pubkey : %s" % resp["pubkey"][:40])
        print("   → verificação criptográfica: OK")
        print()
        print(" Próximo passo: o Bruno precisa enviar o pedido real. Sem")
        print(" race recovery no p2p_unified.py v6.7.7 do Bruno, ele pode")
        print(" recusar por 'altura' mesmo assim. Fix é só no Bruno.")
    else:
        print(" ⚠ MAYRA ASSINOU MAS VERIFICAÇÃO FALHOU")
        print("   → pubkey : %s" % resp["pubkey"][:40])
        print("   → signature inválida (bug de wallet)")
else:
    motivo = resp.get("error", "(motivo nao retornado - versao antiga do p2p_unified)")
    print(" ✗ MAYRA RECUSA — motivo: %s" % motivo)
    print()
    print(" Como corrigir:")
    if "altura_fora" in motivo:
        print("   → Aplicar o p2p_unified.py v6.7.7 (race recovery)")
    elif "nao_no_top5" in motivo:
        print("   → Sua pubkey não está em get_top_validators(5) do trust")
        print("   → Rode: py clean_trust.py   (ou reinicie o main.py)")
    elif "prev_hash_fork" in motivo or "hash_fork" in motivo:
        print("   → Fork local. Rode: py sync_all_peers.py")
    elif "sem_config" in motivo:
        print("   → SK não chegou em chain._local_validator_sk")
        print("   → Verifique secure_store.load_current_wallet()")
    elif "validate_block" in motivo:
        print("   → validate_block do bloco fictício falhou (esperado)")
        print("   → Esse bloco é HASH FICTÍCIO ('a'*64). O teste real")
        print("     é quando o Bruno minerar #h+1 e pedir assinatura.")
        print("     Se _validate_validators_sig chega até aqui, está OK.")
    elif "versao antiga" in motivo:
        print("   → p2p_unified.py ainda é < v6.7.7")
        print("   → Recarregue o arquivo completo que te passei")
    else:
        print("   → Ver o log do main.py para detalhes")

print("=" * 70)

try:
    chain.db.close()
except Exception:
    pass
