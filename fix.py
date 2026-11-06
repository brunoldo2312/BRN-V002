#!/usr/bin/env python3
"""
fix_wallets.py — Verifica e conserta carteiras com senha incorreta.
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# Carrega .env
if os.path.exists("brn_network.env"):
    for line in open("brn_network.env", encoding="utf-8").read().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def main():
    print("=" * 60)
    print(" 🛠️  AUTO-FIX DE CARTEIRAS BRN")
    print("=" * 60)

    pwd = os.environ.get("BRN_NODE_PASSWORD", "").strip() or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
    if not pwd:
        print("[X] ERRO: Nenhuma senha configurada no .env (BRN_NODE_PASSWORD).")
        print("    Coloque a senha no .env e rode novamente.")
        return

    print(f"[*] Usando a senha do .env: {'*' * len(pwd)}")

    try:
        from secure_store import load_current_wallet, save_current_wallet
        from wallet import Wallet
    except ImportError as e:
        print(f"[X] Erro ao importar módulos: {e}")
        return

    # 1. Verifica a carteira atual
    print("\n[1] Testando carteira atual...")
    w = load_current_wallet(pwd)
    
    if w and w.get("ok"):
        print("    \033[92m[OK] A carteira abriu perfeitamente! Nada a fazer.\033[0m")
        print(f"    Endereço: {w.get('address')}")
        print(f"    Pubkey:   {w.get('public_key') or w.get('pub_hex')}")
        return

    # 2. Se falhou, recria a carteira
    print("    \033[91m[X] Falha ao abrir. (Senha incorreta ou arquivo não existe).\033[0m")
    print("    -> Apagando carteira antiga e criando uma nova...")

    # Apaga os arquivos velhos
    for p in ["current_wallet.enc", "current_wallet.json", "wallets/current_wallet.enc"]:
        f = Path(p)
        if f.exists():
            try:
                f.unlink()
                print(f"       - Removido: {p}")
            except Exception as e:
                print(f"       - Erro ao remover {p}: {e}")

    # Cria a nova carteira
    try:
        new_wallet = Wallet()
        data = {
            "address": new_wallet.address,
            "private_key": new_wallet.priv_hex,
            "public_key": new_wallet.pub_hex
        }
        
        r = save_current_wallet(pwd, data)
        
        if r.get("ok"):
            print("\n    \033[92m[OK] NOVA CARTEIRA CRIADA COM SUCESSO!\033[0m")
            print(f"    Endereço: {new_wallet.address}")
            print(f"    Pubkey:   {new_wallet.pub_hex}")
            print("\n    \033[93m⚠️ ATENÇÃO: Vá na máquina Origem (Ubuntu) e rode 'python3 auto.py'\033[0m")
            print("    \033[93mpara atualizar a Trust Network com esta nova Pubkey.\033[0m")
        else:
            print(f"\n    [X] Erro ao salvar nova carteira: {r.get('msg')}")
    except Exception as e:
        print(f"\n    [X] Erro ao gerar nova carteira: {e}")

if __name__ == "__main__":
    main()