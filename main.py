"""main.py — Entrypoint do nó BRN (v13.9.29)
================================================================
v13.9.29 (2026-11-22) — BOOTSTRAP DE GENESIS E DIAGNÓSTICO:
  [NEW] _try_fetch_genesis_from_peers() — antes de criar Blockchain,
        se o DB local está vazio (height < 0), busca o bloco genesis
        via HTTP /api/block/0 de cada BRN_BOOTSTRAP_PEERS. Se algum
        responder, inicializa o DB com ESSE genesis em vez do hardcoded.
        Evita fork imediato quando DB local foi wipado e os peers têm
        genesis diferente (que era o bug do v13.9.28).
  [NEW] _diagnose_genesis_health() — depois do Blockchain criado,
        compara local genesis vs peers (timeout 2s, só 3 primeiros).
        Se há divergência, loga bloco "FORK DE GENESIS" com hashes
        lado a lado (não destrutivo, só aviso).
  [NEW] CLI --no-genesis-bootstrap — desliga o passo acima, volta
        ao comportamento antigo (usa GENESIS_BLOCK hardcoded).
  [NEW] Banner mostra GENESIS: peer <ip> | hardcoded | existing
  [KEEP] v13.9.28: wiring visível (5x), re-wire pós-GUI, banner com
         blockchain version + auto-wire status
  [KEEP] v13.9.27: origin promotion persistente
  [KEEP] v13.9.26: backup + secret persistente
v13.9.28 (2026-11-22) — WIRING VISÍVEL E RE-WIRE PÓS-UNLOCK
v13.9.27 (2026-11-21) — ORIGIN PROMOTION PERSISTENTE
v13.9.26 (2026-11-20) — BACKUP + SECRET PERSISTENTE
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
import urllib.request
import urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

try:
    from version import VERSION, BUILD_DATE
except ImportError:
    VERSION = "13.9.29"
    BUILD_DATE = "2026-11-22"

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
    p.add_argument("--no-genesis-bootstrap", action="store_true",
                   help="[v13.9.29] desliga bootstrap de genesis via peers")
    p.add_argument("--db", metavar="PATH", default=None)
    p.add_argument("--web-port", type=int, default=None)
    p.add_argument("--p2p-port", type=int, default=None)
    p.add_argument("--version", action="store_true")
    p.add_argument("--skip-verify", action="store_true")
    p.add_argument("--force-http-resync", action="store_true")
    p.add_argument("--fast-sync-until", type=int, default=None)
    p.add_argument("--no-fast-sync", action="store_true")
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


def _read_bootstrap_json() -> str:
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


def _setup_logging(level: str = "INFO") -> logging.Logger:
    try:
        from brn_logger import setup_logger, get_logger
        setup_logger(level=level, quiet=False)
        lg = get_logger("main")
        if lg is not None:
            return lg
    except Exception:
        pass
    try:
        logging.basicConfig(
            level=getattr(logging, level, logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
    except Exception:
        pass
    return logging.getLogger("main")


# ============================================================
# [v13.9.28] WIRING VISÍVEL
# ============================================================
def _ensure_wiring(chain, stage: str = "?") -> dict:
    """Verifica e (se possível) injeta trust_net, local_validator_sk e
    sign_collector via auto_wire.wire_all(). Loga o estado real."""
    log = logging.getLogger("main")

    def _snapshot():
        return {
            "trust": type(chain.trust_net).__name__ if getattr(chain, "trust_net", None) else None,
            "sk": "OK" if getattr(chain, "_local_validator_sk", None) else None,
            "p2p": type(chain.p2p).__name__ if getattr(chain, "p2p", None) else None,
            "collector": "OK" if getattr(chain, "_sign_collector", None) else None,
        }

    state = _snapshot()

    needs_wire = (
        state["trust"] is None
        or state["sk"] is None
        or (state["p2p"] is not None and state["collector"] is None)
    )

    if needs_wire and os.environ.get("BRN_AUTO_WIRE", "1") == "1":
        try:
            from auto_wire import wire_all
            wire_all(chain, verbose=False)
            state = _snapshot()
        except ImportError:
            log.debug("[wiring:%s] auto_wire.py ausente", stage)
        except Exception as e:
            log.warning("[wiring:%s] wire_all falhou: %s", stage, e)

    log.info(
        "[wiring:%s] trust=%s sk=%s p2p=%s collector=%s",
        stage,
        state["trust"] or "None",
        state["sk"] or "None",
        state["p2p"] or "None",
        state["collector"] or "None",
    )

    if state["trust"] is None:
        log.warning(
            "[wiring:%s] ⚠ trust_net=None → multi-sig desligado "
            "(bootstrap bypass ativo)", stage,
        )
    if state["sk"] is None:
        log.warning(
            "[wiring:%s] ⚠ _local_validator_sk=None → nó NÃO assina blocos. "
            "Configure BRN_VALIDATOR_SK_HEX, .validator_sk ou "
            "BRN_NODE_PASSWORD + current_wallet.enc", stage,
        )
    if state["p2p"] is not None and state["collector"] is None:
        log.warning(
            "[wiring:%s] ⚠ _sign_collector=None → só assina localmente "
            "(ou nada, se SK=None). Verifique se blockchain.py é ≥ v9.6.0",
            stage,
        )

    return state


# ============================================================
# [v13.9.29] BOOTSTRAP DE GENESIS VIA PEERS
# ============================================================
def _fetch_peer_genesis(peer_ip: str, timeout: float = 3.0):
    """Tenta obter bloco genesis de um peer via HTTP."""
    try:
        url = f"http://{peer_ip}:5000/api/block/0"
        req = urllib.request.Request(url, headers={"User-Agent": "brn-bootstrap/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            b0 = json.loads(resp.read().decode("utf-8"))
        if not isinstance(b0, dict):
            return None
        if b0.get("height") != 0:
            return None
        if not b0.get("hash") or not b0.get("transactions"):
            return None
        return b0
    except Exception as e:
        logging.getLogger("main").debug("[genesis] peer %s falhou: %s", peer_ip, e)
        return None


def _try_fetch_genesis_from_peers():
    """Percorre BRN_BOOTSTRAP_PEERS, retorna o primeiro genesis válido."""
    log = logging.getLogger("main")
    peers = [p.strip() for p in os.environ.get("BRN_BOOTSTRAP_PEERS", "").split(",") if p.strip()]
    if not peers:
        log.warning("[genesis] BRN_BOOTSTRAP_PEERS vazio — não é possível buscar genesis")
        return None

    for peer in peers:
        ip = peer.rsplit(":", 1)[0] if ":" in peer else peer
        log.info("[genesis] tentando %s…", ip)
        b0 = _fetch_peer_genesis(ip)
        if b0:
            return b0, ip
    return None, None


def _diagnose_genesis_health(chain, db_path):
    """Compara genesis local com peers. NÃO destrutivo — só loga."""
    log = logging.getLogger("main")
    
    try:
        local_b0 = chain.db.get_block(0)
        if not local_b0:
            return
        local_hash = local_b0.get("hash", "")
    except Exception as e:
        log.debug("[genesis-health] local falhou: %s", e)
        return
    
    peers = [p.strip() for p in os.environ.get("BRN_BOOTSTRAP_PEERS", "").split(",") if p.strip()]
    if not peers:
        return
    
    local_h = chain.db.height()
    matches = []
    mismatches = []
    offline = []
    peer_hashes = {}
    
    for peer in peers[:3]:
        ip = peer.rsplit(":", 1)[0] if ":" in peer else peer
        b0 = _fetch_peer_genesis(ip, timeout=2.0)
        if not b0:
            offline.append(ip)
            continue
        ph = b0.get("hash", "")
        peer_hashes[ip] = ph
        if ph == local_hash:
            matches.append(ip)
        else:
            mismatches.append(ip)
    
    if matches and not mismatches:
        log.info("[genesis-health] OK: genesis local == %d peer(s)", len(matches))
    elif mismatches and not matches:
        log.warning("=" * 64)
        log.warning(" [genesis-health] ⚠ FORK DE GENESIS DETECTADO")
        log.warning("=" * 64)
        log.warning("  local genesis : %s…", local_hash[:32])
        for ip, ph in peer_hashes.items():
            log.warning("  peer %-15s: %s…", ip, ph[:32])
        log.warning("  chain local (%d blocos) NÃO vai sincronizar com esses peers", local_h)
        log.warning("  Para corrigir:")
        log.warning("    a) Restaure backup: brn_v2_chain.db.bak.* ou backups/chain_*.db")
        log.warning("    b) Copie DB do peer via scp")
        log.warning("    c) Apague DB e rode de novo (bootstrap busca genesis do peer)")
        log.warning("=" * 64)
    elif mismatches and matches:
        log.warning(
            "[genesis-health] PEERS DIVERGEM: %d OK, %d FORK. "
            "Verifique se algum peer está desatualizado.",
            len(matches), len(mismatches),
        )
    
    if offline and not matches and not mismatches:
        log.debug("[genesis-health] nenhum peer respondeu (offline)")


# ============================================================
# FAST-SYNC
# ============================================================
def _get_current_db_height(db_path: str) -> int:
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
    if getattr(args, "no_fast_sync", False):
        return 0
    if getattr(args, "fast_sync_until", None) is not None:
        try:
            return max(0, int(args.fast_sync_until))
        except (ValueError, TypeError):
            pass
    try:
        return max(0, int(os.environ.get("BRN_FAST_SYNC_UNTIL", "4084")))
    except (ValueError, TypeError):
        return 4084


def _maybe_enable_fast_sync(fast_sync_until: int, args) -> None:
    log = logging.getLogger("main")
    os.environ["BRN_FAST_SYNC_UNTIL"] = str(fast_sync_until)

    no_fast_sync = getattr(args, "no_fast_sync", False)
    cli_explicit = getattr(args, "fast_sync_until", None) is not None

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
            "[FAST-SYNC] DB em %d (>= %d) — fast-sync NÃO ativado.",
            current_h, fast_sync_until,
        )
        return

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
    log.warning("  A partir de %d, validação FULL é obrigatória.", fast_sync_until + 1)
    log.warning("  Mineração só libera APÓS %d E validadores sincronizados.", fast_sync_until)
    log.warning("=" * 64)


# ============================================================
# RESYNC FORÇADO
# ============================================================
def _do_force_http_resync(headless: bool = False) -> None:
    log = logging.getLogger("main")
    db_env = os.environ.get("BRN_DB", "brn_v2_chain.db")
    p = Path(db_env) if os.path.isabs(db_env) else (BASE_DIR / db_env)

    if not p.exists():
        log.warning("[RESYNC] %s não existe — nada a fazer", p)
        if not headless:
            try: input("Pressione ENTER para sair…")
            except (EOFError, KeyboardInterrupt): pass
        return

    ts = int(time.time())
    bak = p.with_name(f"{p.name}.bak.{ts}")
    try:
        p.rename(bak)
        log.warning("[RESYNC] DB renomeado: %s → %s", p.name, bak.name)
        for ext in ("-wal", "-shm"):
            src = Path(str(p) + ext)
            if src.exists():
                try:
                    src.rename(Path(str(bak) + ext))
                except Exception:
                    pass
        log.warning("[RESYNC] Reinicie o nó — ele vai re-sincronizar do zero")
    except Exception as e:
        log.error("[RESYNC] falhou: %s", e)

    if not headless:
        try: input("Pressione ENTER para sair…")
        except (EOFError, KeyboardInterrupt): pass


# ============================================================
# VALIDADORES
# ============================================================
def _verify_validators_synced(chain, min_validators: int = 1,
                              tolerance: int = 1,
                              max_age_s: int = 300) -> tuple:
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
# PEER HEALER
# ============================================================
def _peer_healer_loop(chain, p2p, stop_evt: threading.Event, interval: int = 15):
    log = logging.getLogger("main")
    log.info("[Peer-Healer] iniciado (interval=%ds)", interval)

    if not p2p:
        log.warning("[Peer-Healer] P2P não inicializado — healer inativo.")
        return

    if not hasattr(chain.db, "upsert_peer"):
        log.warning("[Peer-Healer] chain.db não tem upsert_peer() — healer inativo")
        return

    try:
        from p2p_unified import P2PClient
    except ImportError:
        log.warning("[Peer-Healer] p2p_unified.P2PClient indisponível")
        return

    while not stop_evt.wait(interval):
        try:
            local_h = int(chain.db.height())
            if local_h < 0:
                continue

            peers = chain.db.listar_peers(apenas_ativos=False)
            for p in peers:
                addr = (p.get("address") or "").strip()
                if not addr or ":" not in addr:
                    continue
                ip, port_s = addr.rsplit(":", 1)
                if ip.startswith("127.") or ip == "0.0.0.0":
                    continue

                try:
                    port = int(port_s)
                    resp, _ = P2PClient.send_message(
                        ip, port, {"type": "get_chain_height"}, timeout=3.0)
                    if resp:
                        h = int(resp.get("height", 0) or 0)
                        if h >= 0:
                            node_id = p.get("node_id") or addr
                            genesis = p.get("genesis_hash") or ""
                            version = p.get("version") or "?"
                            is_miner = bool(p.get("is_miner", False))
                            public_key = p.get("public_key") or ""

                            chain.db.upsert_peer(
                                node_id=node_id, address=addr, genesis_hash=genesis,
                                version=version, height=h, is_miner=is_miner,
                                public_key=public_key,
                            )

                            if abs(h - local_h) <= 1:
                                log.info("[Peer-Healer] Peer %s sincronizado em %d (local=%d)",
                                         addr, h, local_h)
                            else:
                                log.debug("[Peer-Healer] Peer %s altura %d (local=%d)",
                                          addr, h, local_h)
                except Exception as e:
                    log.debug("[Peer-Healer] Falha ao pingar %s: %s", addr, e)
        except Exception as e:
            log.debug("[Peer-Healer] Erro no loop: %s", e)


# ============================================================
# Identidade
# ============================================================
def _get_identity_password() -> str:
    log = logging.getLogger("main")
    pw = (
        os.environ.get("BRN_NODE_PASSWORD", "").strip()
        or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
    )
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


def _load_or_create_node_identity():
    log = logging.getLogger("main")
    try:
        from secure_store import load_node_identity, save_node_identity
        from crypto import Ed25519PrivateKey
    except Exception as e:
        raise RuntimeError(f"crypto/secure_store indisponíveis: {e}") from e

    pw_env = (
        os.environ.get("BRN_NODE_PASSWORD", "").strip()
        or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
    )
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
                log.warning("[identity] %s inacessível — backup em %s",
                            p.name, backup.name)
            except Exception as e2:
                raise RuntimeError(
                    f"[identity] não renomeei {p.name}: {e2}"
                ) from e2

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


def _check_blockchain_version() -> bool:
    log = logging.getLogger("main")
    try:
        import blockchain as _bc
        ver = getattr(_bc, "__version__", "")
    except Exception as e:
        log.error("[version] não importei blockchain: %s", e)
        return os.environ.get("BRN_STRICT_MULTISIG") != "1"

    ok = ver.startswith("9.4") or (ver >= "9.4.10") or ver.startswith("9.5") or ver.startswith("9.6")
    if ok:
        log.info("[version] blockchain.py v%s (multi-sig OK)", ver)
        return True
    log.warning("[version] blockchain.py v%s — multi-sig NÃO enforçado",
                ver or "desconhecida")
    return os.environ.get("BRN_STRICT_MULTISIG") != "1"


# ============================================================
# Setup componentes
# ============================================================
def _setup_trust(chain, node_id_priv, is_origin: bool):
    try:
        from trust_network import TrustNetwork
        trust = TrustNetwork("trust_network.json", is_origin=is_origin,
                             node_sk=node_id_priv)
        chain.set_trust_net(trust)
        return trust
    except Exception as e:
        logging.getLogger("main").error("[trust] init falhou: %s", e)
        return None


def _setup_p2p(chain, node_id_priv, p2p_port: int):
    log = logging.getLogger("main")
    try:
        from p2p_unified import P2PManager
        p2p = P2PManager(chain, node_id_priv=node_id_priv, tcp_port=p2p_port)
        chain.set_p2p(p2p)
        collector_ok = getattr(chain, "_sign_collector", None) is not None
        log.info("[p2p] set_p2p OK — collector=%s",
                 "OK" if collector_ok else "None (⚠ verifique blockchain.py ≥ v9.6.0)")
        p2p.start()
        return p2p
    except Exception as e:
        log.error("[p2p] init falhou: %s", e)
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


def _wallet_watchdog_loop(chain, p2p, trust, stop_evt: threading.Event,
                          interval: int = 30):
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
                    log.warning("[ISOLATION] %s: %s",
                                alert.severity.upper(), alert.reason)
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


def _start_web_server(chain, p2p, pool, sync_mgr, trust, web_port: int,
                      stop_evt: threading.Event = None):
    log = logging.getLogger("main")
    try:
        import server as srv
    except Exception as e:
        log.error("[server] import falhou: %s", e)
        return False

    srv.set_chain(chain)

    if hasattr(srv, "set_peer_id"):
        srv.set_peer_id(os.environ.get("BRN_PEER_ID", ""))

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
        if hasattr(srv, "set_http_sync_state"):
            srv.set_http_sync_state(HTTP_SYNC_PROGRESS)
            log.info("[server] http_sync_state injetado")
    except Exception as e:
        log.debug("[server] set_state falhou: %s", e)

    if stop_evt is not None and hasattr(srv, "set_sk_validation_state"):
        def _refresh():
            while not stop_evt.wait(5):
                try:
                    srv.set_sk_validation_state(SK_VALIDATION_STATE)
                    if hasattr(srv, "set_http_sync_state"):
                        srv.set_http_sync_state(HTTP_SYNC_PROGRESS)
                except Exception:
                    pass
        threading.Thread(target=_refresh, daemon=True,
                         name="StateRefresh").start()
        log.info("[server] state refresh thread iniciada (5s)")

    def _run():
        try:
            import uvicorn
            from server_fastapi import app as fastapi_app
            log.info("[server] Usando FastAPI + Uvicorn na porta %d", web_port)
            uvicorn.run(
                fastapi_app,
                host=os.environ.get("BRN_WEB_HOST", "127.0.0.1"),
                port=web_port,
                log_level="warning",
                access_log=False,
            )
            return
        except Exception as e:
            log.error("[server] Uvicorn falhou: %s", e)

        try:
            srv.app.run(
                host=os.environ.get("BRN_WEB_HOST", "127.0.0.1"),
                port=web_port, threaded=True, use_reloader=False,
            )
        except Exception as e2:
            log.error("[server] app.run fallback morreu: %s", e2)

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
                threaded=True, use_reloader=False,
            )
        threading.Thread(target=_run, daemon=True, name="EXP").start()
    except Exception as e:
        logging.getLogger("main").debug("[explorer] não iniciou: %s", e)


# ============================================================
# GUI / wallet
# ============================================================
def _candidate_gui_passwords() -> list:
    cands = []
    seen = set()

    def _add(src: str, val: str):
        v = (val or "").strip()
        if not v or v in seen:
            return
        seen.add(v)
        cands.append((src, v))

    _add("env:BRN_WALLET_SESSION_PASS",
         os.environ.get("BRN_WALLET_SESSION_PASS", ""))
    _add("env:BRN_VALIDATOR_SEED", os.environ.get("BRN_VALIDATOR_SEED", ""))
    _add("env:BRN_NODE_PASSWORD", os.environ.get("BRN_NODE_PASSWORD", ""))
    p = BASE_DIR / ".node_password"
    if p.exists():
        try:
            _add("file:.node_password", p.read_text(encoding="utf-8").strip())
        except Exception:
            pass
    return cands


def _prompt_password_interactive(api, max_attempts: int = 3) -> tuple:
    try:
        if not sys.stdin.isatty():
            return False, "no_stdin", "", "stdin não é TTY"
    except Exception:
        return False, "no_stdin", "", "stdin check falhou"

    print("\n" + "=" * 60)
    print(" 🔐 ABRIR CARTEIRA BRN")
    print("=" * 60)
    print(" Carteira salva encontrada em disco.")
    print(" Digite a senha para abrir (ou ENTER para abrir a GUI vazia).\n")

    for attempt in range(1, max_attempts + 1):
        try:
            pw = input(f" Senha [{attempt}/{max_attempts}]: ")
        except (EOFError, KeyboardInterrupt):
            print("\n [cancelado]")
            return False, "cancelled", "", "cancelado"
        if not pw:
            print(" [vazio] — abrindo GUI sem carteira")
            return False, "empty_input", "", "usuário deixou vazio"
        try:
            r = api.unlock_wallet(pw)
            if r.get("ok"):
                addr = r.get("address", "")
                if r.get("sk_installed"):
                    print(f" ✅ Carteira aberta: {addr}")
                    SK_VALIDATION_STATE["gui_wallet_source"] = f"interactive:{attempt}"
                    SK_VALIDATION_STATE["address_derived"] = addr
                    SK_VALIDATION_STATE["address_configured"] = (
                        addr[:40] if addr else "")
                    return True, f"interactive:{attempt}", addr, ""
                else:
                    print(f" ⚠️  Carteira aberta: {addr}")
                    print(" ⚠️  MAS SK NÃO instalada no nó (sk_installed=False)")
                    for w in (r.get("warnings") or []):
                        print(f"     [WARN] {w}")
                    for e in (r.get("errors") or []):
                        print(f"     [ERR]  {e}")
                    SK_VALIDATION_STATE["gui_wallet_source"] = (
                        f"interactive:{attempt}:no_sk")
                    SK_VALIDATION_STATE["address_derived"] = addr
                    SK_VALIDATION_STATE["address_configured"] = (
                        addr[:40] if addr else "")
                    return True, f"interactive:{attempt}:no_sk", addr, "sk_not_installed"
            else:
                print(f" ❌ Senha incorreta: {r.get('msg', '?')}")
        except Exception as e:
            print(f" ❌ Erro: {e}")

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
        log.warning("[GUI] has_wallet() falhou: %s", e)
        return False, "has_wallet_error", "", str(e)

    try:
        aw = api.get_active_wallet()
        if aw.get("ok") and aw.get("address"):
            log.info("[GUI] carteira JÁ desbloqueada: %s…", aw["address"][:32])
            SK_VALIDATION_STATE["gui_wallet_source"] = "already_unlocked"
            return True, "already_unlocked", aw["address"], ""
    except Exception:
        pass

    cands = _candidate_gui_passwords()
    for src, pw in cands:
        try:
            r = api.unlock_wallet(pw)
            if r.get("ok"):
                addr = r.get("address", "")
                if r.get("sk_installed"):
                    log.info("[GUI] carteira desbloqueada via %s: %s…",
                             src, addr[:32])
                    SK_VALIDATION_STATE["gui_wallet_source"] = src
                    SK_VALIDATION_STATE["address_derived"] = addr
                    SK_VALIDATION_STATE["address_configured"] = (
                        addr[:40] if addr else "")
                    return True, src, addr, ""
                else:
                    log.warning("[GUI] unlock OK via %s mas sk_installed=False",
                                src)
        except Exception as e:
            log.debug("[GUI] unlock falhou via %s: %s", src, e)

    if allow_interactive:
        return _prompt_password_interactive(api)

    log.info("[GUI] sem senha nos candidatos e interativo desabilitado")
    SK_VALIDATION_STATE["gui_wallet_source"] = "no_candidate"
    return False, "no_candidate", "", "sem senha automática e interativo off"


class _WalletAPIAdapter:
    def __init__(self, chain):
        self.chain = chain

    def has_wallet(self) -> dict:
        try:
            from secure_store import CURRENT_WALLET_ENC, _DEFAULT_WALLET_SUB
            p = Path(CURRENT_WALLET_ENC)
            return {"exists": p.exists() or _DEFAULT_WALLET_SUB.exists()}
        except Exception:
            return {"exists": False}

    def get_active_wallet(self) -> dict:
        try:
            sk = getattr(self.chain, "_local_validator_sk", None)
            if sk is not None:
                addr = getattr(sk, "address", "")
                if addr:
                    return {"ok": True, "address": addr}
        except Exception:
            pass
        return {"ok": False}

    def unlock_wallet(self, pw: str) -> dict:
        if not pw:
            return {"ok": False, "msg": "senha vazia"}
        try:
            from secure_store import load_current_wallet
            w = load_current_wallet(pw)
            if not w.get("ok"):
                return {"ok": False, "msg": w.get("msg", "falha ao decifrar")}

            addr = (w.get("address") or w.get("addr") or "").strip()
            pub = (w.get("public_key") or w.get("pubkey") or "").strip()
            sk_hex = (
                w.get("private_key") or w.get("priv") or w.get("sk") or ""
            ).strip()

            if not sk_hex:
                return {"ok": False, "msg": "carteira sem chave privada"}

            sk_installed = False
            if _wi is not None and hasattr(_wi, "integrate"):
                try:
                    r = _wi.integrate(
                        self.chain, sk_hex,
                        address=addr, pubkey=pub,
                        logger=logging.getLogger("main"),
                    )
                    sk_installed = bool(r.get("ok", False))
                except Exception:
                    sk_installed = False

            if not sk_installed and _HAS_WALLET and _Wallet is not None:
                try:
                    wobj = _Wallet(private_key_hex=sk_hex)
                    self.chain.set_local_validator_sk(wobj)
                    sk_installed = True
                except Exception:
                    pass

            return {
                "ok": True,
                "address": addr,
                "sk_installed": sk_installed,
                "warnings": [] if sk_installed else ["sk_installed=False"],
                "errors": [],
            }
        except Exception as e:
            return {"ok": False, "msg": str(e)}


# ============================================================
# Main
# ============================================================
def main() -> int:
    args = parse_args()

    if args.version:
        print(f"BRN v{VERSION} ({BUILD_DATE})")
        return 0

    n_loaded = _load_env_file()
    log = _setup_logging(os.environ.get("BRN_LOG_LEVEL", "INFO"))
    if n_loaded:
        log.info("[env] %d variável(is) carregada(s) de brn_network.env", n_loaded)

    if args.headless:
        os.environ["BRN_HEADLESS"] = "1"

    if args.db:
        os.environ["BRN_DB"] = args.db

    if args.force_http_resync:
        _do_force_http_resync(headless=args.headless)
        return 0

    if not args.no_auto_config and not args.no_p2p:
        try:
            import auto_discovery
            auto_discovery.auto_configure_bootstrap()
        except ImportError:
            pass
        except Exception as e:
            log.debug("[auto-discovery] falhou: %s", e)

    if not os.environ.get("BRN_BOOTSTRAP_PEERS"):
        bpj = _read_bootstrap_json()
        if bpj:
            os.environ["BRN_BOOTSTRAP_PEERS"] = bpj
            log.info("[bootstrap] %d peer(s) de bootstrap_peers.json", len(bpj.split(",")))

    web_port = args.web_port or int(
        os.environ.get("BRN_WEB_PORT", os.environ.get("PORT", "5000"))
    )
    p2p_port = args.p2p_port or int(os.environ.get("BRN_P2P_PORT", "6001"))

    os.environ["BRN_WEB_PORT"] = str(web_port)
    os.environ["BRN_P2P_PORT"] = str(p2p_port)

    fast_sync_until = _resolve_fast_sync_until(args)
    _maybe_enable_fast_sync(fast_sync_until, args)
    SK_VALIDATION_STATE["fast_sync_until"] = fast_sync_until

    try:
        from network_secret import ensure_network_secret
        ensure_network_secret()
    except ImportError:
        log.debug("[secret] network_secret.py ausente — usando env direto")
    except Exception as e:
        log.warning("[secret] falhou: %s", e)

    try:
        from backup_manager import auto_restore_on_boot
        _r = auto_restore_on_boot()
        if _r.get("ok"):
            log.warning("=" * 64)
            log.warning(" [AUTO-RESTORE] %s", _r.get("msg", "OK"))
            log.warning(" Fonte: %s", _r.get("source", "?"))
            log.warning("=" * 64)
        else:
            log.info("[AUTO-RESTORE] nada a fazer: %s", _r.get("msg"))
    except ImportError:
        log.debug("[backup] backup_manager ausente")
    except Exception as e:
        log.warning("[backup] auto-restore falhou: %s", e)

    is_origin = args.origem or os.environ.get("BRN_IS_ORIGIN", "0") == "1"
    os.environ["BRN_IS_ORIGIN"] = "1" if is_origin else "0"

    _check_blockchain_version()

    try:
        node_priv, node_pub = _load_or_create_node_identity()
    except Exception as e:
        log.error("[identity] falhou: %s", e)
        return 1

    try:
        if len(node_pub) >= 64:
            peer_id = hashlib.sha256(bytes.fromhex(node_pub)).hexdigest()[:16]
        else:
            peer_id = hashlib.sha256(node_pub.encode()).hexdigest()[:16]
    except Exception:
        peer_id = hashlib.sha256(node_pub.encode()).hexdigest()[:16]
    os.environ["BRN_PEER_ID"] = peer_id
    log.info("[peer] PEER_ID = %s", peer_id)

    db_path = os.environ.get("BRN_DB", "brn_v2_chain.db")
    try:
        from origin_manager import apply_origin_override_at_boot
        is_origin = apply_origin_override_at_boot(db_path, is_origin)
        os.environ["BRN_IS_ORIGIN"] = "1" if is_origin else "0"
        log.info("[origin] is_origin final (após override): %s", is_origin)
    except ImportError:
        log.debug("[origin] origin_manager.py ausente — usando env direto")
    except Exception as e:
        log.warning("[origin] apply_origin_override falhou: %s", e)

    try:
        import blockchain as _bc_mod
        _bc_ver = getattr(_bc_mod, "__version__", "?")
    except Exception:
        _bc_ver = "?"

    # Banner (antes de bootstrap de genesis para você ver a config)
    log.warning("=" * 64)
    log.warning(" BRN Node v%s (%s)", VERSION, BUILD_DATE)
    log.warning("=" * 64)
    log.warning(" DB       : %s", db_path)
    log.warning(" Web      : %d", web_port)
    log.warning(" P2P      : %d", p2p_port)
    log.warning(" Origin   : %s", is_origin)
    log.warning(" Headless : %s", args.headless)
    log.warning(" Fast-sync: %d", fast_sync_until)
    log.warning(" BRN Core : blockchain.py v%s", _bc_ver)
    log.warning(" Auto-wire: %s", os.environ.get("BRN_AUTO_WIRE", "1"))

    # ============================================================
    # [v13.9.29] Detecta genesis e bootstrap via peers se DB vazio
    # ============================================================
    db_current_h = _get_current_db_height(db_path)
    db_empty = db_current_h < 0
    genesis_source = "existing"
    
    if db_empty:
        log.warning(" Genesis  : (DB vazio, será inicializado)")
    else:
        # DB já tem dados — mostra o genesis real
        try:
            from db import ChainDB
            _tmp_db = ChainDB(db_path)
            _gb = _tmp_db.get_block(0)
            _tmp_db.close()
            if _gb:
                log.warning(" Genesis  : %s…", _gb["hash"][:32])
        except Exception:
            log.warning(" Genesis  : (não foi possível ler)")

    log.warning("=" * 64)

    # 16. Cria Blockchain — [v13.9.29] com bootstrap de genesis via peers
    try:
        from blockchain import Blockchain

        if db_empty and not args.no_genesis_bootstrap:
            log.warning("[genesis] DB vazio — buscando genesis dos peers")
            peer_genesis, peer_ip = _try_fetch_genesis_from_peers()

            if peer_genesis:
                log.warning("[genesis] genesis obtido de %s: %s…",
                            peer_ip, peer_genesis["hash"][:32])
                chain = Blockchain(
                    db_path=db_path,
                    auto_genesis=False,
                    network_secret=os.environ.get("BRN_NETWORK_SECRET", ""),
                )
                try:
                    chain.db.add_block(peer_genesis)
                    chain.db.apply_tx(peer_genesis["transactions"][0], 0, coinbase=True)
                    chain.db.set_meta("genesis_hash", peer_genesis["hash"])
                    chain.genesis_expected_hash = peer_genesis["hash"]
                    genesis_source = f"peer:{peer_ip}"
                    log.warning("[genesis] DB inicializado com genesis do peer")
                    log.warning("[genesis]   address : %s",
                                peer_genesis["transactions"][0]["outputs"][0]["address"])
                    log.warning("[genesis]   height  : 0 (pronto para sync)")
                except Exception as e:
                    log.error("[genesis] falha ao gravar genesis do peer: %s", e)
                    log.warning("[genesis] caindo para GENESIS_BLOCK hardcoded")
                    chain = Blockchain(
                        db_path=db_path,
                        auto_genesis=True,
                        network_secret=os.environ.get("BRN_NETWORK_SECRET", ""),
                    )
                    genesis_source = "hardcoded(fallback)"
            else:
                log.warning("=" * 64)
                log.warning(" [genesis] ⚠ NENHUM peer respondeu com /api/block/0")
                log.warning(" [genesis] usando GENESIS_BLOCK hardcoded")
                log.warning(" [genesis] ⚠ Se peers têm genesis diferente, sync vai")
                log.warning(" [genesis]    falhar com 'prev_hash diverge em #1'")
                log.warning(" [genesis]    Corrija depois com: --force-http-resync")
                log.warning("=" * 64)
                chain = Blockchain(
                    db_path=db_path,
                    auto_genesis=True,
                    network_secret=os.environ.get("BRN_NETWORK_SECRET", ""),
                )
                genesis_source = "hardcoded(no-peers)"
        elif db_empty and args.no_genesis_bootstrap:
            log.warning("[genesis] --no-genesis-bootstrap setado — usando hardcoded")
            chain = Blockchain(
                db_path=db_path,
                auto_genesis=True,
                network_secret=os.environ.get("BRN_NETWORK_SECRET", ""),
            )
            genesis_source = "hardcoded(forced)"
        else:
            # DB tem dados — abre normal
            chain = Blockchain(
                db_path=db_path,
                auto_genesis=True,
                network_secret=os.environ.get("BRN_NETWORK_SECRET", ""),
            )
            genesis_source = "existing"

        chain.is_origin = is_origin

        # [v13.9.29] Diagnóstico pós-boot (não destrutivo)
        if not db_empty:
            try:
                _diagnose_genesis_health(chain, db_path)
            except Exception as e:
                log.debug("[genesis] diagnose falhou: %s", e)
    except Exception as e:
        log.exception("[chain] init falhou")
        return 1

    log.info("[chain] height=%d tip=%s…",
             chain.db.height(), chain.db.tip_hash()[:16])
    log.info("[chain] genesis_source=%s", genesis_source)
    SK_VALIDATION_STATE["local_height"] = chain.db.height()

    _ensure_wiring(chain, "post-blockchain")

    if os.environ.get("BRN_AUTO_BACKUP", "0") == "1":
        try:
            from backup_manager import start_periodic_backup
            interval = int(os.environ.get("BRN_AUTO_BACKUP_INTERVAL", "300"))
            start_periodic_backup(interval_s=interval)
            log.info("[AUTO-BACKUP] ativo (a cada %ds)", interval)
        except Exception as e:
            log.warning("[AUTO-BACKUP] falhou: %s", e)

    trust = _setup_trust(chain, node_priv, is_origin)
    _ensure_wiring(chain, "post-trust")

    p2p = None
    if not args.no_p2p:
        p2p = _setup_p2p(chain, node_priv, p2p_port)
    _ensure_wiring(chain, "post-p2p")

    sync_mgr = _setup_sync(chain, p2p)
    _setup_broadcast(chain, p2p)
    pool = _setup_pool(chain, is_origin)

    stop_evt = threading.Event()

    def _handle_signal(signum, frame):
        log.warning("[shutdown] sinal %d recebido", signum)
        stop_evt.set()

    try:
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
    except (ValueError, OSError):
        pass

    gui_unlocked = False
    if not args.headless and not args.no_gui:
        try:
            api = _WalletAPIAdapter(chain)
            allow_interactive = (
                not args.no_interactive
                and not args.headless
                and sys.stdin.isatty()
            )
            ok_unlock, src, addr, err = _auto_unlock_gui(
                api, allow_interactive=allow_interactive)
            gui_unlocked = bool(ok_unlock and not err)
            if gui_unlocked:
                _ensure_wiring(chain, "post-gui-unlock")
        except Exception as e:
            log.warning("[GUI] unlock falhou: %s", e)

    _start_web_server(chain, p2p, pool, sync_mgr, trust, web_port, stop_evt)

    if not args.no_explorer:
        _start_explorer(chain)

    threading.Thread(
        target=_wallet_watchdog_loop,
        args=(chain, p2p, trust, stop_evt, 30),
        daemon=True, name="Watchdog",
    ).start()

    if p2p is not None:
        threading.Thread(
            target=_peer_healer_loop,
            args=(chain, p2p, stop_evt, 15),
            daemon=True, name="PeerHealer",
        ).start()

    try:
        _setup_trust_daemon(
            chain, trust, p2p,
            _get_identity_password(), is_origin,
            stop_evt, interval=30,
        )
    except Exception as e:
        log.warning("[trust-daemon] setup falhou: %s", e)

    if os.environ.get("BRN_AUTO_MINE", "0") == "1" and not args.no_pool_client:
        def _auto_mine_loop():
            url = f"http://127.0.0.1:{web_port}/api/pool/start_local"
            while not stop_evt.is_set():
                try:
                    req = urllib.request.Request(
                        url, data=b"{}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        body = resp.read().decode("utf-8", errors="replace")
                        try:
                            j = json.loads(body)
                        except Exception:
                            j = {}
                        if j.get("ok"):
                            log.warning("[AUTO-MINE] pool/start_local OK")
                            return
                        log.debug("[AUTO-MINE] %s", body[:200])
                except urllib.error.HTTPError as e:
                    log.debug("[AUTO-MINE] HTTP %s: %s", e.code,
                              e.read().decode("utf-8", errors="replace")[:200])
                except Exception as e:
                    log.debug("[AUTO-MINE] %s", e)
                if stop_evt.wait(30):
                    return

        threading.Thread(target=_auto_mine_loop,
                         daemon=True, name="AutoMine").start()
        log.info("[AUTO-MINE] thread iniciada (30s entre tentativas)")

    _ensure_wiring(chain, "post-start")

    log.warning("=" * 64)
    log.warning(" NÓ INICIADO — aguardando shutdown (Ctrl+C)")
    log.warning(" Genesis: %s", genesis_source)
    log.warning("=" * 64)

    try:
        while not stop_evt.wait(1):
            pass
    except KeyboardInterrupt:
        log.warning("[shutdown] Ctrl+C")
        stop_evt.set()

    log.warning("[shutdown] parando componentes…")
    try:
        if p2p is not None and hasattr(p2p, "stop"):
            p2p.stop()
    except Exception as e:
        log.debug("[shutdown] p2p.stop falhou: %s", e)
    try:
        if trust is not None and hasattr(trust, "flush"):
            trust.flush()
    except Exception as e:
        log.debug("[shutdown] trust.flush falhou: %s", e)
    try:
        if chain is not None and hasattr(chain.db, "close"):
            chain.db.close()
    except Exception as e:
        log.debug("[shutdown] db.close falhou: %s", e)

    log.warning("[shutdown] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
