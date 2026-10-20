"""btc_watcher.py — Monitor de depósitos BTC para o L2
================================================================
Thread daemon que:
  1. Consulta o endereço BTC configurado em btc_config.py
  2. Detecta depósitos com confirmações >= BTC_MIN_CONFIRMATIONS
  3. Casa com escrows OPEN (por valor)
  4. Anti-replay via db.is_btc_txid_used()
  5. Chama L2Manager.apply_btc_deposit() para liberar BRN via mineração

RPCs usados (em ordem de fallback):
  - https://blockstream.info/api
  - https://mempool.space/api

API usada por main.py v12.5:
    w = BTCWatcher(chain.db, chain)
    w.start()   # daemon thread
    w.stop()

Requer:
    btc_config.py
    l2_manager.py
    requests
================================================================
"""
from __future__ import annotations

import logging
import threading
import time

import requests

import btc_config


log = logging.getLogger("btc_watcher")


# ============================================================
# CONSTANTES
# ============================================================
DEFAULT_RPCS = [
    "https://blockstream.info/api",
    "https://mempool.space/api",
]
REQUEST_TIMEOUT = 15
LOG_PREFIX = "[BTC Watcher]"


# ============================================================
# BTCWatcher
# ============================================================
class BTCWatcher(threading.Thread):
    """Monitora o endereço BTC e libera BRN quando o depósito confirma."""

    def __init__(self, db, blockchain):
        super().__init__(daemon=True, name="BTCWatcher")
        self.db = db
        self.blockchain = blockchain

        self.running = False
        self._stop_evt = threading.Event()

        # Lê config
        self.btc_address = getattr(btc_config, "BTC_RECEIVE_ADDRESS", "")
        self.network     = getattr(btc_config, "BTC_NETWORK", "mainnet")
        self.min_confs   = int(getattr(btc_config, "BTC_MIN_CONFIRMATIONS", 3))
        self.poll_s      = int(getattr(btc_config, "BTC_POLL_INTERVAL", 30))
        self.rpcs        = list(getattr(btc_config, "BTC_RPCS", DEFAULT_RPCS))

        # Lazy: L2Manager
        self.l2 = None
        try:
            from l2_manager import L2Manager
            self.l2 = L2Manager(db, blockchain)
        except Exception as e:
            log.warning(f"L2Manager indisponivel: {e}")

        log.info(f"{LOG_PREFIX} init")
        log.info(f"{LOG_PREFIX} endereco: {self.btc_address}")
        log.info(f"{LOG_PREFIX} rede: {self.network}")
        log.info(f"{LOG_PREFIX} confs minimas: {self.min_confs}")
        log.info(f"{LOG_PREFIX} poll: {self.poll_s}s")

    # ============================================================
    # Ciclo principal
    # ============================================================
    def run(self):
        self.running = True
        log.info(f"{LOG_PREFIX} iniciado")

        # espera 5s no boot para o resto subir
        if self._stop_evt.wait(5):
            return

        while self.running and not self._stop_evt.is_set():
            try:
                self._check_once()
            except Exception as e:
                log.warning(f"{LOG_PREFIX} erro no ciclo: {e}")

            if self._stop_evt.wait(self.poll_s):
                break

        self.running = False
        log.info(f"{LOG_PREFIX} encerrado")

    def stop(self):
        self._stop_evt.set()
        if self.is_alive():
            self.join(timeout=5)
        log.info(f"{LOG_PREFIX} stop()")

    # ============================================================
    # Uma checagem
    # ============================================================
    def _check_once(self):
        # 1. Quais escrows estão abertos?
        try:
            open_escrows = self.db.get_l2_escrows(status="OPEN")
        except Exception as e:
            log.debug(f"{LOG_PREFIX} get_l2_escrows: {e}")
            return

        # 2. Primeiro expira os vencidos (mantém a lista limpa)
        now = int(time.time())
        for e in open_escrows:
            try:
                if int(e.get("expires_at", 0)) < now:
                    self.db.update_l2_escrow_status(
                        e["escrow_id"], "EXPIRED")
                    log.info(f"{LOG_PREFIX} escrow expirado: "
                             f"{e['escrow_id']}")
            except Exception:
                continue

        # Recarrega só os que ainda estão abertos
        try:
            open_escrows = self.db.get_l2_escrows(status="OPEN")
        except Exception:
            return

        if not open_escrows:
            return

        # 3. Consulta endereço BTC
        txs = self._fetch_btc_txs()
        if not txs:
            return

        # 4. Match com escrows
        for escrow in open_escrows:
            self._try_match(escrow, txs)

    # ============================================================
    # Casa escrow com txs BTC
    # ============================================================
    def _try_match(self, escrow: dict, txs: list):
        expected_sats = int(escrow.get("btc_expected_sats", 0) or 0)
        if expected_sats <= 0:
            return

        # tolerância de 1% para fee
        min_ok = int(expected_sats * 0.99)

        for btc_tx in txs:
            txid = btc_tx.get("txid", "")
            if not txid:
                continue

            # Anti-replay: essa tx já foi processada?
            try:
                if self.db.is_btc_txid_used(txid):
                    continue
            except Exception:
                pass

            # Já foi usada em outro escrow?
            try:
                rows = self.db.get_l2_escrows()
                if any(r.get("btc_txid") == txid and
                       r.get("status") != "OPEN" for r in rows):
                    continue
            except Exception:
                pass

            # Valor suficiente?
            if btc_tx["value_sats"] < min_ok:
                continue

            # Confirmações suficientes?
            if btc_tx["confirmations"] < self.min_confs:
                log.debug(
                    f"{LOG_PREFIX} tx {txid[:16]}... com apenas "
                    f"{btc_tx['confirmations']} confs "
                    f"(min {self.min_confs})")
                continue

            # ═══════════════════════════════════════════════════
            # MATCH! Aplica depósito
            # ═══════════════════════════════════════════════════
            log.info("")
            log.info(f"{LOG_PREFIX} ════════ BTC CONFIRMADO ════════")
            log.info(f"{LOG_PREFIX} escrow:  {escrow['escrow_id']}")
            log.info(f"{LOG_PREFIX} txid:    {txid}")
            log.info(f"{LOG_PREFIX} valor:   {btc_tx['value_sats']/1e8} BTC")
            log.info(f"{LOG_PREFIX} confs:   {btc_tx['confirmations']}")
            log.info(f"{LOG_PREFIX} buyer:   {escrow['buyer']}")
            log.info(f"{LOG_PREFIX} BRN:     "
                     f"{escrow['brn_amount']/1e8} BRN")
            log.info(f"{LOG_PREFIX} ═════════════════════════════════")
            log.info("")

            if self.l2 is None:
                log.error(f"{LOG_PREFIX} L2Manager indisponivel — pulando")
                continue

            try:
                r = self.l2.apply_btc_deposit(
                    escrow_id=escrow["escrow_id"],
                    btc_txid=txid,
                    btc_amount_sats=int(btc_tx["value_sats"]),
                )
                if r.get("ok"):
                    log.info(f"{LOG_PREFIX} L2 tx: {r.get('txid', '?')[:16]}..."
                             f" → mempool aguardando mineração")
                else:
                    log.warning(f"{LOG_PREFIX} apply falhou: {r.get('msg')}")
            except Exception as e:
                log.exception(f"{LOG_PREFIX} apply_btc_deposit: {e}")

            # só casa uma vez por escrow
            return

    # ============================================================
    # Consulta endereço BTC
    # ============================================================
    def _fetch_btc_txs(self) -> list:
        for base in self.rpcs:
            try:
                txs = self._fetch_from(base)
                if txs:
                    return txs
            except Exception as e:
                log.debug(f"{LOG_PREFIX} {base} falhou: {e}")
                continue
        return []

    def _fetch_from(self, base: str) -> list:
        """Consulta /address/<addr>/txs em um RPC. Retorna lista normalizada."""
        base = base.rstrip("/")
        # Ajusta testnet/mainnet
        if self.network == "testnet" and "blockstream" in base:
            url = f"{base.replace('/api', '/testnet/api')}/address/{self.btc_address}/txs"
        elif self.network == "testnet" and "mempool" in base:
            url = f"{base.replace('/api', '/testnet/api')}/address/{self.btc_address}/txs"
        else:
            url = f"{base}/address/{self.btc_address}/txs"

        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")

        data = r.json()
        if not isinstance(data, list):
            return []

        # altura atual da rede (para calcular confirmações)
        tip_height = self._fetch_tip_height(base)

        out = []
        for tx in data[:50]:
            # soma o que caiu no nosso endereço
            val = 0
            for vout in tx.get("vout", []):
                if vout.get("scriptpubkey_address") == self.btc_address:
                    val += int(vout.get("value", 0))
            if val == 0:
                continue

            status = tx.get("status", {}) or {}
            confs = 0
            if status.get("confirmed") and status.get("block_height"):
                if tip_height:
                    confs = max(0, tip_height - status["block_height"] + 1)
                else:
                    confs = self.min_confs  # fallback conservador

            out.append({
                "txid":           tx["txid"],
                "value_sats":     val,
                "confirmations":  confs,
                "block_height":   status.get("block_height"),
            })
        return out

    def _fetch_tip_height(self, base: str):
        try:
            base = base.rstrip("/")
            if self.network == "testnet" and "blockstream" in base:
                url = f"{base.replace('/api', '/testnet/api')}/blocks/tip/height"
            elif self.network == "testnet" and "mempool" in base:
                url = f"{base.replace('/api', '/testnet/api')}/blocks/tip/height"
            else:
                url = f"{base}/blocks/tip/height"
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                return int(r.text.strip())
        except Exception:
            pass
        return None

    # ============================================================
    # Status público
    # ============================================================
    def status(self) -> dict:
        return {
            "running":          self.running,
            "btc_address":      self.btc_address,
            "network":          self.network,
            "min_confs":        self.min_confs,
            "poll_s":           self.poll_s,
            "l2_available":     self.l2 is not None,
            "rpcs":             self.rpcs,
        }


