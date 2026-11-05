import urllib.request, json

LOCAL_API = "http://127.0.0.1:5000"

def get_json(path):
    try:
        with urllib.request.urlopen(LOCAL_API + path, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

print("=" * 60)
print(" VERIFICADOR DE ASSINATURAS E QUÓRUM")
print("=" * 60)

# 1. Pega os validadores ativos e o quórum exigido
val_info = get_json('/api/consensus/validators')
required_sigs = 0
if "error" not in val_info:
    required_sigs = val_info.get('required_sigs', 0)
    validators = val_info.get('validators', [])
    print(f"Validadores Ativos : {len(validators)}")
    print(f"Assinaturas Exigidas (Quórum): {required_sigs}")
else:
    print(f"Erro ao consultar validadores: {val_info['error']}")

# 2. Pega o ÚLTIMO bloco minerado (usando /api/latest)
latest_blocks = get_json('/api/latest')
if "error" in latest_blocks or not isinstance(latest_blocks, list) or not latest_blocks:
    print(f"Erro ao consultar último bloco: {latest_blocks.get('error', 'vazio')}")
    exit()

ultimo = latest_blocks[0]  # O /api/latest já retorna do mais novo pro mais antigo
h = ultimo.get('height')
hash_ = ultimo.get('hash', '')
sigs = ultimo.get('validators_sig', []) or []
pbft = ultimo.get('pbft_commits', []) or []

print("-" * 60)
print(f"ÚLTIMO BLOCO   : #{h}")
print(f"Hash           : {hash_[:32]}...")
print(f"Prev Hash      : {ultimo.get('prev_hash','')[:32]}...")
print(f"Timestamp      : {ultimo.get('timestamp')}")
print(f"Dificuldade    : {ultimo.get('difficulty')}")
print(f"Transações     : {len(ultimo.get('transactions', []))}")
print("-" * 60)

# 3. Mostra as assinaturas
print(f"ASSINATURAS (validators_sig): {len(sigs)}")
if not sigs:
    print("  (nenhuma)")
else:
    for i, s in enumerate(sigs, 1):
        pk = s.get('pubkey', '')
        print(f"  [{i}] pubkey : {pk[:48]}...")

# 4. Mostra os commits do PBFT
print(f"COMMITS (PBFT): {len(pbft)}")
if pbft:
    for i, s in enumerate(pbft, 1):
        pk = s.get('pubkey', '')
        print(f"  [{i}] pubkey : {pk[:48]}...")

# 5. Veredito
print("=" * 60)
if len(sigs) == 0:
    print(" STATUS: FALHOU (Sem assinaturas)")
elif len(sigs) >= required_sigs and required_sigs > 0:
    print(f" STATUS: OK (Multi-sig atingiu o quórum de {required_sigs})")
else:
    print(f" STATUS: FALHOU (Tem {len(sigs)} assinaturas, mas precisa de {required_sigs})")

if len(pbft) > 0:
    print(" PBFT: ATIVO (O bloco tem prova de consenso bizantino)")
else:
    print(" PBFT: Vazio (Pode ser um bloco antigo ou PBFT desligado)")
print("=" * 60)
