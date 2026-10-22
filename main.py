"""main.py — Entrypoint unificado do nó BRN (v13.6.3)"""
from __future__ import annotations
import os, sys, signal, argparse, threading, time, json, subprocess, urllib.request
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent

def _bootstrap_env():
    env_file = BASE_DIR / "brn_network.env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() not in os.environ:
                        os.environ[k.strip()] = v.strip().strip('"').strip("'")
        except Exception: pass

_HARDCODED_DEFAULTS = {
    "BRN_BOOTSTRAP_PEERS": "", "BRN_IS_ORIGIN": "0", "BRN_HTTPS": "0",
    "BRN_AUDIT_ENABLED": "1", "BRN_AUDIT_LOG": "audit.log",
    "BRN_P2P_AUTH": "optional", "BRN_P2P_AUTH_WINDOW": "30", "BRN_P2P_NONCE_CACHE": "10000",
    "BRN_SKIP_VERIFY": "0", "BRN_VERIFY_STRICT": "0", "BRN_TRACKER": "",
    "MONGO_USER": "", "MONGO_PASS": "", "MONGO_HOST": "", "MONGO_DB": "brn_analytics",
    "TS_AUTH_KEY": "", "TS_API_KEY": "", "TS_TAILNET": "", "TS_HOSTNAME_PREFIX": "brn-node",
    "BRN_POOL_ENABLED": "1", "BRN_POOL_URL_LOCAL": "http://127.0.0.1:5000", "BRN_WEB_PASS": "",
}

def _apply_hardcoded_defaults():
    aplicados = 0
    for k, v in _HARDCODED_DEFAULTS.items():
        if k not in os.environ or os.environ[k] == "":
            os.environ[k] = v; aplicados += 1
    return aplicados

_bootstrap_env()
_applied = _apply_hardcoded_defaults()

from brn_config import Config
from brn_logger import setup_logger, get_logger
from version import VERSION, BUILD_DATE, GITHUB_USER, GITHUB_REPO

try:
    from trust_network import TrustNetwork
    _HAS_TRUST = True
except ImportError:
    TrustNetwork = None; _HAS_TRUST = False

_shutdown = threading.Event()
NODE_ID_PATH = BASE_DIR / "node_identity.enc"
CLIENT_BOOT_TIMEOUT = int(os.environ.get("BRN_CLIENT_BOOT_TIMEOUT", "120"))
WEB_HOST = os.environ.get("BRN_WEB_HOST", "127.0.0.1")
EXPLORER_HOST = os.environ.get("BRN_EXPLORER_HOST", "127.0.0.1")
AUTO_MINE = os.environ.get("BRN_AUTO_MINE", "0") == "1"
_running_services = []; _services_lock = threading.Lock()

def _register_service(name, svc):
    with _services_lock: _running_services.append((name, svc))

