import os
import json
import urllib.request
import urllib.error
import getpass
from pathlib import Path

# Tenta carregar o .env
ENV_FILE = Path("brn_network.env")
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# IP da Origem (puxa do .env ou usa o padrão)
ORIGIN_URL = os.environ.get("BRN_POOL_URL", "http://100.111.253.8:5000").rstrip("/")

def get_my_pubkey():
    try:
        from secure_store import load_current_wallet
        
        # Pega a senha do ambiente ou pede para o usuário
        pwd = os.environ.get("BRN_WALLET_SESSION_PASS") or os.environ.get("BRN_NODE_PASSWORD")
        if not pwd:
            print("🔒 Para verificar sua chave, preciso abrir sua carteira local.")
            pwd = getpass.getpass("Digite a senha da sua carteira (a mesma da GUI): ")
            
        w = load_current_wallet(pwd)
        if w and w.get("ok"):
            return (w.get("public_key") or w.get("pub_hex")).lower()
        else:
            msg = w.get("msg", "Erro desconhecido") if w else "Carteira não encontrada"
            print(f"[X] Senha incorreta ou carteira não encontrada: {msg}")
    except Exception as e:
        print(f"[X] Erro ao ler carteira local: {e}")
    return None

def get_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brn-check/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"error": "404"}
        return None
    except Exception:
        return None

def main():
    print("=" * 65)
    print(" 🌐 VERIFICADOR AUTOMÁTICO DE REDE (MAYRA -> ORIGEM)")
    print("=" * 65)

    minha_pk = get_my_pubkey()
    if not minha_pk:
        print("[!] Não consegui obter sua chave pública local. Abortando.")
        return

    print(f"\nSua Pubkey : {minha_pk[:16]}...")
    print(f"Origem     : {ORIGIN_URL}")
    print("-" * 65)

    # 1. Testa se a Origem está online
    status = get_json(f"{ORIGIN_URL}/api/status")
    if not status:
        print("[X] Origem OFFLINE. Não consegui conectar no Bruno.")
        print("    Verifique se o main.py dele está rodando e se o Tailscale está conectado.")
        return
    
    if status.get("error") == "404":
        print("[!] Origem respondeu, mas a rota /api/status não existe (versão antiga do server.py).")
    else:
        print(f"[OK] Origem ONLINE (Altura atual: {status.get('height', '?')})")

    print("\n▶ Consultando lista de validadores na Origem (Bruno)...")
    
    validadores = []
    
    # Tenta o endpoint 1: /api/consensus/validators
    cons = get_json(f"{ORIGIN_URL}/api/consensus/validators")
    if cons and isinstance(cons, dict) and cons.get("validators"):
        for v in cons["validators"]:
            pk = v.get("pubkey") or v.get("public_key")
            if pk: validadores.append(pk.lower())
            
    # Tenta o endpoint 2: /api/trust/nodes (fallback)
    if not validadores:
        trust = get_json(f"{ORIGIN_URL}/api/trust/nodes?limit=50")
        if trust and isinstance(trust, dict) and trust.get("nodes"):
            for n in trust["nodes"]:
                pk = n.get("public_key")
                if pk: validadores.append(pk.lower())
    
    if not validadores:
        print("[!] Origem não retornou nenhum validador.")
        print("    (Ela pode estar em modo bootstrap ou as rotas de consulta não existem).")
        return

    # Remove duplicatas mantendo a ordem
    validadores = list(dict.fromkeys(validadores))

    # 3. Verifica se a Mayra está na lista
    print(f"\nValidadores que a Origem conhece: {len(validadores)}")
    for v in validadores:
        marker = "  ← ✅ VOCÊ ESTÁ AQUI!" if v == minha_pk else ""
        print(f"  - {v[:20]}...{marker}")

    print("\n" + "=" * 65)
    if minha_pk in validadores:
        print(" ✅ SUCESSO! A Origem (Bruno) ACEITOU sua chave.")
        print("    O Bruno já pode pedir sua assinatura para os próximos blocos.")
        print("    Mantenha a GUI aberta e minerando para a rede avançar!")
    else:
        print(" ❌ FALHA: A Origem NÃO TEM SUA CHAVE.")
        print("    Para corrigir, peça ao Bruno para rodar no Ubuntu dele:")
        print("       python3 aplicar_correcao.py")
        print("       (reinicie o main.py dele)")
        print("    Depois que ele reiniciar, desbloqueie sua GUI novamente.")
    print("=" * 65)

if __name__ == "__main__":
    main()