# ============================================================
# HELPER — iniciar como singleton simples
# ============================================================
_watcher = None
_lock = threading.Lock()


def get_watcher(db, blockchain) -> BTCWatcher:
    global _watcher
    with _lock:
        if _watcher is None or not _watcher.is_alive():
            _watcher = BTCWatcher(db, blockchain)
        return _watcher


# ============================================================
# AUTO-TESTE (sem rede)
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("Testando BTCWatcher (modo dry-run)...")

    class FakeDB:
        def get_l2_escrows(self, status=None):
            return []
        def is_btc_txid_used(self, txid):
            return False
        def update_l2_escrow_status(self, eid, status, btc_txid=None):
            pass

    class FakeChain:
        pass

    w = BTCWatcher(FakeDB(), FakeChain())

    # 1. Config lida
    print(f"  [info] address  : {w.btc_address}")
    print(f"  [info] network  : {w.network}")
    print(f"  [info] min_confs: {w.min_confs}")
    print(f"  [info] rpcs     : {len(w.rpcs)}")

    # 2. Status
    st = w.status()
    print(f"  [info] status   : {st['running']}, l2={st['l2_available']}")
    assert "btc_address" in st

    # 3. Fetch com endereço falso (espera lista vazia ou erro tratado)
    if not w.btc_address or w.btc_address.startswith("bc1qSEU_"):
        print("  [skip] endereco placeholder — pulando fetch")
    else:
        try:
            txs = w._fetch_btc_txs()
            print(f"  [info] fetch retornou {len(txs)} tx(s)")
        except Exception as e:
            print(f"  [aviso] fetch falhou: {e}")

    print("BTCWatcher OK.")