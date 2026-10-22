"""
miner_loop.py — Loop de mineração do BRN (v6.9)
================================================================
v6.9 (2026-10-22):
  [CRITICAL] WEB_PORT lido em import time no processo do cliente.
             Agora cada peer tem endpoint parseado (ip:p2p:http).
  [CRITICAL] _enviar_para_fila chamava peers sequencialmente.
             Agora ThreadPoolExecutor (paralelo).
  [CRITICAL] IS_ORIGIN vinha de env var. Agora Miner.start(is_origin=).
  [FIX] rede_descentralizada lido do DB no restart.
  [FIX] Backoff por peer (3 falhas → cooldown 60s).
  [FIX] Log spam reduzido.
================================================================
"""
import os
import json
import time
import threading
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

try:
    from brn_logger import get_logger
    def _log(msg):
        get_logger("miner").info(msg)
    def _log_dbg(msg):
        get_logger("miner").debug(msg)
except Exception:
    def _log(msg):
        print(f"[Miner] {msg}", flush=True)
    def _log_dbg(msg):
        pass

print(f"[Miner] Modulo carregado: {os.path.abspath(__file__)}", flush=True)

MINER_INTERVAL      = int(os.environ.get("BRN_MINER_INTERVAL", "30"))
MINER_AUTO          = os.environ.get("BRN_MINER_AUTO", "1") == "1"
IS_ORIGIN           = os.environ.get("BRN_IS_ORIGIN", "0") == "1"

BASE_DIR                 = Path(__file__).resolve().parent
CURRENT_WALLET_ENC_FILE  = str(BASE_DIR / "current_wallet.enc")
CURRENT_WALLET_FILE      = str(BASE_DIR / "current_wallet.json")

FORK_POLL_INTERVAL = float(os.environ.get("BRN_FORK_POLL_INTERVAL", "0.5"))

BOOTSTRAP_HTTP_PORT_DEFAULT = int(
    os.environ.get("BRN_BOOTSTRAP_HTTP_PORT", "5000")
)
BOOTSTRAP_TIMEOUT_S = float(os.environ.get("BRN_BOOTSTRAP_TIMEOUT", "5.0"))
BOOTSTRAP_MAX_FAILS = int(os.environ.get("BRN_BOOTSTRAP_MAX_FAILS", "3"))
BOOTSTRAP_COOLDOWN_S = float(
    os.environ.get("BRN_BOOTSTRAP_COOLDOWN_S", "60")
)
BOOTSTRAP_LOG_INTERVAL_S = float(
    os.environ.get("BRN_BOOTSTRAP_LOG_INTERVAL", "60")
)

_MINER_INSTANCE = None
_MINER_LOCK = threading.Lock()


def get_miner(chain):
    global _MINER_INSTANCE
    with _MINER_LOCK:
        if _MINER_INSTANCE is None:
            _MINER_INSTANCE = Miner(chain)
        else:
            _MINER_INSTANCE.chain = chain
        return _MINER_INSTANCE


def _senha_sessao() -> str:
    return (os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
            or os.environ.get("BRN_NODE_PASSWORD", "").strip())


def _ler_carteira_aberta():
    senha = _senha_sessao()
    if senha:
        try:
            from secure_store import load_current_wallet
            r = load_current_wallet(senha)
            if r and r.get("ok"):
                addr = (r.get("address") or "").strip()
                pub  = (r.get("pub_hex") or r.get("public_key")
                        or r.get("pubkey") or "").strip()
                if addr.startswith("brn1"):
                    return {"address": addr, "pubkey": pub}
        except Exception as e:
            _log(f"_ler_carteira_aberta(.enc) falhou: {e}")

    try:
        p = Path(CURRENT_WALLET_FILE)
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                d = json.load(f)
            addr = (d.get("address") or "").strip()
            pub  = (d.get("pub_hex") or d.get("pubkey")
                    or d.get("public_key") or "").strip()
            if addr.startswith("brn1"):
                return {"address": addr, "pubkey": pub}
    except Exception:
        pass

    return {}


def _pubkey_no_db(bc, addr):
    try:
        utxos = bc.db.get_utxos(addr)
        for u in utxos:
            pk = (u.get("pubkey") or "").strip()
            if pk:
                return pk
    except Exception:
        pass
    return ""


