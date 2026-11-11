"""main.py — Entrypoint do nó BRN (v13.9.9)
================================================================
v13.9.9 (2026-11-11) — GUI VIA HTTP:
  [FIX] _start_gui() agora abre http://127.0.0.1:{web_port}/wallet
        em vez do arquivo local (file://). Isso resolve o erro
        "Failed to fetch" na janela do aplicativo, pois a carteira
        web (index_wallet.html) precisa de um servidor HTTP
        rodando para funcionar.
  [KEEP] v13.9.8: seed determinístico (auto-resolve SK)
  [KEEP] v13.9.7: validação de SK + /health
  [KEEP] v13.9.6: auto-config (SK + mine via env vars)
  [KEEP] v13.9.5: isolation_detector no watchdog
================================================================
"""
from __future__ import annotations

import argparse
import hashlib
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
    VERSION = "13.9.9"
    BUILD_DATE = "2026-11-11"

# [AI v13.9.5] Detector de isolamento
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

# [VALIDAÇÃO v13.9.7] Wallet para checar SK
try:
    from wallet import Wallet as _Wallet
    _HAS_WALLET = True
except ImportError:
    _Wallet = None
    _HAS_WALLET = False

# Estado global da validação de SK (para /health)
SK_VALIDATION_STATE = {
    "checked": False,
    "ok": False,
    "error": "",
    "source": "",
    "address_configured": "",
    "address_derived": "",
    "pubkey_configured": "",
    "pubkey_derived": "",
}


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
        return pw

    local_file = BASE_DIR / ".node_password"
    if local_file.exists():
        try:
            pw = local_file.read_text(encoding="utf-8").strip()
            if pw:
                return pw
        except Exception:
            pass

    pw = secrets.token_hex(16)
    try:
        local_file.write_text(pw + "\n", encoding="utf-8")
        try:
            os.chmod(local_file, 0o600)
        except Exception:
            pass
        log.info("[identity] senha local criada em %s", local_file.name)
        return pw
    except Exception as e:
        log.warning("[identity] não gravei %s: %s", local_file.name, e)
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
            raise RuntimeError(f"[identity] {p.name} sem senha resolvível")
        try:
            data = load_node_identity(pw, str(p))
            if not data or not data.get("sk"):
                raise ValueError("arquivo sem 'sk'")
            priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
            pub = priv.public_key().public_bytes_raw().hex()
            log.info("[identity] carregada pub=%s…", pub[:16])
            return priv, pub
        except Exception as e:
            if pw_env:
                raise RuntimeError(
                    f"[identity] {p.name} não abriu com senha do ambiente: {e}"
                ) from e
            backup = p.with_suffix(f".enc.bak.{int(time.time())}")
            try:
                p.rename(backup)
                log.warning("[identity] %s inacessível — backup em %s", p.name, backup.name)
            except Exception as e2:
                raise RuntimeError(f"[identity] não renomeei {p.name}: {e2}") from e2

    if not pw:
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key().public_bytes_raw().hex()
        log.warning("[identity] VOLÁTIL pub=%s…", pub[:16])
        return priv, pub

    priv = Ed25519PrivateKey.generate()
    sk_hex = priv.private_bytes_raw().hex()
    pub = priv.public_key().public_bytes_raw().hex()
    try:
        save_node_identity(
            {"sk": sk_hex, "pub": pub, "created_at": int(time.time())},
            pw, str(p),
        )
        log.info("[identity] CRIADA pub=%s…", pub[:16])
    except Exception as e:
        raise RuntimeError(f"[identity] não salvei {p.name}: {e}") from e
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
        log.error("[version] não importei blockchain: %s", e)
        return os.environ.get("BRN_STRICT_MULTISIG") != "1"

    ok = ver.startswith("9.4") or (ver >= "9.4.10") or ver.startswith("9.5")
    if ok:
        log.info("[version] blockchain.py v%s (multi-sig OK)", ver)
        return True

    log.warning("[version] blockchain.py v%s — multi-sig NÃO enforçado", ver or "desconhecida")
    if os.environ.get("BRN_STRICT_MULTISIG") == "1":
        return False
    return True


# ============================================================
# TRUST
# ============================================================
def _setup_trust(chain, node_id_priv, is_origin: bool):
    try:
        from trust_network import TrustNetwork
        trust = TrustNetwork("trust_network.json", is_origin=is_origin, node_sk=node_id_priv)
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
        log.info("[P2P] Broadcast ativado")
        return True
    except Exception as e:
        log.warning("[P2P] broadcast falhou: %s", e)
        return False


