"""main.py — Entrypoint do nó BRN (v13.9.1)
================================================================
v13.9.1 (2026-11-06):
  [NEW] Auto-detecção de IP (Tailscale/LAN) e atualização automática
        do BRN_SEU_ENDERECO no arquivo brn_network.env.
v13.9.0 (2026-11-06):
  [NEW] Integração do TrustDaemon (auto-cura da rede).
  [KEEP] Coleta de assinaturas P2P em background.
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
import re
import socket
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

try:
    from version import VERSION, BUILD_DATE
except ImportError:
    VERSION = "13.9.1"
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

def _auto_update_env_ip():
    """Detecta o IP da máquina (Tailscale ou LAN) e atualiza o .env."""
    env_file = BASE_DIR / "brn_network.env"
    if not env_file.exists():
        return

    # 1. Tenta Tailscale
    best_ip = None
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            best_ip = r.stdout.strip().split("\n")[0].strip()
    except Exception:
        pass

    # 2. Fallback para LAN
    if not best_ip:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            best_ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass

    if not best_ip or best_ip.startswith("127."):
        return

    try:
        content = env_file.read_text(encoding="utf-8")
        
        # Pega a porta P2P do arquivo (se existir) ou usa 6001
        p2p_port = "6001"
        port_match = re.search(r"^BRN_P2P_PORT=(\d+)", content, re.MULTILINE)
        if port_match:
            p2p_port = port_match.group(1)

        full_addr = f"{best_ip}:{p2p_port}"

        # Verifica se já está atualizado
        if re.search(rf"^BRN_SEU_ENDERECO={re.escape(full_addr)}$", content, re.MULTILINE):
            return
            
        # Atualiza a linha ou adiciona no final
        if "BRN_SEU_ENDERECO=" in content:
            content = re.sub(r"^BRN_SEU_ENDERECO=.*$", f"BRN_SEU_ENDERECO={full_addr}", content, flags=re.MULTILINE)
        else:
            content += f"\nBRN_SEU_ENDERECO={full_addr}\n"
            
        env_file.write_text(content, encoding="utf-8")
        print(f"[auto-ip] BRN_SEU_ENDERECO atualizado automaticamente para {full_addr}")
        
    except Exception as e:
        print(f"[auto-ip] Falha ao atualizar .env: {e}")

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

    # Chama a auto-detecção de IP antes de carregar o .env para os.environ
    _auto_update_env_ip()
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

    # 7. MULTI-SIG E BROADCAST DE BLOCOS (CRÍTICO)
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

    # 7.1 Inicia o Daemon da Trust Network
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
        try: chain.db.close()
        except Exception: pass
        os._exit(0)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        _shutdown()

    return 0

if __name__ == "__main__":
    sys.exit(main())