def _parse_peer_endpoint(peer: str) -> Optional[tuple]:
    if not peer or ":" not in peer:
        return None
    s = peer.strip()
    scheme = "http"
    if "/" in s:
        s, _, scheme_part = s.partition("/")
        scheme = (scheme_part or "http").strip().lower()
        if scheme not in ("http", "https"):
            return None
    parts = s.split(":")
    if len(parts) == 2:
        host, p2p_s = parts
        http_s = str(BOOTSTRAP_HTTP_PORT_DEFAULT)
    elif len(parts) == 3:
        host, p2p_s, http_s = parts
    else:
        return None
    try:
        p2p_port = int(p2p_s)
        http_port = int(http_s)
    except ValueError:
        return None
    if not (1 <= p2p_port <= 65535 and 1 <= http_port <= 65535):
        return None
    if not host or len(host) > 255:
        return None
    return host, p2p_port, http_port, scheme


class Miner:
    def __init__(self, chain):
        self.chain = chain
        self._thread = None
        self._stop_evt = threading.Event()
        self.address = ""
        self.pubkey = ""
        self.running = False
        self.blocks_mined = 0
        self.last_error = ""
        self.last_height = None
        self.started_at = 0.0
        self.fork_aborts = 0
        self.last_fork_at = 0.0

        self.is_origin = IS_ORIGIN

        self._bootstrap_fails: dict = {}
        self._bootstrap_lock = threading.Lock()
        self._last_queue_log = 0.0
        self._last_queue_value = -1

    def start(self, address, pubkey_hex="", is_origin: Optional[bool] = None):
        if self.running:
            return False, "ja rodando"
        if not address:
            return False, "endereco obrigatorio"
        if not pubkey_hex:
            pubkey_hex = _pubkey_no_db(self.chain, address)
        if not pubkey_hex:
            msg = f"pubkey desconhecida para {address} (receba uma tx antes)"
            self.last_error = msg
            _log(msg)
            return False, msg

        if is_origin is not None:
            self.is_origin = bool(is_origin)

        self.address = address
        self.pubkey = pubkey_hex
        self.blocks_mined = 0
        self.last_error = ""
        self.last_height = None
        self.started_at = time.time()
        self.fork_aborts = 0
        self.last_fork_at = 0.0
        self._stop_evt.clear()
        self.running = True

        self._thread = threading.Thread(target=self._loop, daemon=True, name="Miner")
        self._thread.start()
        _log(f"Miner iniciado | addr={address[:16]}... | pub={pubkey_hex[:16]}... | "
             f"is_origin={self.is_origin}")
        return True, "iniciado"

    def stop(self):
        if not self.running:
            return False, "nao estava rodando"
        self._stop_evt.set()
        self.running = False
        _log(f"Miner parado (total={self.blocks_mined})")
        return True, "parado"

    def status(self):
        uptime = 0
        if self.started_at and self.running:
            uptime = int(time.time() - self.started_at)
        elif self.started_at and not self.running:
            uptime = int(time.time() - self.started_at) if self.blocks_mined else 0

        fork_sig = getattr(self.chain, "_fork_signal", None)
        forking = bool(fork_sig and fork_sig.is_set())

        status_msg = ""
        if not self.chain.rede_descentralizada and not self.is_origin:
            status_msg = f"Aguardando Fila ({len(self.chain.fila_mineracao)}/20)"
        elif not self.chain.rede_descentralizada and self.is_origin:
            status_msg = "Validador (Bootstrap)"
        else:
            status_msg = "Descentralizada"

        with self._bootstrap_lock:
            n_peers = len(self._bootstrap_fails)
            n_blocked = sum(
                1 for e in self._bootstrap_fails.values()
                if e.get("blocked_until", 0) > time.time()
            )

        return {
            "running": self.running,
            "address": self.address,
            "pubkey": self.pubkey,
            "blocks_mined": self.blocks_mined,
            "height": self.chain.db.height(),
            "difficulty": self.chain.current_difficulty(),
            "last_error": self.last_error,
            "count": self.blocks_mined,
            "last_height": self.last_height,
            "uptime": uptime,
            "fork_aborts": self.fork_aborts,
            "last_fork_ago_s": (int(time.time() - self.last_fork_at)
                                 if self.last_fork_at else None),
            "fork_in_progress": forking,
            "bootstrap_status": status_msg,
            "rede_descentralizada": self.chain.rede_descentralizada,
            "is_origin": self.is_origin,
            "bootstrap_peers_tracked": n_peers,
            "bootstrap_peers_blocked": n_blocked,
        }

    def _should_continue(self) -> bool:
        if self._stop_evt.is_set():
            return False
        fork_sig = getattr(self.chain, "_fork_signal", None)
        if fork_sig is not None and fork_sig.is_set():
            self.fork_aborts += 1
            self.last_fork_at = time.time()
            _log("[FORK] abortando bloco atual — reorg em andamento")
            return False
        return True

    def _peer_is_blocked(self, key: str) -> bool:
        with self._bootstrap_lock:
            e = self._bootstrap_fails.get(key)
            if not e:
                return False
            return e.get("blocked_until", 0) > time.time()

    def _peer_record_fail(self, key: str) -> None:
        now = time.time()
        with self._bootstrap_lock:
            e = self._bootstrap_fails.setdefault(
                key, {"fails": 0, "blocked_until": 0}
            )
            e["fails"] = e.get("fails", 0) + 1
            if e["fails"] >= BOOTSTRAP_MAX_FAILS:
                e["blocked_until"] = now + BOOTSTRAP_COOLDOWN_S
                _log(f"[BOOTSTRAP] {key} bloqueado por {BOOTSTRAP_COOLDOWN_S:.0f}s "
                     f"({e['fails']} falhas)")

    def _peer_record_success(self, key: str) -> None:
        with self._bootstrap_lock:
            self._bootstrap_fails.pop(key, None)

    def _load_bootstrap_peers(self) -> list:
        try:
            from brn_config import get_config
            cfg = get_config()
            peers = cfg.get("bootstrap_peers", []) if cfg else []
            if peers:
                return [str(p).strip() for p in peers if str(p).strip()]
        except Exception:
            pass
        env = os.environ.get("BRN_BOOTSTRAP_PEERS", "")
        return [p.strip() for p in env.split(",") if p.strip()]

    def _enviar_para_fila(self) -> bool:
        peers = self._load_bootstrap_peers()
        if not peers:
            self._log_bootstrap_status("Sem peers de bootstrap configurados", 0, force=True)
            return False

        endpoints = []
        for p in peers:
            ep = _parse_peer_endpoint(p)
            if ep is None:
                _log(f"[BOOTSTRAP] peer mal formatado: {p!r}")
                continue
            endpoints.append((p, ep))

        if not endpoints:
            self._log_bootstrap_status(
                "Nenhum peer de bootstrap com endpoint válido", 0, force=True
            )
            return False

        body = json.dumps({
            "address": self.address,
            "pubkey": self.pubkey,
        }).encode("utf-8")

        def _call_one(item):
            raw, (host, _p2p, http_port, scheme) = item
            key = f"{host}:{http_port}/{scheme}"
            if self._peer_is_blocked(key):
                return (item, None, "cooldown")
            url = f"{scheme}://{host}:{http_port}/api/bootstrap/add"
            try:
                req = urllib.request.Request(
                    url, data=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "brn-miner-loop/6.9",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(
                    req, timeout=BOOTSTRAP_TIMEOUT_S
                ) as resp:
                    raw_body = resp.read().decode("utf-8")
                    data = json.loads(raw_body)
                return (item, data, None)
            except Exception as e:
                return (item, None, str(e))

        results = []
        if len(endpoints) == 1:
            results = [_call_one(endpoints[0])]
        else:
            with ThreadPoolExecutor(
                max_workers=min(len(endpoints), 8),
                thread_name_prefix="bootstrap",
            ) as ex:
                results = list(ex.map(_call_one, endpoints))

        confirmed_desc = False
        best_queue = 0
        for item, data, err in results:
            raw_peer, (host, _p2p, http_port, scheme) = item
            key = f"{host}:{http_port}/{scheme}"
            if err:
                if err == "cooldown":
                    _log_dbg(f"[BOOTSTRAP] {key} em cooldown")
                    continue
                _log(f"[BOOTSTRAP] {key} falhou: {err}")
                self._peer_record_fail(key)
                continue
            if not data or not data.get("ok"):
                self._peer_record_fail(key)
                msg = (data or {}).get("msg", "resposta inválida")
                _log_dbg(f"[BOOTSTRAP] {key}: {msg}")
                continue
            self._peer_record_success(key)
            fila = int(data.get("fila_tamanho", 0) or 0)
            desc = bool(data.get("rede_descentralizada", False))
            if fila > best_queue:
                best_queue = fila
            if desc:
                _log(f"[BOOTSTRAP] rede descentralizada confirmada por {key}! "
                     f"Iniciando mineração...")
                confirmed_desc = True

        if confirmed_desc:
            if not self.chain.rede_descentralizada:
                self.chain.rede_descentralizada = True
                try:
                    self.chain.db.set_meta("rede_descentralizada", "1")
                except Exception as e:
                    _log_dbg(f"[BOOTSTRAP] set_meta falhou: {e}")
            return True

        self._log_bootstrap_status(
            f"Aguardando descentralização (fila máx: {best_queue}/20)",
            best_queue,
        )
        return False

    def _log_bootstrap_status(self, msg: str, queue: int, force: bool = False) -> None:
        now = time.time()
        mudou = (queue != self._last_queue_value)
        passou_tempo = (now - self._last_queue_log) >= BOOTSTRAP_LOG_INTERVAL_S
        if force or mudou or passou_tempo:
            _log(f"[BOOTSTRAP] {msg}")
            self._last_queue_log = now
            self._last_queue_value = queue

    def _loop(self):
        try:
            db_flag = self.chain.db.get_meta("rede_descentralizada")
            if db_flag is not None and str(db_flag) in ("1", "true", "True"):
                if not self.chain.rede_descentralizada:
                    self.chain.rede_descentralizada = True
                    _log("[BOOT] rede_descentralizada=1 lido do DB")
        except Exception:
            pass

        while not self._stop_evt.is_set():
            try:
                if self.chain.db.height() < 0:
                    _log("Aguardando genesis (DB vazio)...")
                    if self._stop_evt.wait(timeout=5):
                        break
                    continue

                if not self.pubkey:
                    _log("Sem pubkey — parando")
                    self.running = False
                    return

                if not self.is_origin and not self.chain.rede_descentralizada:
                    t_call = time.time()
                    self._enviar_para_fila()
                    elapsed = time.time() - t_call
                    wait_s = max(1.0, 15.0 - elapsed)
                    if self._stop_evt.wait(timeout=wait_s):
                        break
                    continue

                fork_sig = getattr(self.chain, "_fork_signal", None)
                if fork_sig is not None and fork_sig.is_set():
                    waited = 0.0
                    while (fork_sig.is_set() and waited < 60.0
                           and not self._stop_evt.is_set()):
                        if self._stop_evt.wait(FORK_POLL_INTERVAL):
                            break
                        waited += FORK_POLL_INTERVAL
                    if self._stop_evt.is_set():
                        break
                    continue

                block = self.chain.mine_block_interruptible(
                    self.address,
                    self.pubkey,
                    should_continue=self._should_continue,
                )

                if block is None:
                    if self._stop_evt.is_set():
                        break
                    continue

                self.blocks_mined += 1
                h = block.get("height")
                self.last_height = h
                _log(f"OK Bloco #{h} minerado (total={self.blocks_mined})")

            except ValueError as e:
                self.last_error = str(e)
                _log(f"Miner erro: {e}")
                self.running = False
                return
            except Exception as e:
                self.last_error = str(e)
                _log(f"Miner exception: {e}")
                traceback.print_exc()
                if self._stop_evt.wait(timeout=3):
                    break

        self.running = False
        _log("Miner loop encerrado")


def loop_mineracao(chain, intervalo=None):
    if not MINER_AUTO:
        _log("Auto-mineracao DESATIVADA")
        return
    intervalo = intervalo or MINER_INTERVAL
    time.sleep(5)
    m = get_miner(chain)
    w = _ler_carteira_aberta()
    if w.get("address"):
        ok, msg = m.start(w["address"], w.get("pubkey", ""))
        _log(f"auto-start: {ok} ({msg})")
    else:
        _log("auto-start: sem carteira aberta — aguardando current_wallet")


def iniciar_mineracao(chain, intervalo=None):
    t = threading.Thread(target=loop_mineracao, args=(chain, intervalo),
                         daemon=True, name="MinerLoop")
    t.start()
    return t


start_miner = start_mining = iniciar_miner = iniciar_miner_loop = iniciar_mineracao
