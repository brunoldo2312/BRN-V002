"""main.py — Entrypoint do nó BRN (v13.9.15)
================================================================
v13.9.15 (2026-11-12) — PAINEL DE DOWNLOAD HTTP:
  [NEW] _force_http_sync_loop() atualiza a variável global 
        HTTP_SYNC_PROGRESS com o progresso do download.
  [NEW] O server.py vai expor isso em /api/sync-info, permitindo
        que o painel web mostre a porcentagem e o melhor peer.
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
    VERSION = "13.9.15"
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
}

# [NOVO] Estado do download HTTP para o painel
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

def _start_web_server(chain, p2p, pool, sync_mgr, trust, web_port: int):
    log = logging.getLogger("main")
    try: import server as srv
    except Exception as e:
        log.error("[server] import falhou: %s", e)
        return False

    srv.set_chain(chain)
    for fn_name, obj in (("set_p2p_manager", p2p), ("set_pool_manager", pool), ("set_sync_manager", sync_mgr), ("set_trust_network", trust)):
        if obj is None: continue
        fn = getattr(srv, fn_name, None)
        if fn is None: continue
        try: fn(obj)
        except Exception as e: log.error("[server] %s falhou: %s", fn_name, e)

    try:
        if hasattr(srv, "set_sk_validation_state"):
            srv.set_sk_validation_state(SK_VALIDATION_STATE)
            log.info("[server] sk_validation_state injetado")
        if hasattr(srv, "set_http_sync_state"): # [NOVO]
            srv.set_http_sync_state(HTTP_SYNC_PROGRESS)
            log.info("[server] http_sync_state injetado")
    except Exception as e: log.debug("[server] set_state falhou: %s", e)

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
                print(f" ✅ Carteira aberta: {addr}")
                SK_VALIDATION_STATE["gui_wallet_source"] = f"interactive:{attempt}"
                SK_VALIDATION_STATE["address_derived"] = addr
                SK_VALIDATION_STATE["address_configured"] = addr[:40] if addr else ""
                return True, f"interactive:{attempt}", addr, ""
            else: print(f" ❌ Senha incorreta: {r.get('msg', '?')}")
        except Exception as e: print(f" ❌ Erro: {e}")

    print(f"\n ❌ Senha incorreta {max_attempts} vezes. Abrindo GUI sem carteira.")
    print("    Use 'Carregar' ou 'Gerar nova' na interface.")
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
                log.info("[GUI] ✅ carteira ABERTA com %s — address=%s", src, addr)
                SK_VALIDATION_STATE["gui_wallet_source"] = src
                SK_VALIDATION_STATE["address_derived"] = addr
                SK_VALIDATION_STATE["address_configured"] = addr[:40] if addr else ""
                return True, src, addr, ""
        except Exception as e: log.debug("[GUI] %s exception: %s", src, e)

    if allow_interactive:
        log.info("[GUI] nenhuma senha do env funcionou (%d tentadas). Pedindo no console...", len(cands))
        ok, src, addr, err = _prompt_password_interactive(api, max_attempts=3)
        if ok:
            log.info("[GUI] ✅ carteira ABERTA via %s — address=%s", src, addr)
            return True, src, addr, ""
        else:
            log.warning("[GUI] prompt falhou (%s) — GUI abrirá sem carteira. Use 'Carregar' ou 'Gerar nova'.", src)
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

    seed = os.environ.get("BRN_VALIDATOR_SEED", "").strip()
    if seed:
        try:
            sk_hex = hashlib.sha256(seed.encode("utf-8")).hexdigest()
            w = _Wallet(private_key_hex=sk_hex)
            log.info("[SK] fonte: env:BRN_VALIDATOR_SEED → sha256(seed) = %s…", sk_hex[:16])
            return sk_hex, w.address, w.pub_hex, "env:seed"
        except Exception as e: log.warning("[SK] derivação de seed falhou: %s", e)

    sk_file = BASE_DIR / ".validator_sk"
    if sk_file.exists():
        try:
            content = sk_file.read_text(encoding="utf-8").strip()
            sk_hex = content.split("|")[0].strip()
            if len(sk_hex) == 64 and all(c in "0123456789abcdefABCDEF" for c in sk_hex):
                w = _Wallet(private_key_hex=sk_hex)
                log.info("[SK] fonte: arquivo %s", sk_file.name)
                return sk_hex, w.address, w.pub_hex, "file"
            else: log.warning("[SK] %s corrompido — ignorando", sk_file.name)
        except Exception as e: log.warning("[SK] leitura de %s falhou: %s", sk_file.name, e)

    try:
        w = _Wallet()
        sk_hex = w.priv_hex; address = w.address; pubkey = w.pub_hex
        source = "generated:volatile"
        try:
            sk_file.write_text(sk_hex + "\n", encoding="utf-8")
            try: os.chmod(sk_file, 0o600)
            except Exception: pass
            source = "generated:file"
        except Exception as e: log.warning("[SK] não gravei .validator_sk: %s (volátil)", e)

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
        log.error("[SK] geração falhou: %s", e); return "", "", "", "error"

def _ping_origin(timeout: float = 3.0) -> bool:
    log = logging.getLogger("main")
    pool_url = os.environ.get("BRN_POOL_URL", "").strip().rstrip("/")
    if not pool_url: return False
    if not _HAS_REQUESTS:
        try:
            import urllib.request
            req = urllib.request.Request(f"{pool_url}/api/status", headers={"User-Agent": "brn-main/13.9.15"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status == 200
        except Exception as e:
            log.debug("[ORIGIN] urllib ping falhou: %s", e); return False
    try:
        r = _requests.get(f"{pool_url}/api/status", timeout=timeout)
        return r.status_code == 200
    except Exception as e:
        log.debug("[ORIGIN] requests ping falhou: %s", e); return False

def _install_sk_local_only(chain, sk_hex: str, address: str, pubkey: str) -> bool:
    log = logging.getLogger("main")
    try:
        chain.set_local_validator_sk(sk_hex)
        log.info("[SK] instalada localmente (sem propagar) pk=%s…", pubkey[:16])
        return True
    except Exception as e:
        log.exception("[SK] set_local_validator_sk falhou: %s", e); return False

# ============================================================
# [v13.9.15] FALLBACK HTTP SYNC COM PROGRESSO
# ============================================================
def _force_http_sync_loop(chain, pool_url, stop_evt):
    import requests
    import time
    log = logging.getLogger("main")
    log.info("[HTTP-SYNC] Thread iniciada (fallback via HTTP)")
    time.sleep(15)
    
    while not stop_evt.is_set():
        try:
            local_h = chain.db.height()
            if local_h >= 0:
                # Se já sincronizou, zera o progresso no painel
                HTTP_SYNC_PROGRESS["syncing"] = False
                HTTP_SYNC_PROGRESS["percent"] = 100.0
                HTTP_SYNC_PROGRESS["best_peer"] = pool_url
                stop_evt.wait(60)
                continue
                
            try:
                r = requests.get(f"{pool_url}/api/status", timeout=10)
                target_h = r.json().get("height", 0)
            except Exception as e:
                log.warning(f"[HTTP-SYNC] Origin inacessível: {e}")
                stop_evt.wait(30)
                continue
                
            if target_h > 0:
                HTTP_SYNC_PROGRESS["syncing"] = True
                HTTP_SYNC_PROGRESS["target_height"] = target_h
                HTTP_SYNC_PROGRESS["best_peer"] = pool_url
                HTTP_SYNC_PROGRESS["local_height"] = local_h
                
                log.info(f"[HTTP-SYNC] Iniciando download: local={local_h}, target={target_h}")
                start = max(0, local_h + 1)
                batch = 100
                while start <= target_h and not stop_evt.is_set():
                    try:
                        r = requests.get(f"{pool_url}/api/blocks?start={start}&limit={batch}", timeout=30)
                        blocks = r.json().get("blocks", [])
                        if not blocks: break
                            
                        for blk in blocks:
                            ok, msg = chain.accept_block(blk)
                            if not ok:
                                log.error(f"[HTTP-SYNC] Bloco #{blk.get('height')} rejeitado: {msg}")
                                break
                        
                        start += len(blocks)
                        local_h = chain.db.height()
                        HTTP_SYNC_PROGRESS["local_height"] = local_h
                        if target_h > 0:
                            HTTP_SYNC_PROGRESS["percent"] = round((local_h / target_h) * 100, 1)
                        log.info(f"[HTTP-SYNC] Progresso: {local_h}/{target_h} ({HTTP_SYNC_PROGRESS['percent']}%)")
                    except Exception as e:
                        log.warning(f"[HTTP-SYNC] Erro no lote {start}: {e}")
                        break
            else:
                log.info("[HTTP-SYNC] Origin com altura 0. Aguardando.")
                stop_evt.wait(30)
        except Exception as e:
            log.error(f"[HTTP-SYNC] Erro no loop: {e}")
            stop_evt.wait(30)

def _auto_mine_loop(chain, address: str, pubkey: str, web_port: int, stop_evt: threading.Event) -> None:
    log = logging.getLogger("main")
    SK_VALIDATION_STATE["auto_mine_status"] = "starting"
    log.info("[AUTO-MINE] loop iniciado (checa Origin e Sync a cada 30s)")
    pool_url = os.environ.get("BRN_POOL_URL", "").strip()
    failures = 0

    while not stop_evt.is_set():
        try:
            if getattr(chain, "_local_validator_sk", None) is None:
                SK_VALIDATION_STATE["auto_mine_status"] = "waiting_sk"
                if stop_evt.wait(10): break
                continue

            if _HAS_REQUESTS:
                try:
                    r = _requests.get(f"http://127.0.0.1:{web_port}/api/pool/local_status", timeout=5)
                    if r.status_code == 200:
                        data = r.json()
                        if data.get("running"):
                            SK_VALIDATION_STATE["auto_mine_status"] = "running"
                            if stop_evt.wait(30): break
                            continue
                except Exception: pass

            origin_online = _ping_origin(timeout=3.0)
            SK_VALIDATION_STATE["origin_online"] = origin_online

            if not origin_online:
                SK_VALIDATION_STATE["auto_mine_status"] = "waiting_origin"
                if failures == 0 or failures % 4 == 0:
                    log.warning("[AUTO-MINE] Origin offline (%s) — aguardando 30s", pool_url or "(vazio)")
                failures += 1
                if stop_evt.wait(30): break
                continue

            if _HAS_REQUESTS:
                try:
                    r = _requests.get(f"{pool_url}/api/status", timeout=10)
                    remote_h = r.json().get("height", 0)
                    local_h = chain.db.height()
                    
                    if local_h < remote_h - 1:
                        log.info("[AUTO-MINE] Aguardando sincronização: local=%d / target=%d", local_h, remote_h)
                        SK_VALIDATION_STATE["auto_mine_status"] = "waiting_sync"
                        if stop_evt.wait(15): break
                        continue
                except Exception as e:
                    log.warning("[AUTO-MINE] Erro ao checar sincronização: %s", e)
                    if stop_evt.wait(15): break
                    continue

            SK_VALIDATION_STATE["auto_mine_status"] = "starting_pool"
            log.info("[AUTO-MINE] Sincronizado! Origin online — iniciando PoolClient...")

            if not _HAS_REQUESTS:
                SK_VALIDATION_STATE["auto_mine_status"] = "error_no_requests"
                if stop_evt.wait(60): break
                continue

            try:
                r = _requests.post(f"http://127.0.0.1:{web_port}/api/pool/start_local", json={"address": address, "pubkey": pubkey}, timeout=15)
                try: data = r.json() if r.content else {}
                except Exception: data = {"ok": False, "msg": f"HTTP {r.status_code}"}

                if data.get("ok"):
                    failures = 0
                    SK_VALIDATION_STATE["auto_mine_status"] = "running"
                    log.info("[AUTO-MINE] ✅ PoolClient iniciado (miner_id=%s)", data.get("miner_id", "?"))
                else:
                    failures += 1
                    SK_VALIDATION_STATE["auto_mine_status"] = f"failed:{data.get('msg', '?')[:60]}"
                    log.warning("[AUTO-MINE] PoolClient falhou (%d): %s", failures, data.get("msg", "?"))
            except Exception as e:
                failures += 1
                SK_VALIDATION_STATE["auto_mine_status"] = f"error:{type(e).__name__}"
                log.warning("[AUTO-MINE] start_local exception: %s", e)
        except Exception as e:
            log.exception("[AUTO-MINE] loop exceção: %s", e)
        if stop_evt.wait(30): break
    SK_VALIDATION_STATE["auto_mine_status"] = "stopped"

def _auto_configure(chain, p2p, trust, web_port: int, is_origin: bool, stop_evt: threading.Event, force_auto_mine: bool = False) -> None:
    log = logging.getLogger("main")
    sk_hex, address, pubkey, source = _resolve_validator_sk()

    SK_VALIDATION_STATE.update({
        "checked": True, "ok": bool(sk_hex), "source": source,
        "address_configured": address[:40] if address else "",
        "address_derived": address,
        "pubkey_configured": pubkey[:40] if pubkey else "",
        "pubkey_derived": pubkey,
        "error": "" if sk_hex else "não foi possível resolver SK"
    })

    log.info("=" * 64)
    log.info(" [SK RESOLUTION v13.9.15]")
    log.info("=" * 64)
    log.info("  source           : %s", source or "(nenhum)")
    log.info("  SK_HEX len       : %d", len(sk_hex) if sk_hex else 0)
    log.info("  ADDRESS          : %s", (address[:40] + "…") if address else "(vazio)")
    log.info("  PUBKEY           : %s", (pubkey[:40] + "…") if pubkey else "(vazio)")

    if not sk_hex:
        log.error("  RESULTADO        : ❌ não foi possível resolver SK")
        log.error("=" * 64); return

    log.info("  RESULTADO        : ✅ SK resolvida")
    log.info("=" * 64)
    _install_sk_local_only(chain, sk_hex, address, pubkey)

    log.info("[AUTO-CONFIG] pingando Origin...")
    origin_online = _ping_origin(timeout=3.0)
    SK_VALIDATION_STATE["origin_online"] = origin_online

    if origin_online:
        log.info("[AUTO-CONFIG] Origin ONLINE — propagando SK...")
        if not _HAS_WI: log.error("[AUTO-CONFIG] wallet_integrator indisponível")
        else:
            try:
                r = _wi.integrate_and_propagate(chain, sk_hex, address=address, pubkey=pubkey, p2p=p2p, trust=trust, logger=log)
                if r.get("ok"): log.info("[AUTO-CONFIG] ✅ SK propagada (in_trust=%s in_top5=%s propagated=%s)", r.get("in_trust"), r.get("in_top5"), r.get("propagated"))
                else: log.warning("[AUTO-CONFIG] ⚠️ propagate falhou: %s", r.get("errors") or r.get("msg"))
            except Exception as e: log.exception("[AUTO-CONFIG] integrate_and_propagate falhou: %s", e)
    else:
        log.warning("[AUTO-CONFIG] ⚠️ Origin OFFLINE — SK instalada localmente.")

    env_auto_mine = os.environ.get("BRN_AUTO_MINE", "").strip()
    if env_auto_mine == "1": should_mine = True; reason = "BRN_AUTO_MINE=1"
    elif env_auto_mine == "0": should_mine = False; reason = "BRN_AUTO_MINE=0"
    elif force_auto_mine: should_mine = True; reason = "--auto-mine"
    elif is_origin: should_mine = True; reason = "origin (padrão)"
    else: should_mine = False; reason = "cliente + BRN_AUTO_MINE não setado"

    log.info("[AUTO-CONFIG] auto-mine: %s (%s)", should_mine, reason)

    if not should_mine:
        SK_VALIDATION_STATE["auto_mine_status"] = "disabled"; return

    if not address or not pubkey:
        SK_VALIDATION_STATE["auto_mine_status"] = "error_no_addr"; return

    threading.Thread(target=_auto_mine_loop, args=(chain, address, pubkey, web_port, stop_evt), daemon=True, name="AutoMine").start()

def _delayed_auto_configure(chain, p2p, trust, web_port: int, is_origin: bool, stop_evt: threading.Event, force_auto_mine: bool = False) -> None:
    try:
        time.sleep(3)
        _auto_configure(chain, p2p, trust, web_port, is_origin, stop_evt, force_auto_mine=force_auto_mine)
    except Exception as e: logging.getLogger("main").exception("[AUTO-CONFIG] falhou: %s", e)

def _resolve_config(log) -> dict:
    cfg = {"bootstrap": "", "bootstrap_source": "(vazio)", "pool_url": "", "pool_url_source": "(vazio)"}
    env_bootstrap = os.environ.get("BRN_BOOTSTRAP_PEERS", "").strip()
    if env_bootstrap:
        cfg["bootstrap"] = env_bootstrap; cfg["bootstrap_source"] = "env:BRN_BOOTSTRAP_PEERS"
    else:
        json_bootstrap = _read_bootstrap_json()
        if json_bootstrap:
            cfg["bootstrap"] = json_bootstrap; cfg["bootstrap_source"] = "file:bootstrap_peers.json"
            os.environ["BRN_BOOTSTRAP_PEERS"] = json_bootstrap

    env_pool = os.environ.get("BRN_POOL_URL", "").strip()
    if env_pool:
        cfg["pool_url"] = env_pool; cfg["pool_url_source"] = "env:BRN_POOL_URL"
    else:
        if cfg["bootstrap"]:
            first = cfg["bootstrap"].split(",")[0].strip()
            if ":" in first:
                host = first.rsplit(":", 1)[0]
                cfg["pool_url"] = f"http://{host}:5000"; cfg["pool_url_source"] = "inferido do bootstrap"
                os.environ["BRN_POOL_URL"] = cfg["pool_url"]
    return cfg

def main() -> int:
    args = parse_args()
    if args.version: print(f"BRN Node v{VERSION} (build {BUILD_DATE})"); return 0
    if args.skip_verify: print("[main] --skip-verify ativo")

    loaded = _load_env_file()

    if args.client_mode: os.environ["BRN_IS_ORIGIN"] = "0"
    if args.origem: os.environ["BRN_IS_ORIGIN"] = "1"
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

    cfg = _resolve_config(log)
    log.info("[CONFIG] BRN_BOOTSTRAP_PEERS: %s (%s)", cfg["bootstrap"] or "(vazio)", cfg["bootstrap_source"])
    log.info("[CONFIG] BRN_POOL_URL: %s (%s)", cfg["pool_url"] or "(vazio)", cfg["pool_url_source"])

    if not _check_blockchain_version(): return 6

    try:
        import port_guard
        port_guard.check_brn_ports(checar_explorer=not args.no_explorer)
    except SystemExit: raise
    except Exception: pass

    try:
        node_id_priv, node_id_pub = _load_or_create_node_identity()
        log.info("[identity] pub=%s…", node_id_pub[:16])
    except RuntimeError as e:
        log.error(str(e)); return 3

    try:
        from blockchain import Blockchain
        chain = Blockchain(db_path, auto_genesis=False)
        log.info("[chain] aberta height=%d", chain.db.height())
    except Exception as e:
        log.error("[chain] fatal: %s", e); return 2

    trust = _setup_trust(chain, node_id_priv, is_origin)
    if trust is None:
        log.error("[trust] não carregou")
        if os.environ.get("BRN_STRICT_MULTISIG") == "1": return 7
    else: log.info("[trust] carregado (origin=%s)", is_origin)

    log.info("[wallet] boot sem carteira (design v13.9.15) — GUI tentará abrir carteira salva")

    p2p = None
    if not args.no_p2p:
        p2p = _setup_p2p(chain, node_id_priv, p2p_port)
        if p2p is not None: log.info("[p2p] iniciado na porta %d", p2p_port)

    sync_mgr = None
    if p2p:
        sync_mgr = _setup_sync(chain, p2p)
        if sync_mgr: log.info("[sync] ChainSyncManager iniciado")

    _stop_bg = threading.Event()
    if cfg["pool_url"] and _HAS_REQUESTS:
        threading.Thread(target=_force_http_sync_loop, args=(chain, cfg["pool_url"], _stop_bg), daemon=True, name="HttpSyncFallback").start()

    if p2p: _setup_broadcast(chain, p2p)

    _setup_trust_daemon(chain, trust, p2p, password=_get_identity_password(), is_origin=is_origin, stop_evt=_stop_bg, interval=30)
    threading.Thread(target=_wallet_watchdog_loop, args=(chain, p2p, trust, _stop_bg, 30), daemon=True, name="Wallet-Watchdog").start()

    pool = _setup_pool(chain, is_origin)

    ok_http = _start_web_server(chain, p2p, pool, sync_mgr, trust, web_port)
    if not ok_http: log.error("[server] HTTP não subiu")

    if not args.no_auto_config and not args.no_pool_client:
        threading.Thread(target=_delayed_auto_configure, args=(chain, p2p, trust, web_port, is_origin, _stop_bg), daemon=True, name="AutoConfig").start()
        log.info("[AUTO-CONFIG] agendado (3s)")
    else: log.info("[AUTO-CONFIG] desligado")

    if not args.no_explorer: _start_explorer(chain)

    want_gui = (not args.headless) and (not args.no_gui)
    if want_gui:
        allow_interactive = not args.no_interactive
        log.info("[gui] abrindo (interativo=%s)", allow_interactive)
        if _start_gui(allow_interactive=allow_interactive): return 0
    else:
        if args.no_gui: log.info("[gui] desligada por --no-gui")
        log.info("=" * 64)
        log.info(" MODO HEADLESS — consulte /health:")
        log.info("   GET http://%s:%d/health", os.environ.get("BRN_WEB_HOST", "127.0.0.1"), web_port)
        log.info("=" * 64)

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
    log.info("   API      : http://%s:%d", os.environ.get("BRN_WEB_HOST", "127.0.0.1"), web_port)
    log.info("   Wallet   : http://%s:%d/wallet", os.environ.get("BRN_WEB_HOST", "127.0.0.1"), web_port)
    log.info("   Health   : http://%s:%d/health", os.environ.get("BRN_WEB_HOST", "127.0.0.1"), web_port)
    log.info("   P2P      : :%d", p2p_port)
    log.info("=" * 64)

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: _shutdown()
    return 0

if __name__ == "__main__":
    try: sys.exit(main())
    except KeyboardInterrupt: sys.exit(130)
