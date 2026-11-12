#!/usr/bin/env python3
"""
auto_trust_daemon.py — Mantém a Trust Network sincronizada com a realidade.

v3.0 (2026-11-12) — HONESTIDADE DE last_seen E REMOÇÃO SEGURA:
  [FIX CRÍTICO] last_seen agora espelha o DB (peer.last_seen do P2P),
        não time.time(). Antes, todo peer em listar_peers(janela=1800)
        tinha o last_seen reescrito para AGORA a cada 120s, mesmo que
        estivesse offline há 25min. remove_ghosts(180) então NUNCA
        removia — o critério efetivo virava 30min+3min, e o
        trust_network.json assinado carregava timestamps falsos.
  [FIX CRÍTICO] local_pk entra no trust com trust_score=150,
        is_validator=True e connected=True, igual aos peers. Antes,
        o bloco do local chamava register_node() mas NÃO setava
        score/is_validator — o pk local podia não contar como
        validador dependendo de como get_active_validators() filtra.
        Era o que fazia o painel mostrar "2/3 sigs" no #4301.
  [FIX] Skip de "127.0.0.1" / "100.111.253.8" era hardcoded.
        Agora usa BRN_LOCAL_PEER_ADDRS (CSV) + 127.0.0.1 / ::1.
        Num cliente, 100.111.253.8 é o Origin — deve SER incluído no
        trust, não pulado.
  [FIX] remove_ghosts agora usa BRN_TRUST_GHOST_AGE (default 1800s,
        alinhado com a janela do DB) e nunca remove:
          - local_pk (é este nó!)
          - peers com trust_score >= BRN_TRUST_PROTECT_SCORE
            (default 1000 — "core validators")
  [FIX] flush() só quando algo mudou. Antes, assinava/reescrevia
        a cada 120s mesmo sem alteração.
  [FIX] Reutiliza instâncias de Blockchain/ChainDB/TrustNetwork
        entre iterações do loop (abre 1x). Antes, reabria tudo a
        cada 120s, gerando lock contention com o nó em execução.
  [FIX] Probe sign_block com backoff exponencial por peer. Se o
        peer não tem pk no DB e o probe falha, espera 2min →
        4min → 8min → … até BRN_TRUST_PROBE_BACKOFF_MAX.
  [FIX] Se is_origin=True e node_sk=None: warning explícito.
        Clientes com TRUST_DOWNLOAD_REQUIRE_SIG=1 rejeitariam o
        /api/trust/download por falta de signature.
  [NEW] Fallback WalletManager.load_encrypted_wallet se
        secure_store.load_current_wallet falhar (mesmo problema
        .wallet/.enc que o app_wallet_v3.py v3.20 corrigiu).
  [NEW] CLI: --once (roda 1 ciclo e sai), --dry-run (não flush).
        Útil para testar sem side-effects.
  [NEW] Config por env:
          BRN_LOCAL_PEER_ADDRS     (default "127.0.0.1,::1")
          BRN_TRUST_GHOST_AGE      (default "1800" s)
          BRN_TRUST_PROTECT_SCORE  (default "1000")
          BRN_TRUST_DAEMON_INTERVAL (default "120" s)
          BRN_TRUST_PROBE_TIMEOUT  (default "2.0" s)
          BRN_TRUST_PROBE_BACKOFF_MAX (default "600" s)
================================================================
"""
import os
import sys
import time
import re
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

if os.path.exists("brn_network.env"):
    for line in open("brn_network.env", encoding="utf-8").read().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ============================================================
# CONFIG (env)
# ============================================================
def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        v = int(os.environ.get(name, str(default)))
        return max(minimum, v)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        v = float(os.environ.get(name, str(default)))
        return max(minimum, v)
    except (TypeError, ValueError):
        return default


LOOP_INTERVAL      = _env_int("BRN_TRUST_DAEMON_INTERVAL", 120, 10)
GHOST_AGE_S        = _env_int("BRN_TRUST_GHOST_AGE", 1800, 60)
PROTECT_SCORE      = _env_float("BRN_TRUST_PROTECT_SCORE", 1000.0, 0.0)
PROBE_TIMEOUT_S    = _env_float("BRN_TRUST_PROBE_TIMEOUT", 2.0, 0.5)
PROBE_BACKOFF_MAX  = _env_int("BRN_TRUST_PROBE_BACKOFF_MAX", 600, 60)

