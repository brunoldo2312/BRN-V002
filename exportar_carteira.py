"""
exportar_carteira.py — Extrai a chave privada da current_wallet.enc
================================================================
Útil quando a GUI não carrega automaticamente e você precisa
colar a chave na tela de "Importar Carteira".

Uso:
    python exportar_carteira.py
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENV_FILE = BASE / "brn_network.env"


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main():
    env = load_env()
    pw = env.get("BRN_WALLET_SESSION_PASS", "").strip()
    if not pw:
        import getpass
        pw = getpass.getpass("Senha da carteira (BRN_WALLET_SESSION_PASS): ")
    if not pw:
        print("[X] Senha vazia.")
        return 1

    try:
        from secure_store import load_current_wallet
    except Exception as e:
        print(f"[X] secure_store indisponível: {e}")
        return 1

    r = load_current_wallet(pw)
    if not r or not r.get("ok"):
        print(f"[X] Falha ao abrir: {r.get('msg', '?')}")
        print("    Senha errada ou current_wallet.enc corrompido.")
        return 1

    priv = r.get("private_key") or r.get("priv_hex") or ""
    pub = r.get("public_key") or r.get("pub_hex") or r.get("pubkey") or ""
    addr = r.get("address") or ""

    print()
    print("=" * 64)
    print(" CARTEIRA IMPORTÁVEL")
    print("=" * 64)
    print(f" Endereço    : {addr}")
    print(f" Public Key  : {pub}")
    print(f" Private Key : {priv}")
    print("=" * 64)
    print()
    print("Para importar na GUI:")
    print("  1. Abra a carteira (pywebview) ou http://127.0.0.1:5000/")
    print("  2. Vá em 'Importar Carteira' / 'Restaurar'")
    print("  3. Cole a Private Key acima")
    print("  4. Confirme")
    print()
    print("IMPORTANTE: nunca compartilhe a Private Key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())