#!/usr/bin/env python3
"""
fix_wallet.py — Garante que a carteira local exista e abra corretamente.
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# Carrega .env
if os.path.exists("brn_network.env"):
    for line in open("brn_network.env", encoding="utf-8", errors="ignore").read().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def main():
    print("=" * 60)
    print(" 🛠️  REPARADOR DE CARTEIRA BRN")
    print("=" * 60)

    from secure_store import load_current_wallet, save_current_wallet
    from wallet import Wallet

    pwd = os.environ.get("BRN_WALLET_SESSION_PASS") or os.environ.get("BRN_NODE_PASSWORD")
    
    if not pwd:
        print("[X] ERRO: Você não tem senha definida no brn_network.env (BRN_NODE_PASSWORD).")
        print("    Adicione a senha e rode este script novamente.")
        return

    print(f"[*] Tentando abrir carteira com a senha do .env...")
    
    w_data = None
    try:
        w_data = load_current_wallet(pwd)
    except Exception as e:
        pass

    if w_data and w_data.get("ok"):
        print("\n✅ A carteira JÁ EXISTE e abriu com sucesso!")
        print(f"   Endereço : {w_data.get('address')}")
        print(f"   Pubkey   : {w_data.get('public_key')}")
        print("\n   Você já pode iniciar o main.py e minerar.")
        return

    print("\n❌ A carteira não existe ou a senha está errada.")
    print("   Vamos criar uma NOVA carteira com a senha do .env.")
    
    resp = input("   Deseja criar agora? (s/N): ").strip().lower()
    if resp != 's':
        print("   Cancelado.")
        return

    try:
        w = Wallet()
        dados = {
            "address": w.address,
            "private_key": w.priv_hex,
            "public_key": w.pub_hex
        }
        r = save_current_wallet(pwd, dados)
        if r.get("ok"):
            print("\n✅ NOVA CARTEIRA CRIADA COM SUCESSO!")
            print(f"   Endereço : {w.address}")
            print(f"   Pubkey   : {w.pub_hex}")
            print(f"   Arquivo  : {r.get('path')}")
            print("\n   Agora ligue o main.py novamente e conecte ao Pool.")
        else:
            print(f"\n[X] Erro ao salvar: {r.get('msg')}")
    except Exception as e:
        print(f"\n[X] Falha ao criar carteira: {e}")

if __name__ == "__main__":
    main()