# ============================================================
# WALLET WATCHDOG
# ============================================================
def _wallet_watchdog_loop(chain, p2p, trust, stop_evt: threading.Event, interval: int = 30):
    log = logging.getLogger("main")
    log.info("[watchdog] iniciado (interval=%ds)", interval)

    detector = IsolationDetector() if _HAS_ISOLATION else None
    if detector is not None:
        log.info("[watchdog] isolation_detector ativo")

    while not stop_evt.wait(interval):
        if detector is not None and p2p is not None:
            try:
                peers = p2p.discovery.listar_peers()
                for addr in peers:
                    detector.on_peer_seen(addr)
                alert = detector.check(chain.db.height(), peers)
                if alert is not None:
                    log.warning("[ISOLATION] %s: %s", alert.severity.upper(), alert.reason)
            except Exception as _e:
                log.debug("[ISOLATION] check falhou: %s", _e)

        try:
            sk = getattr(chain, "_local_validator_sk", None)
            if sk is None:
                if not getattr(chain, "_watchdog_sk_warned", False):
                    chain._watchdog_sk_warned = True
                    log.warning("[watchdog] SK=None — nó NÃO assina")
                continue

            if getattr(chain, "_watchdog_sk_warned", False):
                chain._watchdog_sk_warned = False
                log.info("[watchdog] SK detectada pk=%s…", sk.pub_hex[:16])

            collector = getattr(chain, "_sign_collector", None)
            if collector is None:
                if not getattr(chain, "_watchdog_collector_warned", False):
                    chain._watchdog_collector_warned = True
                    log.warning("[watchdog] _sign_collector=None")
                continue
            chain._watchdog_collector_warned = False
        except Exception as e:
            log.warning("[watchdog] loop: %s", e)

    log.info("[watchdog] encerrado")


# ============================================================
# TRUST DAEMON
# ============================================================
def _setup_trust_daemon(chain, trust, p2p, password: str, is_origin: bool,
                        stop_evt: threading.Event, interval: int = 30):
    log = logging.getLogger("main")
    if trust is None:
        return None

    try:
        from trust_daemon import start_daemon
        t = start_daemon(chain, trust, p2p, password, is_origin, stop_evt)
        log.info("[TrustDaemon] start_daemon ativado")
        return t
    except ImportError:
        pass
    except Exception as e:
        log.warning("[TrustDaemon] start_daemon falhou: %s", e)

    def _loop():
        while not stop_evt.wait(interval):
            try:
                sk = getattr(chain, "_local_validator_sk", None)
                if sk is not None and hasattr(trust, "heartbeat_self"):
                    try:
                        trust.heartbeat_self(sk.pub_hex.lower())
                    except Exception:
                        pass
            except Exception:
                pass

    t = threading.Thread(target=_loop, daemon=True, name="TrustDaemon")
    t.start()
    log.info("[TrustDaemon] fallback ativado")
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
        log.error("[server] import falhou: %s", e)
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
            continue
        try:
            fn(obj)
        except Exception as e:
            log.error("[server] %s falhou: %s", fn_name, e)

    try:
        if hasattr(srv, "set_sk_validation_state"):
            srv.set_sk_validation_state(SK_VALIDATION_STATE)
            log.info("[server] sk_validation_state injetado")
    except Exception as e:
        log.debug("[server] set_sk_validation_state falhou: %s", e)

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
    except Exception as e:
        logging.getLogger("main").debug("[explorer] não iniciou: %s", e)


# ============================================================
# GUI (v13.9.9: agora abre via HTTP, resolvendo o "Failed to fetch")
# ============================================================
def _start_gui(web_port: int) -> bool:
    try:
        import webview
        # Abre a URL do servidor local em vez do arquivo file://
        url = f"http://127.0.0.1:{web_port}/wallet"
        
        logging.getLogger("main").info("[gui] abrindo %s", url)
        webview.create_window("BRN Wallet", url=url, width=1100, height=780)
        webview.start()
        return True
    except Exception as e:
        logging.getLogger("main").warning("[gui] falha: %s", e)
        return False


