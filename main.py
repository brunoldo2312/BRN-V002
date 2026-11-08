"""main.py — Entrypoint do nó BRN (v13.9.1)
================================================================
v13.9.1 (2026-11-08):
  [FIX CRÍTICO] _load_or_create_node_identity: se node_identity.enc
                EXISTE mas a senha está errada, ABORTA o boot com
                RuntimeError. Antes, recriava a identidade com
                Ed25519PrivateKey.generate() e sobrescrevia o arquivo
                — o nó perdia o node_id em silêncio, peers rejeitavam,
                trust ficava inconsistente. Uma das piores falhas
                possíveis em produção: você não percebe até ver o
                handshake P2P falhar com todos os peers.
  [FIX CRÍTICO] Removido "gergerger" hardcoded de identity_password
                e de _load_wallet(). Antes, se o .env não carregasse,
                o nó gerava identidade com senha previsível (e a
                primeira execução criava node_identity.enc cifrado
                com "gergerger"). Isso é um vetor de ataque óbvio:
                qualquer um com o arquivo podia decifrá-lo.
  [FIX CRÍTICO] _setup_multisig: se brn_multisig não existir E
                BRN_STRICT_MULTISIG=1, ABORTA o boot. Antes, só
                logava warning e continuava sem _sign_collector —
                a rede inteira rejeitava os blocos do minerador
                (fork silencioso que ninguém percebia).
  [FIX] _setup_multisig: quando brn_multisig ausente E
        STRICT_MULTISIG=0, loga ERROR (era warning). Operador
        precisa saber que a rede está sem multi-sig.
  [KEEP] Tudo do v13.9.0 (multisig, pbft, trust daemon, broadcast).
v13.9.0 (2026-11-05):
  [NEW] _setup_multisig() — configura coletor de assinaturas.
  [NEW] _setup_pbft() — injeta PBFTConsensus na chain.
  [NEW] chain.on_block_mined = p2p.broadcast_block.
  [NEW] _setup_trust_daemon() — auto-gerencia trust.
  [KEEP] --skip-verify, --headless, --client-mode, --origem, etc.
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
    VERSION = "13.9.1"
    BUILD_DATE = "2026-11-08"


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
# IDENTITY — [FIX CRÍTICO v13.9.1]
# ============================================================
def _load_or_create_node_identity(password: str, path: str = "node_identity.enc"):
    """
    Carrega OU cria a identidade Ed25519 do nó.

    Regras (após v13.9.1):
      - Se o arquivo EXISTE e a senha decifra:           OK, usa.
      - Se o arquivo EXISTE e a senha NÃO decifra:       FATAL. Não recria.
      - Se o arquivo NÃO existe e há senha:              cria.
      - Se o arquivo NÃO existe e não há senha:          FATAL (não podemos
        persistir a identidade sem senha — usar senha previsível é inseguro).

    Antes (v13.9.0), o segundo caso recriava a identidade silenciosamente,
    o que apagava o node_id do nó. Todos os peers rejeitavam o handshake
    P2P porque o node_id era diferente, e o trust ficava inconsistente.
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
            "identidade. NÃO vou criar identidade com senha previsível. "
            "Configure BRN_NODE_PASSWORD no brn_network.env."
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
            # [FIX CRÍTICO] NÃO recria silenciosamente. Aborta.
            raise RuntimeError(
                f"[identity] FATAL: não consegui decifrar {p} com a senha "
                f"configurada. Erro: {e}. "
                f"Corrija BRN_NODE_PASSWORD no brn_network.env, ou — se "
                f"você tem certeza de que quer PERDER o node_id — mova "
                f"{p} para outro lugar e reinicie."
            ) from e

    # Arquivo não existe, senha configurada → criar
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
        raise RuntimeError(
            f"[identity] não consegui salvar {p}: {e}"
        ) from e
    return priv, pub


