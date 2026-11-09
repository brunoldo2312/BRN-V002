"""
verificar_carteira_gravada.py — Verifica se a GUI gravou a carteira corretamente.

Checa:
  1. current_wallet.enc (raiz)
  2. wallets/current_wallet.enc (backup)
  3. wallets/*.wallet (nomeadas)
  4. user_wallets.json (índice)
  5. /health do servidor (wallet_sk_installed)

Uso:
    python3 verificar_carteira_gravada.py
"""
import os
import json
import urllib.request
from pathlib import Path

BASE = Path.cwd()
os.chdir(BASE)

# Carrega .env
if (BASE / "brn_network.env").exists():
    for line in (BASE / "brn_network.env").read_text(errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

print("=" * 78)
print(" VERIFICAR CARTEIRA GRAVADA PELA GUI")
print("=" * 78)

# ─── 1. Arquivos no disco ───
print("\n[1] Arquivos no disco:")

arquivos = [
    ("current_wallet.enc (raiz)", BASE / "current_wallet.enc"),
    ("wallets/current_wallet.enc", BASE / "wallets" / "current_wallet.enc"),
    ("user_wallets.json", BASE / "user_wallets.json"),
]

for nome, path in arquivos:
    if path.exists():
        size = path.stat().st_size
        print(f"  [OK] {nome:<36} {size} bytes")
    else:
        print(f"  [ -] {nome:<36} NÃO EXISTE")

# wallets/*.wallet
wdir = BASE / "wallets"
if wdir.exists():
    wallets = sorted(wdir.glob("*.wallet"))
    if wallets:
        print(f"\n  Carteiras nomeadas em wallets/:")
        for p in wallets:
            print(f"  [OK] {p.name:<36} {p.stat().st_size} bytes")
    else:
        print(f"\n  [ -] Nenhuma carteira .wallet em wallets/")

# ─── 2. Conteúdo do user_wallets.json ───
print("\n[2] user_wallets.json:")
uw_path = BASE / "user_wallets.json"
if uw_path.exists():
    try:
        uw = json.loads(uw_path.read_text(encoding="utf-8"))
        if isinstance(uw, dict):
            print(f"  {len(uw)} entrada(s):")
            for k, v in list(uw.items())[:5]:
                print(f"    {k[:48]}")
                if isinstance(v, dict):
                    pk = v.get("public_key", "?")
                    print(f"      public_key: {pk[:48]}...")
        else:
            print(f"  [!] formato inesperado: {type(uw).__name__}")
    except Exception as e:
        print(f"  [X] erro: {e}")
else:
    print(f"  [ -] não existe")

# ─── 3. /health do servidor ───
print("\n[3] Estado do nó (/health):")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    req = urllib.request.Request("http://127.0.0.1:5000/health")
    with opener.open(req, timeout=5) as r:
        h = json.loads(r.read().decode())
    print(f"  wallet_sk_installed    : {h.get('wallet_sk_installed')}")
    print(f"  multisig_configured    : {h.get('multisig_configured')}")
    print(f"  height                 : {h.get('height')}")
    print(f"  version                : {h.get('version')}")
except Exception as e:
    print(f"  [X] servidor não responde: {e}")

# ─── 4. Consegue decifrar a .enc? ───
print("\n[4] Decifrar current_wallet.enc com senha do .env:")

pw = (os.environ.get("BRN_PASSWORD", "").strip()
      or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
      or os.environ.get("BRN_NODE_PASSWORD", "").strip())

if not pw:
    print("  [X] Nenhuma senha no .env")
else:
    try:
        from secure_store import load_current_wallet
        r = load_current_wallet(pw)
        if r and r.get("ok"):
            print(f"  [OK] decifrou!")
            print(f"       address: {r.get('address', '?')}")
            pk = r.get("public_key") or r.get("pub_hex", "?")
            print(f"       pubkey:  {pk[:48]}...")
        else:
            msg = r.get("msg") if isinstance(r, dict) else "?"
            print(f"  [ -] não decifrou: {msg}")
    except Exception as e:
        print(f"  [X] {type(e).__name__}: {e}")

# ─── 5. Resumo ───
print("\n" + "=" * 78)
print(" RESUMO")
print("=" * 78)

tem_enc = (BASE / "current_wallet.enc").exists()
tem_sub = (BASE / "wallets" / "current_wallet.enc").exists()
tem_uw = (BASE / "user_wallets.json").exists()

if tem_enc and tem_sub:
    print(" ✓ GUI está gravando corretamente (raiz + wallets/)")
elif tem_enc:
    print(" ⚠ GUI está gravando SÓ em current_wallet.enc (sem backup)")
elif tem_sub:
    print(" ⚠ GUI está gravando SÓ em wallets/ (sem raiz)")
else:
    print(" ✗ GUI NÃO está gravando carteira. Desbloqueie ou crie a carteira.")

if tem_uw:
    print(" ✓ user_wallets.json existe (aba 'arquivos' funciona)")
else:
    print(" ⚠ user_wallets.json não existe (aba 'arquivos' fica vazia)")

print()
