#!/usr/bin/env python3
"""test_co_sign.py - Verifica se Mayra + Ubuntu estao CO-ASSINANDO blocos reais.

Nao envia nada aos peers. Apenas le os blocos ja aceitos do DB e responde:
  - Quem minerou os ultimos N blocos?
  - Cada bloco tem 1, 2 ou 3 assinaturas?
  - Se Mayra minerou e o bloco dela tem 2+ sigs, o co-sign esta FUNCIONANDO.
  - Se Mayra minerou e so tem 1 sig, o Bruno NAO co-assina (fix nao aplicado nele).
"""
import os, sys, json, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)

# Carrega .env (para BRN_NETWORK_SECRET e senhas)
env_file = os.path.join(BASE, "brn_network.env")
if os.path.exists(env_file):
    with open(env_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f.read().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Pubkeys conhecidas -> nome amigavel (edite se quiser)
KNOWN = {
    "02d3b57df0019f6a93cb70507b814dcad58d6c1b": "Maquina-A",
    "0305817682ff0b7ca51a7bfc836659278c0a4749": "Maquina-B",
    "031be530bf2cbd3709dffb7c1ba5a5a31ab7364c": "Ubuntu-Bruno",
    "039bca3d1f9a936e8f54e9adce7acde99b058964": "Mayra-Windows",
}

def _nome(pk):
    """Retorna nome amigavel para pubkey (ou primeiros 12 chars)."""
    if not pk:
        return "?"
    for full, name in KNOWN.items():
        if pk.startswith(full[:16]):
            return name
    return pk[:16] + "..."

def _get(payload):
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:5000/api/chain-info", timeout=5
        ) as r:
            return json.loads(r.read())
    except Exception as e:
        print("ERRO consultando API: %s" % e)
        print("O no esta rodando? (py main.py)")
        sys.exit(1)

# ------------------------------------------------------------------
# 1. Altura atual
# ------------------------------------------------------------------
info = _get({})
h = int(info.get("height", -1))
if h < 0:
    print("Chain vazia (sem genesis).")
    sys.exit(1)

# ------------------------------------------------------------------
# 2. Pega ultimos 30 blocos
# ------------------------------------------------------------------
N = 30
start = max(0, h - N + 1)
url = "http://127.0.0.1:5000/api/blocks?start=%d&limit=%d" % (start, N)
with urllib.request.urlopen(url, timeout=10) as r:
    data = json.loads(r.read())

blocks = sorted(data.get("blocks", []), key=lambda b: b["height"], reverse=True)

print("=" * 78)
print(" TESTE DE CO-SIGNING: Mayra (Windows) + Ubuntu (Bruno)")
print("=" * 78)
print(" Altura atual: %d" % h)
print(" Analisando os ultimos %d blocos (#%d a #%d)" % (len(blocks), h - len(blocks) + 1, h))
print()

# ------------------------------------------------------------------
# 3. Conta quem minerou e quantas sigs cada bloco tem
# ------------------------------------------------------------------
print(" %-6s %-14s %-38s %s" % ("BLK", "MINEROU", "SIGNERS", "SIGS"))
print(" " + "-" * 76)

mayra_mined = []
bruno_mined = []
outros_mined = []

for b in blocks:
    hh = b["height"]
    sigs = b.get("validators_sig", []) or []
    if not sigs:
        continue
    primeiro = sigs[0].get("pubkey", "")
    minerou_nome = _nome(primeiro)
    signers = ", ".join(_nome(s.get("pubkey", "")) for s in sigs)
    nsig = len(sigs)

    print(" %-6d %-14s %-38s %d" % (hh, minerou_nome, signers, nsig))

    if "Mayra" in minerou_nome:
        mayra_mined.append((hh, nsig))
    elif "Ubuntu" in minerou_nome:
        bruno_mined.append((hh, nsig))
    else:
        outros_mined.append((hh, nsig))

print(" " + "-" * 76)

# ------------------------------------------------------------------
# 4. Resumo
# ------------------------------------------------------------------
print()
print(" RESUMO:")
print("   Blocos minerados pela Mayra: %d" % len(mayra_mined))
print("   Blocos minerados pelo Ubuntu: %d" % len(bruno_mined))
print("   Blocos minerados por outros: %d" % len(outros_mined))
print()

# ------------------------------------------------------------------
# 5. Veredito
# ------------------------------------------------------------------
print("=" * 78)
print(" VEREDITO")
print("=" * 78)

if not mayra_mined and bruno_mined:
    print()
    print(" ⚠ A Mayra NAO minerou nenhum dos ultimos %d blocos." % len(blocks))
    print("   So o Ubuntu esta minerando. Sem bloco da Mayra, nao da")
    print("   para testar se o Ubuntu co-assina o bloco dela.")
    print()
    print("   COMO TESTAR DE VERDADE:")
    print("     1. Pare o main.py da Mayra (Ctrl+C)")
    print("     2. Suba com pool-client: py main.py --pool-client")
    print("     3. Aguarde 2-3 blocos (Mayra minerar)")
    print("     4. Rode este script de novo")
    print()
    print("   Ou, se preferir, ative o PoolClient pela GUI.")

elif mayra_mined:
    ok_2sigs = [(h, n) for h, n in mayra_mined if n >= 2]
    fail_1sig = [(h, n) for h, n in mayra_mined if n < 2]

    if ok_2sigs and not fail_1sig:
        print()
        print(" ✓ CO-SIGN FUNCIONANDO")
        print("   Mayra minerou %d bloco(s), TODOS com 2+ assinaturas." % len(mayra_mined))
        print("   O Ubuntu esta co-assinando os blocos da Mayra.")
        print()
        print("   → Voce pode subir BRN_VALIDATOR_QUORUM=0.67 no .env")
        print("     (required=2/3) e continuar funcionando.")
    elif fail_1sig and not ok_2sigs:
        print()
        print(" ✗ CO-SIGN NAO FUNCIONANDO")
        print("   Mayra minerou %d bloco(s), TODOS com 1 assinatura so." % len(mayra_mined))
        print("   O Ubuntu NAO esta co-assinando (recusa ou nao tem v6.7.7).")
        print()
        print("   → Fix no Ubuntu (via SSH em 100.111.253.8):")
        print("     cd ~/Músicas/BRN-V002-main")
        print("     nano p2p_unified.py   # aplicar v6.7.7 (race recovery)")
        print("     python3 -m py_compile p2p_unified.py")
        print("     python3 main.py")
    else:
        print()
        print(" ⚠ CO-SIGN PARCIAL")
        print("   Mayra minerou %d blocos. %d com 2+ sigs, %d com 1 sig." % (
            len(mayra_mined), len(ok_2sigs), len(fail_1sig)))
        print("   Pode ser race condition (o Ubuntu estava em altura X, o bloco era X+1).")
        print("   Monitore por mais 2-3 blocos antes de decidir.")

else:
    print()
    print(" ⚠ A Mayra nao minerou nenhum dos ultimos %d blocos." % len(blocks))
    print("   Apenas o Ubuntu esta minerando. Nao e possivel testar co-sign ainda.")
    print()
    print("   Para testar: py main.py --pool-client na Mayra")

print("=" * 78)
