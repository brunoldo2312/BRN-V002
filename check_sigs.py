import urllib.request, json

try:
    url = 'http://127.0.0.1:5000/api/blocks?start=0&limit=500'
    d = json.loads(urllib.request.urlopen(url, timeout=5).read())
    blocks = d.get('blocks', [])

    if not blocks:
        print("Nenhum bloco retornado.")
        raise SystemExit(1)

    ultimo = max(blocks, key=lambda b: b.get('height', 0))
    h = ultimo.get('height')
    hash_ = ultimo.get('hash', '')
    sigs = ultimo.get('validators_sig', []) or []

    print("=" * 60)
    print(f"ULTIMO BLOCO: #{h}")
    print("=" * 60)
    print(f"Hash       : {hash_}")
    print(f"Prev hash  : {ultimo.get('prev_hash','')[:64]}")
    print(f"Timestamp  : {ultimo.get('timestamp')}")
    print(f"Difficulty : {ultimo.get('difficulty')}")
    print(f"Nonce      : {ultimo.get('nonce')}")
    print(f"Txs        : {len(ultimo.get('transactions', []))}")
    print()
    print(f"ASSINATURAS: {len(sigs)}")
    print("-" * 60)

    if not sigs:
        print("  (nenhuma - bloco aceito em BOOTSTRAP ou genesis)")
    else:
        for i, s in enumerate(sigs, 1):
            pk = s.get('pubkey', '')
            sig = s.get('signature', '')
            print(f"  [{i}] pubkey : {pk}")
            print(f"      sig    : {sig[:64]}...")
            print()

    # Quorum info
    try:
        info = json.loads(urllib.request.urlopen(
            'http://127.0.0.1:5000/api/consensus/validators', timeout=5).read())
        print(f"VALIDATORS ATIVOS: {len(info.get('validators', []))}")
        print(f"REQUIRED_SIGS    : {info.get('required_sigs', '?')}")
    except Exception:
        pass

    print("=" * 60)
    if len(sigs) >= 2:
        print(" STATUS: MULTI-SIG REAL (quorum atingido)")
    elif len(sigs) == 1:
        print(" STATUS: BOOTSTRAP LOCAL (1 assinante)")
    else:
        print(" STATUS: SEM ASSINATURA")
    print("=" * 60)

except Exception as e:
    print(f"ERRO: {e}")