# ============================================================
# WALLET — [FIX v13.9.1] Removido "gergerger"
# ============================================================
def _load_wallet() -> tuple:
    """
    Tenta as senhas configuradas no .env para decifrar current_wallet.enc.

    Retorna (wallet_obj | None, address, pub_hex, password_usada).

    Ordem:
      1. BRN_WALLET_SESSION_PASS
      2. BRN_NODE_PASSWORD

    Sem fallback hardcoded. Se nenhuma funcionar, retorna (None, "", "", "")
    e o main() loga aviso claro — o nó roda sem SK local (não minera
    nem assina).
    """
    try:
        from secure_store import load_current_wallet
        from wallet import Wallet
    except Exception as e:
        logging.getLogger("main").warning(f"[wallet] modulos indisponiveis: {e}")
        return None, "", "", ""

    candidates = []
    for k in ("BRN_WALLET_SESSION_PASS", "BRN_NODE_PASSWORD"):
        v = (os.environ.get(k, "") or "").strip()
        if v and v not in candidates:
            candidates.append(v)

    if not candidates:
        logging.getLogger("main").warning(
            "[wallet] nenhuma senha configurada "
            "(BRN_WALLET_SESSION_PASS / BRN_NODE_PASSWORD vazias)"
        )
        return None, "", "", ""

    for pwd in candidates:
        try:
            w = load_current_wallet(pwd)
        except Exception:
            continue
        if not w or not w.get("ok"):
            continue
        sk_hex = w.get("private_key") or w.get("priv_hex") or ""
        if not sk_hex:
            continue
        try:
            wallet_obj = Wallet(private_key_hex=sk_hex)
            return wallet_obj, wallet_obj.address, wallet_obj.pub_hex, pwd
        except Exception:
            continue

    return None, "", "", ""


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
# MULTISIG — [FIX CRÍTICO v13.9.1]
# ============================================================
def _setup_multisig(chain, wallet_obj, p2p, trust):
    """
    Configura o coletor de assinaturas (brn_multisig).

    [FIX v13.9.1] Se brn_multisig NÃO existir:
      - STRICT_MULTISIG=1 → aborta o boot (RuntimeError). Não faz sentido
        subir um nó em modo estrito sem o módulo que faz o multi-sig.
      - STRICT_MULTISIG=0 → loga ERROR bem visível. Operador precisa
        saber que o nó está SEM multi-sig (blocos vão sair com 1 sig
        e serão rejeitados pelos peers).
    """
    log = logging.getLogger("main")
    strict = os.environ.get("BRN_STRICT_MULTISIG", "0") == "1"

    if not p2p or not wallet_obj:
        log.info("[MULTISIG] pulado (p2p ou wallet ausente)")
        if strict:
            raise RuntimeError(
                "[MULTISIG] STRICT_MULTISIG=1 mas p2p ou wallet ausente. "
                "Sem multi-sig, o nó não pode operar em modo estrito."
            )
        return False

    try:
        from brn_multisig import ensure_multisig
    except ImportError as e:
        msg = (
            f"[MULTISIG] módulo 'brn_multisig' NÃO ENCONTRADO ({e}). "
            f"O nó vai rodar SEM coletor de assinaturas. "
            f"Blocos minerados aqui vão sair com 1 sig e serão "
            f"REJEITADOS pelos peers em rede multi-nó."
        )
        if strict:
            raise RuntimeError(
                f"[MULTISIG] STRICT_MULTISIG=1 e brn_multisig ausente. "
                f"Abortando o boot (o nó não pode operar em modo estrito "
                f"sem este módulo). Erro: {e}"
            ) from e
        log.error(msg)
        return False

    try:
        ensure_multisig(chain, wallet_obj, p2p, trust=trust, logger=log)
        log.info("[MULTISIG] Coletor de assinaturas configurado")
        return True
    except Exception as e:
        log.error(f"[MULTISIG] ensure_multisig falhou: {e}")
        if strict:
            raise RuntimeError(
                f"[MULTISIG] STRICT_MULTISIG=1 e ensure_multisig falhou. "
                f"Abortando o boot. Erro: {e}"
            ) from e
        return False