_LOCAL_ADDRS_ENV = os.environ.get("BRN_LOCAL_PEER_ADDRS", "127.0.0.1,::1")
LOCAL_ADDRS = tuple(a.strip() for a in _LOCAL_ADDRS_ENV.split(",") if a.strip())

VALIDATOR_SCORE    = 150.0  # score dado a novos validadores
PK_RE              = re.compile(r"^0[23][0-9a-fA-F]{64}$")


def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def _is_local_addr(peer_addr: str) -> bool:
    """True se o peer_addr é local (configurável via BRN_LOCAL_PEER_ADDRS)."""
    if not peer_addr:
        return False
    for a in LOCAL_ADDRS:
        if a and a in peer_addr:
            return True
    return False


def _is_valid_pk(pk: str) -> bool:
    return bool(pk and PK_RE.match(pk))


# ============================================================
# WALLET: tentativa principal + fallback WalletManager
# ============================================================
def _load_local_wallet(password: str):
    """
    Tenta secure_store.load_current_wallet; se falhar, tenta
    WalletManager.load_encrypted_wallet em wallets/*.wallet.
    Retorna dict com public_key/private_key ou None.
    Mesmo padrão do app_wallet_v3 v3.20.
    """
    if not password:
        return None

    try:
        from secure_store import load_current_wallet
        w = load_current_wallet(password)
        if w and w.get("ok"):
            pk = (w.get("public_key") or w.get("pub_hex") or "").strip().lower()
            if _is_valid_pk(pk):
                return {
                    "public_key": pk,
                    "private_key": w.get("private_key") or w.get("priv_hex") or "",
                    "source": "secure_store",
                }
    except Exception as e:
        log(f"secure_store.load_current_wallet falhou: {e}", "WARN")

    # Fallback: WalletManager
    try:
        from wallet import WalletManager
    except Exception:
        return None

    if not hasattr(WalletManager, "load_encrypted_wallet"):
        return None

    names = []
    wdir = BASE_DIR / "wallets"
    if wdir.exists():
        for p in sorted(wdir.glob("*.wallet")):
            names.append(p.stem)

    for name in names:
        try:
            data = WalletManager.load_encrypted_wallet(name, password)
        except Exception as e:
            log(f"WalletManager.load_encrypted_wallet({name}) falhou: {e}", "DEBUG")
            continue
        if not data or not isinstance(data, dict):
            continue
        if data.get("ok") is False:
            continue
        pk = (data.get("public_key") or data.get("pub_hex") or "").strip().lower()
        if _is_valid_pk(pk):
            log(f"Carteira carregada via WalletManager: {name}", "OK")
            return {
                "public_key": pk,
                "private_key": data.get("private_key") or data.get("priv_hex") or "",
                "source": f"WalletManager:{name}",
            }

    return None


