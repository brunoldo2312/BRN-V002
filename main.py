"""main.py — Entrypoint do nó BRN (v13.9.3)
================================================================
v13.9.3 (2026-11-09) — CARTEIRA SÓ PELA GUI:
  [BREAKING] REMOVIDO _load_wallet() do boot. O nó NÃO carrega
        carteira do .env / current_wallet.enc automaticamente.
        A carteira entra SÓ quando a GUI faz
        POST /api/wallet/set_validator_sk (server.py v9.9.28).
  [BREAKING] REMOVIDO _integrate_wallet_at_boot(). A integração
        acontece via server.py quando o GUI desbloqueia.
  [BREAKING] REMOVIDO _setup_multisig() e _setup_pbft() do boot.
        Quem faz é wallet_integrator.integrate_and_propagate().
  [NEW] _wallet_watchdog_loop(): monitora chain._local_validator_sk
        e chain._sign_collector. NÃO reinstala (não tem como sem
        senha). Só loga e re-sincroniza trust se SK sumir do top5.
  [NEW] _setup_trust_daemon() reescrito para operar SEM wallet_obj.
        Heartbeat usa chain._local_validator_sk se existir, senão
        só remove_ghosts.
  [KEEP] v13.9.2: identidade segura, abortar se corrompida,
        STRICT_MULTISIG-aware no boot (mas não bloqueia mais, só avisa).

FLUXO DE BOOT (v13.9.3):
  1. main.py sobe → nó SEM SK, SEM multisig configurado
  2. Servidor HTTP ativo na porta BRN_WEB_PORT
  3. GUI abre (se não --headless)
  4. Usuário digita senha na GUI
  5. app_wallet_v3.unlock_wallet() → POST /api/wallet/set_validator_sk
  6. server.py → wallet_integrator.integrate_and_propagate()
  7. chain._local_validator_sk setada + _sign_collector configurado
  8. Nó começa a assinar blocos
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
    VERSION = "13.9.3"
    BUILD_DATE = "2026-11-09"


# ============================================================
# ARGS
# ============================================================
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


# ============================================================
# ENV
# ============================================================
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


# ============================================================
# LOGGING
# ============================================================
def _setup_logging(level: str = "INFO"):
    try:
        from brn_logger import setup_logger, get_logger
        setup_logger(level=level, quiet=False)
        return get_logger("main")
    except Exception:
        logging.basicConfig(
            level=getattr(logging, level, logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
        return logging.getLogger("main")


# ============================================================
# IDENTITY
# ============================================================
def _load_or_create_node_identity(password: str, path: str = "node_identity.enc"):
    """
    Carrega OU cria a identidade Ed25519 do nó (NÃO é a carteira).

    Regras:
      - Arquivo EXISTE + senha decifra:      OK, usa.
      - Arquivo EXISTE + senha NÃO decifra:  FATAL. Não recria.
      - Arquivo NÃO existe + há senha:       cria.
      - Arquivo NÃO existe + sem senha:      FATAL.
    """
    try:
        from secure_store import load_node_identity, save_node_identity
        from crypto import Ed25519PrivateKey
    except Exception as e:
        raise RuntimeError(f"crypto/secure_store indisponiveis: {e}") from e

    p = Path(path)
    file_exists = p.exists()

    if not password:
        if file_exists:
            raise RuntimeError(
                f"[identity] FATAL: {p} existe mas NENHUMA senha configurada "
                f"(BRN_NODE_PASSWORD vazio). Recusando recriar a identidade. "
                f"Configure BRN_NODE_PASSWORD ou remova {p} manualmente."
            )
        raise RuntimeError(
            "[identity] FATAL: sem BRN_NODE_PASSWORD e sem arquivo de "
            "identidade. Configure BRN_NODE_PASSWORD no brn_network.env."
        )

    if file_exists:
        try:
            data = load_node_identity(password, str(p))
            if not data or not data.get("sk"):
                raise ValueError("arquivo decifrou mas não tem 'sk'")
            priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
            pub = priv.public_key().public_bytes_raw().hex()
            print(f"[identity] carregada ({p.name}) pub={pub[:16]}…")
            return priv, pub
        except Exception as e:
            raise RuntimeError(
                f"[identity] FATAL: não consegui decifrar {p} com a senha "
                f"configurada. Erro: {e}. "
                f"Corrija BRN_NODE_PASSWORD no brn_network.env, ou — se "
                f"você tem certeza de que quer PERDER o node_id — mova "
                f"{p} para outro lugar e reinicie."
            ) from e

    priv = Ed25519PrivateKey.generate()
    sk_hex = priv.private_bytes_raw().hex()
    pub = priv.public_key().public_bytes_raw().hex()
    try:
        save_node_identity(
            {"sk": sk_hex, "pub": pub, "created_at": int(time.time())},
            password, str(p),
        )
        print(f"[identity] CRIADA NOVA ({p.name}) pub={pub[:16]}…")
        print(f"[identity] ⚠ guarde BRN_NODE_PASSWORD — sem ela, o node_id "
              f"é inutilizável.")
    except Exception as e:
        raise RuntimeError(f"[identity] não consegui salvar {p}: {e}") from e
    return priv, pub


# ============================================================
# TRUST
# ============================================================
def _setup_trust(chain, node_id_priv, is_origin: bool):
    try:
        from trust_network import TrustNetwork
        trust = TrustNetwork(
            "trust_network.json",
            is_origin=is_origin,
            node_sk=node_id_priv,
        )
        chain.set_trust_net(trust)
        return trust
    except Exception as e:
        logging.getLogger("main").warning(f"[trust] init falhou: {e}")
        return None


# ============================================================
# P2P
# ============================================================
def _setup_p2p(chain, node_id_priv, p2p_port: int):
    try:
        from p2p_unified import P2PManager
        p2p = P2PManager(chain, node_id_priv=node_id_priv, tcp_port=p2p_port)
        chain.set_p2p(p2p)
        p2p.start()
        return p2p
    except Exception as e:
        logging.getLogger("main").error(f"[p2p] init falhou: {e}")
        return None


# ============================================================
# SYNC
# ============================================================
def _setup_sync(chain, p2p):
    try:
        from sync_manager import ChainSyncManager
        sync_mgr = ChainSyncManager(chain, chain.db, p2p)
        sync_mgr.start_sync_loop()
        return sync_mgr
    except Exception as e:
        logging.getLogger("main").warning(f"[sync] init falhou: {e}")
        return None


# ============================================================
# BROADCAST (não depende de wallet)
# ============================================================
def _setup_broadcast(chain, p2p):
    log = logging.getLogger("main")
    if not p2p:
        return False
    try:
        chain.on_block_mined = p2p.broadcast_block
        log.info("[P2P] Broadcast de blocos ativado")
        return True
    except Exception as e:
        log.warning(f"[P2P] falha ao configurar broadcast: {e}")
        return False


# ============================================================
# WALLET WATCHDOG (NÃO reinstala SK — só monitora)
# ============================================================
def _wallet_watchdog_loop(chain, p2p, trust, stop_evt: threading.Event,
                          interval: int = 30):
    """
    A cada `interval` segundos:
      1. Verifica chain._local_validator_sk.
      2. Se None: LOGA aviso (nó não está assinando) — não reinstala
         porque não tem senha em memória (v13.9.3 não carrega carteira).
      3. Se existe mas _sign_collector=None: LOGA aviso.
      4. Se SK existe e sumiu do top5: re-sincroniza trust (só com
         pubkey em memória — não precisa de senha).
    """
    log = logging.getLogger("main")
    log.info("[watchdog] iniciado (interval=%ds) — NÃO carrega carteira", interval)

    while not stop_evt.wait(interval):
        try:
            sk = getattr(chain, "_local_validator_sk", None)

            # ─── Sem SK ───
            if sk is None:
                if not getattr(chain, "_watchdog_sk_warned", False):
                    chain._watchdog_sk_warned = True
                    log.warning(
                        "[watchdog] _local_validator_sk = None — o nó "
                        "NÃO está assinando blocos. Desbloqueie a "
                        "carteira na GUI (POST /api/wallet/set_validator_sk)."
                    )
                continue

            # SK apareceu — limpa flag
            if getattr(chain, "_watchdog_sk_warned", False):
                chain._watchdog_sk_warned = False
                log.info(
                    "[watchdog] SK detectada — pk=%s…",
                    sk.pub_hex[:16],
                )

            # ─── SK existe mas collector sumiu ───
            collector = getattr(chain, "_sign_collector", None)
            if collector is None:
                if not getattr(chain, "_watchdog_collector_warned", False):
                    chain._watchdog_collector_warned = True
                    log.warning(
                        "[watchdog] _sign_collector = None — nó vai "
                        "minerar mas sem coletar assinaturas dos peers. "
                        "Desbloqueie a GUI novamente."
                    )
                continue
            else:
                chain._watchdog_collector_warned = False

            # ─── SK existe, verifica top5 ───
            try:
                pk = sk.pub_hex.lower()
                t = trust or getattr(chain, "trust_net", None)
                if t is not None and hasattr(t, "get_top_validators"):
                    top = t.get_top_validators(count=5)
                    in_top = pk in [p.lower() for p in top]
                    if not in_top:
                        log.warning(
                            "[watchdog] %s… sumiu do top5 — re-sincronizando trust",
                            pk[:16],
                        )
                        if hasattr(t, "sync_local_validator"):
                            t.sync_local_validator(pk, pk[:16], "")
                            log.info("[watchdog] trust re-sincronizado")
            except Exception as e:
                log.debug("[watchdog] verify trust: %s", e)
        except Exception as e:
            log.warning("[watchdog] loop: %s", e)

    log.info("[watchdog] encerrado")


# ============================================================
# TRUST DAEMON (SEM wallet_obj — só heartbeat + ghosts)
# ============================================================
def _setup_trust_daemon(chain, trust, stop_evt: threading.Event,
                        interval: int = 30):
    """
    Heartbeat + remove_ghosts periódicos.
    Usa chain._local_validator_sk SE existir; se não (antes da GUI
    desbloquear), só faz remove_ghosts.
    """
    log = logging.getLogger("main")
    if trust is None:
        return None

    # Tenta TrustDaemon customizado (do Bruno) primeiro
    try:
        from trust_daemon import TrustDaemon
        try:
            td = TrustDaemon(chain, trust)
            if hasattr(td, "start"):
                td.start()
            log.info("[TrustDaemon] custom ativado")
            return td
        except TypeError:
            pass
    except ImportError:
        pass
    except Exception as e:
        log.debug(f"[TrustDaemon] import/init falhou: {e}")

    # Fallback inline
    def _loop():
        last_ghost = 0.0
        while not stop_evt.wait(interval):
            try:
                sk = getattr(chain, "_local_validator_sk", None)
                if sk is not None and hasattr(trust, "heartbeat_self"):
                    try:
                        trust.heartbeat_self(sk.pub_hex.lower())
                    except Exception:
                        pass

                now = time.time()
                if now - last_ghost >= 60:
                    last_ghost = now
                    try:
                        if hasattr(trust, "remove_ghosts"):
                            n = trust.remove_ghosts(max_age_s=300)
                            if n:
                                log.info(f"[TrustDaemon] {n} fantasma(s) removido(s)")
                    except Exception:
                        pass
            except Exception:
                pass

    t = threading.Thread(target=_loop, daemon=True, name="TrustDaemon")
    t.start()
    log.info("[TrustDaemon] inline ativado (aguarda GUI para heartbeat)")
    return t


# ============================================================
# POOL
# ============================================================
def _setup_pool(chain, is_origin: bool):
    if not is_origin:
        return None
    try:
        from mining_pool import PoolManager
        pool = PoolManager(chain)
        logging.getLogger("main").info("[pool] PoolManager ativo (origin)")
        return pool
    except Exception as e:
        logging.getLogger("main").warning(f"[pool] init falhou: {e}")
        return None


# ============================================================
# WEB SERVER
# ============================================================
def _start_web_server(chain, p2p, pool, sync_mgr, trust, web_port: int):
    try:
        import server as srv
        srv.set_chain(chain)
        if p2p: srv.set_p2p_manager(p2p)
        if pool: srv.set_pool_manager(pool)
        if sync_mgr: srv.set_sync_manager(sync_mgr)
        if trust: srv.set_trust_network(trust)

        def _run():
            srv.app.run(
                host=os.environ.get("BRN_WEB_HOST", "127.0.0.1"),
                port=web_port,
                threaded=True,
                use_reloader=False,
            )
        threading.Thread(target=_run, daemon=True, name="HTTP").start()
        logging.getLogger("main").info(f"[server] HTTP ativo na porta {web_port}")
    except Exception as e:
        logging.getLogger("main").error(f"[server] falhou: {e}")


# ============================================================
# EXPLORER
# ============================================================
def _start_explorer(chain):
    try:
        import explorer as exp
        if hasattr(exp, "set_chain"):
            exp.set_chain(chain)

        def _run():
            exp.app.run(
                host="0.0.0.0",
                port=int(os.environ.get("BRN_EXPLORER_PORT", "8080")),
                threaded=True,
                use_reloader=False,
            )
        threading.Thread(target=_run, daemon=True, name="EXP").start()
        logging.getLogger("main").info(
            f"[explorer] ativo na porta {os.environ.get('BRN_EXPLORER_PORT', '8080')}"
        )
    except Exception:
        pass


# ============================================================
# GUI
# ============================================================
def _start_gui() -> bool:
    try:
        import webview
        from app_wallet_v3 import WalletApi
        api = WalletApi()
        wallet_path = BASE_DIR / "index_wallet.html"
        if not wallet_path.exists():
            wallet_path = BASE_DIR / "panel.html"
        url = wallet_path.as_uri() if wallet_path.exists() else "about:blank"
        logging.getLogger("main").info(f"[gui] Abrindo interface: {wallet_path.name}")
        webview.create_window("BRN Wallet", url=url, width=1100, height=780, js_api=api)
        webview.start()
        return True
    except Exception as e:
        logging.getLogger("main").warning(f"[gui] falha: {e}")
        return False


# ============================================================
# MAIN
# ============================================================
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
    web_port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    p2p_port = int(os.environ.get("BRN_P2P_PORT", "6001"))
    db_path  = args.db or os.environ.get("BRN_DB", "brn_v2_chain.db")

    log = _setup_logging()
    log.info("=" * 64)
    log.info(" BRN Node v%s (build %s) is_origin=%s", VERSION, BUILD_DATE, is_origin)
    log.info(" base=%s env_loaded=%d", BASE_DIR, loaded)
    log.info("=" * 64)

    # 1. Port guard
    try:
        import port_guard
        port_guard.check_brn_ports(checar_explorer=not args.no_explorer)
    except SystemExit:
        raise
    except Exception:
        pass

    # 2. Senha para identidade (não é carteira — é o node_identity.enc)
    identity_password = (
        os.environ.get("BRN_NODE_PASSWORD", "").strip()
        or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
    )
    if not identity_password:
        log.warning(
            "[identity] BRN_NODE_PASSWORD e BRN_WALLET_SESSION_PASS vazios. "
            "A identidade do nó NÃO será persistida."
        )

    # 3. Node identity (Ed25519 do nó — P2P/auth)
    node_id_priv, node_id_pub = None, ""
    try:
        node_id_priv, node_id_pub = _load_or_create_node_identity(identity_password)
        log.info("[identity] carregada pub=%s…", node_id_pub[:16])
    except RuntimeError as e:
        log.error(str(e))
        return 3

    # 4. Blockchain
    try:
        from blockchain import Blockchain
        chain = Blockchain(db_path, auto_genesis=False)
        log.info("[chain] aberta height=%d", chain.db.height())
    except Exception as e:
        log.error(f"[chain] fatal: {e}")
        return 2

    # 5. Trust Network
    trust = _setup_trust(chain, node_id_priv, is_origin)
    if trust is not None:
        log.info("[trust] carregado")

    # ═══════════════════════════════════════════════════════════
    # [v13.9.3] NÃO CARREGA CARTEIRA NO BOOT.
    # A carteira entra via GUI → POST /api/wallet/set_validator_sk
    # → server.py v9.9.28 → wallet_integrator.integrate_and_propagate.
    # ═══════════════════════════════════════════════════════════
    log.info("[wallet] AGUARDANDO GUI (POST /api/wallet/set_validator_sk) — "
             "boot sem carteira por design v13.9.3")

    # 6. P2P
    p2p = None
    if not args.no_p2p:
        p2p = _setup_p2p(chain, node_id_priv, p2p_port)
        if p2p is not None:
            log.info("[p2p] iniciado na porta %d", p2p_port)

    # 7. Sync
    sync_mgr = None
    if p2p:
        sync_mgr = _setup_sync(chain, p2p)
        if sync_mgr:
            log.info("[sync] ChainSyncManager iniciado")

    # 8. Broadcast
    if p2p:
        _setup_broadcast(chain, p2p)

    # 9. TrustDaemon + Watchdog (stop event compartilhado)
    _stop_bg = threading.Event()
    _setup_trust_daemon(chain, trust, _stop_bg, interval=30)

    threading.Thread(
        target=_wallet_watchdog_loop,
        args=(chain, p2p, trust, _stop_bg, 30),
        daemon=True,
        name="Wallet-Watchdog",
    ).start()
    log.info("[watchdog] thread iniciada (30s)")

    # 10. Pool (somente origem)
    pool = _setup_pool(chain, is_origin)

    # 11. Web server
    _start_web_server(chain, p2p, pool, sync_mgr, trust, web_port)

    # 12. Explorer
    if not args.no_explorer:
        _start_explorer(chain)

    # 13. GUI
    if not args.headless:
        log.info("[gui] abrindo — desbloqueie a carteira para instalar a SK")
        if _start_gui():
            return 0

    # 14. Shutdown gracioso
    def _shutdown(*_):
        log.info("[shutdown] encerrando...")
        try:
            _stop_bg.set()
        except Exception:
            pass
        try:
            if trust is not None:
                trust.shutdown()
        except Exception:
            pass
        try:
            chain.db.close()
        except Exception:
            pass
        os._exit(0)

    signal.signal(signal.SIGINT, _shutdown)

    # Estado inicial: SEM SK (a GUI ainda não desbloqueou)
    log.info("=" * 64)
    log.info(" NÓ ATIVO — Ctrl+C para encerrar")
    log.info("   API      : http://%s:%d",
             os.environ.get("BRN_WEB_HOST", "127.0.0.1"), web_port)
    log.info("   Explorer : http://0.0.0.0:%s",
             os.environ.get("BRN_EXPLORER_PORT", "8080"))
    log.info("   P2P      : :%d", p2p_port)
    log.info("   Wallet   : AGUARDANDO GUI")
    log.info("   SK       : NÃO INSTALADA (desbloqueie a GUI)")
    log.info("=" * 64)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown()

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
