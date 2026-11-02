import os, sys, json, time

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)

if os.path.exists("brn_network.env"):
    for line in open("brn_network.env", encoding="utf-8").read().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from blockchain import Blockchain
from trust_network import TrustNetwork
from secure_store import load_current_wallet
from wallet import Wallet
from p2p_unified import P2PClient, PeerDiscovery

chain = Blockchain("brn_v2_chain.db", auto_genesis=False)
trust = TrustNetwork("trust_network.json")
chain.set_trust_net(trust)
pw = os.environ.get("BRN_WALLET_SESSION_PASS") or os.environ.get("BRN_NODE_PASSWORD", "")
w = load_current_wallet(pw)
sk = Wallet(private_key_hex=w["private_key"])
chain.set_local_validator_sk(sk)

h = chain.db.height()
tip = chain.db.tip_hash()
test_block = {
    "height": h + 1, "hash": "a" * 64, "prev_hash": tip or "0" * 64,
    "timestamp": int(time.time()), "nonce": 0,
    "merkle": "b" * 64, "difficulty": chain.current_difficulty(),
    "transactions": [], "validators_sig": [],
}

print("=" * 74)
print(" DIAGNOSTICO MULTI-SIG")
print("=" * 74)
print(f"Altura local : {h}")
print(f"Tip          : {(tip or '')[:32]}...")
print(f"SK local     : {sk.pub_hex[:24]}...")
print()

validators = chain._get_active_validators()
print(f"Validators ATIVOS ({len(validators)}):")
for v in validators:
    m = " <- LOCAL" if v == sk.pub_hex else ""
    print(f"  {v[:40]}...{m}")
print(f"Required     : {chain._required_signatures(validators)}")
print()

disc = PeerDiscovery(6001, "diag")
peers = disc.listar_peers()
print(f"Peers P2P ({len(peers)}):")
for p in peers:
    print(f"  {p}")
print()

print("Testando sign_block em cada peer:")
print("-" * 74)
for p in peers:
    if ":" not in p:
        continue
    try:
        ip, port_s = p.rsplit(":", 1)
        port = int(port_s)
    except Exception:
        continue

    try:
        raw, lat = P2PClient.send_message(
            ip, port,
            {"type": "sign_block", "block": test_block},
            timeout=8,
        )
    except Exception as e:
        print(f"  {p:24} ERRO: {e}")
        continue

    if raw is None:
        print(f"  {p:24} SEM RESPOSTA (timeout/rede)")
        continue

    rtype = raw.get("type", "?")
    pk = (raw.get("pubkey") or "").strip()
    sg = (raw.get("signature") or "").strip()

    if rtype != "sign_block_response":
        print(f"  {p:24} tipo inesperado: {rtype}")
        print(f"    raw: {str(raw)[:120]}")
        continue

    if not pk or not sg:
        print(f"  {p:24} RECUSOU (pubkey vazia)")
        # Pede motivo - tenta ver /api/status do peer
        try:
            from urllib.request import urlopen
            url = f"http://{ip}:5000/api/chain-info"
            ci = json.loads(urlopen(url, timeout=3).read())
            print(f"    peer height = {ci.get('height')} (esperado {h} ou {h+1})")
        except Exception:
            print(f"    (nao consegui consultar /api/chain-info do peer)")
        continue

    in_valid = pk in validators
    print(f"  {p:24} OK pk={pk[:24]}... in_valid={in_valid}")
    if not in_valid:
        print(f"    AVISO: peer assinou mas esta FORA do set local")

print()
print("=" * 74)
print(" LEGENDA DOS PROBLEMAS")
print("=" * 74)
print("  SEM RESPOSTA  -> peer offline ou porta P2P 6001 fechada")
print("  RECUSOU       -> _handle_sign_block do peer retornou vazio.")
print("                   Causas: (a) peer sem _local_validator_sk,")
print("                           (b) peer nao esta em get_top_validators(5),")
print("                           (c) peer em altura diferente")
print("  tipo inesperado -> peer velho (p2p_unified < v6.7.6)")
print("  OK in_valid=True -> ESTE PEER CO-ASSINA")
print("  OK in_valid=False -> peer assina mas trust local nao reconhece")
print()
print("Para 2+ sigs: >=1 peer precisa responder 'OK in_valid=True'")
print("=" * 74)
