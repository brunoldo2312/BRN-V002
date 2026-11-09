import os
import json
import urllib.request
import urllib.error
from pathlib import Path

# Tenta carregar o .env
ENV_FILE = Path("brn_network.env")
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Pega o IP da Origem do .env, ou usa o padrão
ORIGIN_URL = os.environ.get("BRN_POOL_URL", "http://100.111.253.8:5000").rstrip("/")

def get_my_pubkey():
    try:
        from secure_store import load_current_wallet
        pwd = os.environ.get("BRN_WALLET_SESSION_PASS") or os.environ.get("BRN_NODE_PASSWORD")
        if not pwd:
            print("[X] Sem senha no .env para abrir a carteira.")
            return None
        w = load_current_wallet(pwd)
        if w and w.get("ok"):
            return (w.get("public_key") or w.get("pub_hex")).lower()
    except Exception as e:
        print(f"[X] Erro ao ler carteira local: {e}")
    return None

def get_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brn-check/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return None

def main():
    print("=" * 60)
    print(" 🌐 VERIFICADOR AUTOMÁTICO DE REDE (MAYRA -> ORIGEM)")
    print("=" * 60)

    minha_pk = get_my_pubkey()
    if not minha_pk:
        print("[!] Não consegui obter sua chave pública local.")
        return

    print(f"Sua Pubkey : {minha_pk[:16]}...")
    print(f"Origem     : {ORIGIN_URL}")
    print("-" * 60)

    # 1. Testa se a Origem está online
    status = get_json(f"{ORIGIN_URL}/api/status")
    if not status:
        print("[X] Origem OFFLINE. Não consegui conectar no Bruno.")
        print("    Verifique se o main.py dele está rodando e se o Tailscale está conectado.")
        return
    
    print(f"[OK] Origem ONLINE (Altura atual: {status.get('height', '?')})")

    # 2. Pergunta a Origem quais validadores ela aceita
    print("\n▶ Consultando lista de validadores na Origem...")
    
    # Tenta o endpoint de consenso
    cons = get_json(f"{ORIGIN_URL}/api/consensus/validators")
    validadores = []
    
    if cons and cons.get("validators"):
        validadores = [v.get("pubkey", "").lower() for v in cons["validators"]]
    else:
        # Fallback para o endpoint de trust nodes
        trust = get_json(f"{ORIGIN_URL}/api/trust/nodes?limit=50")
        if trust and trust.get("nodes"):
            validadores = [n.get("public_key", "").lower() for n in trust["nodes"]]
    
    if not validadores:
        print("[!] Origem não retornou nenhum validador (pode estar em modo bootstrap).")
        return

    # 3. Verifica se a Mayra está na lista
    print(f"Validadores na Origem: {len(validadores)}")
    for v in validadores:
        marker = "  ← VOCÊ!" if v == minha_pk else ""
        print(f"  - {v[:16]}...{marker}")

    print("\n" + "=" * 60)
    if minha_pk in validadores:
        print(" ✅ SUCESSO! A Origem (Bruno) ACEITOU sua chave.")
        print("    Agora a rede vai exigir sua assinatura nos próximos blocos.")
        if status.get('height') != 4193: # Você pode mudar isso para dinâmico depois
            print("    Sua altura pode estar atrasada, reinicie seu main.py para sincronizar.")
    else:
        print(" ❌ FALHA: A Origem NÃO TEM SUA CHAVE.")
        print("    O Bruno precisa rodar o script 'aplicar_correcao.py' na máquina dele")
        print("    e reiniciar o main.py. Depois, desbloqueie sua carteira na GUI novamente.")
    print("=" * 60)

if __name__ == "__main__":
    main()
