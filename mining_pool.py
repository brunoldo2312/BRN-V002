"""
minerar_pool.py — Inicia mineração no pool para uma carteira específica.
Roda com: python minerar_pool.py
"""
import json
import urllib.request
import urllib.error

URL = "https://brn-v002.onrender.com/api/pool/start_local"

BODY = {
    "address": "brn1ufzzzwu4ulwx5lygc67lfky5nye7tk2wytfjv0",
    "pubkey":  "0248505dcf910a62775788220315a3ba02dab78f16f3cd83e37c3c16db896b9355",
}

print("=" * 60)
print(" Iniciando mineração no pool")
print("=" * 60)
print(f" URL:     {URL}")
print(f" address: {BODY['address']}")
print(f" pubkey:  {BODY['pubkey'][:32]}...")
print()

req = urllib.request.Request(
    URL,
    data=json.dumps(BODY).encode("utf-8"),
    headers={"Content-Type": "application/json", "User-Agent": "brn-miner/1.0"},
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=60) as r:
        status = r.status
        body = r.read().decode("utf-8")
        print(f"[HTTP {status}]")
        print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
except urllib.error.HTTPError as e:
    print(f"[HTTP {e.code}] erro:")
    try:
        print(json.dumps(json.loads(e.read().decode()), indent=2, ensure_ascii=False))
    except Exception:
        print(e.read().decode(errors="replace")[:500])
except Exception as e:
    print(f"[ERRO] {type(e).__name__}: {e}")
