"""main.py — Entrypoint do nó BRN (v13.8.3)
================================================================
v13.8.3 (2026-11-04):
  [FIX] PoolClient: passa a wallet_pub para o construtor (exigido pela classe).
  [FIX] PBFT: passa o wallet_obj (que tem .pub_hex) em vez do node_id_priv.
v13.8.2 (2026-11-04):
  [FIX] Pool: corrigido nome da classe de 'MiningPool' para 'PoolManager'.
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
    from version import VERSION, BUILD_DATE, GITHUB_USER, GITHUB_REPO
except ImportError:
    VERSION     = "13.8.3"
    BUILD_DATE  = "2026-11-04"
    GITHUB_USER = "brunoldo2312"
    GITHUB_REPO = "LRN-L1-L2"


def parse_args():
    p = argparse.ArgumentParser(
        prog="main.py",
        description=f"BRN Node v{VERSION} (build {BUILD_DATE})",
    )
    p.add_argument("--headless", action="store_true", help="sem GUI (só servidor + P2P)")
    p.add_argument("--client-mode", action="store_true", help="nó cliente (BRN_IS_ORIGIN=0, sem minerar)")
    p.add_argument("--origem", action="store_true", help="força BRN_IS_ORIGIN=1")
    p.add_argument("--no-pool-client", action="store_true", help="não inicia PoolClient")
    p.add_argument("--no-p2p", action="store_true", help="desliga P2P (debug isolado)")
    p.add_argument("--no-explorer", action="store_true", help="não inicia Explorer (:8080)")
    p.add_argument("--db", metavar="PATH", default=None, help="caminho do DB SQLite")
    p.add_argument("--web-port", type=int, default=None, help="porta HTTP")
    p.add_argument("--p2p-port", type=int, default=None, help="porta P2P")
    p.add_argument("--version", action="store_true", help="mostra versão e sai")
    p.add_argument("--skip-verify", action="store_true", help="pula verificações internas")
    return p.parse_args()


def _load_env_file():
    env_file = BASE_DIR / "brn_network.env"
    if not env_file.exists():
        return 0
    loaded = 0
    try:
        text = env_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return 0
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
            loaded += 1
    return loaded


def _setup_logging(level: str = "INFO", log_file: str = ""):
    try:
        from brn_logger import setup_logger, get_logger
        setup_logger(level=level, log_file=log_file or None, quiet=False)
        return get_logger("main")
    except Exception:
        logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        return logging.getLogger("main")


def _load_config():
    try:
        from brn_config import Config
        return Config("config.json")
    except Exception:
        class _Fallback:
            def __init__(self): self.data = {}
            def get(self, k, d=None): return os.environ.get(k, d)
            def __getitem__(self, k): return os.environ[k]
            def __contains__(self, k): return k in os.environ
            def apply_to_env(self): pass
        return _Fallback()


def load_or_create_node_identity(password: str, path: str = "node_identity.enc"):
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
                logging.getLogger("main").info("[identity] carregada pub=%s…", pub[:16])
                return priv, pub
        except Exception as e:
            logging.getLogger("main").warning("[identity] falha carregando %s: %s — recriando", p, e)

    priv = Ed25519PrivateKey.generate()
    sk_hex = priv.private_bytes_raw().hex()
    pub = priv.public_key().public_bytes_raw().hex()
    try:
        save_node_identity({"sk": sk_hex, "pub": pub, "created_at": int(time.time())}, password, str(p))
        logging.getLogger("main").warning("[identity] NOVA identidade criada pub=%s… (arquivo %s)", pub[:16], p.name)
    except Exception as e:
        logging.getLogger("main").error("[identity] nao consegui salvar %s: %s", p, e)
    return priv, pub


def _load_wallet(password: str):
    try:
        from secure_store import load_current_wallet
        from wallet import Wallet
    except Exception as e:
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


def _setup_trust(chain, node_id_priv, is_origin: bool):
    try:
        from trust_network import TrustNetwork
        tn = TrustNetwork("trust_network.json", is_origin=is_origin, node_sk=node_id_priv)
        chain.set_trust_net(tn)
        return tn
    except Exception as e:
        logging.getLogger("main").warning("[trust] init falhou: %s", e)
        return None


def _setup_p2p(chain, node_id_priv, p2p_port: int, enable_upnp: bool = True):
    try:
        from p2p_unified import P2PManager
        p2p = P2PManager(chain, node_id_priv=node_id_priv, tcp_port=p2p_port, enable_upnp=enable_upnp)
        chain.set_p2p(p2p)
        return p2p
    except Exception as e:
        logging.getLogger("main").error("[p2p] init falhou: %s", e)
        return None


def _setup_sync(chain, p2p):
    try:
        from sync_manager import ChainSyncManager
        return ChainSyncManager(chain, chain.db, p2p)
    except Exception as e:
        logging.getLogger("main").error("[sync] init falhou: %s", e)
        return None


def _setup_pool(chain, is_origin: bool):
    if not is_origin:
        return None
    try:
        from mining_pool import PoolManager
        pool = PoolManager(chain)
        logging.getLogger("main").info("[pool] PoolManager ativo (origin)")
        return pool
    except Exception as e:
        logging.getLogger("main").warning("[pool] init falhou: %s", e)
        return None


def _setup_pool_client(chain, disabled: bool, is_origin: bool, wallet_pub: str = ""):
    if disabled or is_origin:
        return None
    try:
        from mining_pool import PoolClient
    except Exception as e:
        logging.getLogger("main").debug("[pool_client] indisponivel: %s", e)
        return None
    try:
        # [FIX v13.8.3] Passa a wallet_pub para o construtor
        pc = PoolClient(chain, wallet_pub)
        logging.getLogger("main").info("[pool_client] PoolClient ativo")
        return pc
    except TypeError:
        # Fallback caso o PoolClient não aceite o segundo argumento
        try:
            pc = PoolClient(chain)
            return pc
        except Exception as e:
            logging.getLogger("main").warning("[pool_client] init fallback falhou: %s", e)
            return None
    except Exception as e:
        logging.getLogger("main").warning("[pool_client] init falhou: %s", e)
        return None


def _setup_pbft(chain, wallet_obj, p2p):
    if os.environ.get("BRN_PBFT_ENABLED", "0") != "1":
        logging.getLogger("main").info("[PBFT] desligado (multi-sig continua ativo)")
        return None, "disabled"

    # [FIX v13.8.3] Exige wallet_obj (tem .pub_hex e .sign())
    if wallet_obj is None:
        logging.getLogger("main").warning("[PBFT] wallet_obj ausente — sem SK de validador")
        return None, "no_wallet"

    if p2p is None:
        logging.getLogger("main").warning("[PBFT] P2P ausente")
        return None, "no_p2p"

    try:
        from pbft_consensus import PBFTConsensus
        from p2p_unified import P2PClient
    except Exception as e:
        logging.getLogger("main").error("[PBFT] imports falharam: %s", e)
        return None, "import"

    try:
        def _pbft_send(peer_addr: str, msg: dict):
            try:
                ip, port_s = peer_addr.rsplit(":", 1)
                resp, _ = P2PClient.send_message(ip, int(port_s), msg, timeout=15.0)
                return resp
            except Exception:
                return None

        # [FIX v13.8.3] Passa o wallet_obj no lugar do node_id_priv
        pbft = PBFTConsensus(chain, wallet_obj, _pbft_send)
        chain.set_pbft(pbft)

        try:
            if hasattr(p2p, "server") and p2p.server is not None:
                p2p.server.set_pbft(pbft)
        except Exception:
            pass

        st = pbft.status()
        logging.getLogger("main").info(
            "[PBFT] ATIVO — validators=%d f=%d quorum=%d view=%d seq=%d",
            st.get("validators", 0), st.get("faults_tolerated_f", 0),
            st.get("quorum_2f_plus_1", 0), st.get("view", 0), st.get("seq", 0)
        )
        return pbft, "ok"
    except Exception as e:
        logging.getLogger("main").error("[PBFT] init falhou: %s", e)
        return None, "init"


def _start_web_server(chain, p2p, pool, sync, trust, pool_client, web_port: int):
    try:
        import server as srv
        srv.set_chain(chain)
        if p2p is not None: srv.set_p2p_manager(p2p)
        if pool is not None: srv.set_pool_manager(pool)
        if sync is not None: srv.set_sync_manager(sync)
        if trust is not None: srv.set_trust_network(trust)
        if pool_client is not None: srv.set_pool_client_local(pool_client)

        def _run():
            try:
                srv.app.run(host=os.environ.get("BRN_WEB_HOST", "127.0.0.1"), port=web_port, debug=False, threaded=True, use_reloader=False)
            except Exception as e:
                logging.getLogger("main").error("[server] crash: %s", e)

        t = threading.Thread(target=_run, daemon=True, name="HTTP-Server")
        t.start()
        return t
    except Exception as e:
        logging.getLogger("main").error("[server] import falhou: %s", e)
        return None


def _start_gui():
    try:
        import webview
        from app_wallet_v3 import WalletApi
        api = WalletApi()

        possible_htmls = ["index_wallet.html", "wallet.html", "carteira.html"]
        wallet_path = None
        for name in possible_htmls:
            p = BASE_DIR / name
            if p.exists():
                wallet_path = p
                break
        
        if not wallet_path:
            wallet_path = BASE_DIR / "panel.html"
            
        url = wallet_path.as_uri() if wallet_path.exists() else "about:blank"
        logging.getLogger("main").info(f"[gui] Abrindo interface: {wallet_path.name}")
        
        webview.create_window("BRN Wallet", url=url, width=1100, height=780, js_api=api)
        webview.start()
        return True
    except Exception as e:
        logging.getLogger("main").warning("[gui] falha: %s", e)
        return False


def main() -> int:
    args = parse_args()

    if args.version:
        print(f"BRN Node v{VERSION} (build {BUILD_DATE})")
        return 0

    if args.skip_verify:
        print("[main] --skip-verify: assumindo que o .bat já fez as verificações")

    loaded = _load_env_file()

    if args.client_mode: os.environ["BRN_IS_ORIGIN"] = "0"
    if args.origem:      os.environ["BRN_IS_ORIGIN"] = "1"
    if args.web_port is not None: os.environ["BRN_WEB_PORT"] = str(args.web_port)
    if args.p2p_port is not None: os.environ["BRN_P2P_PORT"] = str(args.p2p_port)

    is_origin = os.environ.get("BRN_IS_ORIGIN", "0") == "1"
    os.environ.setdefault("BRN_IS_ORIGIN", "1" if is_origin else "0")

    web_port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    p2p_port = int(os.environ.get("BRN_P2P_PORT", "6001"))
    db_path  = args.db or os.environ.get("BRN_DB", "brn_v2_chain.db")

    cfg = _load_config()
    try: cfg.apply_to_env()
    except Exception: pass

    log_level = str(cfg.get("log_level", "INFO")).upper()
    log = _setup_logging(level=log_level)

    log.info("=" * 64)
    log.info(" BRN Node v%s (build %s)", VERSION, BUILD_DATE)
    log.info(" base=%s env_loaded=%d is_origin=%s", BASE_DIR, loaded, is_origin)
    log.info("=" * 64)

    try:
        import port_guard
        port_guard.check_brn_ports(checar_explorer=not args.no_explorer)
    except Exception:
        pass

    password = (os.environ.get("BRN_NODE_PASSWORD", "").strip() or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip())
    node_id_priv, node_id_pub = (None, "")
    if password:
        try: node_id_priv, node_id_pub = load_or_create_node_identity(password)
        except Exception as e: log.error("[identity] fatal: %s", e)

    try:
        from blockchain import Blockchain
        chain = Blockchain(db_path, auto_genesis=False)
        log.info("[chain] aberta height=%d", chain.db.height())
    except Exception as e:
        log.error("[chain] fatal: %s", e)
        return 2

    trust = _setup_trust(chain, node_id_priv, is_origin)

    wallet_obj, wallet_addr, wallet_pub = (None, "", "")
    if password:
        wallet_obj, wallet_addr, wallet_pub = _load_wallet(password)
    if wallet_obj is not None:
        try:
            chain.set_local_validator_sk(wallet_obj)
            log.info("[wallet] SK ativa instalada addr=%s", wallet_addr[:24])
        except Exception as e:
            log.warning("[wallet] set_local_validator_sk falhou: %s", e)

    p2p = None
    if not args.no_p2p:
        p2p = _setup_p2p(chain, node_id_priv, p2p_port)
        if p2p:
            try:
                p2p.start()
                log.info("[p2p] iniciado na porta %d", p2p_port)
            except Exception as e:
                log.error("[p2p] start falhou: %s", e)
                p2p = None

    sync_mgr = _setup_sync(chain, p2p) if p2p else None
    if sync_mgr:
        try: sync_mgr.start_sync_loop()
        except Exception: pass

    pool = _setup_pool(chain, is_origin)
    
    # [FIX v13.8.3] Passa wallet_pub
    pool_client = _setup_pool_client(chain, args.no_pool_client, is_origin, wallet_pub)

    # [FIX v13.8.3] Passa wallet_obj em vez de node_id_priv
    pbft, pbft_motivo = _setup_pbft(chain, wallet_obj, p2p)

    _start_web_server(chain, p2p, pool, sync_mgr, trust, pool_client, web_port)

    def _shutdown(signum, frame):
        log.info("[shutdown] sinal %s recebido", signum)
        try: chain.db.close()
        except Exception: pass
        os._exit(0)

    signal.signal(signal.SIGINT, _shutdown)

    if not args.headless:
        if _start_gui():
            _shutdown("GUI-close", None)
            return 0

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        _shutdown("KeyboardInterrupt", None)

    return 0

if __name__ == "__main__":
    try: sys.exit(main())
    except KeyboardInterrupt: sys.exit(130)