# ============================================================
# [v13.9.8] Resolve SK por seed / env / arquivo / geração
# ============================================================
def _resolve_validator_sk() -> tuple:
    log = logging.getLogger("main")

    if not _HAS_WALLET:
        log.error("[SK] wallet.Wallet indisponível")
        return "", "", "", "error"

    # ─── 1. SK explícita ───
    sk_env = os.environ.get("BRN_VALIDATOR_SK_HEX", "").strip()
    if sk_env:
        if len(sk_env) != 64 or any(c not in "0123456789abcdefABCDEF" for c in sk_env):
            log.warning("[SK] BRN_VALIDATOR_SK_HEX inválida (len=%d) — ignorando", len(sk_env))
        else:
            try:
                w = _Wallet(private_key_hex=sk_env)
                log.info("[SK] fonte: env:BRN_VALIDATOR_SK_HEX")
                return sk_env, w.address, w.pub_hex, "env:sk_hex"
            except Exception as e:
                log.warning("[SK] Wallet(sk_env) falhou: %s", e)

    # ─── 2. Seed determinístico ───
    seed = os.environ.get("BRN_VALIDATOR_SEED", "").strip()
    if seed:
        try:
            sk_hex = hashlib.sha256(seed.encode("utf-8")).hexdigest()
            w = _Wallet(private_key_hex=sk_hex)
            log.info("[SK] fonte: env:BRN_VALIDATOR_SEED → sha256(seed) = %s…", sk_hex[:16])
            return sk_hex, w.address, w.pub_hex, "env:seed"
        except Exception as e:
            log.warning("[SK] derivação de seed falhou: %s", e)

    # ─── 3. Arquivo .validator_sk em disco ───
    sk_file = BASE_DIR / ".validator_sk"
    if sk_file.exists():
        try:
            content = sk_file.read_text(encoding="utf-8").strip()
            sk_hex = content.split("|")[0].strip()
            if len(sk_hex) == 64 and all(c in "0123456789abcdefABCDEF" for c in sk_hex):
                w = _Wallet(private_key_hex=sk_hex)
                log.info("[SK] fonte: arquivo %s", sk_file.name)
                return sk_hex, w.address, w.pub_hex, "file"
            else:
                log.warning("[SK] %s corrompido — ignorando", sk_file.name)
        except Exception as e:
            log.warning("[SK] leitura de %s falhou: %s", sk_file.name, e)

    # ─── 4. Gera nova e tenta salvar ───
    try:
        w = _Wallet()
        sk_hex = w.priv_hex
        address = w.address
        pubkey = w.pub_hex
        source = "generated:volatile"
        try:
            sk_file.write_text(sk_hex + "\n", encoding="utf-8")
            try:
                os.chmod(sk_file, 0o600)
            except Exception:
                pass
            source = "generated:file"
        except Exception as e:
            log.warning("[SK] não gravei .validator_sk: %s (volátil)", e)

        log.warning("=" * 64)
        log.warning(" [SK] NOVA CARTEIRA GERADA — GUARDE PARA RECUPERAR")
        log.warning("=" * 64)
        log.warning("   address: %s", address)
        log.warning("   pubkey : %s", pubkey)
        log.warning("   sk_hex : %s", sk_hex)
        log.warning("")
        log.warning("   Para reusar esta carteira em outros boots, sete:")
        log.warning("     BRN_VALIDATOR_SEED=<qualquer-senha-memoravel>")
        log.warning("   Ou copie a sk_hex acima para:")
        log.warning("     BRN_VALIDATOR_SK_HEX=%s", sk_hex)
        log.warning("=" * 64)

        return sk_hex, address, pubkey, source
    except Exception as e:
        log.error("[SK] geração falhou: %s", e)
        return "", "", "", "error"


