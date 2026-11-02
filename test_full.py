#!/usr/bin/env python3
"""test_full.py - Teste completo de consenso e co-assinatura do BRN.

Verifica:
  [1]  .env (variaveis criticas)
  [2]  versoes de p2p_unified.py e blockchain.py
  [3]  trust_network.json (contagem, pubkeys)
  [4]  wallet local (decifra, pubkey)
  [5]  node_identity.enc (decifra, node id)
  [6]  chain local (altura, tip, work, difficulty)
  [7]  validadores ativos + required
  [8]  assinatura local (sign_block com hash ficticio)
  [9]  ultimos 20 blocos (quem minerou, quantas sigs)
  [10] peers via TCP 6001 (altura, tip, sync)
  [11] sign_block remoto (simula pedido aos peers)
  [12] debug: confirma BRN_REQUIRE_AUTH_TAG no blockchain.py
  [13] VEREDITO

Uso (Windows):
    python test_full.py

Nao envia transacoes nem aceita blocos. Apenas LEITURA.
"""
import os
import sys
import json
import time
import socket

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)

# ============================================================
# 0. Carrega .env (com encoding tolerante)
# ============================================================
ENV_FILE = "brn_network.env"
env_loaded = 0
if os.path.exists(ENV_FILE):
    try:
        with open(ENV_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for line in f.read().splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
                    env_loaded += 1
    except Exception as e:
        print(f"[env] ERRO lendo {ENV_FILE}: {e}")

SEP = "=" * 78
SUB = "-" * 78

# Nomes amigaveis (edite se quiser)
NOMES = {
    "0305817682ff0b7ca51a7bfc836659278c0a4749242b7b563d49e15df4e4a6e535": "Mayra",
    "031be530bf2cbd3709dffb7c1ba5a5a31ab7364c5aae977a72d8407d1753e8c575": "Bruno",
    "035af5dc675bb805e3bca689e9a48299453d2d5895efe42aeaab52256942c98d12": "PC3-antigo",
    "0207385a7b60e03b": "PC3-novo",
}

def nome(pk):
    if not pk:
        return "?"
    for full, n in NOMES.items():
        if full.startswith(pk[:16]) or pk.startswith(full[:16]):
            return n
    return pk[:16] + "..."

# ============================================================
# 1. .env
# ============================================================
print(SEP)
print(" TESTE COMPLETO DE CONSENSO E CO-ASSINATURA BRN")
print(f" Diretorio: {BASE}")
print(f" Data: {time.strftime('%Y-%m-%d %H:%M:%S')}")
print(SEP)

print(f"\n[1] .ENV ({env_loaded} variaveis carregadas)")

CRITICAS = [
    ("BRN_NETWORK_SECRET", "64 hex", True),
    ("BRN_NODE_PASSWORD", "min 8 chars", True),
    ("BRN_WALLET_SESSION_PASS", "min 8 chars", True),
    ("BRN_BOOTSTRAP_SKIP_SIGS_UNTIL", "int", False),
    ("BRN_REQUIRE_AUTH_TAG", "0|1", False),
    ("BRN_MAX_VALIDATORS", "int", False),
    ("BRN_VALIDATOR_QUORUM", "float", False),
    ("BRN_REQUIRE_ALL_SIGS", "0|1", False),
    ("BRN_POOL_URL", "http(s)://", False),
    ("BRN_POOL_ENABLED", "0|1", False),
    ("BRN_POOL_CLIENT", "0|1", False),
    ("BRN_IS_ORIGIN", "0|1", False),
    ("BRN_AUTO_MINE", "0|1", False),
    ("BRN_SEU_ENDERECO", "ip:port", False),
    ("BRN_BOOTSTRAP_PEERS", "csv", False),
]

for key, desc, obrig in CRITICAS:
    v = os.environ.get(key, "")
    if not v:
        if obrig:
            print(f"  [X] {key}=AUSENTE ({desc})")
        else:
            print(f"  [ ] {key}= (vazio)")
        continue
    if key in ("BRN_NETWORK_SECRET", "BRN_NODE_PASSWORD", "BRN_WALLET_SESSION_PASS"):
        masked = v[:8] + "..." + v[-4:] if len(v) > 12 else "(curto)"
        print(f"  [ok] {key}={masked} (len={len(v)})")
    else:
        print(f"  [ok] {key}={v}")

# ============================================================
# 2. Versoes dos arquivos
# ============================================================
print(f"\n[2] VERSOES DOS ARQUIVOS")

for fname, padrao, esperado, desc in [
    ("p2p_unified.py", "v6.7.", "v6.7.7", "race recovery"),
    ("blockchain.py", "9.4.", "9.4.10", "skip sigs + auth_tag"),
]:
    if not os.path.exists(fname):
        print(f"  [X] {fname} NAO EXISTE")
        continue
    try:
        txt = open(fname, encoding="utf-8", errors="ignore").read(2000)
        found = None
        for tag in (esperado, "v6.7.7", "9.4.10"):
            if tag in txt:
                found = tag
                break
        if found == esperado or found in (esperado, "v6.7.7", "9.4.10"):
            print(f"  [ok] {fname}: {found} ({desc})")
        else:
            print(f"  [!] {fname}: {found or '(nao detectado)'} — esperado {esperado}")
    except Exception as e:
        print(f"  [X] {fname}: {e}")

# ============================================================
# 3. Trust Network
# ============================================================
print(f"\n[3] TRUST NETWORK")

trust_ok = False
trust_count = 0
trust_pubkeys = []
if os.path.exists("trust_network.json"):
    try:
        d = json.load(open("trust_network.json", encoding="utf-8"))
        nodes = d.get("nodes", {})
        trust_count = len(nodes)
        trust_pubkeys = list(nodes.keys())
        print(f"  Total de nos: {trust_count}")
        for k in nodes:
            ok_len = "OK" if len(k) == 66 else f"len={len(k)} (devia ser 66)"
            print(f"    [{len(k):2d}] {k}  ({nome(k)})  — {ok_len}")
        if trust_count == 3:
            trust_ok = True
        elif trust_count < 3:
            print(f"  [!] Poucos nos no trust ({trust_count}). Devia ter 3.")
        else:
            print(f"  [!] Muitos nos no trust ({trust_count}). Limpar fantasmas.")
    except Exception as e:
        print(f"  [X] trust_network.json: {e}")
else:
    print("  [X] trust_network.json NAO EXISTE")

# ============================================================
# 4. Wallet local
# ============================================================
print(f"\n[4] WALLET LOCAL")

sk = None
local_pub = ""
try:
    from secure_store import load_current_wallet
    from wallet import Wallet

    pw = (os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
          or os.environ.get("BRN_NODE_PASSWORD", "").strip())
    if not pw:
        print("  [X] Sem senha em BRN_WALLET_SESSION_PASS/BRN_NODE_PASSWORD")
    elif not os.path.exists("current_wallet.enc"):
        print("  [X] current_wallet.enc NAO EXISTE")
    else:
        w = load_current_wallet(pw)
        if not w or not w.get("ok"):
            print(f"  [X] Carteira nao decifrou: {(w or {}).get('msg', '?')}")
        else:
            sk = Wallet(private_key_hex=w.get("private_key") or w.get("priv_hex"))
            local_pub = sk.pub_hex
            print(f"  [ok] Carteira OK")
            print(f"       Pubkey : {local_pub[:48]}...")
            print(f"       Address: {sk.address}")
            in_trust = local_pub in trust_pubkeys
            print(f"       No trust: {'SIM' if in_trust else 'NAO — adicionar!'}")
except Exception as e:
    print(f"  [X] Erro wallet: {e}")

# ============================================================
# 5. Node identity
# ============================================================
print(f"\n[5] NODE IDENTITY")

try:
    from secure_store import load_wallet as load_node
    pw = os.environ.get("BRN_NODE_PASSWORD", "").strip()
    if not pw:
        print("  [X] Sem BRN_NODE_PASSWORD")
    elif not os.path.exists("node_identity.enc"):
        print("  [X] node_identity.enc NAO EXISTE")
    else:
        nd = load_node(pw, "node_identity.enc")
        if not nd:
            print("  [X] Nao decifrou")
        else:
            print(f"  [ok] Node ID: {nd.get('pub', '')[:48]}...")
except Exception as e:
    print(f"  [X] Erro node_identity: {e}")

# ============================================================
# 6. Chain local
# ============================================================
print(f"\n[6] CHAIN LOCAL")

chain = None
local_height = -1
local_tip = ""
local_work = 0
try:
    from blockchain import Blockchain
    chain = Blockchain("brn_v2_chain.db", auto_genesis=False)
    local_height = chain.db.height()
    local_tip = chain.db.tip_hash() or ""
    local_work = chain.cumulative_work()
    diff = chain.current_difficulty()

    if local_height < 0:
        print(f"  [X] Chain VAZIA (sem genesis). Sincronizar do Bruno.")
    else:
        print(f"  [ok] Altura     : {local_height}")
        print(f"       Tip hash   : {local_tip[:48]}...")
        print(f"       Work       : {local_work:,}")
        print(f"       Difficulty : {diff}")
except Exception as e:
    print(f"  [X] Erro chain: {e}")

# ============================================================
# 7. Validadores + quorum
# ============================================================
print(f"\n[7] VALIDADORES + QUORUM")

validators = []
required = 0
if chain is not None:
    try:
        from trust_network import TrustNetwork
        trust = TrustNetwork("trust_network.json")
        chain.set_trust_net(trust)
        if sk:
            chain.set_local_validator_sk(sk)
        validators = chain._get_active_validators()
        required = chain._required_signatures(validators)
        print(f"  Validadores ATIVOS: {len(validators)}")
        for v in validators:
            mark = " <- LOCAL" if sk and v == local_pub else ""
            print(f"    {v}  ({nome(v)}){mark}")
        print(f"  Required: {required}/{len(validators)}")
        if sk and local_pub not in validators:
            print(f"  [X] SUA PUBKEY NAO ESTA EM _get_active_validators()!")
    except Exception as e:
        print(f"  [X] Erro: {e}")

# ============================================================
# 8. Assinatura local (simulada)
# ============================================================
print(f"\n[8] ASSINATURA LOCAL (bloco ficticio)")

if sk and chain is not None and local_height >= 0:
    try:
        from blockchain import sign_block_as_validator, verify_block_signature
        from p2p_unified import P2PServer, Metrics

        fake = {
            "height": local_height + 1,
            "hash": "a" * 64,
            "prev_hash": local_tip,
            "timestamp": int(time.time()),
            "nonce": 0,
            "merkle": "b" * 64,
            "difficulty": chain.current_difficulty(),
            "transactions": [],
            "validators_sig": [],
        }
        metrics = Metrics()
        srv = P2PServer(chain, 6001, metrics)
        srv.node_uuid = "test_full"
        resp = srv._handle_sign_block({"block": fake}, addr=("127.0.0.1", 12345), peer_id="test")
        pk = (resp.get("pubkey") or "").strip()
        sig = (resp.get("signature") or "").strip()
        err = resp.get("error", "")
        if pk and sig:
            ok_crypto = verify_block_signature(pk, fake, sig)
            print(f"  [ok] Assinatura OK (crypto={ok_crypto})")
            print(f"       pubkey: {pk[:48]}...")
        else:
            print(f"  [!] Recusou: {err or '(sem motivo)'}")
            if "header_hash_diverge" in err:
                print(f"      → ESPERADO: bloco ficticio tem hash 'a'*64")
                print(f"        Isso prova que o peer PASSOU por todos os checks")
                print(f"        anteriores e so rejeitou no hash do header.")
    except Exception as e:
        print(f"  [X] Erro: {e}")
else:
    print("  [ ] Pulado (sem wallet ou chain vazia)")

# ============================================================
# 9. Ultimos 20 blocos
# ============================================================
print(f"\n[9] ULTIMOS 20 BLOCOS")

if chain is not None and local_height >= 0:
    try:
        N = 20
        start = max(0, local_height - N + 1)
        blocks = chain.db.get_blocks_range(start, local_height + 1)
        blocks.sort(key=lambda b: b["height"], reverse=True)

        print(f"  {'BLK':<6} {'MINEROU':<14} {'SIGS':<6} SIGNERS")
        print("  " + SUB[:-4])

        dist = {}
        for b in blocks[:20]:
            h = b["height"]
            sigs = b.get("validators_sig", []) or []
            n = len(sigs)
            dist[n] = dist.get(n, 0) + 1
            miner = nome((sigs[0].get("pubkey", "") if sigs else ""))
            signers = ", ".join(nome(s.get("pubkey", "")) for s in sigs) or "(sem sig)"
            print(f"  #{h:<5} {miner:<14} {n:<6} {signers}")

        print("  " + SUB[:-4])
        print(f"  Distribuicao: {dict(sorted(dist.items()))}")
        if dist.get(2, 0) + dist.get(3, 0) > 0:
            print(f"  [ok] Multi-sig: {dist.get(2, 0) + dist.get(3, 0)} bloco(s) com 2+ sigs")
        else:
            print(f"  [X] Nenhum bloco com 2+ sigs — co-assinatura falhando")
    except Exception as e:
        print(f"  [X] Erro: {e}")
else:
    print("  [ ] Pulado (chain vazia)")

# ============================================================
# 10. Peers via TCP
# ============================================================
print(f"\n[10] PEERS (TCP 6001)")

NETWORK_MAGIC = b"BRN5"
CHAIN_ID = os.environ.get("BRN_CHAIN_ID", "mainnet").strip()

def send_tcp(ip, port, msg, timeout=6.0):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        msg = dict(msg)
        msg["_chain"] = CHAIN_ID
        msg["_port"] = int(os.environ.get("BRN_P2P_PORT", "6001"))
        s.sendall(NETWORK_MAGIC + json.dumps(msg).encode())
        raw = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            raw += chunk
            if raw.startswith(NETWORK_MAGIC):
                try:
                    json.loads(raw[len(NETWORK_MAGIC):].decode())
                    break
                except Exception:
                    continue
        s.close()
        if raw.startswith(NETWORK_MAGIC):
            return json.loads(raw[len(NETWORK_MAGIC):].decode())
    except Exception as e:
        return {"_error": str(e)[:60]}
    return {"_error": "sem resposta"}

# Pega peers do bootstrap
peers = set()
for src in ("bootstrap_peers.json", "peers_discovered.json"):
    if os.path.exists(src):
        try:
            d = json.load(open(src))
            if isinstance(d, list):
                for p in d:
                    if isinstance(p, str) and ":" in p:
                        peers.add(p.strip())
        except Exception:
            pass
for p in os.environ.get("BRN_BOOTSTRAP_PEERS", "").split(","):
    p = p.strip()
    if p and ":" in p:
        peers.add(p)

# Remove self
if os.environ.get("BRN_SEU_ENDERECO"):
    peers.discard(os.environ["BRN_SEU_ENDERECO"])

peer_results = []
if not peers:
    print("  [ ] Nenhum peer no bootstrap_peers.json")
else:
    print(f"  Testando {len(peers)} peer(s):")
    for p in sorted(peers):
        try:
            ip, port_s = p.rsplit(":", 1)
            port = int(port_s)
        except Exception:
            continue
        print(f"  --- {p} ---")
        r = send_tcp(ip, port, {"type": "get_chain_height"})
        if not r or r.get("_error"):
            print(f"    [X] offline: {(r or {}).get('_error', '?')}")
            peer_results.append({"peer": p, "ok": False})
            continue
        peer_h = r.get("height", -1)
        peer_tip = r.get("hash", "")
        peer_work = r.get("work", 0)
        match = "SAME" if peer_tip == local_tip else "DIFF"
        if peer_h == local_height and match == "SAME":
            status = f"[ok] h={peer_h} SAME"
        elif peer_h > local_height:
            status = f"[!] h={peer_h} (voce esta {peer_h - local_height} atras)"
        elif peer_h < local_height:
            status = f"[!] h={peer_h} (peer esta {local_height - peer_h} atras)"
        else:
            status = f"[X] h={peer_h} DIFF (FORK)"
        print(f"    {status}")
        print(f"    tip: {peer_tip[:48]}...")
        peer_results.append({
            "peer": p, "ok": True,
            "h": peer_h, "tip": peer_tip, "work": peer_work,
            "match": match,
        })

# ============================================================
# 11. Simula sign_block nos peers
# ============================================================
print(f"\n[11] SIGN_BLOCK REMOTO (simulado)")

if not peer_results or local_height < 0:
    print("  [ ] Pulado (sem peers ou chain vazia)")
else:
    fake = {
        "height": local_height + 1,
        "hash": "a" * 64,
        "prev_hash": local_tip,
        "timestamp": int(time.time()),
        "nonce": 0,
        "merkle": "b" * 64,
        "difficulty": (chain.current_difficulty() if chain else 5),
        "transactions": [],
        "validators_sig": [],
    }
    for pr in peer_results:
        if not pr.get("ok"):
            continue
        peer = pr["peer"]
        ip, port_s = peer.rsplit(":", 1)
        port = int(port_s)
        r = send_tcp(ip, port, {"type": "sign_block", "block": fake})
        if not r or r.get("_error"):
            print(f"  {peer:<24} sem resposta ({(r or {}).get('_error', '?')})")
            continue
        pk = (r.get("pubkey") or "").strip()
        sig = (r.get("signature") or "").strip()
        err = r.get("error", "")
        if pk and sig:
            print(f"  {peer:<24} [ok] assinou pk={nome(pk)}")
        else:
            print(f"  {peer:<24} recusou: {err or '(sem motivo)'}")

# ============================================================
# 12. Debug: confirma patch auth_tag no blockchain.py
# ============================================================
print(f"\n[12] DEBUG: auth_tag no blockchain.py")

if os.path.exists("blockchain.py"):
    txt = open("blockchain.py", encoding="utf-8", errors="ignore").read()
    has_patch = "if require_auth_tag is None:" in txt
    has_env = 'os.environ.get("BRN_REQUIRE_AUTH_TAG"' in txt
    has_skip = "BRN_BOOTSTRAP_SKIP_SIGS_UNTIL" in txt
    print(f"  Patch auth_tag     : {'SIM' if has_patch else 'NAO'}")
    print(f"  Le BRN_REQUIRE_AUTH_TAG: {'SIM' if has_env else 'NAO'}")
    print(f"  Le BRN_BOOTSTRAP_SKIP  : {'SIM' if has_skip else 'NAO'}")
    if not has_patch:
        print("  [X] Falta o patch do auth_tag. Cole no blockchain.py:")
        print("      if require_auth_tag is None:")
        print("          require_auth_tag = os.environ.get('BRN_REQUIRE_AUTH_TAG', '1') == '1'")
        print("      self.require_auth_tag = bool(require_auth_tag) and bool(self.network_secret)")

# ============================================================
# 13. Veredito final
# ============================================================
print(f"\n{SEP}")
print(" VEREDITO FINAL")
print(SEP)

problemas = []
avisos = []

# .env
if not os.environ.get("BRN_NETWORK_SECRET"):
    problemas.append("BRN_NETWORK_SECRET ausente")
if not os.environ.get("BRN_NODE_PASSWORD"):
    problemas.append("BRN_NODE_PASSWORD ausente")
if not os.environ.get("BRN_WALLET_SESSION_PASS"):
    avisos.append("BRN_WALLET_SESSION_PASS ausente (usa NODE_PASSWORD)")

# Versoes
if os.path.exists("p2p_unified.py"):
    txt = open("p2p_unified.py", encoding="utf-8", errors="ignore").read(2000)
    if "v6.7.7" not in txt:
        problemas.append("p2p_unified.py NAO e v6.7.7 (sem race recovery)")
if os.path.exists("blockchain.py"):
    txt = open("blockchain.py", encoding="utf-8", errors="ignore").read(3000)
    if "9.4.10" not in txt:
        problemas.append("blockchain.py NAO e v9.4.10 (sem skip/race)")

# Trust
if trust_count < 3:
    problemas.append(f"trust tem {trust_count} nos (devia ser 3)")
elif trust_count > 3:
    avisos.append(f"trust tem {trust_count} nos (limpar para 3)")

# Wallet
if not local_pub:
    problemas.append("wallet local nao carregou")
elif local_pub not in trust_pubkeys:
    problemas.append(f"sua pubkey {nome(local_pub)} NAO esta no trust")

# Chain
if local_height < 0:
    problemas.append("chain vazia — sincronizar do Bruno")

# Validators
if len(validators) < 2:
    avisos.append(f"so {len(validators)} validador ativo — co-sign impossivel")

# Co-assinatura recente
if chain is not None and local_height >= 0:
    try:
        recent = chain.db.get_blocks_range(max(0, local_height - 9), local_height + 1)
        com_2 = sum(1 for b in recent if len(b.get("validators_sig", []) or []) >= 2)
        if com_2 == 0:
            avisos.append(f"ultimos 10 blocos: NENHUM com 2+ sigs — co-sign falhando")
        else:
            print(f"  [ok] Co-assinatura: {com_2}/10 blocos recentes com 2+ sigs")
    except Exception:
        pass

if problemas:
    print()
    for p in problemas:
        print(f"  [X] {p}")
    if avisos:
        print()
        for a in avisos:
            print(f"  [!] {a}")
    print()
    print(" STATUS: FALHA — corrija os [X] acima")
    sys.exit(1)
elif avisos:
    print()
    for a in avisos:
        print(f"  [!] {a}")
    print()
    print(" STATUS: OK com avisos (nao bloqueiam consenso)")
    sys.exit(0)
else:
    print()
    print(" STATUS: TUDO OK — consenso e co-assinatura funcionando")
    print(f"   Altura: {local_height}")
    print(f"   Trust : {trust_count} nos")
    print(f"   Quorum: {required}/{len(validators)}")
    sys.exit(0)

try:
    if chain is not None:
        chain.db.close()
except Exception:
    pass
