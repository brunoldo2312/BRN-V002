"""main.py — Entrypoint do nó BRN (v13.9.17)
================================================================
v13.9.17 (2026-11-12) — FAST-SYNC ATÉ 6000 + GATE DE MINERAÇÃO:
  [NEW] Fast-sync automático: se a altura do DB no boot for
        < BRN_FAST_SYNC_UNTIL (default 6000), main.py seta
        BRN_ASSUME_VALID_UNTIL=6000 e BRN_BOOTSTRAP_SKIP_SIGS_UNTIL=6000
        ANTES de importar o blockchain. Blocos 1..6000 são aceitos
        sem validação completa (PoW/merkle/sigs/auth_tag) — apenas
        prev_hash + hash calculado. Acima de 6000 a validação FULL
        volta automaticamente sem precisar reiniciar.
  [NEW] Se a altura do DB já for >= 6000 no boot, fast-sync NÃO é
        ativado (validação completa desde o genesis).
  [NEW] _verify_validators_synced(): consulta peers do DB, filtra
        por last_seen recente (BRN_VALIDATOR_MAX_AGE_S, default 300s),
        compara alturas com tolerância (BRN_VALIDATOR_HEIGHT_TOLERANCE,
        default 1) e exige BRN_MIN_VALIDATORS_FOR_MINING validadores
        sincronizados (default 1).
  [NEW] _auto_mine_loop reescrito com 4 gates em sequência:
          1. SK instalada (_local_validator_sk != None)
          2. altura >= BRN_FAST_SYNC_UNTIL (sai do fast-sync)
          3. Origin online e não à frente (tolerância)
          4. _verify_validators_synced() OK
        Só então chama POST /api/pool/start_local. Os 4 gates são
        re-verificados a cada 30s.
  [NEW] SK_VALIDATION_STATE ganhou: local_height, fast_sync_active,
        fast_sync_until, validators_ready, validators_reason,
        validators_detail. Exposto via /health.sk_validation.
  [NEW] _start_web_server() agora recebe stop_evt e mantém thread
        StateRefresh que re-injeta SK_VALIDATION_STATE e
        HTTP_SYNC_PROGRESS a cada 5s — server.py copia o dict no
        setter, sem isso o /health ficaria stale.
  [NEW] CLI flags:
          --fast-sync-until N  (override de BRN_FAST_SYNC_UNTIL)
          --no-fast-sync       (desativa fast-sync; validação completa)
  [NEW] Env vars:
          BRN_FAST_SYNC_UNTIL             (default 6000)
          BRN_MIN_VALIDATORS_FOR_MINING   (default 1)
          BRN_VALIDATOR_HEIGHT_TOLERANCE  (default 1)
          BRN_VALIDATOR_MAX_AGE_S         (default 300)
  [FIX] _auto_mine_loop antes só checava o origin (pool_url); agora
        também verifica o quórum de validadores no DB. Sem isso,
        minerar em fork era possível.
  [KEEP] v13.9.16: fix do HTTP sync, --force-http-resync,
        truncate in-place, diagnóstico no reject.
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
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

try:
    from version import VERSION, BUILD_DATE
except ImportError:
    VERSION = "13.9.17"
    BUILD_DATE = "2026-11-12"

try:
    from isolation_detector import IsolationDetector
    _HAS_ISOLATION = True
except ImportError:
    IsolationDetector = None
    _HAS_ISOLATION = False

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

try:
    from wallet import Wallet as _Wallet
    _HAS_WALLET = True
except ImportError:
    _Wallet = None
    _HAS_WALLET = False


SK_VALIDATION_STATE = {
    "checked": False, "ok": False, "error": "", "source": "",
    "address_configured": "", "address_derived": "",
    "pubkey_configured": "", "pubkey_derived": "",
    "origin_online": None, "auto_mine_status": "idle", "gui_wallet_source": "",
    # [v13.9.17] Fast-sync + validator gating
    "local_height": -1,
    "fast_sync_active": False,
    "fast_sync_until": 0,
    "validators_ready": False,
    "validators_reason": "",
    "validators_detail": {},
}

HTTP_SYNC_PROGRESS = {
    "syncing": False,
    "local_height": -1,
    "target_height": 0,
    "percent": 0.0,
    "best_peer": "",
}


def parse_args():
    p = argparse.ArgumentParser(prog="main.py")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--no-gui", action="store_true")
    p.add_argument("--client-mode", action="store_true")
    p.add_argument("--origem", action="store_true")
    p.add_argument("--no-pool-client", action="store_true")
    p.add_argument("--no-auto-config", action="store_true")
    p.add_argument("--no-interactive", action="store_true")
    p.add_argument("--no-p2p", action="store_true")
    p.add_argument("--no-explorer", action="store_true")
    p.add_argument("--db", metavar="PATH", default=None)
    p.add_argument("--web-port", type=int, default=None)
    p.add_argument("--p2p-port", type=int, default=None)
    p.add_argument("--version", action="store_true")
    p.add_argument("--skip-verify", action="store_true")
    p.add_argument("--force-http-resync", action="store_true",
                   help="renomeia o DB para .bak.TIMESTAMP e sai "
                        "(para forçar re-sync do zero na próxima execução)")
    # [v13.9.17] Fast-sync
    p.add_argument("--fast-sync-until", type=int, default=None,
                   help="Altura até a qual pular validação completa "
                        "(default: BRN_FAST_SYNC_UNTIL ou 6000)")
    p.add_argument("--no-fast-sync", action="store_true",
                   help="Desativa fast-sync (validação completa desde o boot)")
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
        k = k.strip(); v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v; loaded += 1
    return loaded


def _read_bootstrap_json() -> str:
    p = BASE_DIR / "bootstrap_peers.json"
    if not p.exists(): return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list): return ",".join(str(x).strip() for x in data if str(x).strip())
        if isinstance(data, dict) and "peers" in data: return ",".join(str(x).strip() for x in data["peers"] if str(x).strip())
    except Exception: pass
    return ""


def _setup_logging(level: str = "INFO"):
    try:
        from brn_logger import setup_logger, get_logger
        setup_logger(level=level, quiet=False)
        return get_logger("main")
    except Exception:
        logging.basicConfig(level=getattr(logging, level, logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        return logging.getLogger("main")


# ============================================================
# [v13.9.17] FAST-SYNC — helpers
# ============================================================
def _get_current_db_height(db_path: str) -> int:
    """Lê MAX(height) do DB sem abrir via ChainDB (evita import de blockchain)."""
    if not os.path.isabs(db_path):
        db_path = str(BASE_DIR / db_path)
    if not os.path.exists(db_path):
        return -1
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            row = conn.execute("SELECT MAX(height) FROM blocks").fetchone()
            return int(row[0]) if row and row[0] is not None else -1
        finally:
            conn.close()
    except Exception:
        return -1


def _resolve_fast_sync_until(args) -> int:
    """Resolve BRN_FAST_SYNC_UNTIL considerando CLI > env > default."""
    if getattr(args, "no_fast_sync", False):
        return 0
    if getattr(args, "fast_sync_until", None) is not None:
        try:
            return max(0, int(args.fast_sync_until))
        except (ValueError, TypeError):
            pass
    try:
        return max(0, int(os.environ.get("BRN_FAST_SYNC_UNTIL", "6000")))
    except (ValueError, TypeError):
        return 6000


def _maybe_enable_fast_sync(fast_sync_until: int, args) -> None:
    """
    Se a altura atual do DB < fast_sync_until, ativa os bypasses do
    blockchain via env vars ANTES do import de blockchain.

    Regras:
      - CLI --no-fast-sync           → não faz nada (retorna antes)
      - CLI --fast-sync-until N      → override (sobrescreve env)
      - Env BRN_FAST_SYNC_UNTIL=N    → usa N
      - Env BRN_ASSUME_VALID_UNTIL já setado → respeita (não sobrescreve)
      - DB >= fast_sync_until        → não ativa (validação completa)
    """
    log = logging.getLogger("main")
    cli_explicit = getattr(args, "fast_sync_until", None) is not None
    no_fast_sync = getattr(args, "no_fast_sync", False)

    if no_fast_sync:
        log.info("[FAST-SYNC] --no-fast-sync ativo — validação completa desde o boot")
        return

    if fast_sync_until <= 0:
        log.info("[FAST-SYNC] desativado (fast_sync_until=%d)", fast_sync_until)
        return

    db_path = os.environ.get("BRN_DB", "brn_v2_chain.db")
    current_h = _get_current_db_height(db_path)

    if current_h >= fast_sync_until:
        log.info(
            "[FAST-SYNC] DB em %d (>= %d) — fast-sync NÃO ativado. "
            "Validação completa desde o boot.",
            current_h, fast_sync_until,
        )
        return

    # CLI explicit overrides env; caso contrário respeita env existente
    def _should_set(key: str) -> bool:
        if cli_explicit:
            return True
        return not os.environ.get(key)

    if _should_set("BRN_ASSUME_VALID_UNTIL"):
        os.environ["BRN_ASSUME_VALID_UNTIL"] = str(fast_sync_until)
    if _should_set("BRN_BOOTSTRAP_SKIP_SIGS_UNTIL"):
        os.environ["BRN_BOOTSTRAP_SKIP_SIGS_UNTIL"] = str(fast_sync_until)

    log.warning("=" * 64)
    log.warning(" [FAST-SYNC] ATIVADO")
    log.warning("=" * 64)
    log.warning("  altura atual    : %d", current_h)
    log.warning("  sync rápido até : %d", fast_sync_until)
    log.warning("  Blocos 1..%d aceitos SEM validação completa", fast_sync_until)
    log.warning("  (PoW/merkle/multi-sig/auth_tag ignorados; só prev_hash")
    log.warning("   e hash calculado são verificados).")
    log.warning("  A partir de %d, validação FULL é obrigatória.",
                fast_sync_until + 1)
    log.warning("  Mineração só libera APÓS %d E validadores sincronizados.",
                fast_sync_until)
    log.warning("=" * 64)


def _verify_validators_synced(chain, min_validators: int = 1,
                              tolerance: int = 1,
                              max_age_s: int = 300) -> tuple:
    """
    [v13.9.17] Verifica que pelo menos `min_validators` peers estão
    na mesma altura que nós (dentro de `tolerance`).

    Fonte: chain.db.listar_peers(apenas_ativos=False) — peers vistos
    via P2P handshake. `last_seen` do DB indica freshness; peers com
    last_seen < now - max_age_s são ignorados (stale).

    Retorna (ok: bool, reason: str, details: dict).
    """
    our_h = int(chain.db.height())
    now = time.time()

    details = {
        "our_height": our_h,
        "min_validators": min_validators,
        "tolerance": tolerance,
        "max_age_s": max_age_s,
        "synced_peers": [],
        "ahead_peers": [],
        "behind_peers": [],
        "stale_peers": [],
        "total_peers": 0,
    }

    if our_h < 0:
        return False, f"nosso DB está vazio (height={our_h})", details

    try:
        peers = chain.db.listar_peers(apenas_ativos=False)
    except Exception as e:
        return False, f"falha ao listar peers do DB: {e}", details

    peers = peers or []
    details["total_peers"] = len(peers)

    for p in peers:
        if not isinstance(p, dict):
            continue
        addr = (p.get("address") or "").strip()
        if not addr:
            continue
        ip = addr.rsplit(":", 1)[0] if ":" in addr else addr
        if not ip or ip.startswith("127.") or ip == "0.0.0.0":
            continue

        try:
            peer_h = int(p.get("height", 0) or 0)
        except (ValueError, TypeError):
            peer_h = 0
        try:
            last_seen = float(p.get("last_seen", 0) or 0)
        except (ValueError, TypeError):
            last_seen = 0.0
        age_s = int(now - last_seen) if last_seen > 0 else -1

        entry = {"ip": ip, "height": peer_h, "age_s": age_s}

        if age_s < 0 or age_s > max_age_s:
            details["stale_peers"].append(entry)
            continue

        delta = peer_h - our_h
        if abs(delta) <= tolerance:
            details["synced_peers"].append({**entry, "delta": delta})
        elif delta > 0:
            details["ahead_peers"].append({**entry, "delta": delta})
        else:
            details["behind_peers"].append({**entry, "delta": delta})

    if len(details["synced_peers"]) >= min_validators:
        return True, (
            f"{len(details['synced_peers'])} validador(es) em altura "
            f"~{our_h} (tolerância ±{tolerance})"
        ), details

    return False, (
        f"apenas {len(details['synced_peers'])}/{min_validators} "
        f"validador(es) em altura ~{our_h} "
        f"(à frente={len(details['ahead_peers'])}, "
        f"atrás={len(details['behind_peers'])}, "
        f"stale={len(details['stale_peers'])}, "
        f"total={details['total_peers']})"
    ), details


# ============================================================
# Identidade / wallet / trust / p2p / sync / pool
# ============================================================
def _get_identity_password() -> str:
    log = logging.getLogger("main")
    pw = (os.environ.get("BRN_NODE_PASSWORD", "").strip() or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip())
    if pw: return pw
    local_file = BASE_DIR / ".node_password"
    if local_file.exists():
        try:
            pw = local_file.read_text(encoding="utf-8").strip()
            if pw: return pw
        except Exception: pass
    pw = secrets.token_hex(16)
    try:
        local_file.write_text(pw + "\n", encoding="utf-8")
        try: os.chmod(local_file, 0o600)
        except Exception: pass
        log.info("[identity] senha local criada em %s", local_file.name)
        return pw
    except Exception as e:
        log.warning("[identity] não gravei %s: %s", local_file.name, e)
        return ""


def _load_or_create_node_identity():
    log = logging.getLogger("main")
    try:
        from secure_store import load_node_identity, save_node_identity
        from crypto import Ed25519PrivateKey
    except Exception as e: raise RuntimeError(f"crypto/secure_store indisponíveis: {e}") from e

    pw_env = (os.environ.get("BRN_NODE_PASSWORD", "").strip() or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip())
    pw = _get_identity_password()
    p = BASE_DIR / "node_identity.enc"

    if p.exists():
        if not pw: raise RuntimeError(f"[identity] {p.name} sem senha resolvível")
        try:
            data = load_node_identity(pw, str(p))
            if not data or not data.get("sk"): raise ValueError("arquivo sem 'sk'")
            priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
            pub = priv.public_key().public_bytes_raw().hex()
            log.info("[identity] carregada pub=%s…", pub[:16])
            return priv, pub
        except Exception as e:
            if pw_env: raise RuntimeError(f"[identity] {p.name} não abriu com senha do ambiente: {e}") from e
            backup = p.with_suffix(f".enc.bak.{int(time.time())}")
            try:
                p.rename(backup)
                log.warning("[identity] %s inacessível — backup em %s", p.name, backup.name)
            except Exception as e2: raise RuntimeError(f"[identity] não renomeei {p.name}: {e2}") from e2

    if not pw:
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key().public_bytes_raw().hex()
        log.warning("[identity] VOLÁTIL pub=%s…", pub[:16])
        return priv, pub

    priv = Ed25519PrivateKey.generate()
    sk_hex = priv.private_bytes_raw().hex()
    pub = priv.public_key().public_bytes_raw().hex()
    try:
        save_node_identity({"sk": sk_hex, "pub": pub, "created_at": int(time.time())}, pw, str(p))
        log.info("[identity] CRIADA pub=%s…", pub[:16])
    except Exception as e: raise RuntimeError(f"[identity] não salvei {p.name}: {e}") from e
    return priv, pub


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
    return os.environ.get("BRN_STRICT_MULTISIG") != "1"


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
        chain.set_p2p(p2p); p2p.start()
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
    if not p2p: return False
    try:
        chain.on_block_mined = p2p.broadcast_block
        log.info("[P2P] Broadcast ativado")
        return True
    except Exception as e:
        log.warning("[P2P] broadcast falhou: %s", e)
        return False


def _setup_pool(chain, is_origin: bool):
    if not is_origin: return None
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
    if detector is not None: log.info("[watchdog] isolation_detector ativo")

    while not stop_evt.wait(interval):
        if detector is not None and p2p is not None:
            try:
                peers = p2p.discovery.listar_peers()
                for addr in peers: detector.on_peer_seen(addr)
                alert = detector.check(chain.db.height(), peers)
                if alert is not None: log.warning("[ISOLATION] %s: %s", alert.severity.upper(), alert.reason)
            except Exception as _e: log.debug("[ISOLATION] check falhou: %s", _e)
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
        except Exception as e: log.warning("[watchdog] loop: %s", e)
    log.info("[watchdog] encerrado")


def _setup_trust_daemon(chain, trust, p2p, password: str, is_origin: bool, stop_evt: threading.Event, interval: int = 30):
    log = logging.getLogger("main")
    if trust is None: return None
    try:
        from trust_daemon import start_daemon
        t = start_daemon(chain, trust, p2p, password, is_origin, stop_evt)
        log.info("[TrustDaemon] start_daemon ativado")
        return t
    except ImportError: pass
    except Exception as e: log.warning("[TrustDaemon] start_daemon falhou: %s", e)

    def _loop():
        while not stop_evt.wait(interval):
            try:
                sk = getattr(chain, "_local_validator_sk", None)
                if sk is not None and hasattr(trust, "heartbeat_self"):
                    try: trust.heartbeat_self(sk.pub_hex.lower())
                    except Exception: pass
            except Exception: pass
    t = threading.Thread(target=_loop, daemon=True, name="TrustDaemon")
    t.start(); log.info("[TrustDaemon] fallback ativado")
    return t


def _start_web_server(chain, p2p, pool, sync_mgr, trust, web_port: int,
                     stop_evt: threading.Event = None):
    log = logging.getLogger("main")
    try: import server as srv
    except Exception as e:
        log.error("[server] import falhou: %s", e)
        return False

    srv.set_chain(chain)
    for fn_name, obj in (("set_p2p_manager", p2p), ("set_pool_manager", pool),
                          ("set_sync_manager", sync_mgr), ("set_trust_network", trust)):
        if obj is None: continue
        fn = getattr(srv, fn_name, None)
        if fn is None: continue
        try: fn(obj)
        except Exception as e: log.error("[server] %s falhou: %s", fn_name, e)

    try:
        if hasattr(srv, "set_sk_validation_state"):
            srv.set_sk_validation_state(SK_VALIDATION_STATE)
            log.info("[server] sk_validation_state injetado")
        if hasattr(srv, "set_http_sync_state"):
            srv.set_http_sync_state(HTTP_SYNC_PROGRESS)
            log.info("[server] http_sync_state injetado")
    except Exception as e: log.debug("[server] set_state falhou: %s", e)

    # [v13.9.17] Refresh thread: server copia o dict no setter,
    # sem isso /health.sk_validation fica stale.
    if stop_evt is not None and hasattr(srv, "set_sk_validation_state"):
        def _refresh():
            while not stop_evt.wait(5):
                try:
                    srv.set_sk_validation_state(SK_VALIDATION_STATE)
                    if hasattr(srv, "set_http_sync_state"):
                        srv.set_http_sync_state(HTTP_SYNC_PROGRESS)
                except Exception:
                    pass
        threading.Thread(target=_refresh, daemon=True, name="StateRefresh").start()
        log.info("[server] state refresh thread iniciada (5s)")

    def _run():
        try: srv.app.run(host=os.environ.get("BRN_WEB_HOST", "127.0.0.1"), port=web_port, threaded=True, use_reloader=False)
        except Exception as e: log.error("[server] app.run morreu: %s", e)

    threading.Thread(target=_run, daemon=True, name="HTTP").start()
    log.info("[server] HTTP ativo na porta %d", web_port)
    return True


def _start_explorer(chain):
    try:
        import explorer as exp
        if hasattr(exp, "set_chain"): exp.set_chain(chain)
        def _run(): exp.app.run(host="0.0.0.0", port=int(os.environ.get("BRN_EXPLORER_PORT", "8080")), threaded=True, use_reloader=False)
        threading.Thread(target=_run, daemon=True, name="EXP").start()
    except Exception as e: logging.getLogger("main").debug("[explorer] não iniciou: %s", e)


def _candidate_gui_passwords() -> list:
    cands = []; seen = set()
    def _add(src: str, val: str):
        v = (val or "").strip()
        if not v or v in seen: return
        seen.add(v); cands.append((src, v))
    _add("env:BRN_WALLET_SESSION_PASS", os.environ.get("BRN_WALLET_SESSION_PASS", ""))
    _add("env:BRN_VALIDATOR_SEED", os.environ.get("BRN_VALIDATOR_SEED", ""))
    _add("env:BRN_NODE_PASSWORD", os.environ.get("BRN_NODE_PASSWORD", ""))
    p = BASE_DIR / ".node_password"
    if p.exists():
        try: _add("file:.node_password", p.read_text(encoding="utf-8").strip())
        except Exception: pass
    return cands


def _prompt_password_interactive(api, max_attempts: int = 3) -> tuple:
    log = logging.getLogger("main")
    try:
        if not sys.stdin.isatty(): return False, "no_stdin", "", "stdin não é TTY"
    except Exception: return False, "no_stdin", "", "stdin check falhou"

    print("\n" + "=" * 60)
    print(" 🔐 ABRIR CARTEIRA BRN")
    print("=" * 60)
    print(" Carteira salva encontrada em disco.")
    print(" Digite a senha para abrir (ou ENTER para abrir a GUI vazia).\n")

    for attempt in range(1, max_attempts + 1):
        try: pw = input(f" Senha [{attempt}/{max_attempts}]: ")
        except (EOFError, KeyboardInterrupt):
            print("\n [cancelado]"); return False, "cancelled", "", "cancelado"
        if not pw:
            print(" [vazio] — abrindo GUI sem carteira"); return False, "empty_input", "", "usuário deixou vazio"
        try:
            r = api.unlock_wallet(pw)
            if r.get("ok"):
                addr = r.get("address", "")
                if r.get("sk_installed"):
                    print(f" ✅ Carteira aberta: {addr}")
                    SK_VALIDATION_STATE["gui_wallet_source"] = f"interactive:{attempt}"
                    SK_VALIDATION_STATE["address_derived"] = addr
                    SK_VALIDATION_STATE["address_configured"] = addr[:40] if addr else ""
                    return True, f"interactive:{attempt}", addr, ""
                else:
                    print(f" ⚠️  Carteira aberta: {addr}")
                    print(f" ⚠️  MAS SK NÃO instalada no nó (sk_installed=False)")
                    for w in (r.get("warnings") or []):
                        print(f"     [WARN] {w}")
                    for e in (r.get("errors") or []):
                        print(f"     [ERR]  {e}")
                    SK_VALIDATION_STATE["gui_wallet_source"] = f"interactive:{attempt}:no_sk"
                    SK_VALIDATION_STATE["address_derived"] = addr
                    SK_VALIDATION_STATE["address_configured"] = addr[:40] if addr else ""
                    return True, f"interactive:{attempt}:no_sk", addr, "sk_not_installed"
            else:
                print(f" ❌ Senha incorreta: {r.get('msg', '?')}")
        except Exception as e: print(f" ❌ Erro: {e}")

    print(f"\n ❌ Senha incorreta {max_attempts} vezes. Abrindo GUI sem carteira.")
    SK_VALIDATION_STATE["gui_wallet_source"] = "interactive_failed"
    return False, "interactive_failed", "", f"{max_attempts} tentativas falharam"


def _auto_unlock_gui(api, allow_interactive: bool = True) -> tuple:
    log = logging.getLogger("main")
    try:
        has = api.has_wallet()
        if not has.get("exists"):
            log.info("[GUI] nenhuma carteira em disco — GUI abrirá para GERAR nova")
            SK_VALIDATION_STATE["gui_wallet_source"] = "no_wallet_on_disk"
            return False, "no_wallet_on_disk", "", "sem carteira em disco"
    except Exception as e:
        log.warning("[GUI] has_wallet() falhou: %s", e); return False, "has_wallet_error", "", str(e)

    try:
        aw = api.get_active_wallet()
        if aw.get("ok") and aw.get("address"):
            log.info("[GUI] carteira JÁ desbloqueada: %s…", aw["address"][:32])
            SK_VALIDATION_STATE["gui_wallet_source"] = "already_unlocked"
            return True, "already_unlocked", aw["address"], ""
    except Exception: pass

    cands = _candidate_gui_passwords()
    for src, pw in cands:
        try:
            log.info("[GUI] tentando unlock com %s (len=%d)…", src, len(pw))
            r = api.unlock_wallet(pw)
            if r.get("ok"):
                addr = r.get("address", "")
                if r.get("sk_installed"):
                    log.info("[GUI] ✅ carteira ABERTA com %s — address=%s", src, addr)
                else:
                    log.warning(
                        "[GUI] ⚠️ carteira ABERTA com %s mas SK NÃO instalada "
                        "(sk_installed=False). Nó NÃO vai assinar. "
                        "warnings=%s errors=%s",
                        src, r.get("warnings"), r.get("errors"),
                    )
                SK_VALIDATION_STATE["gui_wallet_source"] = (
                    src if r.get("sk_installed") else f"{src}:no_sk"
                )
                SK_VALIDATION_STATE["address_derived"] = addr
                SK_VALIDATION_STATE["address_configured"] = addr[:40] if addr else ""
                return True, src, addr, ("" if r.get("sk_installed") else "sk_not_installed")
        except Exception as e: log.debug("[GUI] %s exception: %s", src, e)

    if allow_interactive:
        log.info("[GUI] nenhuma senha do env funcionou (%d tentadas). Pedindo no console...", len(cands))
        ok, src, addr, err = _prompt_password_interactive(api, max_attempts=3)
        if ok:
            log.info("[GUI] ✅ carteira ABERTA via %s — address=%s", src, addr)
            return True, src, addr, err
        else:
            log.warning("[GUI] prompt falhou (%s) — GUI abrirá sem carteira.", src)
            return False, src, "", err

    log.warning("[GUI] ❌ não consegui abrir a carteira. Clique em 'Carregar' ou 'Gerar nova' na GUI.")
    SK_VALIDATION_STATE["gui_wallet_source"] = "all_passwords_failed"
    return False, "all_passwords_failed", "", "todas as senhas falharam"


def _start_gui(allow_interactive: bool = True) -> bool:
    try:
        import webview
        from app_wallet_v3 import WalletApi
        log = logging.getLogger("main")
        api = WalletApi()
        log.info("[GUI] tentando abrir carteira salva...")
        ok, source, addr, err = _auto_unlock_gui(api, allow_interactive=allow_interactive)
        if ok: log.info("[GUI] auto-unlock OK (source=%s) — janela com carteira", source)
        elif source == "no_wallet_on_disk": log.info("[GUI] sem carteira — janela abre em modo CRIAÇÃO")
        else: log.info("[GUI] sem carteira aberta (%s) — janela abre vazia", source)

        wallet_path = BASE_DIR / "index_wallet.html"
        if not wallet_path.exists(): wallet_path = BASE_DIR / "panel.html"
        url = wallet_path.as_uri() if wallet_path.exists() else "about:blank"
        log.info("[gui] abrindo %s", wallet_path.name)
        webview.create_window("BRN Wallet", url=url, width=1100, height=780, js_api=api)
        webview.start()
        return True
    except Exception as e:
        logging.getLogger("main").warning("[gui] falha: %s", e); return False


def _resolve_validator_sk() -> tuple:
    log = logging.getLogger("main")
    if not _HAS_WALLET:
        log.error("[SK] wallet.Wallet indisponível"); return "", "", "", "error"

    sk_env = os.environ.get("BRN_VALIDATOR_SK_HEX", "").strip()
    if sk_env:
        if len(sk_env) != 64 or any(c not in "0123456789abcdefABCDEF" for c in sk_env):
            log.warning("[SK] BRN_VALIDATOR_SK_HEX inválida (len=%d) — ignorando", len(sk_env))
        else:
            try:
                w = _Wallet(private_key_hex=sk_env)
                log.info("[SK] fonte: env:BRN_VALIDATOR_SK_HEX")
                return sk_env, w.address, w.pub_hex, "env:sk_hex"
            except Exception as e: log.warning("[SK] Wallet(sk_env) falhou: %s", e)

    seed = os.environ.get("BRN_VALIDATOR_SEED",