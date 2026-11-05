"""main.py — Entrypoint do nó BRN (v13.8.4)
================================================================
v13.8.4 (2026-11-04):
  [FIX] PoolClient e PBFT agora são inicializados dinamicamente 
        pelo server.py quando a carteira é desbloqueada.
================================================================
"""
from __future__ import annotations
import argparse, os, signal, sys, threading, time, json, logging
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

try:
    from version import VERSION, BUILD_DATE
except ImportError:
    VERSION = "13.8.4"
    BUILD_DATE = "2026-11-04"

def parse_args():
    p = argparse.ArgumentParser(prog="main.py")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--client-mode", action="store_true")
    p.add_argument("--origem", action="store_true")
    p.add_argument("--no-pool-client", action="store_true")
    p.add_argument("--no-p2p", action="store_true")
    p.add_argument("--no-explorer", action="store_true")
    p.add_argument("--db", metavar="PATH", default=None)
    p.add_argument("--web-port", type=int, default=None)
    p.add_argument("--p2p-port", type=int, default=None)
    p.add_argument("--version", action="store_true")
    p.add_argument("--skip-verify", action="store_true")
    return p.parse_args()

def _load_env_file():
    env_file = BASE_DIR / "brn_network.env"
    if not env_file.exists(): return 0
    loaded = 0
    try: text = env_file.read_text(encoding="utf-8", errors="ignore")
    except Exception: return 0
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s: continue
        k, _, v = s.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
            loaded += 1
    return loaded

def _setup_logging(level: str = "INFO"):
    try:
        from brn_logger import setup_logger, get_logger
        setup_logger(level=level, quiet=False)
        return get_logger("main")
    except Exception:
        logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        return logging.getLogger("main")

def main() -> int:
    args = parse_args()
    if args.version:
        print(f"BRN Node v{VERSION} (build {BUILD_DATE})")
        return 0

    if args.skip_verify:
        print("[main] --skip-verify: assumindo que o .bat já fez as verificações")

    _load_env_file()

    if args.client_mode: os.environ["BRN_IS_ORIGIN"] = "0"
    if args.origem:      os.environ["BRN_IS_ORIGIN"] = "1"
    if args.web_port is not None: os.environ["BRN_WEB_PORT"] = str(args.web_port)
    if args.p2p_port is not None: os.environ["BRN_P2P_PORT"] = str(args.p2p_port)

    is_origin = os.environ.get("BRN_IS_ORIGIN", "0") == "1"
    web_port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    p2p_port = int(os.environ.get("BRN_P2P_PORT", "6001"))
    db_path  = args.db or os.environ.get("BRN_DB", "brn_v2_chain.db")

    log = _setup_logging()
    log.info(" BRN Node v%s (build %s) is_origin=%s", VERSION, BUILD_DATE, is_origin)

    try:
        import port_guard
        port_guard.check_brn_ports(checar_explorer=not args.no_explorer)
    except Exception: pass

    try:
        from blockchain import Blockchain
        chain = Blockchain(db_path, auto_genesis=False)
        log.info("[chain] aberta height=%d", chain.db.height())
    except Exception as e:
        log.error("[chain] fatal: %s", e)
        return 2

    # P2P
    p2p = None
    if not args.no_p2p:
        try:
            from p2p_unified import P2PManager
            p2p = P2PManager(chain, tcp_port=p2p_port)
            chain.set_p2p(p2p)
            p2p.start()
            log.info("[p2p] iniciado na porta %d", p2p_port)
        except Exception as e:
            log.error("[p2p] init falhou: %s", e)

    # Sync
    sync_mgr = None
    if p2p:
        try:
            from sync_manager import ChainSyncManager
            sync_mgr = ChainSyncManager(chain, chain.db, p2p)
            sync_mgr.start_sync_loop()
        except Exception: pass

    # Pool (somente Origem)
    pool = None
    if is_origin:
        try:
            from mining_pool import PoolManager
            pool = PoolManager(chain)
            log.info("[pool] PoolManager ativo (origin)")
        except Exception as e:
            log.warning("[pool] init falhou: %s", e)

    # Web Server
    try:
        import server as srv
        srv.set_chain(chain)
        if p2p: srv.set_p2p_manager(p2p)
        if pool: srv.set_pool_manager(pool)
        if sync_mgr: srv.set_sync_manager(sync_mgr)
        
        def _run_web():
            srv.app.run(host=os.environ.get("BRN_WEB_HOST", "127.0.0.1"), port=web_port, threaded=True, use_reloader=False)
        threading.Thread(target=_run_web, daemon=True, name="HTTP").start()
        log.info("[server] HTTP ativo na porta %d", web_port)
    except Exception as e:
        log.error("[server] falhou: %s", e)

    # Explorer
    if not args.no_explorer:
        try:
            import explorer as exp
            if hasattr(exp, "set_chain"): exp.set_chain(chain)
            def _run_exp():
                exp.app.run(host="0.0.0.0", port=int(os.environ.get("BRN_EXPLORER_PORT", "8080")), threaded=True, use_reloader=False)
            threading.Thread(target=_run_exp, daemon=True, name="EXP").start()
        except Exception: pass

    # GUI
    if not args.headless:
        try:
            import webview
            from app_wallet_v3 import WalletApi
            api = WalletApi()
            wallet_path = BASE_DIR / "index_wallet.html"
            if not wallet_path.exists(): wallet_path = BASE_DIR / "panel.html"
            url = wallet_path.as_uri() if wallet_path.exists() else "about:blank"
            log.info("[gui] Abrindo interface: %s", wallet_path.name)
            webview.create_window("BRN Wallet", url=url, width=1100, height=780, js_api=api)
            webview.start()
        except Exception as e:
            log.warning("[gui] falha: %s", e)

    def _shutdown(*_):
        try: chain.db.close()
        except Exception: pass
        os._exit(0)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        _shutdown()

    return 0

if __name__ == "__main__":
    sys.exit(main())