# ============================================================
# [v13.9.8] AUTO-CONFIG (com resolve)
# ============================================================
def _auto_configure(chain, p2p, trust, web_port: int) -> None:
    log = logging.getLogger("main")

    auto_mine = os.environ.get("BRN_AUTO_MINE", "0") == "1"

    # ─── 1. Resolve SK (env / seed / arquivo / gera) ───
    sk_hex, address, pubkey, source = _resolve_validator_sk()

    SK_VALIDATION_STATE["checked"] = True
    SK_VALIDATION_STATE["ok"] = bool(sk_hex)
    SK_VALIDATION_STATE["error"] = "" if sk_hex else "não foi possível resolver SK"
    SK_VALIDATION_STATE["source"] = source
    SK_VALIDATION_STATE["address_configured"] = address[:40] if address else ""
    SK_VALIDATION_STATE["address_derived"] = address
    SK_VALIDATION_STATE["pubkey_configured"] = pubkey[:40] if pubkey else ""
    SK_VALIDATION_STATE["pubkey_derived"] = pubkey

    # Log detalhado
    log.info("=" * 64)
    log.info(" [SK RESOLUTION v13.9.8]")
    log.info("=" * 64)
    log.info("  source           : %s", source or "(nenhum)")
    log.info("  SK_HEX len       : %d", len(sk_hex) if sk_hex else 0)
    log.info("  ADDRESS          : %s", (address[:40] + "…") if address else "(vazio)")
    log.info("  PUBKEY           : %s", (pubkey[:40] + "…") if pubkey else "(vazio)")

    if not sk_hex:
        log.error("  RESULTADO        : ❌ não foi possível resolver SK")
        log.error("=" * 64)
        return

    log.info("  RESULTADO        : ✅ SK resolvida")
    log.info("=" * 64)

    # ─── 2. Instala SK ───
    if not _HAS_WI:
        log.error("[AUTO-CONFIG] wallet_integrator indisponível")
        return

    log.info("[AUTO-CONFIG] instalando SK via wallet_integrator...")
    try:
        r = _wi.integrate_and_propagate(
            chain, sk_hex,
            address=address, pubkey=pubkey,
            p2p=p2p, trust=trust, logger=log,
        )
        if r.get("ok"):
            log.info(
                "[AUTO-CONFIG] ✅ SK instalada (sk=%s ms=%s pbft=%s in_trust=%s in_top5=%s)",
                r.get("sk_installed"),
                r.get("multisig_ready"),
                r.get("pbft_ready"),
                r.get("in_trust"),
                r.get("in_top5"),
            )
        else:
            log.warning(
                "[AUTO-CONFIG] ⚠️ SK falhou: %s",
                r.get("errors") or r.get("msg"),
            )
    except Exception as e:
        log.exception("[AUTO-CONFIG] integrate falhou: %s", e)

    # ─── 3. Auto-mine ───
    if not auto_mine:
        log.info("[AUTO-CONFIG] BRN_AUTO_MINE desligado — não inicia mineração")
        return

    if not address or not pubkey:
        log.warning("[AUTO-CONFIG] BRN_AUTO_MINE=1 mas faltam ADDRESS/PUBKEY")
        return

    if not _HAS_REQUESTS:
        log.warning("[AUTO-CONFIG] 'requests' não instalado")
        return

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
                data = {"ok": False, "msg": f"HTTP {r.status_code}"}

            if data.get("ok"):
                log.info(
                    "[AUTO-CONFIG] ✅ PoolClient iniciado (miner_id=%s)",
                    data.get("miner_id", "?"),
                )
                return
            else:
                log.warning(
                    "[AUTO-CONFIG] PoolClient tentativa %d: %s",
                    attempt, data.get("msg", "?"),
                )
        except Exception as e:
            log.warning("[AUTO-CONFIG] PoolClient tentativa %d: %s", attempt, e)
        time.sleep(5)

    log.warning("[AUTO-CONFIG] PoolClient não iniciou após 3 tentativas")


def _delayed_auto_configure(chain, p2p, trust, web_port: int) -> None:
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

    # [v13.9.6] Defaults de bootstrap e pool
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
        log.error("[trust] não carregou — multi-sig DESLIGADO")
        if os.environ.get("BRN_STRICT_MULTISIG") == "1":
            return 7
    else:
        log.info("[trust] carregado (origin=%s)", is_origin)

    log.info("[wallet] boot sem carteira (design v13.9.9)")

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
        log.error("[server] HTTP não subiu")

    # [11.5] [AUTO-CONFIG v13.9.8] resolve + valida + instala + mine
    if not args.no_pool_client:
        threading.Thread(
            target=_delayed_auto_configure,
            args=(chain, p2p, trust, web_port),
            daemon=True,
            name="AutoConfig",
        ).start()
        log.info("[AUTO-CONFIG] agendado (3s para resolver SK + instalar + mine)")

    # [12] Explorer
    if not args.no_explorer:
        _start_explorer(chain)

    # [13] GUI ou modo headless
    if not args.headless:
        log.info("[gui] abrindo")
        if _start_gui(web_port):  # <--- Alterado para passar a porta
            return 0
    else:
        log.info("=" * 64)
        log.info(" MODO HEADLESS — consulte /health para estado da SK:")
        log.info("   GET http://%s:%d/health",
                 os.environ.get("BRN_WEB_HOST", "127.0.0.1"), web_port)
        log.info("=" * 64)

    # [14] Shutdown
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
    log.info("   Health   : http://%s:%d/health",
             os.environ.get("BRN_WEB_HOST", "127.0.0.1"), web_port)
    log.info("   P2P      : :%d", p2p_port)
    log.info("   Bootstrap: %s", os.environ.get("BRN_BOOTSTRAP_PEERS", "(vazio)"))
    log.info("   Pool URL : %s", os.environ.get("BRN_POOL_URL", "(vazio)"))
    log.info("   SK source: %s",
             os.environ.get("BRN_VALIDATOR_SEED") and "env:seed"
             or (os.environ.get("BRN_VALIDATOR_SK_HEX") and "env:sk_hex" or "(resolve no boot)"))
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