# ============================================================
# DAEMON
# ============================================================
class TrustDaemon:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.password = os.environ.get("BRN_NODE_PASSWORD", "").strip()
        self.is_origin = os.environ.get("BRN_IS_ORIGIN", "0") == "1"
        self._trust = None
        self._chain = None
        self._node_sk = None
        self._probe_backoff = {}  # peer_addr -> (next_at_ts, delay)

    # --------------------------------------------------------
    # Inicialização (chamada 1x)
    # --------------------------------------------------------
    def _init(self) -> bool:
        try:
            from db import ChainDB
            from trust_network import TrustNetwork
            from secure_store import load_node_identity
            from crypto import Ed25519PrivateKey
            from blockchain import Blockchain
        except ImportError as e:
            log(f"Erro de importação: {e}", "ERROR")
            return False

        # Identidade do Origin (para assinar trust)
        if self.is_origin:
            if not self.password:
                log("BRN_NODE_PASSWORD vazio — não dá para carregar node_identity", "ERROR")
            else:
                try:
                    data = load_node_identity(self.password, "node_identity.enc")
                    if data and data.get("sk"):
                        self._node_sk = Ed25519PrivateKey.from_private_bytes(
                            bytes.fromhex(data["sk"]))
                        log("Identidade do Origin carregada (pode assinar o trust).", "OK")
                    else:
                        log("node_identity.enc sem 'sk' — trust NÃO será assinado", "WARN")
                except Exception as e:
                    log(f"Identidade do Origin falhou: {e}", "WARN")

            if self._node_sk is None:
                log(
                    "is_origin=True mas node_sk=None — trust_network.json "
                    "sairá SEM signature. Clientes com "
                    "BRN_TRUST_REQUIRE_SIGNATURE=1 rejeitarão "
                    "/api/trust/download.",
                    "WARN",
                )

        try:
            self._chain = Blockchain("brn_v2_chain.db", auto_genesis=False)
        except Exception as e:
            log(f"Blockchain não abriu: {e}", "ERROR")
            return False

        try:
            self._trust = TrustNetwork(
                "trust_network.json",
                is_origin=self.is_origin,
                node_sk=self._node_sk,
            )
        except Exception as e:
            log(f"TrustNetwork não abriu: {e}", "ERROR")
            return False

        return True

    # --------------------------------------------------------
    # Local pk (com trust_score e is_validator)
    # --------------------------------------------------------
    def _ensure_local_in_trust(self, changed_flag: list) -> str:
        """
        Garante que a carteira local está no trust como validador
        de pleno direito. Retorna o local_pk (ou "" se não deu).
        """
        if not self.password:
            log("Sem BRN_NODE_PASSWORD — não consigo inspecionar a carteira local.", "DEBUG")
            return ""

        w = _load_local_wallet(self.password)
        if not w:
            log(
                "Carteira local não carregou (senha errada? formato .wallet?). "
                "Local pk NÃO será adicionado ao trust.",
                "WARN",
            )
            return ""

        local_pk = w["public_key"]
        trust = self._trust

        try:
            from bech32 import address_from_pubkey
            addr = address_from_pubkey(bytes.fromhex(local_pk))
        except Exception:
            addr = "brn1_desconhecido"

        if local_pk not in trust.nodes:
            try:
                trust.register_node(
                    node_id=local_pk,
                    address=addr,
                    public_key=local_pk,
                    peer_addr=f"{LOCAL_ADDRS[0] if LOCAL_ADDRS else '127.0.0.1'}:6001",
                )
            except Exception as e:
                log(f"register_node(local) falhou: {e}", "ERROR")
                return local_pk

            n = trust.nodes[local_pk]
            n.trust_score = VALIDATOR_SCORE
            n.is_validator = True
            n.trust_level = "validator"
            n.connected = True
            n.last_seen = time.time()  # agora SIM: é este nó
            changed_flag[0] = True
            log(f"[ADD] Carteira local ({local_pk[:16]}...) → validador", "OK")
        else:
            n = trust.nodes[local_pk]
            # Não rebaixa: se já é validador, mantém. Só corrige se
            # estiver marcado como não-validador por engano.
            if not getattr(n, "is_validator", False):
                n.is_validator = True
                changed_flag[0] = True
            if float(getattr(n, "trust_score", 0) or 0) < VALIDATOR_SCORE:
                n.trust_score = VALIDATOR_SCORE
                changed_flag[0] = True
            if not getattr(n, "connected", False):
                n.connected = True
                changed_flag[0] = True
            # last_seen do local é SEMPRE agora (é este nó).
            n.last_seen = time.time()

        return local_pk

    # --------------------------------------------------------
    # Probe sign_block com backoff por peer
    # --------------------------------------------------------
    def _probe_peer_pubkey(self, peer_addr: str, test_block: dict) -> str:
        """
        Tenta descobrir a pubkey do peer via sign_block.
        Aplica backoff exponencial: se falhar, próximo retry é
        2min, 4min, 8min... até PROBE_BACKOFF_MAX.
        Retorna "" em caso de falha ou se ainda em backoff.
        """
        now = time.time()
        state = self._probe_backoff.get(peer_addr)
        if state and now < state[0]:
            return ""  # ainda em backoff

        try:
            from p2p_unified import P2PClient, CHAIN_ID
        except ImportError:
            return ""

        try:
            ip, port_s = peer_addr.rsplit(":", 1)
            msg = {
                "type": "sign_block",
                "block": test_block,
                "_chain": CHAIN_ID,
                "_port": int(os.environ.get("BRN_P2P_PORT", "6001")),
            }
            resp, _ = P2PClient.send_message(
                ip, int(port_s), msg, timeout=PROBE_TIMEOUT_S)
        except Exception as e:
            log(f"probe {peer_addr} exception: {e}", "DEBUG")
            resp = None

        new_pk = ""
        if resp:
            new_pk = (resp.get("pubkey") or "").strip().lower()
            if not new_pk and resp.get("error"):
                m = re.search(r"my_pub=([0-9a-fA-F]+)", resp["error"])
                if m:
                    new_pk = m.group(1).lower()

        if _is_valid_pk(new_pk):
            self._probe_backoff.pop(peer_addr, None)  # reset
            log(f"  -> {peer_addr}: pubkey={new_pk[:16]}...", "OK")
            return new_pk

        # Backoff exponencial
        prev_delay = state[1] if state else 0
        delay = max(120, prev_delay * 2) if prev_delay else 120
        delay = min(delay, PROBE_BACKOFF_MAX)
        self._probe_backoff[peer_addr] = (now + delay, delay)
        log(f"  -> {peer_addr}: probe falhou, próximo retry em {delay}s", "DEBUG")
        return ""

    # --------------------------------------------------------
    # Sincronização dos peers do DB com o trust
    # --------------------------------------------------------
    def _sync_peers(self, test_block: dict, changed_flag: list) -> dict:
        """
        Lê peers ativos do DB, garante que estão no trust com
        last_seen REAL (do DB), e retorna contadores.
        """
        from db import ChainDB
        from bech32 import address_from_pubkey

        stats = {"added": 0, "updated": 0, "skipped_local": 0,
                 "skipped_no_pk": 0, "skipped_probe": 0}

        try:
            db = ChainDB("brn_v2_chain.db")
            peers = db.listar_peers(apenas_ativos=True, janela=1800)
            db.close()
        except Exception as e:
            log(f"ChainDB não abriu: {e}", "ERROR")
            return stats

        trust = self._trust
        now = time.time()

        for p in peers:
            pk = (p.get("public_key") or "").strip().lower()
            peer_addr = p.get("address", "") or ""
            db_last_seen = float(p.get("last_seen", 0) or 0)

            if _is_local_addr(peer_addr):
                stats["skipped_local"] += 1
                continue

            if not _is_valid_pk(pk):
                pk = self._probe_peer_pubkey(peer_addr, test_block)
                if not _is_valid_pk(pk):
                    stats["skipped_no_pk"] += 1
                    stats["skipped_probe"] += 1
                    continue

            try:
                brn_address = address_from_pubkey(bytes.fromhex(pk))
            except Exception:
                brn_address = "brn1_desconhecido"

            if pk in trust.nodes:
                n = trust.nodes[pk]
                # last_seen = MAX(atual, db) — nunca inventa "agora".
                effective_last_seen = max(
                    float(getattr(n, "last_seen", 0) or 0),
                    db_last_seen,
                )
                dirty = False
                if float(getattr(n, "last_seen", 0) or 0) != effective_last_seen:
                    n.last_seen = effective_last_seen
                    dirty = True
                if (peer_addr and getattr(n, "peer_addr", "") != peer_addr):
                    n.peer_addr = peer_addr
                    dirty = True
                if not getattr(n, "connected", False):
                    n.connected = True
                    dirty = True
                if float(getattr(n, "trust_score", 0) or 0) < VALIDATOR_SCORE:
                    n.trust_score = VALIDATOR_SCORE
                    dirty = True
                if not getattr(n, "is_validator", False):
                    n.is_validator = True
                    dirty = True
                if dirty:
                    stats["updated"] += 1
                    changed_flag[0] = True
            else:
                try:
                    trust.register_node(
                        node_id=pk,
                        address=brn_address,
                        public_key=pk,
                        peer_addr=peer_addr,
                    )
                except Exception as e:
                    log(f"register_node({pk[:16]}) falhou: {e}", "ERROR")
                    continue
                n = trust.nodes[pk]
                n.trust_score = VALIDATOR_SCORE
                n.is_validator = True
                n.trust_level = "validator"
                n.connected = True
                n.last_seen = db_last_seen or now
                stats["added"] += 1
                changed_flag[0] = True
                log(f"[ADD] {pk[:16]}... ({brn_address[:12]}...) "
                    f"via {peer_addr or '?'}", "OK")

        return stats

    # --------------------------------------------------------
    # Remoção de fantasmas (honesta)
    # --------------------------------------------------------
    def _remove_ghosts(self, local_pk: str, changed_flag: list) -> int:
        """
        Remove do trust quem está de fato offline:
          - last_seen < now - GHOST_AGE_S
          - NÃO é local_pk
          - trust_score < PROTECT_SCORE (não remove core)

        Não confia em remove_ghosts() do TrustNetwork porque o
        critério dele pode não ser o mesmo. Faz aqui e reporta.
        """
        trust = self._trust
        if not hasattr(trust, "nodes"):
            return 0

        now = time.time()
        threshold = now - GHOST_AGE_S
        to_remove = []

        for pk, n in list(trust.nodes.items()):
            if pk == local_pk:
                continue
            score = float(getattr(n, "trust_score", 0) or 0)
            if score >= PROTECT_SCORE:
                continue
            last = float(getattr(n, "last_seen", 0) or 0)
            if last and last < threshold:
                to_remove.append((pk, last))

        if not to_remove:
            return 0

        for pk, last in to_remove:
            try:
                del trust.nodes[pk]
                changed_flag[0] = True
                age = int(now - last) if last else -1
                log(f"[DEL] {pk[:16]}... (offline há {age}s > {GHOST_AGE_S}s)", "WARN")
            except Exception as e:
                log(f"[DEL] {pk[:16]}... falhou: {e}", "ERROR")

        return len(to_remove)

    # --------------------------------------------------------
    # Loop
    # --------------------------------------------------------
    def run_once(self) -> bool:
        h = self._chain.db.height()
        test_block = self._chain.db.get_block(h)
        if not test_block:
            log("Blockchain vazia. Aguardando…", "WARN")
            return False

        changed_flag = [False]
        local_pk = self._ensure_local_in_trust(changed_flag)
        stats = self._sync_peers(test_block, changed_flag)
        removed = self._remove_ghosts(local_pk, changed_flag)

        validators = [n for n in self._trust.nodes.values()
                      if getattr(n, "is_validator", False)]

        log(
            f"ciclo: height={h} add={stats['added']} upd={stats['updated']} "
            f"del={removed} local={'yes' if local_pk else 'no'} "
            f"validators={len(validators)} changed={changed_flag[0]}"
        )

        if changed_flag[0] and not self.dry_run:
            try:
                self._trust.flush()
                if self.is_origin and self._node_sk is not None:
                    log("trust_network.json gravado e ASSINADO", "OK")
                elif self.is_origin:
                    log("trust_network.json gravado SEM signature "
                        "(node_sk=None)", "WARN")
                else:
                    log("trust_network.local.json gravado", "OK")
            except Exception as e:
                log(f"trust.flush() falhou: {e}", "ERROR")
                return False
        elif changed_flag[0] and self.dry_run:
            log("dry-run: NÃO gravei trust", "INFO")
        else:
            log("nada mudou, trust não regravado", "DEBUG")

        return True

    def loop(self) -> None:
        log("=" * 60)
        log(f" AUTO-TRUST DAEMON v3.0 (loop {LOOP_INTERVAL}s, "
            f"ghost_age={GHOST_AGE_S}s, protect_score={PROTECT_SCORE:.0f})")
        log(f"  local_addrs={LOCAL_ADDRS}")
        log(f"  is_origin={self.is_origin} dry_run={self.dry_run}")
        log("=" * 60)

        if not self._init():
            return

        try:
            while True:
                try:
                    self.run_once()
                except Exception as e:
                    log(f"Erro no ciclo: {e}", "ERROR")
                    import traceback
                    log(traceback.format_exc(), "DEBUG")
                time.sleep(LOOP_INTERVAL)
        except KeyboardInterrupt:
            log("Encerrando daemon (Ctrl+C)…", "INFO")
        finally:
            try:
                if self._chain is not None:
                    self._chain.db.close()
            except Exception:
                pass


# ============================================================
# MAIN
# ============================================================
def main() -> int:
    p = argparse.ArgumentParser(prog="auto_trust_daemon.py")
    p.add_argument("--once", action="store_true",
                   help="roda 1 ciclo e sai (para teste)")
    p.add_argument("--dry-run", action="store_true",
                   help="não grava trust_network (só log)")
    args = p.parse_args()

    d = TrustDaemon(dry_run=args.dry_run)

    if args.once:
        if not d._init():
            return 1
        try:
            d.run_once()
        finally:
            try:
                if d._chain is not None:
                    d._chain.db.close()
            except Exception:
                pass
        return 0

    d.loop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
