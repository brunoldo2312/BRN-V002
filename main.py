"""main.py — Entrypoint unificado do nó BRN (v13.6.10)
================================================================
v13.6.10 (2026-10-25):
  [FIX] Descoberta automática agora respeita o .env (não sobrescreve
        se BRN_BOOTSTRAP_PEERS estiver definido).
  [FIX] Descoberta usa SEMPRE a porta 6001 para o peer remoto.
  [FIX] Descoberta testa a porta P2P antes de escolher o peer.
v13.6.9 (2026-10-24):
  [NEW] Integração com trust_system.
v13.6.8 (2026-10-23):
  [NEW] Migração de Waitress para Uvicorn.
================================================================
"""
from __future__ import annotations

import os
import sys
import signal
import argparse
import threading
import time
import json
import subprocess
import socket
import urllib.request
import urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ============================================================
# CARREGAMENTO DE VARIÁVEIS DE AMBIENTE
# ============================================================
def _bootstrap_env() -> None:
    env_file = BASE_DIR / "brn_network.env"
    if not env_file.exists(): return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ: os.environ[k] = v
    except Exception as e:
        print(f"[boot] aviso: falha lendo {env_file}: {e}")


_HARDCODED_DEFAULTS = {
    "BRN_BOOTSTRAP_PEERS":     "",
    "BRN_IS_ORIGIN":           "0",
    "BRN_HTTPS":               "0",
    "BRN_AUDIT_ENABLED":       "1",
    "BRN_AUDIT_LOG":           "audit.log",
    "BRN_P2P_AUTH":            "optional",
    "BRN_P2P_AUTH_WINDOW":     "30",
    "BRN_P2P_NONCE_CACHE":     "10000",
    "BRN_SKIP_VERIFY":         "0",
    "BRN_VERIFY_STRICT":       "0",
    "BRN_TRACKER":             "",
    "MONGO_USER":              "",
    "MONGO_PASS":              "",
    "MONGO_HOST":              "",
    "MONGO_DB":                "brn_analytics",
    "TS_AUTH_KEY":             "",
    "TS_API_KEY":              "",
    "TS_TAILNET":              "",
    "TS_HOSTNAME_PREFIX":      "brn-node",
    "BRN_POOL_ENABLED":        "1",
    "BRN_POOL_URL_LOCAL":      "http://127.0.0.1:5000",
}

def _apply_hardcoded_defaults() -> int:
    aplicados = 0
    for k, v in _HARDCODED_DEFAULTS.items():
        if k not in os.environ or os.environ[k] == "":
            os.environ[k] = v
            aplicados += 1
    return aplicados

_bootstrap_env()
_applied = _apply_hardcoded_defaults()

