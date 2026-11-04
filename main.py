"""main.py — Entrypoint do nó BRN (v13.8.0)
================================================================
v13.8.0 (2026-11-04):
  [NEW] Integração PBFT. Após p2p.start() e chain.set_local_validator_sk(),
        chama _setup_pbft(chain, node_id_priv, p2p) que:
          - cria PBFTConsensus(chain, wallet_sk, send_fn)
          - injeta via chain.set_pbft(pbft)
          - P2PManager.start() injeta no P2PServer via server.set_pbft()
        Ativa só se BRN_PBFT_ENABLED=1.
  [KEEP] Comportamento anterior: headless, client-mode, pool-client,
         GUI, relay, sync, rendezvous, BTC watcher.

⚠️ Este arquivo é uma RECONSTRUÇÃO. O main.py original v13.7.12
   não foi compartilhado. Antes de substituir, faça diff com o seu.

Uso:
    python main.py                       # GUI + nó completo
    python main.py --headless            # sem GUI
    python main.py --client-mode         # nó cliente (sem minerar)
    python main.py --no-pool-client      # não inicia PoolClient
    python main.py --origem              # força BRN_IS_ORIGIN=1
    python main.py --no-p2p              # só servidor HTTP (debug)
    python main.py --no-explorer         # sem Explorer
    python main.py --db X.db             # DB alternativo
    python main.py --version             # versão e sai
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


# ============================================================
# Path / version
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

try:
    from version import VERSION, BUILD_DATE, GITHUB_USER, GITHUB_REPO
except ImportError:
    VERSION     = "13.8.0"
    BUILD_DATE  = "2026-11-04"
    GITHUB_USER = "brunoldo2312"
    GITHUB_REPO = "LRN-L1-L2"


# ============================================================
# Parse args
# ============================================================
def parse_args():
    p = argparse.ArgumentParser(
        prog="main.py",
        description=f"BRN Node v{VERSION} (build {BUILD_DATE})",
    )
    p.add_argument("--headless", action="store_true",
                   help="sem GUI (só servidor + P2P)")
    p.add_argument("--client-mode", action="store_true",
                   help="nó cliente (BRN_IS_ORIGIN=0, sem minerar)")
    p.add_argument("--origem", action="store_true",
                   help="força BRN_IS_ORIGIN=1")
    p.add_argument("--no-pool-client", action="store_true",
                   help="não inicia PoolClient")
    p.add_argument("--no-p2p", action="store_true",
                   help="desliga P2P (debug isolado)")
    p.add_argument("--no-explorer", action="store_true",
                   help="não inicia Explorer (:8080)")
    p.add_argument("--db", metavar="PATH", default=None,
                   help="caminho do DB SQLite (default: brn_v2_chain.db)")
    p.add_argument("--web-port", type=int, default=None,
                   help="porta HTTP (default: BRN_WEB_PORT ou 5000)")
    p.add_argument("--p2p-port", type=int, default=None,
                   help="porta P2P (default: BRN_P2P_PORT ou 6001)")
    p.add_argument("--version", action="store_true",
                   help="mostra versão e sai")
    return p.parse_args()


# ============================================================
# .env loader (compat com formato simples do projeto)
# ============================================================
def _load_env_file():
    env_file = BASE_DIR / "brn_network.env"
    if not env_file.exists():
        return 0
    loaded = 0
    try:
        text = env_file.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"[env] falha lendo {env_file.name}: {e}", file=sys.stderr)
        return 0

    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not k:
            continue
        # não sobrescreve variável já exportada no shell
        if k not in os.environ:
            os.environ[k] = v
            loaded += 1
    return loaded


# ============================================================
# Logging (via brn_logger se disponível)
# ============================================================
def _setup_logging(level: str = "INFO", log_file: str = ""):
    try:
        from brn_logger import setup_logger, get_logger
        setup_logger(level=level, log_file=log_file or None, quiet=False)
        return get_logger("main")
    except Exception:
        logging.basicConfig(
            level=getattr(logging, level, logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
        return logging.getLogger("main")


# ============================================================
# Config (brn_config)
# ============================================================
def _load_config():
    try:
        from brn_config import Config
        return Config("config.json")
    except Exception as e:
        logging.getLogger("main").warning(
            "brn_config indisponivel (%s) — usando defaults", e,
        )
        class _Fallback:
            def __init__(self): self.data = {}
            def get(self, k, d=None): return os.environ.get(k, d)
            def __getitem__(self, k): return os.environ[k]
            def __contains__(self, k): return k in os.environ
        return _Fallback()


# ============================================================
# Node identity (Ed25519 para P2P auth)
# ============================================================
def load_or_create_node_identity(password: str, path: str = "node_identity.enc"):
    """
    Carrega ou cria a identidade Ed25519 do nó (usada por p2p_auth).
    Retorna (Ed25519PrivateKey, pub_hex_str).
    """
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
                priv = Ed25519PrivateKey.from_private_bytes(
                    bytes.fromhex(data["sk"])
                )
                pub = priv.public_key().public_bytes_raw().hex()
                logging.getLogger("main").info(
                    "[identity] carregada pub=%s…", pub[:16],
                )
                return priv, pub
        except Exception as e:
            logging.getLogger("main").warning(
                "[identity] falha carregando %s: %s — recriando", p, e,
            )

    # cria nova
    priv = Ed25519PrivateKey.generate()
    sk_hex = priv.private_bytes_raw().hex()
    pub = priv.public_key().public_bytes_raw().hex()
    try:
        save_node_identity(
            {"sk": sk_hex, "pub": pub, "created_at": int(time.time())},
            password, str(p),
        )
        logging.getLogger("main").warning(
            "[identity] NOVA identidade criada pub=%s… (arquivo %s)",
            pub[:16], p.name,
        )
    except Exception as e:
        logging.getLogger("main").error(
            "[identity] nao consegui salvar %s: %s", p, e,
        )
    return priv, pub


# ============================================================
# Wallet ativa (current_wallet.enc)
# ============================================================
def _load_wallet(password: str):
    """
    Carrega current_wallet.enc e retorna (Wallet, address, pub_hex) ou
    (None, "", "") se ausente.
    """
    try:
        from secure_store import load_current_wallet
        from wallet import Wallet
    except Exception as e:
        logging.getLogger("main").warning(
            "[wallet] modulos indisponiveis: %s", e,
        )
        return None, "", ""

    try:
        w = load_current_wallet(password)
    except Exception as e:
        logging.getLogger("main").warning("[wallet] load falhou: %s", e)
        return None, "", ""

    if not w or not w.get("ok"):
        logging.getLogger("main").info(
            "[wallet] sem current_wallet.enc — nó vai rodar sem SK local",
        )
        return None, "", ""

    sk_hex = w.get("private_key") or w.get("priv_hex") or ""
    if not sk_hex:
        logging.getLogger("main").warning(
            "[wallet] current_wallet.enc sem private_key",
        )
        return None, "", ""

    try:
        wallet_obj = Wallet(private_key_hex=sk_hex)
        return wallet_obj, wallet_obj.address, wallet_obj.pub_hex
    except Exception as e:
        logging.getLogger("main").warning(
            "[wallet] reconstrucao falhou: %s", e,
        )
        return None, "", ""


# ============================================================
# Trust Network
# ============================================================
def _setup_trust(chain, node_id_priv, is_origin: bool):
    try:
        from trust_network import TrustNetwork
    except Exception as e:
        logging.getLogger("main").warning(
            "[trust] indisponivel: %s", e,
        )
        return None

    try:
        tn = TrustNetwork(
            "trust_network.json",
            is_origin=is_origin,
            node_sk=node_id_priv,
        )
        chain.set_trust_net(tn)
        logging.getLogger("main").info(
            "[trust] carregado (%d nos, %d validadores)",
            len(tn.nodes), len(tn.active_validators),
        )
        return tn
    except Exception as e:
        logging.getLogger("main").warning("[trust] init falhou: %s", e)
        return None


# ============================================================
# P2P
# ============================================================
def _setup_p2p(chain, node_id_priv, p2p_port: int, enable_upnp: bool = True):
    try:
        from p2p_unified import P2PManager
    except Exception as e:
        logging.getLogger("main").warning("[p2p] indisponivel: %s", e)
        return None

    try:
        p2p = P2PManager(
            chain,
            node_id_priv=node_id_priv,
            tcp_port=p2p_port,
            enable_upnp=enable_upnp,
        )
        chain.set_p2p(p2p)
        return p2p
    except Exception as e:
        logging.getLogger("main").error("[p2p] init falhou: %s", e)
        return None


# ============================================================
# Sync Manager
# ============================================================
def _setup_sync(chain, p2p):
    try:
        from sync_manager import ChainSyncManager
    except Exception as e:
        logging.getLogger("main").warning("[sync] indisponivel: %s", e)
        return None
    try:
        sm = ChainSyncManager(chain, chain.db, p2p)
        return sm
    except Exception as e:
        logging.getLogger("main").error("[sync] init falhou: %s", e)
        return None


# ============================================================
# Pool (Origin)
# ============================================================
def _setup_pool(chain, is_origin: bool):
    if not is_origin:
        return None
    try:
        from mining_pool import MiningPool
    except Exception:
        try:
            from pool_server import MiningPool  # type: ignore
        except Exception as e:
            logging.getLogger("main").debug(
                "[pool] indisponivel: %s", e,
            )
            return None
    try:
        pool = MiningPool(chain)
        logging.getLogger("main").info("[pool] MiningPool ativo (origin)")
        return pool
    except Exception as e:
        logging.getLogger("main").warning("[pool] init falhou: %s", e)
        return None


# ============================================================
# PoolClient (Cliente)
# ============================================================
def _setup_pool_client(chain, disabled: bool, is_origin: bool):
    if disabled or is_origin:
        return None
    try:
        from miner_pool_client import PoolClient
    except Exception as e:
        logging.getLogger("main").debug(
            "[pool_client] indisponivel: %s", e,
        )
        return None
    try:
        pc = PoolClient(chain)
        logging.getLogger("main").info("[pool_client] PoolClient ativo")
        return pc
    except Exception as e:
        logging.getLogger("main").warning(
            "[pool_client] init falhou: %s", e,
        )
        return None


# ============================================================
# Rendezvous
# ============================================================
def _setup_rendezvous(chain, node_uuid: str, p2p, stop_flag):
    try:
        from rendezvous_discovery import RendezvousDiscovery
    except Exception as e:
        logging.getLogger("main").debug(
            "[rdv] indisponivel: %s", e,
        )
        return None

    try:
        def _on_peer(ip, port):
            try: p2p.add_peer(ip, port)
            except Exception: pass

        rdv = RendezvousDiscovery(
            tcp_port=p2p.tcp_port if p2p else 6001,
            node_uuid=node_uuid,
            on_peer=_on_peer,
            running_flag=lambda: not stop_flag.is_set(),
            blockchain=chain,
        )
        ok = rdv.start()
        if ok:
            logging.getLogger("main").info("[rdv] ativo")
        return rdv
    except Exception as e:
        logging.getLogger("main").warning("[rdv] init falhou: %s", e)
        return None


# ============================================================
# Relay (opcional)
# ============================================================
def _setup_relay(chain):
    """
    Relay TCP (p2p_relay) — opcional. Só ativa se BRN_RELAY_ENABLED=1.
    verify.py indica que main exporta _setup_relay.
    """
    if os.environ.get("BRN_RELAY_ENABLED", "0") != "1":
        return None
    try:
        from p2p_relay import P2PRelay
    except Exception as e:
        logging.getLogger("main").debug("[relay] indisponivel: %s", e)
        return None
    try:
        rly = P2PRelay()
        logging.getLogger("main").info("[relay] pronto (nao iniciado)")
        return rly
    except Exception as e:
        logging.getLogger("main").warning("[relay] init falhou: %s", e)
        return None


# ============================================================
# [v13.8.0] PBFT — motor de consenso bizantino
# ============================================================
def _setup_pbft(chain, node_id_priv, p2p):
    """
    Injeta PBFTConsensus se BRN_PBFT_ENABLED=1.

    Pré-requisitos:
      - chain.set_local_validator_sk(sk) JÁ foi chamado (para
        _get_active_validators retornar a pubkey local)
      - p2p.start() JÁ foi chamado (para discovery.listar_peers()
        funcionar)
      - node_id_priv definido (chave Ed25519 do nó)

    Retorna (pbft ou None, motivo).
    """
    if os.environ.get("BRN_PBFT_ENABLED", "0") != "1":
        logging.getLogger("main").info(
            "[PBFT] desligado (BRN_PBFT_ENABLED!=1) — multi-sig continua ativo",
        )
        return None, "disabled"

    if node_id_priv is None:
        logging.getLogger("main").warning(
            "[PBFT] node_id_priv ausente — PBFT NAO será ativado",
        )
        return None, "no_priv"

    if p2p is None:
        logging.getLogger("main").warning(
            "[PBFT] P2P ausente — PBFT NAO será ativado",
        )
        return None, "no_p2p"

    try:
        from pbft_consensus import PBFTConsensus
        from p2p_unified import P2PClient
    except Exception as e:
        logging.getLogger("main").error(
            "[PBFT] imports falharam: %s — PBFT desligado", e,
        )
        return None, "import"

    try:
        def _pbft_send(peer_addr: str, msg: dict):
            try:
                ip, port_s = peer_addr.rsplit(":", 1)
                resp, _ = P2PClient.send_message(
                    ip, int(port_s), msg, timeout=15.0,
                )
                return resp
            except Exception as e:
                logging.getLogger("pbft").debug(
                    "[PBFT] send %s falhou: %s", peer_addr, e,
                )
                return None

        pbft = PBFTConsensus(chain, node_id_priv, _pbft_send)
        chain.set_pbft(pbft)

        # O P2PManager.start() lê chain._pbft e injeta no P2PServer.
        # Se p2p já foi startado antes de chegar aqui, forçamos também.
        try:
            if hasattr(p2p, "server") and p2p.server is not None:
                p2p.server.set_pbft(pbft)
        except Exception:
            pass

        st = pbft.status()
        logging.getLogger("main").info(
            "[PBFT] ATIVO — validators=%d f=%d quorum=%d view=%d seq=%d",
            st.get("validators", 0),
            st.get("faults_tolerated_f", 0),
            st.get("quorum_2f_plus_1", 0),
            st.get("view", 0),
            st.get("seq", 0),
        )
        return pbft, "ok"
    except Exception as e:
        logging.getLogger("main").error(
            "[PBFT] init falhou: %s", e, exc_info=True,
        )
        return None, "init"


# ============================================================
# BTC Watcher (opcional)
# ============================================================
def _setup_btc_watcher(chain):
    if os.environ.get("BRN_BTC_ENABLED", "0") != "1":
        return None
    try:
        from btc_watcher import get_watcher
    except Exception as e:
        logging.getLogger("main").debug(
            "[btc] indisponivel: %s", e,
        )
        return None
    try:
        w = get_watcher(chain.db, chain)
        w.start()
        logging.getLogger("main").info("[btc] watcher iniciado")
        return w
    except Exception as e:
        logging.getLogger("main").warning(
            "[btc] init falhou: %s", e,
        )
        return None


# ============================================================
# Flask server (thread)
# ============================================================
def _start_web_server(chain, p2p, pool, sync, trust, pool_client,
                      web_port: int):
    try:
        import server as srv
    except Exception as e:
        logging.getLogger("main").error(
            "[server] import falhou: %s", e,
        )
        return None

    srv.set_chain(chain)
    if p2p is not None:
        srv.set_p2p_manager(p2p)
    if pool is not None:
        srv.set_pool_manager(pool)
    if sync is not None:
        srv.set_sync_manager(sync)
    if trust is not None:
        srv.set_trust_network(trust)
    if pool_client is not None:
        srv.set_pool_client_local(pool_client)

    def _run():
        try:
            srv.app.run(
                host=os.environ.get("BRN_WEB_HOST", "127.0.0.1"),
                port=web_port,
                debug=False,
                threaded=True,
                use_reloader=False,
            )
        except Exception as e:
            logging.getLogger("main").error(
                "[server] crash: %s", e, exc_info=True,
            )

    t = threading.Thread(target=_run, daemon=True, name="HTTP-Server")
    t.start()
    logging.getLogger("main").info(
        "[server] HTTP em %s:%d",
        os.environ.get("BRN_WEB_HOST", "127.0.0.1"), web_port,
    )
    return t


# ============================================================
# Explorer (thread)
# ============================================================
def _start_explorer(chain):
    try:
        import explorer as exp
    except Exception as e:
        logging.getLogger("main").debug(
            "[explorer] indisponivel: %s", e,
        )
        return None

    # injeta chain se explorer tiver set_chain
    try:
        if hasattr(exp, "set_chain"):
            exp.set_chain(chain)
    except Exception:
        pass

    def _run():
        try:
            port = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
            exp.app.run(
                host="0.0.0.0", port=port,
                debug=False, threaded=True, use_reloader=False,
            )
        except Exception as e:
            logging.getLogger("main").debug(
                "[explorer] crash: %s", e,
            )

    t = threading.Thread(target=_run, daemon=True, name="Explorer")
    t.start()
    logging.getLogger("main").info(
        "[explorer] http://0.0.0.0:%d",
        int(os.environ.get("BRN_EXPLORER_PORT", "8080")),
    )
    return t


# ============================================================
# GUI (pywebview)
# ============================================================
def _start_gui():
    try:
        import webview
    except Exception as e:
        logging.getLogger("main").warning(
            "[gui] pywebview indisponivel: %s — use --headless", e,
        )
        return False

    try:
        from app_wallet_v3 import WalletApi
        api = WalletApi()
    except Exception as e:
        logging.getLogger("main").warning(
            "[gui] WalletApi indisponivel: %s", e,
        )
        api = None

    try:
        panel = str(BASE_DIR / "panel.html")
        url = Path(panel).as_uri() if Path(panel).exists() else "about:blank"
        window = webview.create_window(
            "BRN Wallet",
            url=url,
            width=1100, height=780,
            js_api=api,
        )
        webview.start()
        return True
    except Exception as e:
        logging.getLogger("main").warning("[gui] falha: %s", e)
        return False


# ============================================================
# MAIN
# ============================================================
def main() -> int:
    args = parse_args()

    if args.version:
        print(f"BRN Node v{VERSION} (build {BUILD_DATE})")
        return 0

    # ─── 1. .env ─────────────────────────────────────────────
    loaded = _load_env_file()

    # ─── 2. flags CLI sobrepõem env ──────────────────────────
    if args.client_mode:
        os.environ["BRN_IS_ORIGIN"] = "0"
    if args.origem:
        os.environ["BRN_IS_ORIGIN"] = "1"
    if args.web_port is not None:
        os.environ["BRN_WEB_PORT"] = str(args.web_port)
    if args.p2p_port is not None:
        os.environ["BRN_P2P_PORT"] = str(args.p2p_port)

    is_origin = os.environ.get("BRN_IS_ORIGIN", "0") == "1"
    os.environ.setdefault("BRN_IS_ORIGIN", "1" if is_origin else "0")

    web_port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    p2p_port = int(os.environ.get("BRN_P2P_PORT", "6001"))
    db_path  = args.db or os.environ.get("BRN_DB", "brn_v2_chain.db")

    # ─── 3. Config ───────────────────────────────────────────
    cfg = _load_config()
    try: cfg.apply_to_env()
    except Exception: pass

    # ─── 4. Logging ──────────────────────────────────────────
    log_level = str(cfg.get("log_level", "INFO")).upper()
    log_file  = str(cfg.get("log_file", "") or "")
    log = _setup_logging(level=log_level, log_file=log_file)

    log.info("=" * 64)
    log.info(" BRN Node v%s (build %s)", VERSION, BUILD_DATE)
    log.info(" base=%s env_loaded=%d is_origin=%s", BASE_DIR, loaded, is_origin)
    log.info(" web_port=%d p2p_port=%d db=%s", web_port, p2p_port, db_path)
    log.info("=" * 64)

    # ─── 5. Port guard (DEVE vir antes de qualquer bind) ─────
    try:
        import port_guard
        port_guard.check_brn_ports(checar_explorer=not args.no_explorer)
    except SystemExit:
        return 1
    except Exception as e:
        log.warning("[port_guard] falhou (seguindo mesmo assim): %s", e)

    # ─── 6. Senha para decifrar arquivos ─────────────────────
    password = (
        os.environ.get("BRN_NODE_PASSWORD", "").strip()
        or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
    )
    if not password:
        log.warning(
            "BRN_NODE_PASSWORD / BRN_WALLET_SESSION_PASS vazias — "
            "identity e wallet NAO serao carregadas",
        )

    # ─── 7. Node identity ────────────────────────────────────
    node_id_priv = None
    node_id_pub = ""
    if password:
        try:
            node_id_priv, node_id_pub = load_or_create_node_identity(password)
        except Exception as e:
            log.error("[identity] fatal: %s", e)

    # ─── 8. Blockchain ───────────────────────────────────────
    try:
        from blockchain import Blockchain
        chain = Blockchain(db_path, auto_genesis=False)
        log.info("[chain] aberta height=%d tip=%s…",
                 chain.db.height(),
                 (chain.db.tip_hash() or "")[:16])
    except Exception as e:
        log.error("[chain] fatal: %s", e, exc_info=True)
        return 2

    # ─── 9. Trust Network ────────────────────────────────────
    trust = _setup_trust(chain, node_id_priv, is_origin)

    # ─── 10. Wallet ativa → SK do validador ──────────────────
    wallet_obj, wallet_addr, wallet_pub = (None, "", "")
    if password:
        wallet_obj, wallet_addr, wallet_pub = _load_wallet(password)
    if wallet_obj is not None:
        try:
            chain.set_local_validator_sk(wallet_obj)
            log.info(
                "[wallet] SK ativa instalada addr=%s pub=%s…",
                wallet_addr[:24], wallet_pub[:16],
            )
        except Exception as e:
            log.warning("[wallet] set_local_validator_sk falhou: %s", e)

    # ─── 11. P2P ─────────────────────────────────────────────
    p2p = None
    if not args.no_p2p:
        p2p = _setup_p2p(chain, node_id_priv, p2p_port,
                         enable_upnp=bool(cfg.get("upnp", True)))
        if p2p is not None:
            try:
                p2p.start()
                log.info("[p2p] iniciado na porta %d", p2p_port)
            except Exception as e:
                log.error("[p2p] start falhou: %s", e)
                p2p = None

    # ─── 12. Sync Manager ────────────────────────────────────
    sync_mgr = _setup_sync(chain, p2p) if p2p else None
    if sync_mgr is not None:
        try:
            sync_mgr.start_sync_loop()
            log.info("[sync] ChainSyncManager iniciado")
        except Exception as e:
            log.warning("[sync] start falhou: %s", e)

    # ─── 13. Pool (Origin) / PoolClient (Cliente) ────────────
    pool = _setup_pool(chain, is_origin)
    pool_client = _setup_pool_client(chain, args.no_pool_client, is_origin)

    # ─── 14. Relay (opcional) ────────────────────────────────
    relay = _setup_relay(chain)

    # ─── 15. Rendezvous ──────────────────────────────────────
    stop_flag = threading.Event()
    rdv = None
    if p2p is not None and node_id_pub:
        rdv = _setup_rendezvous(chain, node_id_pub, p2p, stop_flag)

    # ─── 16. [PBFT] Motor de consenso bizantino ──────────────
    # IMPORTANTE: só depois de chain.set_local_validator_sk() e
    # p2p.start() — o motor lê _get_active_validators() e envia
    # mensagens via discovery.listar_peers().
    pbft, pbft_motivo = _setup_pbft(chain, node_id_priv, p2p)
    if pbft is None and os.environ.get("BRN_PBFT_ENABLED") == "1":
        log.warning("[PBFT] NAO ativado (motivo=%s) — multi-sig continua", pbft_motivo)

    # ─── 17. BTC Watcher (opcional) ──────────────────────────
    btc_watcher = _setup_btc_watcher(chain)

    # ─── 18. HTTP Server ─────────────────────────────────────
    _start_web_server(
        chain, p2p, pool, sync_mgr, trust, pool_client, web_port,
    )

    # ─── 19. Explorer ────────────────────────────────────────
    if not args.no_explorer:
        _start_explorer(chain)

    # ─── 20. Miner (automático se env) ───────────────────────
    auto_mine = os.environ.get("BRN_AUTO_MINE", "0") == "1"
    if auto_mine and wallet_addr and wallet_pub:
        try:
            from miner_loop import get_miner
            m = get_miner(chain)
            ok, msg = m.start(wallet_addr, wallet_pub,
                              is_origin=is_origin)
            log.info("[miner] start auto → ok=%s msg=%s", ok, msg)
        except Exception as e:
            log.warning("[miner] auto start falhou: %s", e)

    # ─── 21. Signal handler (Ctrl+C graceful) ────────────────
    def _shutdown(signum, frame):
        log.info("[shutdown] sinal %s recebido", signum)
        stop_flag.set()
        for name, obj in (
            ("sync", sync_mgr), ("rdv", rdv),
            ("btc", btc_watcher), ("p2p", p2p),
        ):
            if obj is None: continue
            try:
                obj.stop() if hasattr(obj, "stop") else None
            except Exception:
                pass
        try: chain.db.close()
        except Exception: pass
        log.info("[shutdown] bye")
        os._exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except AttributeError:
        pass

    # ─── 22. GUI ou headless ─────────────────────────────────
    if not args.headless:
        log.info("[gui] abrindo pywebview...")
        if _start_gui():
            _shutdown("GUI-close", None)
            return 0
        log.info("[gui] cai para headless")

    log.info("=" * 64)
    log.info(" NÓ ATIVO — Ctrl+C para encerrar")
    log.info("   API      : http://%s:%d",
             os.environ.get("BRN_WEB_HOST", "127.0.0.1"), web_port)
    log.info("   Explorer : http://0.0.0.0:%d",
             int(os.environ.get("BRN_EXPLORER_PORT", "8080")))
    log.info("   P2P      : :%d", p2p_port)
    log.info("   PBFT     : %s", "ATIVO" if pbft else "desligado")
    log.info("=" * 64)

    # loop principal
    try:
        while not stop_flag.wait(1.0):
            pass
    except KeyboardInterrupt:
        _shutdown("KeyboardInterrupt", None)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
