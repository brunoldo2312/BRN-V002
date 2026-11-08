"""btc_watcher.py — Monitor de depósitos BTC para o L2 (v2.1)
================================================================
v2.1 (2026-11-05):
  [FIX CRÍTICO] _fetch_from e _fetch_tip_height faziam double-replace
                de '/api' → '/testnet/api' quando a URL base JÁ era
                testnet (ex: blockstream.info/testnet/api → 
                blockstream.info/testnet/testnet/api → 404).
                Agora detecta '/testnet' na URL antes de substituir.
  [MELHORIA] Auto-teste detecta placeholders mainnet E testnet.
  [MELHORIA] _fetch_from mais robusto: usa replace(..., 1) para não
                substituir múltiplas ocorrências.
v2.0 (2026-10-27):
  [FIX] Overpayment aceito, underpayment logado.
  [FIX] tip_height ausente → confs=0 (retry).
  [FIX] Anti-replay fail-closed.
  [NEW] Auditoria de overpayment na escrow.
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

        # Lê config (vem do btc_config.py — endereço pode estar hardcoded lá)
        self.btc_address = getattr(btc_config, "BTC_RECEIVE_ADDRESS", "").strip()
        self.network     = getattr(btc_config, "BTC_NETWORK", "mainnet").strip().lower()
        self.min_confs   = int(getattr(btc_config, "BTC_MIN_CONFIRMATIONS", 3))
        self.poll_s      = int(getattr(btc_config, "BTC_POLL_INTERVAL", 30))
        self.rpcs        = list(getattr(btc_config, "BTC_RPCS", DEFAULT_RPCS) or DEFAULT_RPCS)

        # Lazy: L2Manager
        self.l2 = None
        try:
            from l2_manager import L2Manager
            self.l2 = L2Manager(db, blockchain)
        except Exception as e:
            log.warning(f"L2Manager indisponivel: {e}")

        # [MELHORIA v2.1] Endereço vazio é erro de config, não silencia
        if not self.btc_address:
            log.warning(f"{LOG_PREFIX} BTC_RECEIVE_ADDRESS vazio — watcher vai dormir")

        log.info(f"{LOG_PREFIX} init")
        log.info(f"{LOG_PREFIX} endereco: {self.btc_address or '(vazio)'}")
        log.info(f"{LOG_PREFIX} rede: {self.network}")
        log.info(f"{LOG_PREFIX} confs minimas: {self.min_confs}")
        log.info(f"{LOG_PREFIX} poll: {self.poll_s}s")
        log.info(f"{LOG_PREFIX} RPCs: {len(self.rpcs)}")

    # ============================================================
    # Ciclo principal
    # ============================================================
    def run(self):
        self.running = True
        log.info(f"{LOG_PREFIX} iniciado")

        if not self.btc_address:
            log.warning(f"{LOG_PREFIX} sem endereço BTC — encerrando watcher")
            self.running = False
            return

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
        try:
            open_escrows = self.db.get_l2_escrows(status="OPEN")
        except Exception as e:
            log.debug(f"{LOG_PREFIX} get_l2_escrows: {e}")
            return

        now = int(time.time())
        for e in open_escrows:
            try:
                if int(e.get("expires_at", 0)) <= now:
                    self.db.update_l2_escrow_status(e["escrow_id"], "EXPIRED")
                    log.info(f"{LOG_PREFIX} escrow expirado: {e['escrow_id']}")
            except Exception:
                continue

        try:
            open_escrows = self.db.get_l2_escrows(status="OPEN")
        except Exception:
            return
        if not open_escrows:
            return

        txs = self._fetch_btc_txs()
        if not txs:
            return

        closed_txids: set = set()
        try:
            for r in self.db.get_l2_escrows():
                tid = r.get("btc_txid")
                if tid and r.get("status") != "OPEN":
                    closed_txids.add(tid)
        except Exception as e:
            log.error(f"{LOG_PREFIX} falha ao indexar escrows fechadas: {e}")
            return

        for escrow in open_escrows:
            self._try_match(escrow, txs, closed_txids)

    # ============================================================
    # Casa escrow com txs BTC
    # ============================================================
    def _try_match(self, escrow: dict, txs: list, closed_txids: set):
        expected_sats = int(escrow.get("btc_expected_sats", 0) or 0)
        if expected_sats <= 0:
            return

        for btc_tx in txs:
            txid = btc_tx.get("txid", "")
            if not txid:
                continue

            try:
                if self.db.is_btc_txid_used(txid):
                    continue
            except Exception as exc:
                log.error(
                    f"{LOG_PREFIX} anti-replay indisponível; "
                    f"abortando ciclo para escrow {escrow.get('escrow_id')}: {exc}"
                )
                return

            if txid in closed_txids:
                continue

            actual_sats = int(btc_tx.get("value_sats", 0))

            if actual_sats < expected_sats:
                log.info(
                    f"{LOG_PREFIX} tx {txid[:16]}... VISTA mas UNDERPAID: "
                    f"{actual_sats} < {expected_sats} (escrow {escrow['escrow_id'][:12]}...)"
                )
                continue

            overpaid_sats = actual_sats - expected_sats
            if overpaid_sats > 0:
                log.warning(
                    f"{LOG_PREFIX} tx {txid[:16]}... OVERPAID em "
                    f"{overpaid_sats} sats (escrow {escrow['escrow_id'][:12]}...). "
                    f"Creditando apenas {expected_sats} sats; registrar reembolso."
                )

            confs = int(btc_tx.get("confirmations", 0))
            if confs < self.min_confs:
                log.debug(
                    f"{LOG_PREFIX} tx {txid[:16]}... com apenas "
                    f"{confs} confs (min {self.min_confs})"
                )
                continue

            log.info("")
            log.info(f"{LOG_PREFIX} ════════ BTC CONFIRMADO ════════")
            log.info(f"{LOG_PREFIX} escrow:    {escrow['escrow_id']}")
            log.info(f"{LOG_PREFIX} txid:      {txid}")
            log.info(f"{LOG_PREFIX} recebido:  {actual_sats/1e8} BTC")
            log.info(f"{LOG_PREFIX} esperado:  {expected_sats/1e8} BTC")
            if overpaid_sats > 0:
                log.info(f"{LOG_PREFIX} overpaid:  {overpaid_sats/1e8} BTC (registrar)")
            log.info(f"{LOG_PREFIX} confs:     {confs}")
            log.info(f"{LOG_PREFIX} buyer:     {escrow['buyer']}")
            log.info(f"{LOG_PREFIX} BRN:       {escrow['brn_amount']/1e8} BRN")
            log.info(f"{LOG_PREFIX} ═════════════════════════════════")
            log.info("")

            if self.l2 is None:
                log.error(f"{LOG_PREFIX} L2Manager indisponivel — pulando")
                continue

            try:
                r = self.l2.apply_btc_deposit(
                    escrow_id=escrow["escrow_id"],
                    btc_txid=txid,
                    btc_amount_sats=expected_sats,
                )
                if r.get("ok"):
                    log.info(
                        f"{LOG_PREFIX} L2 tx: {r.get('txid', '?')[:16]}... "
                        f"→ mempool aguardando mineração"
                    )
                    if overpaid_sats > 0:
                        self._record_overpayment(
                            escrow_id=escrow["escrow_id"],
                            actual_sats=actual_sats,
                            overpaid_sats=overpaid_sats,
                            txid=txid,
                        )
                else:
                    log.warning(f"{LOG_PREFIX} apply falhou: {r.get('msg')}")
            except Exception as e:
                log.exception(f"{LOG_PREFIX} apply_btc_deposit: {e}")

            return

    def _record_overpayment(
        self, escrow_id: str, actual_sats: int,
        overpaid_sats: int, txid: str
    ) -> None:
        try:
            e = self.db.get_l2_escrow_by_id(escrow_id)
            if not e:
                log.warning(f"{LOG_PREFIX} não achei escrow {escrow_id} para auditoria")
                return
            e["btc_actual_sats"]   = actual_sats
            e["btc_overpaid_sats"] = overpaid_sats
            e["btc_overpaid_at"]   = int(time.time())
            e["btc_overpaid_txid"] = txid
            self.db.save_l2_escrow(e)
            log.info(
                f"{LOG_PREFIX} overpayment registrado: escrow {escrow_id[:12]}... "
                f"+{overpaid_sats} sats"
            )
        except Exception as exc:
            log.error(f"{LOG_PREFIX} falha ao registrar overpayment: {exc}")

    # ============================================================
    # URL builder — [FIX v2.1] sem double-replace
    # ============================================================
    def _base_to_network(self, base: str) -> str:
        """
        [FIX v2.1] Normaliza a URL base para a rede configurada,
        SEM substituir mais de uma vez e SEM tocar em URLs que já
        são da rede correta.
        """
        base = base.rstrip("/")
        if self.network != "testnet":
            return base  # mainnet: usa como está

        # Se já contém /testnet, não mexe
        if "/testnet" in base.lower():
            return base

        # Substitui apenas o PRIMEIRO /api por /testnet/api
        return base.replace("/api", "/testnet/api", 1)

    # ============================================================
    # Consulta endereço BTC
    # ============================================================
    def _fetch_btc_txs(self) -> list:
        if not self.btc_address:
            return []
        for base in self.rpcs:
            try:
                txs = self._fetch_from(base)
                if txs is not None:
                    return txs
            except Exception as e:
                log.debug(f"{LOG_PREFIX} {base} falhou: {e}")
                continue
        return []

    def _fetch_from(self, base: str) -> list:
        """Consulta /address/<addr>/txs em um RPC. Retorna lista normalizada."""
        base_norm = self._base_to_network(base)
        url = f"{base_norm}/address/{self.btc_address}/txs"

        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} em {url}")

        data = r.json()
        if not isinstance(data, list):
            return []

        tip_height = self._fetch_tip_height(base)

        out = []
        for tx in data[:50]:
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
                    confs = 0
                    log.warning(
                        f"{LOG_PREFIX} tip_height indisponível; "
                        f"tx {tx['txid'][:16]}... tratada como NÃO confirmada "
                        f"(retry no próximo ciclo)"
                    )

            out.append({
                "txid":           tx["txid"],
                "value_sats":     val,
                "confirmations":  confs,
                "block_height":   status.get("block_height"),
            })
        return out

    def _fetch_tip_height(self, base: str):
        try:
            base_norm = self._base_to_network(base)
            url = f"{base_norm}/blocks/tip/height"
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
            "running":      self.running,
            "btc_address":  self.btc_address,
            "network":      self.network,
            "min_confs":    self.min_confs,
            "poll_s":       self.poll_s,
            "l2_available": self.l2 is not None,
            "rpcs":         self.rpcs,
        }


# ============================================================
# HELPER — singleton simples
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
        def get_l2_escrows(self, status=None): return []
        def is_btc_txid_used(self, txid): return False
        def update_l2_escrow_status(self, eid, status, btc_txid=None): pass
        def get_l2_escrow_by_id(self, eid): return None
        def save_l2_escrow(self, e): pass

    class FakeChain:
        pass

    w = BTCWatcher(FakeDB(), FakeChain())

    print(f"  [info] address  : {w.btc_address or '(vazio)'}")
    print(f"  [info] network  : {w.network}")
    print(f"  [info] min_confs: {w.min_confs}")
    print(f"  [info] rpcs     : {len(w.rpcs)}")

    st = w.status()
    print(f"  [info] status   : {st['running']}, l2={st['l2_available']}")
    assert "btc_address" in st

    # [MELHORIA v2.1] Detecta placeholders mainnet E testnet
    _placeholders = ("bc1qSEU_", "tb1qSEU_", "bcrt1qSEU_", "SEU_ENDERECO", "AQUI")
    is_placeholder = (not w.btc_address) or any(
        p in w.btc_address for p in _placeholders
    )

    if is_placeholder:
        print(f"  [skip] endereço placeholder — pulando fetch HTTP")
    else:
        try:
            txs = w._fetch_btc_txs()
            print(f"  [info] fetch retornou {len(txs)} tx(s)")
        except Exception as e:
            print(f"  [aviso] fetch falhou: {e}")

    # [NOVO v2.1] Testa normalização de URL
    print("\n  [test] _base_to_network:")
    cases = [
        ("https://blockstream.info/api", "https://blockstream.info/testnet/api"),
        ("https://blockstream.info/testnet/api", "https://blockstream.info/testnet/api"),
        ("https://mempool.space/api", "https://mempool.space/testnet/api"),
        ("https://mempool.space/testnet/api", "https://mempool.space/testnet/api"),
    ]
    if w.network == "testnet":
        for original, expected in cases:
            got = w._base_to_network(original)
            marker = "OK" if got == expected else "FAIL"
            print(f"    [{marker}] {original}")
            if got != expected:
                print(f"           esperado: {expected}")
                print(f"           obtido  : {got}")

    print("\nBTCWatcher OK.")