"""
miner_loop.py — Loop de mineracao do BRN (v6.8 - Bootstrap HTTP Fix)
================================================================
v6.8:
  [FIX] _enviar_para_fila agora usa HTTP (porta 5000) em vez de P2P.
        O P2P não tem handler para "join_mining_queue", então a
        mensagem era sempre rejeitada e o cliente ficava preso no
        Bootstrap para sempre.
        Agora usa POST /api/bootstrap/add e lê a resposta real para
        atualizar chain.rede_descentralizada localmente.
"""
import os
import json
import time
import threading
import traceback
import urllib.request
import urllib.error
from pathlib import Path

try:
    from brn_logger import get_logger
    def _log(msg):
        get_logger("miner").info(msg)
except Exception:
    def _log(msg):
        print(f"[Miner] {msg}", flush=True)

print(f"[Miner] Modulo carregado: {os.path.abspath(__file__)}", flush=True)

MINER_INTERVAL      = int(os.environ.get("BRN_MINER_INTERVAL", "30"))
MINER_AUTO          = os.environ.get("BRN_MINER_AUTO", "1") == "1"
IS_ORIGIN           = os.environ.get("BRN_IS_ORIGIN", "0") == "1"

BASE_DIR                 = Path(__file__).resolve().parent
CURRENT_WALLET_ENC_FILE  = str(BASE_DIR / "current_wallet.enc")
CURRENT_WALLET_FILE      = str(BASE_DIR / "current_wallet.json")

FORK_POLL_INTERVAL = float(os.environ.get("BRN_FORK_POLL_INTERVAL", "0.5"))

# Porta HTTP do nó Origem (para POST /api/bootstrap/add)
WEB_PORT = int(os.environ.get("BRN_WEB_PORT", "5000"))

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
            if pk:
                return pk
    except Exception:
        pass
    return ""


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

    def start(self, address, pubkey_hex=""):
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
        _log(f"Miner iniciado | addr={address[:16]}... | pub={pubkey_hex[:16]}...")
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
        if not self.chain.rede_descentralizada and not IS_ORIGIN:
            status_msg = f"Aguardando Fila ({len(self.chain.fila_mineracao)}/20)"
        elif not self.chain.rede_descentralizada and IS_ORIGIN:
            status_msg = "Validador (Bootstrap)"
        else:
            status_msg = "Descentralizada"

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
            "last_fork_ago_s": (int(time.time() - self.last_fork_at) if self.last_fork_at else None),
            "fork_in_progress": forking,
            "bootstrap_status": status_msg,
            "rede_descentralizada": self.chain.rede_descentralizada
        }

    def _should_continue(self) -> bool:
        if self._stop_evt.is_set():
            return False
        fork_sig = getattr(self.chain, "_fork_signal", None)
        if fork_sig is not None and fork_sig.is_set():
            self.fork_aborts += 1
            self.last_fork_at = time.time()
            _log(f"[FORK] abortando bloco atual — reorg em andamento")
            return False
        return True

    # ============================================================
    # [FIX v6.8] Envia para a fila via HTTP (não P2P)
    # ============================================================
    def _enviar_para_fila(self):
        """
        Envia um POST HTTP para /api/bootstrap/add em cada peer
        de bootstrap. Lê a resposta REAL para atualizar
        chain.rede_descentralizada localmente.
        """
        try:
            from brn_config import get_config
            
            cfg = get_config()
            peers = cfg.get("bootstrap_peers", [])
            
            if not peers:
                _log("Sem peers de bootstrap configurados")
                return

            for peer in peers:
                if ":" not in peer:
                    continue
                
                # Peer vem como "ip:6001" (P2P), mas a API HTTP é 5000
                ip = peer.split(":")[0]
                url = f"http://{ip}:{WEB_PORT}/api/bootstrap/add"
                
                data = json.dumps({
                    "address": self.address,
                    "pubkey": self.pubkey,
                }).encode("utf-8")

                try:
                    req = urllib.request.Request(
                        url, data=data,
                        headers={"Content-Type": "application/json",
                                 "User-Agent": "brn-miner-loop"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        body = json.loads(resp.read().decode("utf-8"))
                        
                        if body.get("ok"):
                            fila = body.get("fila_tamanho", 0)
                            desc = body.get("rede_descentralizada", False)
                            
                            if desc:
                                _log(f"[BOOTSTRAP] Rede descentralizada confirmada pelo Origem! "
                                     f"Iniciando mineração...")
                                self.chain.rede_descentralizada = True
                                try:
                                    self.chain.db.set_meta("rede_descentralizada", "1")
                                except Exception:
                                    pass
                                return
                            else:
                                _log(f"Fila atualizada pelo Origem: {fila}/20")
                except urllib.error.URLError as e:
                    _log(f"Nao consegui contactar {ip}:{WEB_PORT}: {e.reason}")
                except Exception as e:
                    _log(f"Erro HTTP em {ip}: {e}")
                    
        except Exception as e:
            _log(f"_enviar_para_fila falhou: {e}")

    def _loop(self):
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

                # ============================================================
                # FASE DE BOOTSTRAP: se não é Origin e rede não descentralizou
                # ============================================================
                if not IS_ORIGIN and not self.chain.rede_descentralizada:
                    self._enviar_para_fila()
                    _log(f"Aguardando 20 mineradores... "
                         f"({len(self.chain.fila_mineracao)}/20 na fila local)")
                    if self._stop_evt.wait(timeout=15):
                        break
                    continue

                # Se chegou aqui, o Validador está minerando OU a rede já descentralizou
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


# ============================================================
# AUTO-START
# ============================================================
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