def _thread_excepthook(args):
    try: get_logger("thread").error("Thread morreu: " + str(args.exc_value), exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    except Exception: pass
threading.excepthook = _thread_excepthook

def _thread_wrap(name, fn):
    def runner():
        try: fn()
        except Exception as e: get_logger(name).exception("thread " + name + " caiu: " + str(e))
    return runner

def _check_required_modules():
    required = ["brn_config", "brn_logger", "version", "blockchain", "db", "crypto", "wallet", "bech32", "secure_store", "chain_validator", "checkpoints", "p2p_auth", "p2p_cipher", "p2p_secure", "p2p_unified", "discovery_v2", "miner_loop", "trust_network"]
    missing = []
    for m in required:
        try: __import__(m)
        except Exception: missing.append(m)
    return missing

L2_ENABLED = False; btc_config = None; BTCWatcher = None; L2Manager = None
try:
    import btc_config
    from btc_watcher import BTCWatcher
    from l2_manager import L2Manager
    L2_ENABLED = True
except Exception as _e: pass

def _l2_disponivel(): return bool(L2_ENABLED and btc_config and BTCWatcher and L2Manager)

POOL_ENABLED = os.environ.get("BRN_POOL_ENABLED", "1") == "1"
PoolManager = None; PoolClient = None; _POOL_MANAGER_AVAILABLE = False
if POOL_ENABLED:
    try:
        from mining_pool import PoolManager
        _POOL_MANAGER_AVAILABLE = True
    except Exception: PoolManager = None
    try: from miner_pool_client import PoolClient
    except Exception: PoolClient = None

def _pool_disponivel(): return bool(POOL_ENABLED and _POOL_MANAGER_AVAILABLE and PoolManager)

def _audit(event, data=None):
    try: from security import audit_log; audit_log(event, data or {})
    except Exception: pass

def _close_audit():
    try: from security import close_audit; close_audit()
    except Exception: pass

def _setup_tailscale_auto(log):
    auth_key = os.environ.get("TS_AUTH_KEY", "").strip()
    if not auth_key: return True
    setup_path = BASE_DIR / "brn_tailscale_setup.py"
    if not setup_path.exists(): return True
    try:
        r = subprocess.run([sys.executable, str(setup_path), "--auto"], capture_output=True, text=True, timeout=180, cwd=str(BASE_DIR))
        return r.returncode == 0
    except Exception: return False

def parse_args():
    p = argparse.ArgumentParser(prog="main.py")
    p.add_argument("--status", action="store_true"); p.add_argument("--headless", action="store_true"); p.add_argument("--read-only", action="store_true")
    p.add_argument("--version", action="store_true"); p.add_argument("--check-update", action="store_true"); p.add_argument("--config", default="config.json")
    p.add_argument("--log-level", default=None); p.add_argument("--log-file", default=None); p.add_argument("--password-file", default=None)
    p.add_argument("--rotate-node-id", action="store_true"); p.add_argument("--client-mode", action="store_true"); p.add_argument("--discover", action="store_true")
    p.add_argument("--l2", action="store_true"); p.add_argument("--l2-stats", action="store_true"); p.add_argument("--mine", action="store_true")
    p.add_argument("--no-p2p", action="store_true"); p.add_argument("--relay", dest="relay", action="store_true", default=None); p.add_argument("--no-relay", dest="relay", action="store_false")
    p.add_argument("--verify-only", action="store_true"); p.add_argument("--skip-verify", action="store_true"); p.add_argument("--no-tailscale", action="store_true")
    p.add_argument("--no-sync", action="store_true"); p.add_argument("--no-pool", action="store_true"); p.add_argument("--no-pool-client", action="store_true")
    p.add_argument("--pool-client", action="store_true"); p.add_argument("--pool-stats", action="store_true"); p.add_argument("--gui", action="store_true")
    p.add_argument("--no-gui", action="store_true")
    return p.parse_args()

def _validate_required_secrets(log, is_origin, wants_mining):
    critical = {"BRN_NODE_PASSWORD": "senha identidade", "BRN_NETWORK_SECRET": "segredo HMAC"}
    if wants_mining: critical["BRN_WALLET_SESSION_PASS"] = "senha carteira"
    OLD_DEFAULTS = ("senha-no-temp-123", "senha-temp-123", "senha-forte-da-carteira-2026", "9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9")
    missing, weak = [], []
    for k, desc in critical.items():
        val = os.environ.get(k, "").strip()
        if not val: missing.append(f"  - {k}  ({desc})")
        elif val in OLD_DEFAULTS: weak.append(f"  - {k}  ({desc})")
    if missing:
        log.error("SEGREDOS OBRIGATORIOS AUSENTES"); return False
    if weak: log.warning("SEGREDOS FRACOS - continuando")
    return True

def _resolve_password(args):
    if args.password_file:
        p = Path(args.password_file)
        if not p.exists(): raise SystemExit("Senha nao encontrada")
        return p.read_text(encoding="utf-8").rstrip("\r\n")
    env = os.environ.get("BRN_NODE_PASSWORD")
    if env: return env
    raise SystemExit("Senha do no nao informada.")

def _resolve_wallet_password(): return os.environ.get("BRN_WALLET_SESSION_PASS", "").strip() or os.environ.get("BRN_NODE_PASSWORD", "").strip()

def load_or_create_node_identity(password, rotate=False):
    from crypto import Ed25519PrivateKey
    from secure_store import save_wallet, load_wallet
    log = get_logger("identity")
    if rotate and NODE_ID_PATH.exists(): NODE_ID_PATH.replace(NODE_ID_PATH.with_suffix(".enc.bak"))
    if NODE_ID_PATH.exists():
        data = load_wallet(password, str(NODE_ID_PATH))
        if not data: raise SystemExit("Falha ao decifrar " + str(NODE_ID_PATH))
        sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
        pub_hex = data["pub"]
    else:
        sk = Ed25519PrivateKey.generate()
        pub_hex = sk.public_key().public_bytes_raw().hex()
        save_wallet({"sk": sk.private_bytes_raw().hex(), "pub": pub_hex}, password, str(NODE_ID_PATH))
        log.info("Identidade nova criada: " + pub_hex[:16] + "...")
    return sk, pub_hex

def _read_role():
    try:
        p = BASE_DIR / "brn_role.txt"
        if p.exists(): return p.read_text(encoding="utf-8").strip().lower()
    except Exception: pass
    return "auto"

def _is_origin(client_mode):
    if client_mode: return False
    if os.environ.get("BRN_IS_ORIGIN", "0") == "1": return True
    return _read_role() in ("origin", "hub", "servidor")

VERIFY_OK = "ok"; VERIFY_INVALID = "invalid"; VERIFY_ERROR = "error"

def _verify_chain(chain):
    try: from chain_validator import verify_chain
    except ImportError: return VERIFY_ERROR
    log = get_logger("verify"); log.info("Verificando integridade da cadeia...")
    try:
        r = verify_chain(chain); valid = getattr(r, "valid", bool(r))
        return VERIFY_OK if valid else VERIFY_INVALID
    except Exception: return VERIFY_ERROR

def _inject_into_server(what, obj, log):
    setter_name = {"p2p": "set_p2p_manager", "sync": "set_sync_manager", "pool": "set_pool_manager", "trust": "set_trust_network"}.get(what)
    if not setter_name: return False
    try:
        import server as _srv
        setter = getattr(_srv, setter_name, None)
        if not setter: return False
        setter(obj); return True
    except Exception: return False

def _init_http_server(chain, log):
    try: from server import app as http_app, set_chain, auto_registrar_carteira_atual
    except Exception as e:
        log.error("Falha importando server.py: " + str(e)); return None
    try: set_chain(chain)
    except Exception as e:
        log.error("Falha set_chain: " + str(e)); return None
    try: auto_registrar_carteira_atual()
    except Exception: pass
    return http_app

def run_http(app_instance):
    log = get_logger("http"); port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    log.info("HTTP http://" + WEB_HOST + ":" + str(port))
    app_instance.run(host=WEB_HOST, port=port, threaded=True, debug=False, use_reloader=False)

def run_status_loop(chain, p2p, sync=None, pool=None, pool_client=None, trust_net=None, node_id_short=""):
    log = get_logger("status"); last_peers_hb = 0.0; PEERS_HB_INTERVAL = 60.0
    while not _shutdown.is_set():
        time.sleep(30)
        try:
            peer_count = p2p.get_status().get("peer_count", 0) if p2p else 0
            mempool_n = len(chain.db.all_mempool(limit=1000)); utxos_n = chain.db.count_utxos()
            msg = ("Altura=" + str(chain.db.height()) + " Peers=" + str(peer_count) + " Mempool=" + str(mempool_n) + " UTXOs=" + str(utxos_n))
            if sync: msg += " Sync=" + str(sync.get_status().get("progress", 0)) + "%"
            log.info(msg)
            agora = time.time()
            if agora - last_peers_hb >= PEERS_HB_INTERVAL:
                last_peers_hb = agora
                try:
                    from update_peers import update_local_timestamp
                    update_local_timestamp(height=chain.db.height(), node_id=node_id_short)
                except Exception: pass
        except Exception as e: log.warning("status loop: " + str(e))

def run_wallet_main_thread():
    try:
        if not os.environ.get("BRN_WEB_PASS"): return
        from app_wallet_v3 import WalletApi
        import webview
        index_path = BASE_DIR / "index_wallet.html"
        if not index_path.exists(): return
        api = WalletApi()
        webview.create_window("BRN RWA", url=index_path.resolve().as_uri(), js_api=api, width=1020, height=880)
        webview.start(debug=False)
    except Exception as e: get_logger("wallet").error("Falha: " + str(e))

def _setup_pool_client_local(chain, pool, log, disabled, is_origin_node):
    if disabled or PoolClient is None: return None
    wallet_pass = _resolve_wallet_password()
    if not wallet_pass: return None
    addr, pub = "", ""
    try:
        from secure_store import load_current_wallet
        w = load_current_wallet(wallet_pass)
        if w and w.get("ok"):
            addr = (w.get("address") or "").strip(); pub = (w.get("public_key") or w.get("pub_hex") or w.get("pubkey") or "").strip()
    except Exception: pass
    if not addr.startswith("brn1") or not pub: return None
    pool_url = os.environ.get("BRN_POOL_URL_LOCAL", "http://127.0.0.1:5000") if is_origin_node else os.environ.get("BRN_POOL_URL", "http://127.0.0.1:5000")
    try:
        pc = PoolClient(addr, pub, pool_url=pool_url)
        ok, msg = pc.start_mining()
        if ok: log.info("PoolClient iniciado"); return pc
    except Exception as e: log.warning("PoolClient: excecao: " + str(e))
    return None

def _setup_local_miner_broadcast(chain, p2p, log):
    if p2p is None: return False
    try:
        def _on_local_block(block): p2p.broadcast_block(block)
        chain.on_block_mined = _on_local_block; return True
    except Exception: return False

def _stop_with_timeout(name, svc, timeout=5.0, log=None):
    try:
        fn = svc.stop if hasattr(svc, "stop") else svc.stop_mining if hasattr(svc, "stop_mining") else svc if callable(svc) else None
        if not fn: return
        t = threading.Thread(target=fn, daemon=True); t.start(); t.join(timeout)
    except Exception: pass

def _graceful_shutdown(log, trust_net=None):
    with _services_lock: services = list(_running_services)
    for name, svc in reversed(services): _stop_with_timeout(name, svc)
    try: from wallet import WalletManager; WalletManager.lock_session()
    except Exception: pass
    try: from mongo_client import mongo; mongo.close()
    except Exception: pass
    if trust_net:
        try: trust_net.shutdown()
        except Exception: pass
    _audit("node_stop", {}); _close_audit()

def main():
    args = parse_args(); cfg = Config(args.config)
    if args.read_only: cfg.data["read_only"] = True
    if args.headless or args.no_gui: cfg.data["headless"] = True
    if args.gui: cfg.data["headless"] = False
    if args.log_level: cfg.data["log_level"] = args.log_level
    if args.log_file: cfg.data["log_file"] = args.log_file
    cfg.apply_to_env()
    log = setup_logger(level=cfg["log_level"], log_file=cfg["log_file"] or None)
    is_origin = _is_origin(args.client_mode)
    log.info("=" * 60); log.info(" BRN Node v" + VERSION + " (" + BUILD_DATE + ")")
    l2_mode = "ON" if args.l2 and _l2_disponivel() else "off"
    pool_mode = "off" if args.no_pool else "ON" if _pool_disponivel() else "off"
    log.info(" Config: " + cfg.source + " | Modo: " + ("ORIGEM" if is_origin else "CLIENTE") + " | L2: " + l2_mode + " | Pool: " + pool_mode)
    log.info("=" * 60)
    if args.version: print(VERSION); return 0
    if args.status: print("Status ok"); return 0
    if args.check_update: print('{"ok": false}'); return 0
    missing = _check_required_modules()
    if missing: log.error("Faltando: " + ", ".join(missing)); return 1
    wants_mining = bool(args.mine or AUTO_MINE or (not args.no_pool))
    if not _validate_required_secrets(log, is_origin, wants_mining): return 1
    if not args.no_tailscale: _setup_tailscale_auto(log)
    try: node_password = _resolve_password(args)
    except SystemExit as e: log.error(str(e)); return 1
    try: node_sk, node_pub = load_or_create_node_identity(node_password, rotate=args.rotate_node_id)
    except Exception as e: log.exception("Falha identidade"); return 1
    log.info("Nó ID: " + node_pub[:16] + "...")

    from blockchain import Blockchain
    require_auth_tag = (os.environ.get("BRN_REQUIRE_AUTH_TAG", "1") == "1")
    chain = Blockchain(cfg["db_path"], auto_genesis=is_origin, network_secret=cfg["network_secret"], require_auth_tag=require_auth_tag)
    chain._fork_signal = threading.Event(); chain.is_origin = is_origin
    log.info("Blockchain pronta (altura=" + str(chain.db.height()) + ")")

    trust_net = None
    if _HAS_TRUST:
        try: trust_net = TrustNetwork(str(BASE_DIR / "trust_network.json")); chain.trust_net = trust_net
        except Exception: pass

    if not args.skip_verify and os.environ.get("BRN_SKIP_VERIFY", "0") != "1":
        st = _verify_chain(chain)
        if st == VERIFY_INVALID and os.environ.get("BRN_VERIFY_STRICT", "0") == "1": return 2

    p2p = None
    if not args.no_p2p:
        try:
            from p2p_unified import P2PManager
            p2p = P2PManager(chain, node_sk, tcp_port=cfg["p2p_port"], enable_upnp=cfg["upnp"])
            p2p.start(); _register_service("p2p", p2p); log.info("P2P iniciado na porta " + str(cfg["p2p_port"]))
        except Exception as e: log.error("Falha P2P: " + str(e))

    sync = None
    if not args.no_sync and p2p is not None:
        try:
            from sync_manager import ChainSyncManager
            sync = ChainSyncManager(chain, chain.db, p2p)
            sync.set_sync_mode("full"); sync.start_sync_loop(); _register_service("sync", sync)
            log.info("Sync iniciado (full mode)")
        except Exception as e: log.error("Falha Sync: " + str(e))

    pool = None
    if not args.no_pool and _pool_disponivel():
        try: pool = PoolManager(chain); _register_service("pool", pool); log.info("PoolManager iniciado")
        except Exception as e: log.warning("Falha PoolManager: " + str(e))

    http_app = _init_http_server(chain, log)
    if http_app: threading.Thread(target=_thread_wrap("HTTP", lambda: run_http(http_app)), daemon=True).start()

    if p2p: _inject_into_server("p2p", p2p, log)
    if sync: _inject_into_server("sync", sync, log)
    if pool: _inject_into_server("pool", pool, log)
    if trust_net: _inject_into_server("trust", trust_net, log)

    disabled_pool_client = args.no_pool_client or args.client_mode
    if args.pool_client: disabled_pool_client = False
    pool_client = _setup_pool_client_local(chain, pool, log, disabled_pool_client, is_origin)
    if pool_client: _register_service("pool_client", pool_client)

    _setup_local_miner_broadcast(chain, p2p, log)
    quer_miner_local = bool(args.mine or AUTO_MINE)
    if quer_miner_local and (pool or pool_client): quer_miner_local = False
    if quer_miner_local:
        try:
            from miner_loop import get_miner
            miner = get_miner(chain); wallet_pass = _resolve_wallet_password()
            from secure_store import load_current_wallet
            wd = load_current_wallet(wallet_pass)
            if wd.get("ok") and wd.get("address") and (wd.get("public_key") or wd.get("pubkey")):
                miner.start(wd.get("address"), wd.get("public_key") or wd.get("pubkey"), is_origin=is_origin)
                _register_service("miner", miner)
        except Exception: pass

    threading.Thread(target=_thread_wrap("StatusLoop", lambda: run_status_loop(chain, p2p, sync, pool, pool_client, trust_net, node_pub[:16])), daemon=True).start()

    if not is_origin and chain.db.height() < 0:
        log.info("Aguardando genesis...")
        t0 = time.time()
        while chain.db.height() < 0 and (time.time() - t0) < CLIENT_BOOT_TIMEOUT and not _shutdown.is_set(): time.sleep(1)

    _audit("node_start", {"mode": "origin" if is_origin else "client", "height": chain.db.height()})
    log.info("Nó pronto. Ctrl+C para encerrar.")

    try:
        if not cfg["headless"]: run_wallet_main_thread()
        else:
            while not _shutdown.is_set(): time.sleep(1)
    except KeyboardInterrupt: pass

    _shutdown.set(); _graceful_shutdown(log, trust_net)
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
    signal.signal(signal.SIGINT, _on_signal); signal.signal(signal.SIGTERM, _on_signal)
    try: sys.exit(main())
    except SystemExit: raise
    except Exception as e:
        get_logger("main").exception("Erro fatal: " + str(e)); sys.exit(1)