# ============================================================
# AUTO-DETECÇÃO DE IP LOCAL (TAILSCALE / LAN)
# ============================================================
def _auto_set_self_address() -> None:
    if os.environ.get("BRN_SEU_ENDERECO"): return
    porta = os.environ.get("BRN_P2P_PORT", "6001")
    try:
        out = subprocess.check_output(["tailscale", "ip", "-4"], text=True, timeout=3, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if line.startswith("100."):
                ip = line.strip()
                os.environ["BRN_SEU_ENDERECO"] = f"{ip}:{porta}"
                print(f"[boot] IP Tailscale detectado automaticamente: {ip}")
                return
    except Exception: pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        os.environ["BRN_SEU_ENDERECO"] = f"{ip}:{porta}"
        print(f"[boot] IP LAN detectado automaticamente: {ip}")
    except Exception: pass

_auto_set_self_address()


# ============================================================
# DESCOBERTA AUTOMÁTICA VIA TAILSCALE
# ============================================================
def _check_p2p_port(ip: str, port: int, timeout: float = 2.0) -> bool:
    """Testa se a porta P2P do peer está realmente aberta."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False

def _auto_discover_bootstrap(log) -> None:
    """Encontra o peer Tailscale com a maior blockchain e P2P acessível."""
    if os.environ.get("BRN_IS_ORIGIN", "0") == "1": return

    # [FIX] Se o .env já tem um IP configurado, usa ele e não deixa o Tailscale sobrescrever
    env_bootstrap = os.environ.get("BRN_BOOTSTRAP_PEERS", "").strip()
    if env_bootstrap:
        log.info(f"Tailscale: Usando BRN_BOOTSTRAP_PEERS fixo do .env: {env_bootstrap}")
        return

    log.info("Tailscale: buscando o melhor peer (Origem) automaticamente...")
    try:
        out = subprocess.check_output(
            ["tailscale", "status", "--json"],
            text=True, timeout=10, stderr=subprocess.DEVNULL
        )
        data = json.loads(out)
        peers = []

        for pid, p in data.get("Peer", {}).items():
            if not p.get("Online"): continue
            for ip in p.get("TailscaleIPs", []):
                if ip.startswith("100."):
                    peers.append({"ip": ip, "hostname": p.get("HostName", "peer")})
                    break

        if not peers:
            log.warning("Tailscale: nenhum peer online encontrado.")
            return

        best_peer = None
        best_height = -1
        porta_http = int(os.environ.get("BRN_WEB_PORT", "5000"))
        
        # [FIX] O peer remoto SEMPRE vai usar a porta 6001 para P2P.
        porta_p2p_origem = 6001 

        for p in peers:
            try:
                url = f"http://{p['ip']}:{porta_http}/api/chain-info"
                req = urllib.request.Request(url, headers={"User-Agent": "brn-boot/1.0"})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    info = json.loads(resp.read().decode("utf-8"))
                    h = int(info.get("height", -1))
                    
                    if h > best_height:
                        if _check_p2p_port(p['ip'], porta_p2p_origem):
                            best_height = h
                            best_peer = p
                        else:
                            log.warning(f"Tailscale: Peer {p['hostname']} ({p['ip']}) tem altura {h}, mas porta P2P {porta_p2p_origem} fechada.")
            except Exception:
                continue

        if best_peer:
            novo_bootstrap = f"{best_peer['ip']}:{porta_p2p_origem}"
            os.environ["BRN_BOOTSTRAP_PEERS"] = novo_bootstrap
            log.info(f"Tailscale: Melhor peer VALIDADO -> {best_peer['hostname']} ({best_peer['ip']}) | Altura: {best_height}")
            log.info(f"Tailscale: BRN_BOOTSTRAP_PEERS definido para {novo_bootstrap}")
        else:
            log.warning("Tailscale: nenhum peer respondeu na porta P2P.")
            
    except Exception as e:
        log.warning(f"Tailscale: falha na descoberta automática: {e}")


# ============================================================
# IMPORTS
# ============================================================
from brn_config import Config
from brn_logger import setup_logger, get_logger
from version import VERSION, BUILD_DATE, GITHUB_USER, GITHUB_REPO

try:
    from trust_system import trust_system
    _HAS_TRUST = True
except ImportError:
    trust_system = None
    _HAS_TRUST = False

_shutdown = threading.Event()
NODE_ID_PATH        = BASE_DIR / "node_identity.enc"
CLIENT_BOOT_TIMEOUT = int(os.environ.get("BRN_CLIENT_BOOT_TIMEOUT", "120"))
WEB_HOST_DEFAULT      = "127.0.0.1"
EXPLORER_HOST_DEFAULT = "127.0.0.1"
_running_services: list = []
_services_lock = threading.Lock()


def _resolve_auto_mine() -> bool:
    return (os.environ.get("BRN_AUTO_MINE", "").strip() == "1" or os.environ.get("BRN_MINER_ENABLED", "").strip() == "1")

def _register_service(name: str, svc) -> None:
    with _services_lock: _running_services.append((name, svc))

def _thread_excepthook(args):
    try:
        log = get_logger("thread")
        thread_name = args.thread.name if args.thread else "?"
        log.error(f"Thread '{thread_name}' morreu: {args.exc_value}", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    except Exception: pass

threading.excepthook = _thread_excepthook

def _thread_wrap(name: str, fn):
    def runner():
        try: fn()
        except Exception as e:
            try: get_logger(name).exception("thread " + name + " caiu: " + str(e))
            except Exception: pass
    return runner

def _check_required_modules() -> list:
    required = [
        "brn_config", "brn_logger", "version", "blockchain", "db", "crypto", "wallet", "bech32",
        "secure_store", "chain_validator", "checkpoints", "p2p_auth", "p2p_cipher", "p2p_secure", "p2p_unified",
        "discovery_v2", "miner_loop", "trust_system", "mining_pool", "miner_pool_client"
    ]
    missing = []
    for m in required:
        try: __import__(m)
        except Exception: missing.append(m)
    return missing

L2_ENABLED = False
btc_config = None
BTCWatcher = None
L2Manager = None
try:
    import btc_config
    from btc_watcher import BTCWatcher
    from l2_manager import L2Manager
    L2_ENABLED = True
except Exception as _e: pass

def _l2_disponivel() -> bool: return bool(L2_ENABLED and btc_config is not None and BTCWatcher is not None and L2Manager is not None)
def _l2_operacional() -> bool:
    if not _l2_disponivel(): return False
    try:
        from blockchain import Blockchain
        return callable(getattr(Blockchain, 'add_l2_transaction', None))
    except Exception: return False

POOL_ENABLED = os.environ.get("BRN_POOL_ENABLED", "1") == "1"
PoolManager = None
PoolClient = None
_POOL_MANAGER_AVAILABLE = False

if POOL_ENABLED:
    try:
        from mining_pool import PoolManager
        _POOL_MANAGER_AVAILABLE = True
    except Exception as _e: print(f"[AVISO] mining_pool indisponível: {_e}")
    try:
        from miner_pool_client import PoolClient
    except Exception as _e: print(f"[AVISO] miner_pool_client indisponível: {_e}")

def _pool_disponivel() -> bool: return bool(POOL_ENABLED and _POOL_MANAGER_AVAILABLE and PoolManager is not None)

def _audit(event: str, data: dict = None) -> None:
    try:
        from security import audit_log
        audit_log(event, data or {})
    except Exception: pass

def _close_audit() -> None:
    try:
        from security import close_audit
        close_audit()
    except Exception: pass

def _setup_tailscale_auto(log) -> bool:
    auth_key = os.environ.get("TS_AUTH_KEY", "").strip()
    if not auth_key:
        log.info("Tailscale: desabilitado (TS_AUTH_KEY vazia)")
        return True
    setup_path = BASE_DIR / "brn_tailscale_setup.py"
    if not setup_path.exists(): return True
    try:
        r = subprocess.run([sys.executable, str(setup_path), "--auto"], capture_output=True, text=True, timeout=180, cwd=str(BASE_DIR))
        return r.returncode == 0
    except Exception: return False

def parse_args():
    p = argparse.ArgumentParser(prog="main.py", description="BRN Node v13")
    p.add_argument("--status", action="store_true")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--version", action="store_true")
    p.add_argument("--check-update", action="store_true")
    p.add_argument("--config", default="config.json")
    p.add_argument("--log-level", default=None)
    p.add_argument("--log-file", default=None)
    p.add_argument("--password-file", default=None)
    p.add_argument("--rotate-node-id", action="store_true")
    p.add_argument("--client-mode", action="store_true")
    p.add_argument("--discover", action="store_true")
    p.add_argument("--l2", action="store_true")
    p.add_argument("--l2-stats", action="store_true")
    p.add_argument("--mine", action="store_true")
    p.add_argument("--no-p2p", action="store_true")
    p.add_argument("--relay", dest="relay", action="store_true", default=None)
    p.add_argument("--no-relay", dest="relay", action="store_false")
    p.add_argument("--verify-only", action="store_true")
    p.add_argument("--skip-verify", action="store_true")
    p.add_argument("--no-tailscale", action="store_true")
    p.add_argument("--no-sync", action="store_true")
    p.add_argument("--no-pool", action="store_true")
    p.add_argument("--no-pool-client", action="store_true")
    p.add_argument("--pool-client", action="store_true")
    p.add_argument("--pool-stats", action="store_true")
    p.add_argument("--gui", action="store_true")
    p.add_argument("--no-gui", action="store_true")
    return p.parse_args()

def _validate_required_secrets(log, is_origin: bool, wants_mining: bool) -> bool:
    critical = {"BRN_NODE_PASSWORD": "senha", "BRN_NETWORK_SECRET": "segredo"}
    if wants_mining: critical["BRN_WALLET_SESSION_PASS"] = "senha carteira"
    OLD_DEFAULTS = ("senha-no-temp-123", "senha-temp-123", "senha-forte-da-carteira-2026", "9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9")
    missing, weak = [], []
    for k, desc in critical.items():
        val = os.environ.get(k, "").strip()
        if not val: missing.append(f"  - {k}  ({desc})")
        elif val in OLD_DEFAULTS: weak.append(f"  - {k}  ({desc})")
    if missing:
        for m in missing: log.error(m)
        return False
    return True

def _resolve_password(args) -> str:
    if args.password_file:
        p = Path(args.password_file)
        if not p.exists(): raise SystemExit("--password-file não encontrado")
        return p.read_text(encoding="utf-8").rstrip("\r\n")
    env = os.environ.get("BRN_NODE_PASSWORD")
    if env: return env
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass("Senha do nó: ")
    raise SystemExit("Senha do nó não informada.")

def _resolve_wallet_password() -> str:
    return os.environ.get("BRN_WALLET_SESSION_PASS", "").strip() or os.environ.get("BRN_NODE_PASSWORD", "").strip()

def load_or_create_node_identity(password: str, rotate: bool = False):
    from crypto import Ed25519PrivateKey
    from secure_store import save_wallet, load_wallet
    log = get_logger("identity")
    if rotate and NODE_ID_PATH.exists():
        backup = NODE_ID_PATH.with_suffix(".enc.bak")
        NODE_ID_PATH.replace(backup)
    if NODE_ID_PATH.exists():
        data = load_wallet(password, str(NODE_ID_PATH))
        if not data: raise SystemExit("Falha ao decifrar")
        sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
        pub_hex = data["pub"]
    else:
        sk = Ed25519PrivateKey.generate()
        pub_hex = sk.public_key().public_bytes_raw().hex()
        save_wallet({"sk": sk.private_bytes_raw().hex(), "pub": pub_hex}, password, str(NODE_ID_PATH))
    return sk, pub_hex

def _read_role() -> str:
    try:
        p = BASE_DIR / "brn_role.txt"
        if p.exists(): return p.read_text(encoding="utf-8").strip().lower()
    except Exception: pass
    return "auto"

def _is_origin(client_mode: bool) -> bool:
    if client_mode: return False
    if os.environ.get("BRN_IS_ORIGIN", "0") == "1": return True
    return _read_role() in ("origin", "hub", "servidor")

VERIFY_OK = "ok"; VERIFY_INVALID = "invalid"; VERIFY_ERROR = "error"

def _verify_chain(chain) -> str:
    try:
        from chain_validator import verify_chain
    except ImportError: return VERIFY_ERROR
    log = get_logger("verify")
    try: r = verify_chain(chain)
    except Exception as e: return VERIFY_ERROR
    valid = getattr(r, "valid", None)
    if valid is None: valid = bool(r)
    if valid: return VERIFY_OK
    return VERIFY_INVALID

def _inject_into_server(what: str, obj, log) -> bool:
    setter_name = {"p2p": "set_p2p_manager", "sync": "set_sync_manager", "pool": "set_pool_manager", "trust": "set_trust_network"}.get(what)
    try:
        import server as _srv
        setter = getattr(_srv, setter_name, None)
        if setter is None: return False
        setter(obj)
        return True
    except Exception: return False

def _init_http_server(chain, log):
    try:
        from server import app as http_app, set_chain, auto_registrar_carteira_atual
    except Exception as e:
        log.error(f"Falha importando server.py: {e}")
        return None
    try:
        set_chain(chain)
        auto_registrar_carteira_atual()
    except Exception as e: pass
    return http_app

def run_http(app_instance):
    log = get_logger("http")
    port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    host = os.environ.get("BRN_WEB_HOST", WEB_HOST_DEFAULT)
    ssl_ctx = None
    try:
        from waitress import serve
        threads_count = int(os.environ.get("BRN_HTTP_THREADS", "16"))
        log.info(f"HTTP (Waitress) http://{host}:{port} com {threads_count} threads")
        serve(app_instance, host=host, port=port, threads=threads_count)
    except ImportError:
        log.warning("Waitress NAO instalado! Usando Flask dev server.")
        app_instance.run(host=host, port=port, threaded=True)

def run_explorer():
    from explorer import app as explorer_app
    log = get_logger("explorer")
    port = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
    host = os.environ.get("BRN_EXPLORER_HOST", EXPLORER_HOST_DEFAULT)
    log.info(f"Explorer http://{host}:{port}")
    explorer_app.run(host=host, port=port, threaded=True)

def run_status_loop(chain, p2p, sync=None, pool=None, pool_client=None, trust_net=None, node_id_short=""):
    log = get_logger("status")
    last_peers_hb = 0.0
    while not _shutdown.is_set():
        time.sleep(30)
        try:
            peer_count = p2p.get_status().get("peer_count", 0) if p2p else 0
            msg = f"Altura={chain.db.height()} Peers={peer_count} Mempool={len(chain.db.all_mempool(limit=1000))} UTXOs={chain.db.count_utxos()}"
            if sync:
                s = sync.get_status()
                msg += f" Sync={s.get('mode','?')}/{s.get('local_height',0)}→{s.get('target_height',0)} ({s.get('progress',0)}%)"
            if pool_client and pool_client.status().get("running"):
                pcs = pool_client.status()
                msg += f" PoolClient[{pcs.get('hashrate_khs',0)} kH/s shares={pcs.get('shares_accepted',0)}/{pcs.get('shares_submitted',0)}]"
            if trust_net:
                peers = getattr(trust_net, "peers", {})
                msg += f" Trust[Nodes={len(peers)} Valid={sum(1 for p in peers.values() if p.get('status')=='trusted')}]"
            log.info(msg)

            agora = time.time()
            if agora - last_peers_hb >= 60.0:
                last_peers_hb = agora
                try:
                    from update_peers import update_local_timestamp
                    update_local_timestamp(height=chain.db.height(), node_id=node_id_short or "")
                except Exception: pass
        except Exception as e: log.warning(f"status loop: {e}")

def run_wallet_main_thread():
    try:
        from app_wallet_v3 import WalletApi
        import webview
        api = WalletApi()
        webview.create_window("BRN RWA — Carteira Digital", url=(BASE_DIR / "index_wallet.html").resolve().as_uri(), js_api=api, width=1020, height=880)
        webview.start(debug=False)
    except Exception as e: get_logger("wallet").error(f"Falha: {e}")

def _setup_pool_client_local(chain, pool, log, disabled: bool, is_origin_node: bool):
    if disabled or PoolClient is None: return None
    wallet_pass = _resolve_wallet_password()
    if not wallet_pass: return None
    try:
        from secure_store import load_current_wallet
        w = load_current_wallet(wallet_pass)
        if not w or not w.get("ok"): return None
        addr, pub = w.get("address", ""), (w.get("public_key") or w.get("pub_hex") or w.get("pubkey") or "")
    except Exception: return None
    if not addr.startswith("brn1") or not pub: return None
    pool_url = os.environ.get("BRN_POOL_URL_LOCAL" if is_origin_node else "BRN_POOL_URL", "http://127.0.0.1:5000")
    try:
        pc = PoolClient(addr, pub, pool_url=pool_url)
        if pc.start_mining()[0]: return pc
    except Exception: pass
    return None

def _setup_local_miner_broadcast(chain, p2p, log):
    if p2p:
        def _on_local_block(block):
            try: p2p.broadcast_block(block)
            except Exception: pass
        chain.on_block_mined = _on_local_block

def _stop_with_timeout(name: str, svc, timeout: float = 5.0, log=None) -> None:
    try:
        fn = svc.stop if hasattr(svc, "stop") else (svc.stop_mining if hasattr(svc, "stop_mining") else svc)
        if callable(fn):
            t = threading.Thread(target=fn, daemon=True)
            t.start(); t.join(timeout)
    except Exception: pass

def _graceful_shutdown(log, trust_net=None) -> None:
    with _services_lock:
        for name, svc in reversed(_running_services): _stop_with_timeout(name, svc)
    if trust_net:
        try: trust_net.save_data()
        except Exception: pass
    _audit("node_stop", {})

def main():
    args = parse_args()
    cfg = Config(args.config)
    if args.read_only: cfg.data["read_only"] = True
    if args.headless or args.no_gui: cfg.data["headless"] = True
    if args.gui: cfg.data["headless"] = False
    if args.log_level: cfg.data["log_level"] = args.log_level
    if args.log_file: cfg.data["log_file"] = args.log_file
    cfg.apply_to_env()
    log = setup_logger(level=cfg["log_level"], log_file=cfg["log_file"] or None)
    is_origin = _is_origin(args.client_mode)
    auto_mine = _resolve_auto_mine()

    if args.discover:
        from tailscale_discovery import list_online_peers
        print(json.dumps(list_online_peers(force_refresh=True), indent=2))
        return 0

    log.info("=" * 60)
    log.info(f" BRN Node v{VERSION} ({BUILD_DATE})")
    log.info(f" Config: {cfg.source} | Modo: {'ORIGEM' if is_origin else 'CLIENTE'} | Pool: {'ON' if _pool_disponivel() else 'off'}")
    log.info("=" * 60)

    if args.version:
        print(VERSION); return 0

    missing = _check_required_modules()
    if missing:
        log.error("Módulos obrigatórios faltando: " + ", ".join(missing))
        return 1

    if not _validate_required_secrets(log, is_origin, bool(args.mine or auto_mine or (not args.no_pool))): return 1

    if not args.no_tailscale: _setup_tailscale_auto(log)

    # [FIX] Descoberta automática blindada
    if not is_origin:
        _auto_discover_bootstrap(log)

    try: node_password = _resolve_password(args)
    except SystemExit as e: log.error(str(e)); return 1

    try: node_sk, node_pub = load_or_create_node_identity(node_password, rotate=args.rotate_node_id)
    except SystemExit as e: log.error(str(e)); return 1

    from blockchain import Blockchain
    require_auth_tag = (os.environ.get("BRN_REQUIRE_AUTH_TAG", "1") == "1")
    chain = Blockchain(cfg["db_path"], auto_genesis=is_origin, network_secret=cfg["network_secret"], require_auth_tag=require_auth_tag)
    chain._fork_signal = threading.Event()
    chain.is_origin = is_origin

    trust_net = None
    if _HAS_TRUST:
        trust_net = trust_system
        chain.trust_net = trust_net

    if not args.skip_verify and os.environ.get("BRN_SKIP_VERIFY", "0") != "1":
        _verify_chain(chain)

    p2p = None
    if not args.no_p2p:
        try:
            from p2p_unified import P2PManager
            p2p = P2PManager(chain, node_sk, tcp_port=cfg["p2p_port"], enable_upnp=cfg["upnp"])
            p2p.start()
            _register_service("p2p", p2p)
        except Exception as e: log.error(f"Falha P2P: {e}")

    if p2p and trust_net: p2p.set_trust(trust_net)

    sync = None
    if not args.no_sync and p2p:
        try:
            from sync_manager import ChainSyncManager
            sync = ChainSyncManager(chain, chain.db, p2p)
            sync.set_sync_mode("full")
            sync.start_sync_loop()
            _register_service("sync", sync)
        except Exception as e: log.warning(f"Falha Sync: {e}")

    pool = None
    if not args.no_pool and _pool_disponivel():
        try:
            pool = PoolManager(chain)
            _register_service("pool", pool)
        except Exception as e: log.warning(f"Falha Pool: {e}")

    http_app = _init_http_server(chain, log)
    if http_app:
        threading.Thread(target=_thread_wrap("HTTP", lambda: run_http(http_app)), daemon=True, name="HTTP").start()

    if (os.environ.get("BRN_EXPLORER_ENABLED", "1") == "1" and (BASE_DIR / "explorer.py").exists()):
        threading.Thread(target=_thread_wrap("Explorer", run_explorer), daemon=True, name="Explorer").start()

    if p2p: _inject_into_server("p2p", p2p, log)
    if sync: _inject_into_server("sync", sync, log)
    if pool: _inject_into_server("pool", pool, log)
    if trust_net: _inject_into_server("trust", trust_net, log)

    disabled_pool_client = args.no_pool_client if not args.pool_client else False
    pool_client = _setup_pool_client_local(chain, pool, log, disabled=disabled_pool_client, is_origin_node=is_origin)
    if pool_client: _register_service("pool_client", pool_client)

    _setup_local_miner_broadcast(chain, p2p, log)

    quer_miner_local = bool(args.mine or auto_mine)
    if quer_miner_local and (pool or pool_client):
        quer_miner_local = False

    if quer_miner_local:
        try:
            from miner_loop import get_miner
            miner = get_miner(chain)
            wallet_pass = _resolve_wallet_password()
            from secure_store import load_current_wallet
            wd = load_current_wallet(wallet_pass)
            if wd and wd.get("ok"):
                w = {"address": wd.get("address", ""), "pubkey": (wd.get("public_key") or wd.get("pub_hex") or wd.get("pubkey") or "")}
                if w["address"] and w["pubkey"]:
                    miner.start(w["address"], w["pubkey"], is_origin=is_origin)
                    _register_service("miner", miner)
        except Exception as e: log.warning(f"Miner local falhou: {e}")

    node_id_short = node_pub[:16]
    threading.Thread(target=_thread_wrap("StatusLoop", lambda: run_status_loop(chain, p2p, sync, pool, pool_client, trust_net, node_id_short)), daemon=True, name="StatusLoop").start()

    if not is_origin and chain.db.height() < 0:
        log.info(f"Aguardando genesis (timeout {CLIENT_BOOT_TIMEOUT}s)...")
        t0 = time.time()
        while (chain.db.height() < 0 and (time.time() - t0) < CLIENT_BOOT_TIMEOUT and not _shutdown.is_set()): time.sleep(1)

    log.info("Nó pronto. Ctrl+C para encerrar.")

    try:
        if not cfg["headless"]:
            run_wallet_main_thread()
        else:
            while not _shutdown.is_set(): time.sleep(1)
    except KeyboardInterrupt: pass

    _shutdown.set()
    _graceful_shutdown(log, trust_net)
    try: chain.db.close()
    except Exception: pass

    return 0

_signal_count = 0
def _on_signal(signum, frame):
    global _signal_count
    _signal_count += 1
    if _signal_count >= 2: os._exit(1)
    _shutdown.set()

if __name__ == "__main__":
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    try: sys.exit(main())
    except SystemExit: raise
    except Exception as e:
        try: get_logger("main").exception("Erro fatal: " + str(e))
        except Exception: pass
        sys.exit(1)
