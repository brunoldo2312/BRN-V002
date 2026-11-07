"""main.py — Entrypoint do nó BRN (v13.9.2)
================================================================
v13.9.2 (2026-11-06):
  [NEW] Integração do BTCWatcher (L2 Bridge BTC -> BRN).
        O monitor de depósitos BTC agora inicia junto com a Origem.
v13.9.1 (2026-11-06):
  [NEW] Inicialização do PBFTConsensus (Consenso Bizantino).
  [NEW] Integração com TrustDaemon (auto-cura da rede).
  [FIX] Carregamento seguro de carteira (permite iniciar sem senha no .env
        para usar a GUI).
================================================================
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
import json
import logging
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

try:
    from version import VERSION, BUILD_DATE
except ImportError:
    VERSION = "13.9.2"
    BUILD_DATE = "2026-11-06"

def parse_args():
    p = argparse.ArgumentParser(prog="main.py")
    p.add_argument("--headless", action="store_true", help="sem GUI")
    p.add_argument("--client-mode", action="store_true", help="nó cliente")
    p.add_argument("--origem", action="store_true", help="força BRN_IS_ORIGIN=1")
    p.add_argument("--no-pool-client", action="store_true", help="não inicia PoolClient")
    p.add_argument("--no-p2p", action="store_true", help="desliga P2P")
    p.add_argument("--no-explorer", action="store_true", help="não inicia Explorer")
    p.add_argument("--db", metavar="PATH", default=None, help="caminho do DB")
    p.add_argument("--web-port", type=int, default=None, help="porta HTTP")
    p.add_argument("--p2p-port", type=int, default=None, help="porta P2P")
    p.add_argument("--version", action="store_true", help="mostra versão")
    p.add_argument("--skip-verify", action="store_true", help="pula verificações")
    return p.parse_args()

def _load_env_file():
    env_file = BASE_DIR / "brn_network.env"
    if not env_file.exists(): return 0
    loaded = 0
    try: 
        text = env_file.read_text(encoding="utf-8", errors="ignore")
    except Exception: 
        return 0
        
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

def _load_or_create_node_identity(password: str, path: str = "node_identity.enc"):
    try:
        from secure_store import load_node_identity, save_node_identity
        from crypto import Ed25519PrivateKey
    except Exception as e:
        raise RuntimeError(f"crypto/secure_store indisponiveis: {e}") from e

    p = Path(path)
    if p.exists():
        try:
            data = load_node_identity(password, str(p))
            if data and data.get("sk"):
                priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
                pub = priv.public_key().public_bytes_raw().hex()
                return priv, pub
        except Exception as e:
            print(f"[identity] falha carregando {p}: {e} — recriando")

    priv = Ed25519PrivateKey.generate()
    sk_hex = priv.private_bytes_raw().hex()
    pub = priv.public_key().public_bytes_raw().hex()
    try:
        save_node_identity({"sk": sk_hex, "pub": pub, "created_at": int(time.time())}, password, str(p))
    except Exception as e:
        print(f"[identity] nao consegui salvar {p}: {e}")
    return priv, pub

def _load_wallet(password: str):
    try:
        from secure_store import load_current_wallet
        from wallet import Wallet
    except Exception:
        return None, "", ""

    try:
        w = load_current_wallet(password)
    except Exception:
        return None, "", ""

    if not w or not w.get("ok"):
        return None, "", ""

    sk_hex = w.get("private_key") or w.get("priv_hex") or ""
    if not sk_hex:
        return None, "", ""

    try:
        wallet_obj = Wallet(private_key_hex=sk_hex)
        return wallet_obj, wallet_obj.address, wallet_obj.pub_hex
    except Exception:
        return None, "", ""

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
    log.info("=" * 64)
    log.info(" BRN Node v%s (build %s) is_origin=%s", VERSION, BUILD_DATE, is_origin)
    log.info("=" * 64)

    try:
        import port_guard
        port_guard.check_brn_ports(checar_explorer=not args.no_explorer)
    except Exception: pass

    password = (
        os.environ.get("BRN_NODE_PASSWORD", "").strip()
        or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
    )

    # 1. Identity (Ed25519 para P2P)
    node_id_priv, node_id_pub = None, ""
    if password:
        try:
            node_id_priv, node_id_pub = _load_or_create_node_identity(password)
            log.info("[identity] carregada pub=%s…", node_id_pub[:16])
        except Exception as e:
            log.warning("[identity] falha: %s", e)

    # 2. Blockchain
    try:
        from blockchain import Blockchain
        chain = Blockchain(db_path, auto_genesis=False)
        log.info("[chain] aberta height=%d", chain.db.height())
    except Exception as e:
        log.error("[chain] fatal: %s", e)
        return 2

    # 3. Trust Network
    trust = None
    try:
        from trust_network import TrustNetwork
        trust = TrustNetwork("trust_network.json", is_origin=is_origin, node_sk=node_id_priv)
        chain.set_trust_net(trust)
        log.info("[trust] carregado")
    except Exception as e:
        log.warning("[trust] init falhou: %s", e)

    # 4. Carteira (Wallet) e SK do Validador
    wallet_obj, wallet_addr, wallet_pub = (None, "", "")
    if password:
        wallet_obj, wallet_addr, wallet_pub = _load_wallet(password)
    if wallet_obj is not None:
        try:
            chain.set_local_validator_sk(wallet_obj)
            log.info("[wallet] SK ativa instalada addr=%s pub=%s…", wallet_addr[:24], wallet_pub[:16])
        except Exception as e:
            log.warning("[wallet] set_local_validator_sk falhou: %s", e)

    # 5. P2P
    p2p = None
    if not args.no_p2p:
        try:
            from p2p_unified import P2PManager
            p2p = P2PManager(chain, node_id_priv=node_id_priv, tcp_port=p2p_port)
            chain.set_p2p(p2p)
            p2p.start()
            log.info("[p2p] iniciado na porta %d", p2p_port)
        except Exception as e:
            log.error("[p2p] init falhou: %s", e)

    # 6. Sync Manager
    sync_mgr = None
    if p2p:
        try:
            from sync_manager import ChainSyncManager
            sync_mgr = ChainSyncManager(chain, chain.db, p2p)
            sync_mgr.start_sync_loop()
            log.info("[sync] ChainSyncManager iniciado")
        except Exception as e: 
            log.warning("[sync] init falhou: %s", e)

    # 7. MULTI-SIG E BROADCAST DE BLOCOS
    if p2p and wallet_obj:
        try:
            from brn_multisig import ensure_multisig
            ensure_multisig(chain, wallet_obj, p2p, trust=trust, logger=log)
            log.info("[MULTISIG] Coletor de assinaturas configurado")
        except Exception as e:
            log.warning("[MULTISIG] Falha: %s", e)

        try:
            chain.on_block_mined = p2p.broadcast_block
            log.info("[P2P] Broadcast de blocos ativado (propagação)")
        except Exception as e:
            log.warning("[P2P] Falha ao configurar broadcast: %s", e)

    # 7.2 PBFT (Consenso Bizantino)
    if os.environ.get("BRN_PBFT_ENABLED") == "1" and wallet_obj is not None and p2p is not None:
        try:
            from pbft_consensus import PBFTConsensus
            from p2p_unified import P2PClient
            
            def _pbft_send(peer_addr, msg):
                ip, port_s = peer_addr.rsplit(":", 1)
                resp, _ = P2PClient.send_message(ip, int(port_s), msg, timeout=5.0)
                return resp
                
            pbft = PBFTConsensus(chain, wallet_obj, _pbft_send)
            chain.set_pbft(pbft)
            
            if hasattr(p2p, "server") and p2p.server:
                p2p.server.set_pbft(pbft)
            log.info("[PBFT] Motor de consenso bizantino ativado")
        except Exception as e:
            log.warning("[PBFT] Falha ao iniciar motor: %s", e)

    # 7.3 Trust Daemon
    if trust:
        try:
            from trust_daemon import start_daemon
            if not hasattr(chain, "_stop_trust_daemon"):
                chain._stop_trust_daemon = threading.Event()
            
            start_daemon(
                chain=chain, 
                trust=trust, 
                p2p=p2p, 
                password=password, 
                is_origin=is_origin, 
                stop_event=chain._stop_trust_daemon
            )
            log.info("[TrustDaemon] Auto-gerenciamento da rede ativado.")
        except Exception as e:
            log.warning("[TrustDaemon] Falha ao iniciar: %s", e)

    # 8. Pool Manager (somente Origem)
    pool = None
    if is_origin:
        try:
            from mining_pool import PoolManager
            pool = PoolManager(chain)
            log.info("[pool] PoolManager ativo (origin)")
        except Exception as e:
            log.warning("[pool] init falhou: %s", e)

    # 8.2 [NOVO] BTC Watcher (L2 Bridge)
    btc_watcher = None
    if is_origin:
        try:
            from btc_watcher import get_watcher
            btc_watcher = get_watcher(chain.db, chain)
            btc_watcher.start()
            log.info("[btc_watcher] Monitor de depósitos BTC iniciado")
        except Exception as e:
            log.warning("[btc_watcher] init falhou: %s", e)

    # 9. Web Server
    try:
        import server as srv
        srv.set_chain(chain)
        if p2p: srv.set_p2p_manager(p2p)
        if pool: srv.set_pool_manager(pool)
        if sync_mgr: srv.set_sync_manager(sync_mgr)
        if trust: srv.set_trust_network(trust)
        
        def _run_web():
            srv.app.run(host=os.environ.get("BRN_WEB_HOST", "127.0.0.1"), port=web_port, threaded=True, use_reloader=False)
        threading.Thread(target=_run_web, daemon=True, name="HTTP").start()
        log.info("[server] HTTP ativo na porta %d", web_port)
    except Exception as e:
        log.error("[server] falhou: %s", e)

    # 10. Explorer
    if not args.no_explorer:
        try:
            import explorer as exp
            if hasattr(exp, "set_chain"): exp.set_chain(chain)
            def _run_exp():
                exp.app.run(host="0.0.0.0", port=int(os.environ.get("BRN_EXPLORER_PORT", "8080")), threaded=True, use_reloader=False)
            threading.Thread(target=_run_exp, daemon=True, name="EXP").start()
            log.info("[explorer] ativo na porta 8080")
        except Exception: pass

    # 11. GUI
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
        log.info("[shutdown] encerrando...")
        if hasattr(chain, "_stop_trust_daemon"):
            chain._stop_trust_daemon.set()
        if btc_watcher:
            try: btc_watcher.stop()
            except Exception: pass
        try: chain.db.close()
        except Exception: pass
        os._exit(0)
    
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        _shutdown()

    return 0

if __name__ == "__main__":
    sys.exit(main())