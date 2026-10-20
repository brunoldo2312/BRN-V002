"""verify.py — Verificação completa do ambiente BRN
================================================================
Verifica sem modificar nada:
  1. Arquivos essenciais no disco (existência + tamanho mínimo)
  2. Importação de módulos e símbolos públicos esperados
  3. Assinaturas de funções críticas (compat entre módulos)
  4. Blockchain aceita `auto_genesis`
  5. Schema do DB SQLite (mempool, peers, transactions, etc.)
  6. Variáveis de ambiente essenciais
  7. Estrutura de pastas (contracts/, wallets/)

Uso:
    python3 verify.py
    python3 verify.py --db brn_v2_chain.db
    python3 verify.py --strict
    python3 verify.py --no-db

Exit code:
    0 = tudo OK (avisos opcionais)
    1 = há erros bloqueantes
================================================================
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import os
import sqlite3
import sys
from pathlib import Path


BASE = Path(__file__).resolve().parent


# ============================================================
# 1) ARQUIVOS ESPERADOS
# ============================================================
FILES = [
    # Núcleo
    ("crypto.py",               1500),
    ("bech32.py",               1000),
    ("secure_store.py",         3000),
    ("wallet.py",               4000),
    ("blockchain.py",           5000),
    ("db.py",                   4000),
    ("chain_validator.py",      2000),
    ("checkpoints.py",          1500),

    # P2P
    ("p2p_auth.py",             4000),
    ("p2p_cipher.py",           4000),
    ("p2p_secure.py",           2000),
    ("p2p_unified.py",          5000),
    ("discovery_v2.py",         3000),
    ("rendezvous_mongo.py",     4000),
    ("rendezvous_discovery.py", 1500),
    ("p2p_relay.py",            5000),
    ("sync_manager.py",         5000),

    # L2 / Contratos
    ("l2_manager.py",           4000),
    ("btc_config.py",           1000),
    ("btc_watcher.py",          3000),

    # Serviços
    ("server.py",               4000),
    ("explorer.py",             3000),
    ("miner_loop.py",           3000),
    ("brn_config.py",           2000),
    ("brn_logger.py",           1000),
    ("security.py",             1000),
    ("mongo_client.py",         1500),

    # Entrypoint / Metadata
    ("main.py",                 8000),
    ("version.py",              100),
]


# ============================================================
# 2) MÓDULOS E SÍMBOLOS ESPERADOS
# ============================================================
# módulo: (símbolos obrigatórios, símbolos opcionais)
MODULES = {
    "crypto": (
        ["Ed25519PrivateKey", "Ed25519PublicKey", "sha256", "double_sha256",
         "hash160", "generate_private_key", "pubkey_from_priv",
         "pubkey_to_address", "sign_ecdsa", "verify_ecdsa"],
        ["sign_schnorr", "verify_schnorr"],
    ),
    "bech32": (
        ["address_from_pubkey", "validate_address",
         "bech32_encode", "bech32_decode"],
        [],
    ),
    "secure_store": (
        ["encrypt_blob", "decrypt_blob",
         "save_wallet", "load_wallet",
         "save_current_wallet", "load_current_wallet"],
        ["delete_identity", "WalletSession"],
    ),
    "wallet": (
        ["Wallet", "WalletManager", "HDWalletManager"],
        ["sign_transaction", "verify_transaction"],
    ),
    "blockchain": (
        ["Blockchain", "txid", "signing_hash", "make_coinbase",
         "compute_merkle_root", "block_hash", "meets_difficulty"],
        ["work_from_difficulty", "set_checkpoints", "set_checkpoint_priv",
         "AUTH_TAG_START_HEIGHT", "verify_auth_tag", "compute_auth_tag"],
    ),
    "db": (
        ["ChainDB"],
        [],
    ),
    "chain_validator": (
        ["verify_chain", "verify_chain_dict"],
        ["VerifyResult", "validate_block", "verify_range",
         "find_fork_point", "_verify_tx_signature"],
    ),
    "checkpoints": (
        ["load_checkpoints", "sign_checkpoint", "verify_checkpoint",
         "save_checkpoints"],
        ["merge_checkpoints", "merge_from_file"],
    ),
    "p2p_auth": (
        ["auth_enabled", "auth_required", "build_auth", "verify_auth",
         "set_node_id_priv", "reload_config"],
        ["status", "info"],
    ),
    "p2p_cipher": (
        ["wrap_client", "wrap_server", "CipherSocket", "cipher_enabled"],
        ["info"],
    ),
    "p2p_secure": (
        ["handshake_client", "handshake_server", "SecureConn"],
        ["info"],
    ),
    "p2p_unified": (
        ["P2PManager", "P2PServer", "P2PClient"],
        ["PeerDiscovery"],
    ),
    "discovery_v2": (
        ["UDPDiscovery", "get_primary_ip", "get_all_local_ips",
         "same_subnet", "TOKEN"],
        ["MDNSDiscovery", "PeerLiveness"],
    ),
    "rendezvous_mongo": (
        ["RendezvousMongo"],
        [],
    ),
    "rendezvous_discovery": (
        ["RendezvousDiscovery"],
        [],
    ),
    "p2p_relay": (
        ["RelayManager"],
        [],
    ),
    "sync_manager": (
        ["ChainSyncManager"],
        [],
    ),
    "l2_manager": (
        ["L2Manager", "L2Error"],
        ["sats_to_coin", "coin_to_sats"],
    ),
    "btc_config": (
        ["BTC_RECEIVE_ADDRESS", "BTC_NETWORK", "BRN_PER_BTC",
         "FEE_PERCENT", "BTC_MIN_CONFIRMATIONS", "MIN_BTC_SATS",
         "MAX_BTC_SATS"],
        ["validate", "info"],
    ),
    "btc_watcher": (
        ["BTCWatcher"],
        ["get_watcher"],
    ),
    "server": (
        ["app", "set_chain", "auto_registrar_carteira_atual"],
        ["set_sync_manager"],
    ),
    "explorer": (
        ["app"],
        [],
    ),
    "miner_loop": (
        ["get_miner", "Miner"],
        ["loop_mineracao", "iniciar_mineracao"],
    ),
    "brn_config": (
        ["Config"],
        ["DEFAULTS"],
    ),
    "brn_logger": (
        ["setup_logger", "get_logger"],
        [],
    ),
    "security": (
        ["require_rate_limit", "audit_log", "close_audit",
         "hash_short", "get_client_ip"],
        ["ensure_self_signed_cert"],
    ),
    "mongo_client": (
        ["mongo"],
        ["Mongo"],
    ),
    "main": (
        ["parse_args", "main"],
        ["load_or_create_node_identity", "_setup_relay", "_setup_sync"],
    ),
    "version": (
        ["VERSION", "BUILD_DATE", "GITHUB_USER", "GITHUB_REPO"],
        [],
    ),
}


# ============================================================
# 3) VARIÁVEIS DE AMBIENTE
# ============================================================
ENV_REQUIRED = [
    ("BRN_NETWORK_SECRET", "segredo da rede — IGUAL em todos os PCs"),
    ("BRN_NODE_PASSWORD",  "senha do nó (cifra identidade + miner wallet)"),
]

ENV_OPTIONAL = [
    ("BRN_WEB_PASS",            "senha da carteira web"),
    ("BRN_WALLET_SESSION_PASS", "senha da carteira ativa (current_wallet)"),
    ("BRN_ADMIN_TOKEN",         "token admin (opcional — protege /api/faucet)"),
    ("BRN_BOOTSTRAP_PEERS",     "peers iniciais (CSV: ip:porta,...)"),
    ("BRN_TRACKER",             "URL do tracker HTTP (opcional)"),
    ("BRN_RENDEZVOUS_URL",      "URL do rendezvous HTTP (opcional)"),
    ("MONGO_PASS",              "senha do MongoDB Atlas (opcional)"),
    ("MONGO_HOST",              "host do MongoDB Atlas (opcional)"),
    ("MONGO_USER",              "usuário do MongoDB Atlas (opcional)"),
    ("MONGO_DB",                "nome do DB Mongo (default brn_analytics)"),
    ("TS_AUTH_KEY",             "chave do Tailscale (opcional)"),
    ("TS_TAILNET",              "nome da tailnet (opcional, só log)"),
]


# ============================================================
# 4) SCHEMA ESPERADO DO DB
# ============================================================
EXPECTED_DB_COLUMNS = {
    "mempool":      {"txid", "raw", "fee", "received_at"},
    "peers":        {"address", "node_id", "genesis_hash", "version",
                     "height", "is_miner", "public_key", "metadata",
                     "first_seen", "last_seen", "score"},
    "transactions": {"txid", "block_height", "raw"},
    "blocks":       {"height", "hash", "prev_hash", "timestamp",
                     "nonce", "merkle", "difficulty", "raw"},
    "utxos":        {"txid", "vout", "address", "amount", "pubkey",
                     "block_height", "spent", "spent_by"},
    "l2_escrows":   {"escrow_id", "creator", "buyer", "brn_amount",
                     "btc_expected_sats", "btc_address", "btc_txid",
                     "status", "created_at", "expires_at", "l2_hash",
                     "raw_json"},
    "btc_used":     {"btc_txid", "escrow_id", "used_at"},
}


# ============================================================
# HELPERS
# ============================================================
class Result:
    def __init__(self):
        self.errors: list = []
        self.warnings: list = []

    def err(self, msg: str):
        self.errors.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


# ============================================================
# 1) ARQUIVOS
# ============================================================
def check_files(r: Result):
    print("\n1) Arquivos no disco")
    print("-" * 64)
    for nome, min_bytes in FILES:
        p = BASE / nome
        if not p.exists():
            print(f"  [X] {nome:<28} NÃO EXISTE")
            r.err(f"arquivo faltando: {nome}")
            continue
        size = p.stat().st_size
        if size < min_bytes:
            print(f"  [!] {nome:<28} {size} bytes (esperado >= {min_bytes})")
            r.warn(f"{nome} tem {size} bytes (minimo {min_bytes})")
        else:
            print(f"  [ok] {nome:<28} {size} bytes")

    # pasta contracts/
    contracts_dir = BASE / "contracts"
    if contracts_dir.is_dir():
        print(f"  [ok] contracts/                    (diretorio existe)")
        for f in ("__init__.py", "vm.py", "escrow_btc_brn.py", "factory.py"):
            if not (contracts_dir / f).exists():
                print(f"  [!] contracts/{f:<20} NÃO EXISTE")
                r.warn(f"contracts/{f} faltando")
    else:
        print(f"  [!] contracts/                    NÃO EXISTE")
        r.warn("pasta contracts/ nao existe")


# ============================================================
# 2) MÓDULOS
# ============================================================
def check_modules(r: Result):
    print("\n2) Importação de módulos e símbolos")
    print("-" * 64)

    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    for modname, (required, optional) in MODULES.items():
        try:
            mod = importlib.import_module(modname)
        except ImportError as e:
            print(f"  [X] {modname:<24} ImportError: {e}")
            r.err(f"{modname}: ImportError {e}")
            continue
        except Exception as e:
            print(f"  [X] {modname:<24} {type(e).__name__}: {e}")
            r.err(f"{modname}: {type(e).__name__} {e}")
            continue

        faltando = [s for s in required if not hasattr(mod, s)]
        if faltando:
            print(f"  [X] {modname:<24} faltam: {faltando}")
            r.err(f"{modname}: simbolos faltando {faltando}")
            continue

        faltando_opt = [s for s in optional if not hasattr(mod, s)]
        if faltando_opt:
            print(f"  [ok] {modname:<24} (opcionais ausentes: {faltando_opt})")
        else:
            print(f"  [ok] {modname:<24} todos os símbolos OK")


# ============================================================
# 3) ASSINATURAS CRÍTICAS
# ============================================================
def check_signatures(r: Result):
    print("\n3) Assinaturas de funções críticas")
    print("-" * 64)

    # secure_store.save_wallet(data, password, path)
    try:
        from secure_store import save_wallet, load_wallet

        params = list(inspect.signature(save_wallet).parameters)
        if params[:2] not in (["data", "password"],
                              ["wallet_data", "password"]):
            print(f"  [!] save_wallet{tuple(params)} — esperado (data, password, path)")
            r.warn(f"save_wallet tem assinatura inesperada: {params}")
        else:
            print(f"  [ok] secure_store.save_wallet{tuple(params)}")

        params = list(inspect.signature(load_wallet).parameters)
        if params[:1] != ["password"]:
            print(f"  [!] load_wallet{tuple(params)} — esperado (password, path)")
            r.warn(f"load_wallet tem assinatura inesperada: {params}")
        else:
            print(f"  [ok] secure_store.load_wallet{tuple(params)}")
    except Exception as e:
        print(f"  [!] secure_store: {e}")
        r.warn(f"nao consegui verificar secure_store: {e}")

    # blockchain.Blockchain.__init__ aceita auto_genesis
    try:
        from blockchain import Blockchain
        params = list(inspect.signature(Blockchain.__init__).parameters)
        if "auto_genesis" not in params:
            print(f"  [X] Blockchain.__init__ NAO tem auto_genesis")
            r.err("Blockchain.__init__ sem auto_genesis")
        else:
            print(f"  [ok] Blockchain.__init__ aceita auto_genesis")
    except Exception as e:
        print(f"  [!] Blockchain: {e}")
        r.warn(f"nao consegui inspecionar Blockchain: {e}")

    # wallet.Wallet.verify estático
    try:
        from wallet import Wallet
        if not hasattr(Wallet, "verify"):
            print(f"  [X] Wallet sem método verify")
            r.err("Wallet.verify faltando")
        else:
            print(f"  [ok] Wallet.verify existe")
    except Exception as e:
        print(f"  [!] Wallet: {e}")
        r.warn(f"Wallet: {e}")

    # server.set_chain
    try:
        import server
        if not hasattr(server, "set_chain"):
            print(f"  [!] server.set_chain faltando")
            r.warn("server.set_chain faltando")
        else:
            print(f"  [ok] server.set_chain existe")
    except Exception as e:
        print(f"  [!] server: {e}")
        r.warn(f"server: {e}")

    # P2PManager aceita (chain, node_id_priv, tcp_port, enable_upnp)
    try:
        from p2p_unified import P2PManager
        params = set(inspect.signature(P2PManager.__init__).parameters)
        required = {"blockchain", "node_id_priv"}
        if not required.issubset(params):
            print(f"  [!] P2PManager.__init__{tuple(sorted(params))}")
            r.warn(f"P2PManager.__init__ tem parametros inesperados")
        else:
            print(f"  [ok] P2PManager.__init__ aceita {sorted(required)}")
    except Exception as e:
        print(f"  [!] P2PManager: {e}")
        r.warn(f"P2PManager: {e}")


# ============================================================
# 4) DB SCHEMA
# ============================================================
def check_db_schema(r: Result, db_path: str):
    print(f"\n4) Schema do DB: {db_path}")
    print("-" * 64)

    if not os.path.exists(db_path):
        print(f"  [!] DB não existe ainda (ok se for primeira execução)")
        r.warn(f"DB {db_path} não existe — primeira execução?")
        return

    try:
        conn = sqlite3.connect(db_path)
    except Exception as e:
        print(f"  [X] nao consegui abrir DB: {e}")
        r.err(f"DB abrir: {e}")
        return

    for table, expected in EXPECTED_DB_COLUMNS.items():
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            if not rows:
                print(f"  [!] {table:<14} TABELA AUSENTE")
                r.warn(f"tabela {table} ausente")
                continue
            cols = {r[1] for r in rows}
            faltando = expected - cols
            if faltando:
                print(f"  [X] {table:<14} faltam: {sorted(faltando)}")
                r.err(f"{table} sem colunas: {sorted(faltando)}")
            else:
                print(f"  [ok] {table:<14} {len(cols)} colunas OK")
        except Exception as e:
            print(f"  [!] {table:<14} erro: {e}")
            r.warn(f"{table}: {e}")

    # altura
    try:
        row = conn.execute("SELECT MAX(height) FROM blocks").fetchone()
        h = row[0] if row else None
        print(f"  [info] altura atual: {h}")
    except Exception:
        pass

    conn.close()


# ============================================================
# 5) ENV
# ============================================================
def check_env(r: Result):
    print("\n5) Variáveis de ambiente")
    print("-" * 64)

    for var, desc in ENV_REQUIRED:
        v = os.environ.get(var, "")
        if not v:
            print(f"  [X] {var:<26} NÃO DEFINIDA — {desc}")
            r.err(f"env obrigatória ausente: {var}")
        else:
            mostra = v if var == "BRN_NETWORK_SECRET" else f"{v[:4]}..."
            print(f"  [ok] {var:<26} = {mostra}")

    for var, desc in ENV_OPTIONAL:
        v = os.environ.get(var, "")
        if not v:
            print(f"  [ ] {var:<26} (opcional — {desc})")
        else:
            print(f"  [ok] {var:<26} definida")


# ============================================================
# 6) RESUMO
# ============================================================
def print_summary(r: Result) -> int:
    print()
    print("=" * 64)
    if r.ok and not r.warnings:
        print("  TUDO OK — pode rodar: python3 main.py --client-mode --headless")
        print("=" * 64)
        return 0
    if r.ok:
        print(f"  OK com {len(r.warnings)} aviso(s):")
        for w in r.warnings[:20]:
            print(f"    - {w}")
        print("=" * 64)
        return 0

    print(f"  {len(r.errors)} erro(s) — corrija antes de rodar")
    for e in r.errors[:20]:
        print(f"    [X] {e}")
    if r.warnings:
        print(f"\n  {len(r.warnings)} aviso(s):")
        for w in r.warnings[:10]:
            print(f"    - {w}")
    print("=" * 64)
    return 1


# ============================================================
# MAIN
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(prog="verify.py")
    ap.add_argument("--db", default="brn_v2_chain.db",
                    help="caminho do DB (default brn_v2_chain.db)")
    ap.add_argument("--strict", action="store_true",
                    help="avisos contam como erro")
    ap.add_argument("--no-db", action="store_true",
                    help="pular checagem do DB")
    args = ap.parse_args()

    print("=" * 64)
    print("  BRN — Verificação completa (arquivos, módulos, DB, env)")
    print(f"  Base: {BASE}")
    print("=" * 64)

    r = Result()

    check_files(r)
    check_modules(r)
    check_signatures(r)

    if not args.no_db:
        check_db_schema(r, args.db)

    check_env(r)

    code = print_summary(r)

    if args.strict and r.ok and r.warnings:
        return 1
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(130)
