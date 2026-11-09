"""
miner_loop.py — Loop de mineração do BRN (v7.2)
================================================================
v7.2 (2026-11-09):
  [NEW] Adicionado cálculo de Hashrate para Mineração Solo (Local).
        Agora a GUI consegue exibir a velocidade (kH/s) baseada no
        tempo gasto para encontrar o bloco e na dificuldade.
  [KEEP] Toda a lógica de v7.1 (sync check, quórum, bootstrap, forks).
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
    def _log(msg): get_logger("miner").info(msg)
    def _log_dbg(msg): get_logger("miner").debug(msg)
except Exception:
    def _log(msg): print(f"[Miner] {msg}", flush=True)
    def _log_dbg(msg): pass

print(f"[Miner] Modulo carregado: {os.path.abspath(__file__)}", flush=True)

MINER_INTERVAL      = int(os.environ.get("BRN_MINER_INTERVAL", "30"))
MINER_AUTO          = os.environ.get("BRN_MINER_AUTO", "1") == "1"
IS_ORIGIN           = os.environ.get("BRN_IS_ORIGIN", "0") == "1"

BASE_DIR                 = Path(__file__).resolve().parent
CURRENT_WALLET_ENC_FILE  = str(BASE_DIR / "current_wallet.enc")
CURRENT_WALLET_FILE      = str(BASE_DIR / "current_wallet.json")

FORK_POLL_INTERVAL = float(os.environ.get("BRN_FORK_POLL_INTERVAL", "0.5"))

BOOTSTRAP_HTTP_PORT_DEFAULT = int(os.environ.get("BRN_BOOTSTRAP_HTTP_PORT", "5000"))
BOOTSTRAP_TIMEOUT_S = float(os.environ.get("BRN_BOOTSTRAP_TIMEOUT", "5.0"))
BOOTSTRAP_MAX_FAILS = int(os.environ.get("BRN_BOOTSTRAP_MAX_FAILS", "3"))
BOOTSTRAP_COOLDOWN_S = float(os.environ.get("BRN_BOOTSTRAP_COOLDOWN_S", "60"))
BOOTSTRAP_LOG_INTERVAL_S = float(os.environ.get("BRN_BOOTSTRAP_LOG_INTERVAL", "60"))

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
                pub  = (r.get("pub_hex") or r.get("public_key") or r.get("pubkey") or "").strip()
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
            pub  = (d.get("pub_hex") or d.get("pubkey") or d.get("public_key") or "").strip()
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
            if pk and set(pk) != {"0"}:
                return pk
    except Exception:
        pass
    return ""

def _parse_peer_endpoint(peer: str) -> Optional[tuple]:
    if not peer or ":" not in peer: return None
    s = peer.strip()
    scheme = "http"
    if "/" in s:
        s, _, scheme_part = s.partition("/")
        scheme = (scheme_part or "http").strip().lower()
        if scheme not in ("http", "https"): return None
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
    if not (1 <= p2p_port <= 65535 and 1 <= http_port <= 65535): return None
    if not host or len(host) > 255: return None
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
        self._fork_aborts_for_signal = False
        self.is_origin = IS_ORIGIN
        self._bootstrap_fails: dict = {}
        self._bootstrap_lock = threading.Lock()
        self._last_queue_log = 0.0
        self._last_queue_value = -1
        
        # [NOVO v7.2] Variáveis de Hashrate
        self.hashrate = 0.0
        self.last_block_dt = 0.0

    def start(self, address, pubkey_hex="", is_origin: Optional[bool] = None):
        if self.running: return False, "ja rodando"
        if not address: return False, "endereco obrigatorio"
        if not pubkey_hex: pubkey_hex = _pubkey_no_db(self.chain, address)
        if not pubkey_hex:
            msg = f"pubkey desconhecida para {address} (receba uma tx antes)"
            self.last_error = msg
            _log(msg)
            return False, msg

        if is_origin is not None: self.is_origin = bool(is_origin)

        self.address = address
        self.pubkey = pubkey_hex
        self.blocks_mined = 0
        self.last_error = ""
        self.last_height = None
        self.started_at = time.time()
        self.fork_aborts = 0
        self.last_fork_at = 0.0
        self._fork_aborts_for_signal = False
        self._stop_evt.clear()
        self.running = True
        self.hashrate = 0.0
        self.last_block_dt = 0.0

        self._thread = threading.Thread(target=self._loop, daemon=True, name="Miner")
        self._thread.start()
        _log(f"Miner iniciado | addr={address[:16]}... | pub={pubkey_hex[:16]}... | is_origin={self.is_origin}")
        return True, "iniciado"

    def stop(self):
        if not self.running: return False, "nao estava rodando"
        self._stop_evt.set()
        self.running = False
        _log(f"Miner parado (total={self.blocks_mined})")
        return True, "parado"

    def status(self):
        uptime = int(time.time() - self.started_at) if self.started_at else 0
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
            n_blocked = sum(1 for e in self._bootstrap_fails.values() if e.get("blocked_until", 0) > time.time())

        return {
            "running": self.running, "address": self.address, "pubkey": self.pubkey,
            "blocks_mined": self.blocks_mined, "height": self.chain.db.height(),
            "difficulty": self.chain.current_difficulty(), "last_error": self.last_error,
            "count": self.blocks_mined, "last_height": self.last_height, "uptime": uptime,
            "fork_aborts": self.fork_aborts, "last_fork_ago_s": (int(time.time() - self.last_fork_at) if self.last_fork_at else None),
            "fork_in_progress": forking, "bootstrap_status": status_msg,
            "rede_descentralizada": self.chain.rede_descentralizada, "is_origin": self.is_origin,
            "bootstrap_peers_tracked": n_peers, "bootstrap_peers_blocked": n_blocked,
            # [NOVO v7.2] Hashrate no status
            "hashrate_khs": round(self.hashrate / 1000.0, 2),
            "hashrate": round(self.hashrate, 2),
        }

    def _network_is_ready(self) -> tuple[bool, str]:
        try:
            local_h = self.chain.db.height()
            max_peer_h = local_h
            p2p = getattr(self.chain, "p2p", None)
            if p2p and hasattr(p2p, "discovery"):
                peers = p2p.discovery.listar_peers_com_altura()
                if peers:
                    for p in peers:
                        h = int(p.get("height", 0) or 0)
                        if h > max_peer_h: max_peer_h = h
            if max_peer_h > local_h + 2:
                return False, f"Sincronizando (Local: {local_h}, Rede: {max_peer_h})"
        except Exception as e:
            _log_dbg(f"Falha ao checar altura dos peers: {e}")

        try:
            validators = self.chain._get_active_validators()
            if validators:
                required = self.chain._required_signatures(validators)
                trust = getattr(self.chain, "trust_net", None)
                if trust and hasattr(trust, "nodes"):
                    online_validators = 0
                    for pk in validators:
                        node = trust.nodes.get(pk)
                        if node and getattr(node, "connected", False):
                            online_validators += 1
                    if online_validators < required:
                        return False, f"Sem quórum para minerar (Validadores online: {online_validators}/{required})"
        except Exception as e:
            _log_dbg(f"Falha ao checar quórum de validadores: {e}")

        return True, "OK"

    def _should_continue(self) -> bool:
        if self._stop_evt.is_set(): return False
        fork_sig = getattr(self.chain, "_fork_signal", None)
        if fork_sig is not None and fork_sig.is_set():
            if not self._fork_aborts_for_signal:
                self.fork_aborts += 1
                self.last_fork_at = time.time()
                _log("[FORK] abortando bloco atual — reorg em andamento")
                self._fork_aborts_for_signal = True
            return False
        self._fork_aborts_for_signal = False
        return True

    def _peer_is_blocked(self, key: str) -> bool:
        with self._bootstrap_lock:
            e = self._bootstrap_fails.get(key)
            return e and e.get("blocked_until", 0) > time.time()

    def _peer_record_fail(self, key: str) -> None:
        now = time.time()
        with self._bootstrap_lock:
            e = self._bootstrap_fails.setdefault(key, {"fails": 0, "blocked_until": 0})
            e["fails"] = e.get("fails", 0) + 1
            if e["fails"] >= BOOTSTRAP_MAX_FAILS:
                e["blocked_until"] = now + BOOTSTRAP_COOLDOWN_S
                _log(f"[BOOTSTRAP] {key} bloqueado por {BOOTSTRAP_COOLDOWN_S:.0f}s ({e['fails']} falhas)")

    def _peer_record_success(self, key: str) -> None:
        with self._bootstrap_lock:
            self._bootstrap_fails.pop(key, None)

    def _load_bootstrap_peers(self) -> list:
        try:
            from brn_config import get_config
            cfg = get_config()
            peers = cfg.get("bootstrap_peers", []) if cfg else []
            if peers: return [str(p).strip() for p in peers if str(p).strip()]
        except Exception: pass
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
            if ep: endpoints.append((p, ep))

        if not endpoints: return False

        body = json.dumps({"address": self.address, "pubkey": self.pubkey}).encode("utf-8")

        def _call_one(item):
            raw, (host, _p2p, http_port, scheme) = item
            key = f"{host}:{http_port}/{scheme}"
            if self._peer_is_blocked(key): return (item, None, "cooldown")
            url = f"{scheme}://{host}:{http_port}/api/bootstrap/add"
            try:
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=BOOTSTRAP_TIMEOUT_S) as resp:
                    return (item, json.loads(resp.read().decode("utf-8")), None)
            except Exception as e:
                return (item, None, str(e))

        results = []
        if len(endpoints) == 1:
            results = [_call_one(endpoints[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(len(endpoints), 8)) as ex:
                results = list(ex.map(_call_one, endpoints))

        confirmed_desc = False
        best_queue = 0
        for item, data, err in results:
            raw_peer, (host, _p2p, http_port, scheme) = item
            key = f"{host}:{http_port}/{scheme}"
            if err:
                if err != "cooldown": self._peer_record_fail(key)
                continue
            if not data or not data.get("ok"):
                self._peer_record_fail(key)
                continue
            self._peer_record_success(key)
            fila = int(data.get("fila_tamanho", 0) or 0)
            desc = bool(data.get("rede_descentralizada", False))
            if fila > best_queue: best_queue = fila
            if desc: confirmed_desc = True

        if confirmed_desc:
            if not self.chain.rede_descentralizada:
                self.chain.rede_descentralizada = True
                try: self.chain.db.set_meta("rede_descentralizada", "1")
                except Exception: pass
            return True

        self._log_bootstrap_status(f"Aguardando descentralização (fila máx: {best_queue}/20)", best_queue)
        return False

    def _log_bootstrap_status(self, msg: str, queue: int, force: bool = False) -> None:
        now = time.time()
        if force or (queue != self._last_queue_value) or (now - self._last_queue_log >= BOOTSTRAP_LOG_INTERVAL_S):
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
        except Exception: pass

        while not self._stop_evt.is_set():
            try:
                if self.chain.db.height() < 0:
                    if self._stop_evt.wait(timeout=5): break
                    continue

                if not self.pubkey:
                    self.running = False
                    return

                if not self.is_origin and not self.chain.rede_descentralizada:
                    t_call = time.time()
                    self._enviar_para_fila()
                    if self._stop_evt.wait(timeout=max(1.0, 15.0 - (time.time() - t_call))): break
                    continue

                fork_sig = getattr(self.chain, "_fork_signal", None)
                if fork_sig is not None and fork_sig.is_set():
                    waited = 0.0
                    while fork_sig.is_set() and waited < 60.0 and not self._stop_evt.is_set():
                        if self._stop_evt.wait(FORK_POLL_INTERVAL): break
                        waited += FORK_POLL_INTERVAL
                    if self._stop_evt.is_set(): break
                    continue

                ready, reason = self._network_is_ready()
                if not ready:
                    self.last_error = reason
                    _log(f"[MINER] Pausado: {reason}")
                    if self._stop_evt.wait(5.0): break
                    continue

                self.last_error = ""
                
                # [NOVO v7.2] Início da contagem de tempo para o Hashrate
                _t0 = time.time()
                
                block = self.chain.mine_block_interruptible(
                    self.address, self.pubkey, should_continue=self._should_continue,
                )
                
                # [NOVO v7.2] Cálculo do Hashrate após minerar
                if block is not None:
                    _dt = time.time() - _t0
                    self.last_block_dt = _dt
                    if _dt > 0:
                        diff = block.get("difficulty", self.chain.current_difficulty())
                        # 16^diff é a média de tentativas para achar o bloco
                        expected_hashes = 16 ** diff
                        self.hashrate = expected_hashes / _dt

                if block is None:
                    if self._stop_evt.is_set(): break
                    continue

                self.blocks_mined += 1
                self.last_height = block.get("height")
                _log(f"OK Bloco #{self.last_height} minerado (total={self.blocks_mined})")

            except ValueError as e:
                self.last_error = f"ValueError: {e}"
                _log(f"Miner ValueError (retry em 5s): {e}")
                if self._stop_evt.wait(timeout=5): break
                continue
            except Exception as e:
                self.last_error = str(e)
                _log(f"Miner exception: {e}")
                traceback.print_exc()
                if self._stop_evt.wait(timeout=3): break

        self.running = False
        _log("Miner loop encerrado")

def loop_mineracao(chain, intervalo=None):
    if not MINER_AUTO: return
    time.sleep(5)
    m = get_miner(chain)
    w = _ler_carteira_aberta()
    if w.get("address"):
        m.start(w["address"], w.get("pubkey", ""))

def iniciar_mineracao(chain, intervalo=None):
    t = threading.Thread(target=loop_mineracao, args=(chain, intervalo), daemon=True, name="MinerLoop")
    t.start()
    return t

start_miner = start_mining = iniciar_miner = iniciar_miner_loop = iniciar_mineracao
