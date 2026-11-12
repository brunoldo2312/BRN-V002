"""main.py — Entrypoint do nó BRN (v13.9.10)
================================================================
v13.9.10 (2026-11-11) — MULTI-MÁQUINA (CLIENTES WINDOWS):
  [NEW] --no-auto-config: cliente que só quer sincronizar sem
        tentar auto-SK nem auto-mine.
  [NEW] --no-gui: força headless mesmo em Windows com display.
  [NEW] Defaults NEUTROS: não força IP do Origin. Prioriza:
          1. env var (BRN_BOOTSTRAP_PEERS / BRN_POOL_URL)
          2. bootstrap_peers.json (lido automaticamente)
          3. vazio (cliente só sync, sem pool)
        Evita PCs tentando conectar em Origin errada.
  [NEW] Detecção de contexto:
          - is_origin=True  → auto-mine padrão se env não setado
          - is_origin=False → só auto-mine se BRN_AUTO_MINE=1
  [NEW] Log [CONFIG] mostra origem de cada parâmetro.
  [KEEP] v13.9.9: ping rápido + auto-mine persistente
  [KEEP] v13.9.8: seed determinístico
  [KEEP] v13.9.7: /health.sk_validation
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
    VERSION = "13.9.10"
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

# [VALIDAÇÃO v13.9.7] Wallet
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
    "origin_online": None,
    "auto_mine_status": "idle",
}


# ============================================================
# ARGS
# ============================================================
def parse_args():
    p = argparse.ArgumentParser(prog="main.py")
    p.add_argument("--headless", action="store_true", help="sem GUI")
    p.add_argument("--no-gui", action="store_true", help="força headless mesmo com display")
    p.add_argument("--client-mode", action="store_true", help="nó cliente")
    p.add_argument("--origem", action="store_true", help="força BRN_IS_ORIGIN=1")
    p.add_argument("--no-pool-client", action="store_true", help="não inicia PoolClient")
    p.add_argument("--no-auto-config", action="store_true",
                   help="não tenta auto-SK nem auto-mine (só sync)")
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


def _read_bootstrap_json() -> str:
    """Lê bootstrap_peers.json (lista) → 'peer1,peer2'."""
    p = BASE_DIR / "bootstrap_peers.json"
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return ",".join(str(x).strip() for x in data if str(x).strip())
        if isinstance(data, dict) and "peers" in data:
            return ",".join(str(x).strip() for x in data["peers"] if str(x).strip())
    except Exception:
        pass
    return ""


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
# SETUP (trust, p2p, sync, broadcast, pool, web, explorer, gui)
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


def _setup_sync(chain, p2p):
    try:
        from sync_manager import ChainSyncManager
        sync_mgr = ChainSyncManager(chain, chain.db, p2p)
        sync_mgr.start_sync_loop()
        return sync_mgr
    except Exception as e:
        logging.getLogger("main").warning("[sync] init falhou: %s", e)
        return None


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


def _start_gui() -> bool:
    try:
        import webview
        from app_wallet_v3 import WalletApi
        api = WalletApi()
        wallet_path = BASE_DIR / "index_wallet.html"
        if not wallet_path.exists():
            wallet_path = BASE_DIR / "panel.html"
        url = wallet_path.as_uri() if wallet_path.exists() else "about:blank"
        webview.create_window("BRN Wallet", url=url, width=1100, height=780, js_api=api)
        webview.start()
        return True
    except Exception as e:
        logging.getLogger("main").warning("[gui] falha: %s", e)
        return False


# ============================================================
# [v13.9.8] Resolve SK
# ============================================================
def _resolve_validator_sk() -> tuple:
    log = logging.getLogger("main")

    if not _HAS_WALLET:
        log.error("[SK] wallet.Wallet indisponível")
        return "", "", "", "error"

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

    seed = os.environ.get("BRN_VALIDATOR_SEED", "").strip()
    if seed:
        try:
            sk_hex = hashlib.sha256(seed.encode("utf-8")).hexdigest()
            w = _Wallet(private_key_hex=sk_hex)
            log.info("[SK] fonte: env:BRN_VALIDATOR_SEED → sha256(seed) = %s…", sk_hex[:16])
            return sk_hex, w.address, w.pub_hex, "env:seed"
        except Exception as e:
            log.warning("[SK] derivação de seed falhou: %s", e)

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
        log.warning("   Para reusar: BRN_VALIDATOR_SEED=<qualquer-senha>")
        log.warning("   Ou copie a sk_hex para: BRN_VALIDATOR_SK_HEX=%s", sk_hex)
        log.warning("=" * 64)

        return sk_hex, address, pubkey, source
    except Exception as e:
        log.error("[SK] geração falhou: %s", e)
        return "", "", "", "error"


# ============================================================
# [v13.9.9] Ping rápido no Origin
# ============================================================
def _ping_origin(timeout: float = 3.0) -> bool:
    log = logging.getLogger("main")
    pool_url = os.environ.get("BRN_POOL_URL", "").strip().rstrip("/")
    if not pool_url:
        log.debug("[ORIGIN] BRN_POOL_URL vazio — não posso pingar")
        return False

    if not _HAS_REQUESTS:
        try:
            import urllib.request
            import urllib.error
            req = urllib.request.Request(
                f"{pool_url}/api/status",
                headers={"User-Agent": "brn-main/13.9.10"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status == 200
        except Exception as e:
            log.debug("[ORIGIN] urllib ping falhou: %s", e)
            return False

    try:
        r = _requests.get(f"{pool_url}/api/status", timeout=timeout)
        return r.status_code == 200
    except Exception as e:
        log.debug("[ORIGIN] requests ping falhou: %s", e)
        return False


# ============================================================
# [v13.9.9] Instala SK local sem propagar
# ============================================================
def _install_sk_local_only(chain, sk_hex: str, address: str, pubkey: str) -> bool:
    log = logging.getLogger("main")
    try:
        chain.set_local_validator_sk(sk_hex)
        log.info("[SK] instalada localmente (sem propagar) pk=%s…", pubkey[:16])
        return True
    except Exception as e:
        log.exception("[SK] set_local_validator_sk falhou: %s", e)
        return False


# ============================================================
# [v13.9.9] Auto-mine loop persistente
# ============================================================
def _auto_mine_loop(chain, address: str, pubkey: str, web_port: int,
                    stop_evt: threading.Event) -> None:
    log = logging.getLogger("main")
    SK_VALIDATION_STATE["auto_mine_status"] = "starting"
    log.info("[AUTO-MINE] loop iniciado (checa Origin a cada 30s)")

    pool_url = os.environ.get("BRN_POOL_URL", "").strip()
    failures = 0

    while not stop_evt.is_set():
        try:
            if getattr(chain, "_local_validator_sk", None) is None:
                log.info("[AUTO-MINE] SK ainda ausente — aguardando instalação")
                SK_VALIDATION_STATE["auto_mine_status"] = "waiting_sk"
                if stop_evt.wait(10):
                    break
                continue

            if _HAS_REQUESTS:
                try:
                    r = _requests.get(
                        f"http://127.0.0.1:{web_port}/api/pool/local_status",
                        timeout=5,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        if data.get("running"):
                            SK_VALIDATION_STATE["auto_mine_status"] = "running"
                            log.debug(
                                "[AUTO-MINE] PoolClient já rodando (miner_id=%s)",
                                data.get("miner_id", "?"),
                            )
                            if stop_evt.wait(30):
                                break
                            continue
                except Exception:
                    pass

            origin_online = _ping_origin(timeout=3.0)
            SK_VALIDATION_STATE["origin_online"] = origin_online

            if not origin_online:
                SK_VALIDATION_STATE["auto_mine_status"] = "waiting_origin"
                if failures == 0 or failures % 4 == 0:
                    log.warning(
                        "[AUTO-MINE] Origin offline (%s) — aguardando 30s",
                        pool_url or "(BRN_POOL_URL vazio)",
                    )
                failures += 1
                if stop_evt.wait(30):
                    break
                continue

            SK_VALIDATION_STATE["auto_mine_status"] = "starting_pool"
            log.info("[AUTO-MINE] Origin online — iniciando PoolClient...")

            if not _HAS_REQUESTS:
                log.error("[AUTO-MINE] requests indisponível")
                SK_VALIDATION_STATE["auto_mine_status"] = "error_no_requests"
                if stop_evt.wait(60):
                    break
                continue

            try:
                r = _requests.post(
                    f"http://127.0.0.1:{web_port}/api/pool/start_local",
                    json={"address": address, "pubkey": pubkey},
                    timeout=15,
                )
                try:
                    data = r.json() if r.content else {}
                except Exception:
                    data = {"ok": False, "msg": f"HTTP {r.status_code} sem JSON"}

                if data.get("ok"):
                    failures = 0
                    SK_VALIDATION_STATE["auto_mine_status"] = "running"
                    log.info(
                        "[AUTO-MINE] ✅ PoolClient iniciado (miner_id=%s)",
                        data.get("miner_id", "?"),
                    )
                else:
                    failures += 1
                    SK_VALIDATION_STATE["auto_mine_status"] = f"failed:{data.get('msg', '?')[:60]}"
                    log.warning(
                        "[AUTO-MINE] PoolClient falhou (%d): %s",
                        failures, data.get("msg", "?"),
                    )
            except Exception as e:
                failures += 1
                SK_VALIDATION_STATE["auto_mine_status"] = f"error:{type(e).__name__}"
                log.warning("[AUTO-MINE] start_local exception: %s", e)

        except Exception as e:
            log.exception("[AUTO-MINE] loop exceção: %s", e)

        if stop_evt.wait(30):
            break

    SK_VALIDATION_STATE["auto_mine_status"] = "stopped"
    log.info("[AUTO-MINE] loop encerrado")


# ============================================================
# [v13.9.10] AUTO-CONFIG (multi-máquina)
# ============================================================
def _auto_configure(chain, p2p, trust, web_port: int,
                    is_origin: bool, stop_evt: threading.Event,
                    force_auto_mine: bool = False) -> None:
    log = logging.getLogger("main")

    # ─── 1. Resolve SK ───
    sk_hex, address, pubkey, source = _resolve_validator_sk()

    SK_VALIDATION_STATE["checked"] = True
    SK_VALIDATION_STATE["ok"] = bool(sk_hex)
    SK_VALIDATION_STATE["error"] = "" if sk_hex else "não foi possível resolver SK"
    SK_VALIDATION_STATE["source"] = source
    SK_VALIDATION_STATE["address_configured"] = address[:40] if address else ""
    SK_VALIDATION_STATE["address_derived"] = address
    SK_VALIDATION_STATE["pubkey_configured"] = pubkey[:40] if pubkey else ""
    SK_VALIDATION_STATE["pubkey_derived"] = pubkey

    log.info("=" * 64)
    log.info(" [SK RESOLUTION v13.9.10]")
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

    # ─── 2. Instala SK local (sempre) ───
    _install_sk_local_only(chain, sk_hex, address, pubkey)

    # ─── 3. Ping Origin ───
    log.info("[AUTO-CONFIG] pingando Origin...")
    t0 = time.time()
    origin_online = _ping_origin(timeout=3.0)
    dt_ms = (time.time() - t0) * 1000
    SK_VALIDATION_STATE["origin_online"] = origin_online

    if origin_online:
        log.info("[AUTO-CONFIG] Origin ONLINE (%.0fms) — propagando SK...", dt_ms)
        if not _HAS_WI:
            log.error("[AUTO-CONFIG] wallet_integrator indisponível")
        else:
            try:
                r = _wi.integrate_and_propagate(
                    chain, sk_hex,
                    address=address, pubkey=pubkey,
                    p2p=p2p, trust=trust, logger=log,
                )
                if r.get("ok"):
                    log.info(
                        "[AUTO-CONFIG] ✅ SK propagada (in_trust=%s in_top5=%s propagated=%s)",
                        r.get("in_trust"), r.get("in_top5"), r.get("propagated"),
                    )
                else:
                    log.warning(
                        "[AUTO-CONFIG] ⚠️ propagate falhou: %s",
                        r.get("errors") or r.get("msg"),
                    )
            except Exception as e:
                log.exception("[AUTO-CONFIG] integrate_and_propagate falhou: %s", e)
    else:
        log.warning(
            "[AUTO-CONFIG] ⚠️ Origin OFFLINE (%.0fms) — SK instalada localmente. "
            "Auto-mine monitora até Origin voltar.",
            dt_ms,
        )

    # ─── 4. Decide se inicia auto-mine ───
    env_auto_mine = os.environ.get("BRN_AUTO_MINE", "").strip()
    if env_auto_mine == "1":
        should_mine = True
        reason = "BRN_AUTO_MINE=1"
    elif env_auto_mine == "0":
        should_mine = False
        reason = "BRN_AUTO_MINE=0 (explícito)"
    elif force_auto_mine:
        should_mine = True
        reason = "--auto-mine (flag CLI)"
    elif is_origin:
        should_mine = True
        reason = "origin (padrão)"
    else:
        should_mine = False
        reason = "cliente + BRN_AUTO_MINE não setado"

    log.info("[AUTO-CONFIG] auto-mine: %s (%s)", should_mine, reason)

    if not should_mine:
        SK_VALIDATION_STATE["auto_mine_status"] = "disabled"
        return

    if not address or not pubkey:
        log.warning("[AUTO-CONFIG] faltam ADDRESS/PUBKEY")
        SK_VALIDATION_STATE["auto_mine_status"] = "error_no_addr"
        return

    threading.Thread(
        target=_auto_mine_loop,
        args=(chain, address, pubkey, web_port, stop_evt),
        daemon=True,
        name="AutoMine",
    ).start()


def _delayed_auto_configure(chain, p2p, trust, web_port: int,
                            is_origin: bool, stop_evt: threading.Event,
                            force_auto_mine: bool = False) -> None:
    try:
        time.sleep(3)
        _auto_configure(chain, p2p, trust, web_port, is_origin, stop_evt,
                        force_auto_mine=force_auto_mine)
    except Exception as e:
        logging.getLogger("main").exception("[AUTO-CONFIG] falhou: %s", e)


# ============================================================
# [v13.9.10] CONFIG — resolve defaults neutros
# ============================================================
def _resolve_config(log) -> dict:
    """
    Resolve os parâmetros de rede com prioridade:
      1. env var explícita
      2. bootstrap_peers.json (para bootstrap)
      3. vazio (sem forçar IP)
    """
    cfg = {
        "bootstrap": "",
        "bootstrap_source": "(vazio)",
        "pool_url": "",
        "pool_url_source": "(vazio)",
    }

    # ─── bootstrap ───
    env_bootstrap = os.environ.get("BRN_BOOTSTRAP_PEERS", "").strip()
    if env_bootstrap:
        cfg["bootstrap"] = env_bootstrap
        cfg["bootstrap_source"] = "env:BRN_BOOTSTRAP_PEERS"
    else:
        json_bootstrap = _read_bootstrap_json()
        if json_bootstrap:
            cfg["bootstrap"] = json_bootstrap
            cfg["bootstrap_source"] = "file:bootstrap_peers.json"
            os.environ["BRN_BOOTSTRAP_PEERS"] = json_bootstrap

    # ─── pool_url ───
    env_pool = os.environ.get("BRN_POOL_URL", "").strip()
    if env_pool:
        cfg["pool_url"] = env_pool
        cfg["pool_url_source"] = "env:BRN_POOL_URL"
    else:
        # Tenta inferir do primeiro bootstrap peer (host:porta)
        if cfg["bootstrap"]:
            first = cfg["bootstrap"].split(",")[0].strip()
            if ":" in first:
                host = first.rsplit(":", 1)[0]
                cfg["pool_url"] = f"http://{host}:5000"
                cfg["pool_url_source"] = "inferido do bootstrap"
                os.environ["BRN_POOL_URL"] = cfg["pool_url"]

    return cfg


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

    # [v13.9.10] Resolve config neutra
    cfg = _resolve_config(log)
    log.info("[CONFIG] BRN_BOOTSTRAP_PEERS: %s (%s)",
             cfg["bootstrap"] or "(vazio)", cfg["bootstrap_source"])
    log.info("[CONFIG] BRN_POOL_URL: %s (%s)",
             cfg["pool_url"] or "(vazio)", cfg["pool_url_source"])

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

    log.info("[wallet] boot sem carteira (design v13.9.10)")

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

    # [11.5] [AUTO-CONFIG v13.9.10]
    if not args.no_auto_config and not args.no_pool_client:
        threading.Thread(
            target=_delayed_auto_configure,
            args=(chain, p2p, trust, web_port, is_origin, _stop_bg),
            daemon=True,
            name="AutoConfig",
        ).start()
        log.info("[AUTO-CONFIG] agendado (3s)")
    else:
        if args.no_auto_config:
            log.info("[AUTO-CONFIG] desligado por --no-auto-config")
        else:
            log.info("[AUTO-CONFIG] desligado por --no-pool-client")

    # [12] Explorer
    if not args.no_explorer:
        _start_explorer(chain)

    # [13] GUI ou headless
    want_gui = (not args.headless) and (not args.no_gui)
    if want_gui:
        log.info("[gui] abrindo")
        if _start_gui():
            return 0
    else:
        if args.no_gui:
            log.info("[gui] desligada por --no-gui")
        log.info("=" * 64)
        log.info(" MODO HEADLESS — consulte /health:")
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
    log.info("   Bootstrap: %s", cfg["bootstrap"] or "(vazio)")
    log.info("   Pool URL : %s", cfg["pool_url"] or "(vazio)")
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
