"""main.py — Entrypoint do nó BRN (v13.9.6)
================================================================
v13.9.6 (2026-11-11) — AUTO-CONFIG VIA ENV VARS:
  [NEW] _auto_configure(): instala SK e inicia mineração no pool
        automaticamente no boot, lendo:
          - BRN_VALIDATOR_SK_HEX       (64 hex)
          - BRN_VALIDATOR_ADDRESS      (brn1...)
          - BRN_VALIDATOR_PUBKEY       (66 hex)
          - BRN_AUTO_MINE              ("1" = inicia PoolClient)
        Resolve o problema de SK perdida quando o Render hiberna
        (free tier reinicia a cada 15min sem tráfego). Após cada
        restart, o nó se reconfigura sozinho, sem precisar de Shell.
  [NEW] Defaults automáticos:
          - BRN_BOOTSTRAP_PEERS=100.111.253.8:6001 (se vazio)
          - BRN_POOL_URL=http://100.111.253.8:5000 (se vazio)
        Resolve peers_known=0 quando o env var não foi setada.
  [KEEP] v13.9.5: isolation_detector no watchdog
  [KEEP] v13.9.4: boot sem senha no env (.node_password)
  [KEEP] v13.9.3: carteira via API (GUI/curl)
  [KEEP] v13.9.2: identidade segura

FLUXO DE BOOT (v13.9.6):
  1. main.py sobe → senha resolvida (env OU .node_password local)
  2. node_identity.enc carregado/criado
  3. blockchain.py validado
  4. Defaults de BOOTSTRAP_PEERS e POOL_URL aplicados
  5. chain + trust + p2p + sync + pool + web sobem
  6. GUI abre (ou modo --headless)
  7. [AUTO-CONFIG] após 3s, instala SK via env BRN_VALIDATOR_SK_HEX
  8. [AUTO-CONFIG] após SK ok, inicia PoolClient via env BRN_AUTO_MINE=1
  9. Watchdog monitora isolamento + SK + trust a cada 30s

VARIÁVEIS DE AMBIENTE (opcionais, mas recomendadas no Render):
  BRN_BOOTSTRAP_PEERS=100.111.253.8:6001
  BRN_POOL_URL=http://100.111.253.8:5000
  BRN_VALIDATOR_SK_HEX=<64 hex>
  BRN_VALIDATOR_ADDRESS=brn1qw0mdf08qlzl9l8t0zps6rhqm4xul7jvdz39jv
  BRN_VALIDATOR_PUBKEY=03b992d788c15470143a69651c05587e5483cc8344166d0ecb4f6e18dc981d3626
  BRN_AUTO_MINE=1
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
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

try:
    from version import VERSION, BUILD_DATE
except ImportError:
    VERSION = "13.9.6"
    BUILD_DATE = "2026-11-11"

# [AI v13.9.5] Detector de isolamento (opcional)
try:
    from isolation_detector import IsolationDetector
    _HAS_ISOLATION = True
except ImportError:
    IsolationDetector = None
    _HAS_ISOLATION = False

# [AUTO-CONFIG v13.9.6] SK + mineração automáticas
try:
    import wallet_integrator as _wi
    _HAS_WI = True
except ImportError:
    _wi = None
    _HAS_WI = False

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _requests = None
    _HAS_REQUESTS = False


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
# IDENTITY PASSWORD
# ============================================================
def _get_identity_password() -> str:
    log = logging.getLogger("main")

    pw = (os.environ.get("BRN_NODE_PASSWORD", "").strip()
          or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip())
    if pw:
        log.debug("[identity] senha vinda do ambiente (len=%d)", len(pw))
        return pw

    local_file = BASE_DIR / ".node_password"
    if local_file.exists():
        try:
            pw = local_file.read_text(encoding="utf-8").strip()
            if pw:
                log.debug("[identity] senha vinda de %s", local_file.name)
                return pw
        except Exception as e:
            log.debug("[identity] leitura de %s falhou: %s", local_file.name, e)

    pw = secrets.token_hex(16)
    try:
        local_file.write_text(pw + "\n", encoding="utf-8")
        try:
            os.chmod(local_file, 0o600)
        except Exception:
            pass
        log.info(
            "[identity] senha local criada em %s (0600) — sem .env",
            local_file.name,
        )
        return pw
    except Exception as e:
        log.warning(
            "[identity] não consegui gravar %s: %s — identidade volátil",
            local_file.name, e,
        )
        return ""


# ============================================================
# IDENTITY
# ============================================================
def _load_or_create_node_identity():
    log = logging.getLogger("main")

    try:
        from secure_store import load_node_identity, save_node_identity
        from crypto import Ed25519PrivateKey
    except Exception as e:
        raise RuntimeError(f"crypto/secure_store indisponíveis: {e}") from e

    pw_env = (os.environ.get("BRN_NODE_PASSWORD", "").strip()
              or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip())
    pw = _get_identity_password()
    p = BASE_DIR / "node_identity.enc"

    if p.exists():
        if not pw:
            raise RuntimeError(
                f"[identity] {p.name} existe mas nenhuma senha pôde ser "
                f"resolvida (nem env nem .node_password)."
            )
        try:
            data = load_node_identity(pw, str(p))
            if not data or not data.get("sk"):
                raise ValueError("arquivo decifrou mas não tem 'sk'")
            priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
            pub = priv.public_key().public_bytes_raw().hex()
            log.info("[identity] carregada (%s) pub=%s…", p.name, pub[:16])
            return priv, pub
        except Exception as e:
            if pw_env:
                raise RuntimeError(
                    f"[identity] FATAL: {p.name} não abriu com a senha do "
                    f"ambiente. Erro: {e}. "
                    f"Corrija BRN_NODE_PASSWORD ou mova {p.name}."
                ) from e
            backup = p.with_suffix(f".enc.bak.{int(time.time())}")
            try:
                p.rename(backup)
                log.warning(
                    "[identity] %s inacessível (%s) — backup em %s, criando nova",
                    p.name, e, backup.name,
                )
            except Exception as e2:
                raise RuntimeError(
                    f"[identity] {p.name} inacessível e não pude renomear: {e2}"
                ) from e2

    if not pw:
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key().public_bytes_raw().hex()
        log.warning(
            "[identity] VOLÁTIL (%s) pub=%s… — não sobrevive a restart",
            p.name, pub[:16],
        )
        return priv, pub

    priv = Ed25519PrivateKey.generate()
    sk_hex = priv.private_bytes_raw().hex()
    pub = priv.public_key().public_bytes_raw().hex()
    try:
        save_node_identity(
            {"sk": sk_hex, "pub": pub, "created_at": int(time.time())},
            pw, str(p),
        )
        log.info("[identity] CRIADA (%s) pub=%s…", p.name, pub[:16])
    except Exception as e:
        raise RuntimeError(f"[identity] não consegui salvar {p.name}: {e}") from e
    return priv, pub


# ============================================================
# VERSION GATE
# ============================================================
def _check_blockchain_version() -> bool:
    log = logging.getLogger("main")
    try:
        import blockchain as _bc
        ver = getattr(_bc, "__version__", "")
    except Exception as e:
        log.error("[version] não consegui importar blockchain: %s", e)
        return os.environ.get("BRN_STRICT_MULTISIG") != "1"

    ok = ver.startswith("9.4") or (ver >= "9.4.10") or (ver.startswith("9.5"))
    if ok:
        log.info("[version] blockchain.py v%s (multi-sig OK)", ver)
        return True

    log.warning(
        "[version] blockchain.py v%s — multi-sig NÃO será enforçado em "
        "accept_block. Blocos com 1/N assinaturas serão aceitos.",
        ver or "desconhecida",
    )
    if os.environ.get("BRN_STRICT_MULTISIG") == "1":
        log.error("[version] BRN_STRICT_MULTISIG=1 e versão < 9.4 — abortando")
        return False
    return True


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
        logging.getLogger("main").error("[trust] init falhou: %s", e)
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
        logging.getLogger("main").error("[p2p] init falhou: %s", e)
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
        logging.getLogger("main").warning("[sync] init falhou: %s", e)
        return None


# ============================================================
# BROADCAST
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
        log.warning("[P2P] falha ao configurar broadcast: %s", e)
        return False


# ============================================================
# WALLET WATCHDOG (v13.9.5: + isolation_detector)
# ============================================================
def _wallet_watchdog_loop(chain, p2p, trust, stop_evt: threading.Event,
                          interval: int = 30):
    log = logging.getLogger("main")
    log.info("[watchdog] iniciado (interval=%ds)", interval)

    detector = IsolationDetector() if _HAS_ISOLATION else None
    if detector is not None:
        log.info("[watchdog] isolation_detector ativo")
    else:
        log.info("[watchdog] isolation_detector ausente — modo padrão")

    while not stop_evt.wait(interval):
        # [AI v13.9.5] Check de isolamento
        if detector is not None and p2p is not None:
            try:
                peers = p2p.discovery.listar_peers()
                for addr in peers:
                    detector.on_peer_seen(addr)
                alert = detector.check(chain.db.height(), peers)
                if alert is not None:
                    log.warning(
                        "[ISOLATION] %s: %s\n  Sugestões:\n  - %s",
                        alert.severity.upper(),
                        alert.reason,
                        "\n  - ".join(alert.suggested_fix),
                    )
            except Exception as _e:
                log.debug("[ISOLATION] check falhou: %s", _e)

        # Bloco original do watchdog (SK + collector + trust)
        try:
            sk = getattr(chain, "_local_validator_sk", None)
            if sk is None:
                if not getattr(chain, "_watchdog_sk_warned", False):
                    chain._watchdog_sk_warned = True
                    log.warning(
                        "[watchdog] _local_validator_sk=None — nó NÃO "
                        "assina. Desbloqueie a carteira na GUI "
                        "(POST /api/wallet/set_validator_sk)."
                    )
                continue

            if getattr(chain, "_watchdog_sk_warned", False):
                chain._watchdog_sk_warned = False
                log.info("[watchdog] SK detectada pk=%s…", sk.pub_hex[:16])

            collector = getattr(chain, "_sign_collector", None)
            if collector is None:
                if not getattr(chain, "_watchdog_collector_warned", False):
                    chain._watchdog_collector_warned = True
                    log.warning(
                        "[watchdog] _sign_collector=None — vai minerar "
                        "sem coletar assinaturas."
                    )
                continue
            chain._watchdog_collector_warned = False

            try:
                pk = sk.pub_hex.lower()
                t = trust or getattr(chain, "trust_net", None)
                if t is not None and hasattr(t, "get_top_validators"):
                    top = t.get_top_validators(count=5)
                    if pk not in [p.lower() for p in top]:
                        log.warning(
                            "[watchdog] %s… sumiu do top5 — re-sync trust",
                            pk[:16],
                        )
                        if hasattr(t, "sync_local_validator"):
                            t.sync_local_validator(pk, pk[:16], "")
            except Exception as e:
                log.debug("[watchdog] verify trust: %s", e)
        except Exception as e:
            log.warning("[watchdog] loop: %s", e)

    log.info("[watchdog] encerrado")


# ============================================================
# TRUST DAEMON
# ============================================================
def _setup_trust_daemon(chain, trust, p2p, password: str,
                        is_origin: bool, stop_evt: threading.Event,
                        interval: int = 30):
    log = logging.getLogger("main")
    if trust is None:
        return None

    try:
        from trust_daemon import start_daemon
        t = start_daemon(
            chain, trust, p2p, password, is_origin, stop_evt,
        )
        log.info("[TrustDaemon] start_daemon() original ativado")
        return t
    except ImportError as e:
        log.warning("[TrustDaemon] import: %s — usando fallback", e)
    except Exception as e:
        log.warning("[TrustDaemon] start_daemon falhou: %s — usando fallback", e)

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
                                log.info("[TrustDaemon] %d fantasma(s)", n)
                    except Exception:
                        pass
            except Exception:
                pass

    t = threading.Thread(target=_loop, daemon=True, name="TrustDaemon")
    t.start()
    log.info("[TrustDaemon] fallback inline ativado")
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
        logging.getLogger("main").warning("[pool] init falhou: %s", e)
        return None


# ============================================================
# WEB SERVER
# ============================================================
def _start_web_server(chain, p2p, pool, sync_mgr, trust, web_port: int):
    log = logging.getLogger("main")
    try:
        import server as srv
    except Exception as e:
        log.error("[server] import falhou: %s — HTTP NÃO vai subir", e)
        return False

    srv.set_chain(chain)

    for fn_name, obj in (
        ("set_p2p_manager", p2p),
        ("set_pool_manager", pool),
        ("set_sync_manager", sync_mgr),
        ("set_trust_network", trust),
    ):
        if obj is None:
            continue
        fn = getattr(srv, fn_name, None)
        if fn is None:
            log.warning("[server] %s() ausente — ignorando", fn_name)
            continue
        try:
            fn(obj)
        except Exception as e:
            log.error("[server] %s(%s) falhou: %s", fn_name, type(obj).__name__, e)

    def _run():
        try:
            srv.app.run(
                host=os.environ.get("BRN_WEB_HOST", "127.0.0.1"),
                port=web_port,
                threaded=True,
                use_reloader=False,
            )
        except Exception as e:
            log.error("[server] app.run morreu: %s", e)

    threading.Thread(target=_run, daemon=True, name="HTTP").start()
    log.info("[server] HTTP ativo na porta %d", web_port)
    return True


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
            "[explorer] ativo na porta %s",
            os.environ.get("BRN_EXPLORER_PORT", "8080"),
        )
    except Exception as e:
        logging.getLogger("main").debug("[explorer] não iniciou: %s", e)


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
        logging.getLogger("main").info("[gui] abrindo %s", wallet_path.name)
        webview.create_window("BRN Wallet", url=url,
                              width=1100, height=780, js_api=api)
        webview.start()
        return True
    except Exception as e:
        logging.getLogger("main").warning("[gui] falha: %s", e)
        return False


# ============================================================
# AUTO-CONFIG (v13.9.6)
# ============================================================
def _auto_configure(chain, p2p, trust, web_port: int) -> None:
    """
    Executa auto-SK e auto-mine baseado em env vars.
    Chamada automaticamente no boot após o web server subir.

    Envs lidas:
      BRN_VALIDATOR_SK_HEX       (64 hex — chave privada)
      BRN_VALIDATOR_ADDRESS      (brn1... — endereço)
      BRN_VALIDATOR_PUBKEY       (66 hex — chave pública)
      BRN_AUTO_MINE              ("1" = inicia PoolClient no pool)
    """
    log = logging.getLogger("main")

    sk_hex  = os.environ.get("BRN_VALIDATOR_SK_HEX", "").strip()
    address = os.environ.get("BRN_VALIDATOR_ADDRESS", "").strip()
    pubkey  = os.environ.get("BRN_VALIDATOR_PUBKEY", "").strip()
    auto_mine = os.environ.get("BRN_AUTO_MINE", "0") == "1"

    if not sk_hex:
        log.info("[AUTO-CONFIG] BRN_VALIDATOR_SK_HEX vazio — SK não instalada automaticamente")
        log.info("[AUTO-CONFIG] (instale via /wallet ou POST /api/wallet/set_validator_sk)")
    else:
        # ─── 1. Auto-instalar SK ───
        if not _HAS_WI:
            log.warning("[AUTO-CONFIG] wallet_integrator indisponível — SK não instalada")
        else:
            log.info("[AUTO-CONFIG] instalando SK via wallet_integrator...")
            try:
                r = _wi.integrate_and_propagate(
                    chain, sk_hex,
                    address=address, pubkey=pubkey,
                    p2p=p2p, trust=trust, logger=log,
                )
                if r.get("ok"):
                    log.info(
                        "[AUTO-CONFIG] ✅ SK instalada (sk=%s ms=%s pbft=%s)",
                        r.get("sk_installed"),
                        r.get("multisig_ready"),
                        r.get("pbft_ready"),
                    )
                else:
                    log.warning(
                        "[AUTO-CONFIG] ⚠️ SK falhou: %s",
                        r.get("errors") or r.get("msg"),
                    )
            except Exception as e:
                log.exception("[AUTO-CONFIG] integrate falhou: %s", e)

    # ─── 2. Auto-mine ───
    if not auto_mine:
        return

    if not address or not pubkey:
        log.warning("[AUTO-CONFIG] BRN_AUTO_MINE=1 mas faltam BRN_VALIDATOR_ADDRESS/PUBKEY")
        return

    if not _HAS_REQUESTS:
        log.warning("[AUTO-CONFIG] BRN_AUTO_MINE=1 mas 'requests' não instalado")
        return

    # Aguarda SK estar de fato no chain (até 30s)
    for _ in range(30):
        if getattr(chain, "_local_validator_sk", None) is not None:
            break
        time.sleep(1)

    if getattr(chain, "_local_validator_sk", None) is None:
        log.warning("[AUTO-CONFIG] SK ainda ausente após 30s — abortando auto-mine")
        return

    log.info("[AUTO-CONFIG] iniciando mineração no pool...")
    for attempt in range(1, 4):
        try:
            r = _requests.post(
                f"http://127.0.0.1:{web_port}/api/pool/start_local",
                json={"address": address, "pubkey": pubkey},
                timeout=30,
            )
            try:
                data = r.json() if r.content else {}
            except Exception:
                data = {"ok": False, "msg": f"HTTP {r.status_code} sem JSON"}

            if data.get("ok"):
                log.info(
                    "[AUTO-CONFIG] ✅ PoolClient iniciado (miner_id=%s)",
                    data.get("miner_id", "?"),
                )
                return
            else:
                log.warning(
                    "[AUTO-CONFIG] ⚠️ PoolClient tentativa %d: %s",
                    attempt, data.get("msg", "?"),
                )
        except Exception as e:
            log.warning("[AUTO-CONFIG] PoolClient tentativa %d: %s", attempt, e)
        time.sleep(5)

    log.warning("[AUTO-CONFIG] PoolClient não iniciou após 3 tentativas")


def _delayed_auto_configure(chain, p2p, trust, web_port: int) -> None:
    """Aguarda 3s (web server subir) e chama _auto_configure."""
    try:
        time.sleep(3)
        _auto_configure(chain, p2p, trust, web_port)
    except Exception as e:
        logging.getLogger("main").exception("[AUTO-CONFIG] falhou: %s", e)


# ============================================================
# MAIN
# ============================================================
def main() -> int:
    args = parse_args()
    if args.version:
        print(f"BRN Node v{VERSION} (build {BUILD_DATE})")
        return 0

    if args.skip_verify:
        print("[main] --skip-verify ativo")

    loaded = _load_env_file()

    if args.client_mode: os.environ["BRN_IS_ORIGIN"] = "0"
    if args.origem:      os.environ["BRN_IS_ORIGIN"] = "1"
    if args.web_port is not None: os.environ["BRN_WEB_PORT"] = str(args.web_port)
    if args.p2p_port is not None: os.environ["BRN_P2P_PORT"] = str(args.p2p_port)

    is_origin = os.environ.get("BRN_IS_ORIGIN", "0") == "1"
    web_port  = int(os.environ.get("BRN_WEB_PORT", "5000"))
    p2p_port  = int(os.environ.get("BRN_P2P_PORT", "6001"))
    db_path   = args.db or os.environ.get("BRN_DB", "brn_v2_chain.db")

    log = _setup_logging()
    log.info("=" * 64)
    log.info(" BRN Node v%s (build %s) is_origin=%s", VERSION, BUILD_DATE, is_origin)
    log.info(" base=%s env_loaded=%d", BASE_DIR, loaded)
    log.info("=" * 64)

    # [v13.9.6] Defaults de bootstrap e pool (antes do P2P)
    if not os.environ.get("BRN_BOOTSTRAP_PEERS"):
        os.environ["BRN_BOOTSTRAP_PEERS"] = "100.111.253.8:6001"
        log.info("[config] BRN_BOOTSTRAP_PEERS default: 100.111.253.8:6001")
    else:
        log.info("[config] BRN_BOOTSTRAP_PEERS: %s", os.environ.get("BRN_BOOTSTRAP_PEERS"))

    if not os.environ.get("BRN_POOL_URL"):
        os.environ["BRN_POOL_URL"] = "http://100.111.253.8:5000"
        log.info("[config] BRN_POOL_URL default: http://100.111.253.8:5000")
    else:
        log.info("[config] BRN_POOL_URL: %s", os.environ.get("BRN_POOL_URL"))

    # [1] Version gate
    if not _check_blockchain_version():
        return 6

    # [2] Port guard
    try:
        import port_guard
        port_guard.check_brn_ports(checar_explorer=not args.no_explorer)
    except SystemExit:
        raise
    except Exception:
        pass

    # [3] Node identity
    try:
        node_id_priv, node_id_pub = _load_or_create_node_identity()
        log.info("[identity] pub=%s…", node_id_pub[:16])
    except RuntimeError as e:
        log.error(str(e))
        return 3

    # [4] Chain
    try:
        from blockchain import Blockchain
        chain = Blockchain(db_path, auto_genesis=False)
        log.info("[chain] aberta height=%d", chain.db.height())
    except Exception as e:
        log.error("[chain] fatal: %s", e)
        return 2

    # [5] Trust
    trust = _setup_trust(chain, node_id_priv, is_origin)
    if trust is None:
        log.error(
            "[trust] não carregou — multi-sig fica DESLIGADO "
            "(chain.trust_net=None)"
        )
        if os.environ.get("BRN_STRICT_MULTISIG") == "1":
            return 7
    else:
        log.info("[trust] carregado (origin=%s)", is_origin)

    log.info("[wallet] boot sem carteira (design v13.9.6) — "
             "instale SK via API (GUI ou curl)")

    # [6] P2P
    p2p = None
    if not args.no_p2p:
        p2p = _setup_p2p(chain, node_id_priv, p2p_port)
        if p2p is not None:
            log.info("[p2p] iniciado na porta %d", p2p_port)

    # [7] Sync
    sync_mgr = None
    if p2p:
        sync_mgr = _setup_sync(chain, p2p)
        if sync_mgr:
            log.info("[sync] ChainSyncManager iniciado")

    # [8] Broadcast
    if p2p:
        _setup_broadcast(chain, p2p)

    # [9] TrustDaemon + Watchdog
    _stop_bg = threading.Event()
    _setup_trust_daemon(
        chain, trust, p2p,
        password=_get_identity_password(),
        is_origin=is_origin,
        stop_evt=_stop_bg,
        interval=30,
    )
    threading.Thread(
        target=_wallet_watchdog_loop,
        args=(chain, p2p, trust, _stop_bg, 30),
        daemon=True,
        name="Wallet-Watchdog",
    ).start()

    # [10] Pool (origin only)
    pool = _setup_pool(chain, is_origin)

    # [11] Web server
    ok_http = _start_web_server(chain, p2p, pool, sync_mgr, trust, web_port)
    if not ok_http:
        log.error("[server] HTTP não subiu — a GUI não vai conseguir instalar SK")

    # [11.5] [AUTO-CONFIG v13.9.6] SK + auto-mine
    if not args.no_pool_client:
        threading.Thread(
            target=_delayed_auto_configure,
            args=(chain, p2p, trust, web_port),
            daemon=True,
            name="AutoConfig",
        ).start()
        log.info("[AUTO-CONFIG] agendado (executa em 3s, após HTTP subir)")

    # [12] Explorer
    if not args.no_explorer:
        _start_explorer(chain)

    # [13] GUI ou modo headless
    if not args.headless:
        log.info("[gui] abrindo — desbloqueie a carteira para instalar a SK")
        if _start_gui():
            return 0
    else:
        log.info("=" * 64)
        log.info(" MODO HEADLESS")
        log.info("   - SK: instale via POST /api/wallet/set_validator_sk")
        log.info("         ou defina BRN_VALIDATOR_SK_HEX nas env vars")
        log.info("   - Mine: POST /api/pool/start_local")
        log.info("          ou defina BRN_AUTO_MINE=1 nas env vars")
        log.info("   - Wallet web: http://%s:%d/wallet",
                 os.environ.get("BRN_WEB_HOST", "127.0.0.1"), web_port)
        log.info("=" * 64)

    # [14] Shutdown gracioso
    def _shutdown(*_):
        log.info("[shutdown] encerrando...")
        try: _stop_bg.set()
        except Exception: pass
        try:
            if trust is not None: trust.shutdown()
        except Exception: pass
        try: chain.db.close()
        except Exception: pass
        try:
            from secure_store import WalletSession
            WalletSession().lock()
        except Exception: pass
        os._exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("=" * 64)
    log.info(" NÓ ATIVO — Ctrl+C para encerrar")
    log.info("   API      : http://%s:%d",
             os.environ.get("BRN_WEB_HOST", "127.0.0.1"), web_port)
    if not args.no_explorer:
        log.info("   Explorer : http://0.0.0.0:%s",
                 os.environ.get("BRN_EXPLORER_PORT", "8080"))
    log.info("   Wallet   : http://%s:%d/wallet",
             os.environ.get("BRN_WEB_HOST", "127.0.0.1"), web_port)
    log.info("   P2P      : :%d", p2p_port)
    log.info("   Bootstrap: %s", os.environ.get("BRN_BOOTSTRAP_PEERS", "(vazio)"))
    log.info("   Pool URL : %s", os.environ.get("BRN_POOL_URL", "(vazio)"))
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