def _setup_broadcast(chain, p2p):
    """Configura chain.on_block_mined = p2p.broadcast_block."""
    log = logging.getLogger("main")
    if not p2p:
        return False
    try:
        chain.on_block_mined = p2p.broadcast_block
        log.info("[P2P] Broadcast de blocos ativado (propagação)")
        return True
    except Exception as e:
        log.warning(f"[P2P] falha ao configurar broadcast: {e}")
        return False


# ============================================================
# PBFT
# ============================================================
def _setup_pbft(chain, wallet_obj, p2p):
    """
    Injeta o motor PBFT se BRN_PBFT_ENABLED=1.
    Chamado APÓS p2p.start() e wallet carregada.
    """
    log = logging.getLogger("main")
    if os.environ.get("BRN_PBFT_ENABLED", "0") != "1":
        log.info("[PBFT] desligado (BRN_PBFT_ENABLED!=1)")
        return None

    if wallet_obj is None:
        log.warning("[PBFT] wallet ausente — PBFT nao sera ativado agora "
                    "(pode ser ativado via /api/wallet/set_validator_sk)")
        return None

    if p2p is None:
        log.warning("[PBFT] p2p ausente — PBFT nao sera ativado")
        return None

    try:
        from pbft_consensus import PBFTConsensus
        from p2p_unified import P2PClient

        def _pbft_send(peer_addr, msg):
            try:
                ip, port_s = peer_addr.rsplit(":", 1)
                resp, _ = P2PClient.send_message(ip, int(port_s), msg, timeout=15.0)
                return resp
            except Exception:
                return None

        pbft = PBFTConsensus(chain, wallet_obj, _pbft_send)
        chain.set_pbft(pbft)

        try:
            if hasattr(p2p, "server") and p2p.server is not None:
                p2p.server.set_pbft(pbft)
        except Exception as e:
            log.warning(f"[PBFT] falha ao injetar no P2PServer: {e}")

        st = pbft.status()
        log.info(
            "[PBFT] ATIVO — validators=%d f=%d quorum=%d view=%d seq=%d",
            st.get("validators", 0),
            st.get("faults_tolerated_f", 0),
            st.get("quorum_2f_plus_1", 0),
            st.get("view", 0),
            st.get("seq", 0),
        )
        return pbft
    except Exception as e:
        log.error(f"[PBFT] init falhou: {e}")
        return None


