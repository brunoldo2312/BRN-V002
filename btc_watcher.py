"""btc_watcher.py — Monitor de depósitos BTC para o L2 (v2.0)
================================================================
v2.0 (2026-10-27):
  [FIX CRÍTICO] Overpayment agora é ACEITO. Antes, exigir valor
                exato (`value != expected`) travava os fundos do
                usuário que enviou a mais, sem crédito BRN e sem
                reconciliação. Agora: value >= expected → credita
                expected_sats em BRN, registra btc_actual_sats e
                btc_overpaid_sats na escrow para reembolso manual.
  [FIX CRÍTICO] tip_height ausente → confs=0 (retry no próximo ciclo).
                Antes usava confs=min_confs, assumindo confirmações
                que podiam não existir. Isso podia creditar BRN para
                tx ainda não suficientemente confirmada.
  [FIX] Verificação defensiva "txid em outra escrow" agora usa set
        pré-computado por ciclo (era full scan por par escrow×tx).
  [FIX] apply_btc_deposit recebe btc_amount_sats=expected_sats
        (valor combinado), não o real. Evita crédito inflado.
  [NEW] Log explícito de underpayment (antes era silencioso).
  [NEW] Escrow overpaid recebe btc_actual_sats, btc_overpaid_sats,
        btc_overpaid_at — visível em /api/l2/escrows para reconciliação.
  [KEEP] Anti-replay fail-closed (return em erro de DB).
  [KEEP] RPCs em ordem de fallback (blockstream → mempool.space).
  [KEEP] API: BTCWatcher(db, blockchain).start() / .stop() / .status()
v1.0:
  - Thread daemon que monitora endereço BTC, detecta depósitos com
    >= BTC_MIN_CONFIRMATIONS, casa com escrows OPEN por valor, e
    libera BRN via L2Manager.apply_btc_deposit().
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
        # 1. Expira escrows vencidos
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

        # 2. Recarrega só os OPEN
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

        # 4. [NEW v2.0] Pré-computa txids já usados em outras escrows.
        # Antes era um full scan por (escrow, tx). Agora 1 query por ciclo.
        closed_txids: set = set()
        try:
            for r in self.db.get_l2_escrows():
                tid = r.get("btc_txid")
                if tid and r.get("status") != "OPEN":
                    closed_txids.add(tid)
        except Exception as e:
            log.error(f"{LOG_PREFIX} falha ao indexar escrows fechadas: {e}")
            # fail-closed: sem índice, não casa nada neste ciclo
            return

        # 5. Match
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

            # Anti-replay: já processado por nós?
            try:
                if self.db.is_btc_txid_used(txid):
                    continue
            except Exception as exc:
                # Fail-closed: sem anti-replay não processa depósito.
                log.error(
                    f"{LOG_PREFIX} anti-replay indisponível; "
                    f"abortando ciclo para escrow {escrow.get('escrow_id')}: {exc}"
                )
                return

            # Defesa adicional (agora com set pré-computado)
            if txid in closed_txids:
                continue

            actual_sats = int(btc_tx.get("value_sats", 0))

            # [FIX v2.0] UNDERPAYMENT: rejeita com log claro
            if actual_sats < expected_sats:
                log.info(
                    f"{LOG_PREFIX} tx {txid[:16]}... VISTA mas UNDERPAID: "
                    f"{actual_sats} < {expected_sats} (escrow {escrow['escrow_id'][:12]}...)"
                )
                continue

            # [FIX v2.0] OVERPAYMENT: aceita, credita só o combinado
            overpaid_sats = actual_sats - expected_sats
            if overpaid_sats > 0:
                log.warning(
                    f"{LOG_PREFIX} tx {txid[:16]}... OVERPAID em "
                    f"{overpaid_sats} sats (escrow {escrow['escrow_id'][:12]}...). "
                    f"Creditando apenas {expected_sats} sats; registrar reembolso."
                )

            # Confirmações suficientes?
            confs = int(btc_tx.get("confirmations", 0))
            if confs < self.min_confs:
                log.debug(
                    f"{LOG_PREFIX} tx {txid[:16]}... com apenas "
                    f"{confs} confs (min {self.min_confs})"
                )
                continue

            # ═══════════════════════════════════════════════════
            # MATCH! Aplica depósito
            # ═══════════════════════════════════════════════════
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
                # [FIX v2.0] credita o valor combinado, não o real
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
                    # [NEW v2.0] registra auditoria de overpayment
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

            return  # só casa uma vez por escrow

    def _record_overpayment(
        self, escrow_id: str, actual_sats: int,
        overpaid_sats: int, txid: str
    ) -> None:
        """
        [NEW v2.0] Grava btc_actual_sats / btc_overpaid_sats na escrow.
        Não altera status nem créditos — só auditoria para reembolso manual.
        Chamado APÓS apply_btc_deposit OK (para não sobrescrever status).
        """
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

        # tip height para calcular confirmações
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
                    # [FIX v2.0] fail-safe: sem tip, trata como não confirmado
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
        def get_l2_escrows(self, status=None):
            return []
        def is_btc_txid_used(self, txid):
            return False
        def update_l2_escrow_status(self, eid, status, btc_txid=None):
            pass
        def get_l2_escrow_by_id(self, eid):
            return None
        def save_l2_escrow(self, e):
            pass

    class FakeChain:
        pass

    w = BTCWatcher(FakeDB(), FakeChain())

    print(f"  [info] address  : {w.btc_address}")
    print(f"  [info] network  : {w.network}")
    print(f"  [info] min_confs: {w.min_confs}")
    print(f"  [info] rpcs     : {len(w.rpcs)}")

    st = w.status()
    print(f"  [info] status   : {st['running']}, l2={st['l2_available']}")
    assert "btc_address" in st

    if not w.btc_address or w.btc_address.startswith("bc1qSEU_"):
        print("  [skip] endereco placeholder — pulando fetch")
    else:
        try:
            txs = w._fetch_btc_txs()
            print(f"  [info] fetch retornou {len(txs)} tx(s)")
        except Exception as e:
            print(f"  [aviso] fetch falhou: {e}")

    print("BTCWatcher OK.")
