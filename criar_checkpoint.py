#!/usr/bin/env python3
"""
criar_checkpoint.py — Assina um checkpoint para uma altura específica
======================================================================
Uso:
    python3 criar_checkpoint.py               # usa altura 5000 (default)
    python3 criar_checkpoint.py 5758          # altura customizada
    python3 criar_checkpoint.py 5758 --show   # só mostra, não salva

Requer:
    - node_identity.enc na pasta (para carregar a chave Ed25519)
    - BRN_NODE_PASSWORD no ambiente (ou brn_network.env)
    - db.py, checkpoints.py, secure_store.py, crypto.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


# ============================================================
# Bootstrap: carrega brn_network.env ANTES de tudo
# ============================================================
BASE = Path(__file__).resolve().parent


def _bootstrap_env():
    env_file = BASE / "brn_network.env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_bootstrap_env()


# ============================================================
# Imports (após bootstrap)
# ============================================================
try:
    from db import ChainDB
    from checkpoints import sign_checkpoint, load_checkpoints, save_checkpoints
    from secure_store import load_wallet
    from crypto import Ed25519PrivateKey
except ImportError as e:
    print(f"[X] Falha importando módulos: {e}")
    print("    Verifique se está na pasta do projeto e que todos os ficheiros existem.")
    sys.exit(1)


# ============================================================
# Parse args
# ============================================================
ALTURA = 5000
SHOW_ONLY = False

args = sys.argv[1:]
for a in args:
    if a == "--show":
        SHOW_ONLY = True
    elif a.isdigit():
        ALTURA = int(a)

print("=" * 60)
print(f" BRN — Criar Checkpoint para altura #{ALTURA}")
print("=" * 60)
print()

# ============================================================
# 1. Localiza a chave privada do nó
# ============================================================
NODE_ID = BASE / "node_identity.enc"

if not NODE_ID.exists():
    print(f"[X] {NODE_ID.name} não encontrado em {BASE}")
    print("    Execute o nó uma vez para criar a identidade, ou")
    print("    copie o node_identity.enc do nó origem.")
    sys.exit(1)

senha = (
    os.environ.get("BRN_NODE_PASSWORD", "").strip()
    or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
)

if not senha:
    import getpass
    senha = getpass.getpass("Senha do nó (identidade Ed25519): ")

print(f"[*] node_identity.enc encontrado: {NODE_ID}")
print(f"[*] Carregando com senha de {len(senha)} caracteres...")

try:
    data = load_wallet(senha, str(NODE_ID))
    if not data:
        print("[X] Falha ao decifrar node_identity.enc")
        print("    Senha errada ou ficheiro corrompido.")
        sys.exit(1)
except Exception as e:
    print(f"[X] Erro decifrando: {e}")
    sys.exit(1)

if "sk" not in data:
    print("[X] node_identity.enc não contém 'sk' (chave privada)")
    sys.exit(1)

try:
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
    pub_hex = priv.public_key().public_bytes_raw().hex()
except Exception as e:
    print(f"[X] Erro reconstruindo chave Ed25519: {e}")
    sys.exit(1)

print(f"[ok] Chave carregada: pub={pub_hex[:16]}...")
print()

# ============================================================
# 2. Abre o DB e localiza o bloco
# ============================================================
DB_PATH = os.environ.get("BRN_DB", str(BASE / "brn_v2_chain.db"))
print(f"[*] Abrindo DB: {DB_PATH}")

try:
    db = ChainDB(DB_PATH)
except Exception as e:
    print(f"[X] Erro abrindo DB: {e}")
    sys.exit(1)

altura_atual = db.height()
print(f"[ok] DB altura atual: {altura_atual}")
print()

if ALTURA > altura_atual:
    print(f"[X] Altura {ALTURA} é maior que a altura atual ({altura_atual})")
    db.close()
    sys.exit(1)

block = db.get_block(ALTURA)
if not block:
    print(f"[X] Bloco #{ALTURA} não encontrado")
    db.close()
    sys.exit(1)

block_hash = block["hash"]
print(f"[ok] Bloco #{ALTURA} encontrado")
print(f"     Hash: {block_hash}")
print()

# ============================================================
# 3. Assina
# ============================================================
print(f"[*] Assinando checkpoint com Ed25519...")

try:
    cp = sign_checkpoint(ALTURA, block_hash, priv)
except Exception as e:
    print(f"[X] Erro assinando: {e}")
    db.close()
    sys.exit(1)

if not cp:
    print("[X] sign_checkpoint retornou None")
    db.close()
    sys.exit(1)

print(f"[ok] Checkpoint assinado:")
for k, v in cp.items():
    v_str = str(v)
    if len(v_str) > 60:
        v_str = v_str[:60] + "..."
    print(f"     {k}: {v_str}")
print()

# ============================================================
# 4. Salva
# ============================================================
if SHOW_ONLY:
    print("[i] Modo --show: checkpoint NÃO foi salvo")
    db.close()
    sys.exit(0)

# Verifica se já existe
checkpoints = load_checkpoints()
if str(ALTURA) in checkpoints:
    existing_hash = checkpoints[str(ALTURA)].get("hash", "")[:16]
    print(f"[!] Checkpoint #{ALTURA} já existe (hash={existing_hash}...)")
    print(f"    Vai ser substituído.")

checkpoints[str(ALTURA)] = cp

try:
    save_checkpoints(checkpoints)
except Exception as e:
    print(f"[X] Erro salvando checkpoints: {e}")
    db.close()
    sys.exit(1)

print(f"[ok] Checkpoint #{ALTURA} salvo")
print(f"[ok] Total de checkpoints: {len(checkpoints)}")
print()

# ============================================================
# 5. Mostra todos os checkpoints
# ============================================================
print("=" * 60)
print(" Checkpoints registados")
print("=" * 60)
for h in sorted(checkpoints.keys(), key=lambda x: int(x)):
    cp_i = checkpoints[h]
    h_str = str(cp_i.get("hash", ""))[:16] + "..."
    print(f"  #{h:<8} {h_str}")
print()

db.close()
print("[ok] Pronto.")