# ============================================================
# TRUST DAEMON
# ============================================================
def _setup_trust_daemon(chain, trust, wallet_obj, p2p):
    """
    Tenta importar trust_daemon.TrustDaemon (módulo custom do Bruno).
    Se não existir, cria versão inline básica que:
      - a cada 30s renova last_seen da wallet no trust
      - a cada 60s remove fantasmas (remove_ghosts)
    """
    log = logging.getLogger("main")
    if trust is None:
        return None

    # 1) Tenta importar módulo custom
    try:
        from trust_daemon import TrustDaemon  # type: ignore
        try:
            td = TrustDaemon(chain, trust, wallet=wallet_obj, p2p=p2p)
            if hasattr(td, "start"):
                td.start()
            log.info("[TrustDaemon] Auto-gerenciamento da rede ativado.")
            return td
        except TypeError:
            # Assinatura diferente — tenta só (chain, trust)
            try:
                td = TrustDaemon(chain, trust)
                if hasattr(td, "start"):
                    td.start()
                log.info("[TrustDaemon] Auto-gerenciamento da rede ativado.")
                return td
            except Exception as e:
                log.debug(f"[TrustDaemon] init falhou: {e}")
    except ImportError:
        pass
    except Exception as e:
        log.debug(f"[TrustDaemon] import/init falhou: {e}")

    # 2) Fallback inline
    pub = ""
    try:
        pub = wallet_obj.pub_hex if wallet_obj is not None else ""
    except Exception:
        pass
    if not pub:
        log.info("[TrustDaemon] sem wallet — daemon inline nao iniciado")
        return None

    stop_evt = threading.Event()

    def _loop():
        last_ghost = 0.0
        while not stop_evt.wait(30):
            try:
                if hasattr(trust, "heartbeat_self"):
                    trust.heartbeat_self(pub)
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

    t = threading.Thread(target=_loop, daemon=True, name="TrustDaemon")
    t.start()
    log.info("[TrustDaemon] Auto-gerenciamento da rede ativado. (inline)")
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

    # 2. Senha para identidade — SEM fallback hardcoded (v13.9.1)
    identity_password = (
        os.environ.get("BRN_NODE_PASSWORD", "").strip()
        or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
    )
    if not identity_password:
        log.warning(
            "[identity] BRN_NODE_PASSWORD e BRN_WALLET_SESSION_PASS vazios. "
            "A identidade do nó NÃO será persistida. Defina uma senha no "
            "brn_network.env para operar em produção."
        )

    # 3. Node identity — [FIX CRÍTICO] aborta se arquivo existe + senha errada
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

    # 6. Carteira (sem fallback hardcoded) + injeta SK na chain
    wallet_obj, wallet_addr, wallet_pub, used_pwd = _load_wallet()
    if wallet_obj is not None:
        try:
            chain.set_local_validator_sk(wallet_obj)
            log.info(
                "[wallet] SK ativa instalada addr=%s pub=%s… (senha: %s…)",
                wallet_addr[:24], wallet_pub[:16], used_pwd[:3] + "***",
            )
        except Exception as e:
            log.warning(f"[wallet] set_local_validator_sk falhou: {e}")
    else:
        log.warning("[wallet] NAO consegui decifrar current_wallet.enc "
                    "(nenhuma senha configurada ou todas falharam). "
                    "O nó vai rodar sem SK local (não minera nem assina).")

    # 7. P2P
    p2p = None
    if not args.no_p2p:
        p2p = _setup_p2p(chain, node_id_priv, p2p_port)
        if p2p is not None:
            log.info("[p2p] iniciado na porta %d", p2p_port)

    # 8. Sync
    sync_mgr = None
    if p2p:
        sync_mgr = _setup_sync(chain, p2p)
        if sync_mgr:
            log.info("[sync] ChainSyncManager iniciado")

    # 9. MULTISIG + BROADCAST — [FIX CRÍTICO] pode abortar com STRICT_MULTISIG=1
    if p2p and wallet_obj is not None:
        try:
            _setup_multisig(chain, wallet_obj, p2p, trust)
        except RuntimeError as e:
            log.error(str(e))
            return 4
        _setup_broadcast(chain, p2p)

    # 10. PBFT
    pbft = _setup_pbft(chain, wallet_obj, p2p)

    # 11. TrustDaemon
    _setup_trust_daemon(chain, trust, wallet_obj, p2p)

    # 12. Pool (somente origem)
    pool = _setup_pool(chain, is_origin)

    # 13. Web server
    _start_web_server(chain, p2p, pool, sync_mgr, trust, web_port)

    # 14. Explorer
    if not args.no_explorer:
        _start_explorer(chain)

    # 15. GUI
    if not args.headless:
        if _start_gui():
            return 0

    # 16. Shutdown gracioso
    def _shutdown(*_):
        log.info("[shutdown] encerrando...")
        try:
            chain.db.close()
        except Exception:
            pass
        os._exit(0)

    signal.signal(signal.SIGINT, _shutdown)

    log.info("=" * 64)
    log.info(" NÓ ATIVO — Ctrl+C para encerrar")
    log.info("   API      : http://%s:%d",
             os.environ.get("BRN_WEB_HOST", "127.0.0.1"), web_port)
    log.info("   Explorer : http://0.0.0.0:%s",
             os.environ.get("BRN_EXPLORER_PORT", "8080"))
    log.info("   P2P      : :%d", p2p_port)
    log.info("   Wallet   : %s", wallet_addr or "(não carregada)")
    log.info("   PBFT     : %s", "ATIVO" if pbft else "desligado")
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
