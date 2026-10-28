"""offramp_manager.py — Gerencia ordens BRN → BTC (OFFRAMP)
================================================================
v1.1 (2026-10-28):
  [FIX] _check_confirmation: import requests movido para o topo.
        Antes estourava NameError no primeiro ciclo de confirmação.
  [FIX] _process_pending: try/except por escrow individual — um escrow
        com dados corrompidos não trava a fila inteira.
  [FIX] list_orders retorna cópias (dict(e)) em vez de referências
        mutáveis — evitar mutação acidental pelo caller.
  [NEW] Retry logic no envio BTC (3 tentativas com backoff curto).
  [NEW] Rate-limit com limpeza de memória (dict _rate_last não cresce
        indefinidamente).
  [NEW] status_desc amigável nos escrows retornados.
  [KEEP] Fluxo: OPEN → BRN_LOCKED → BTC_SENT → RELEASED
                        ↘ FAILED / EXPIRED / CANCELLED
  [KEEP] WIF nunca logada. Tx assinada em memória.
================================================================
"""
from __future__ import annotations

import time
import uuid
import threading
import logging

import requests

log = logging.getLogger("offramp")

# Limites de taxa BTC (dust threshold Bitcoin é 546 sats)
DUST_SATS = 546

# Rate-limit default: 1 saque a cada 60s por endereço
DEFAULT_RATE_S = 60

# TTL do rate-limit (limpa entradas antigas do _rate_last)
RATE_ENTRY_TTL_S = 3600

# Retry do broadcast BTC
BTC_SEND_RETRIES = 3
BTC_SEND_RETRY_DELAY_S = 3.0

# Descrições amigáveis de status
STATUS_DESCRIPTIONS = {
    "OPEN":         "Aguardando bloqueio de BRN",
    "BRN_LOCKED":   "BRN bloqueado — enviando BTC",
    "BTC_SENT":     "BTC enviado — aguardando confirmação",
    "RELEASED":     "Concluído — BTC confirmado",
    "FAILED":       "Falhou — BRN será reembolsado",
    "EXPIRED":      "Expirado (24h sem ação)",
    "CANCELLED":    "Cancelado pelo usuário",
}


class OfframpError(Exception):
    """Erro de validação ou regra de negócio do OFFRAMP."""
    pass


class OfframpManager:
    """
    Gerencia o ciclo de vida completo de ordens BRN → BTC.

    Estados:
        OPEN         — criado, aguardando o bloqueio de BRN (server faz isso)
        BRN_LOCKED   — BRN enviado para o cofre BRN, pronto p/ envio BTC
        BTC_SENT     — BTC broadcast feito, aguardando confirmação
        RELEASED     — BTC confirmado on-chain (ordem finalizada)
        FAILED       — erro no envio BTC (BRN deve ser reembolsado manualmente)
        EXPIRED      — TTL expirou antes do envio de BTC
        CANCELLED    — usuário cancelou antes do lock BRN

    Worker (thread) processa BRN_LOCKED → BTC_SENT → RELEASED automaticamente.
    """

    def __init__(self, db, blockchain, btc_sender,
                 rate_brn_per_btc: float, fee_pct: float,
                 min_brn_sats: int, max_brn_sats: int,
                 min_btc_sats: int = 5000,
                 ttl_s: int = 1800,
                 treasury_brn: str = "",
                 poll_interval_s: float = 10.0):
        if btc_sender is None:
            raise OfframpError("BTCSender obrigatório (WIF ausente?)")
        if db is None:
            raise OfframpError("db obrigatório")
        if blockchain is None:
            raise OfframpError("blockchain obrigatório")

        self.db = db
        self.bc = blockchain
        self.sender = btc_sender
        self.rate = float(rate_brn_per_btc)
        self.fee_pct = float(fee_pct)
        self.min_brn_sats = int(min_brn_sats)
        self.max_brn_sats = int(max_brn_sats)
        self.min_btc_sats = int(min_btc_sats)
        self.ttl_s = int(ttl_s)
        self.treasury_brn = (treasury_brn or "").strip()
        self.poll_interval_s = max(1.0, float(poll_interval_s))

        self._lock = threading.RLock()
        self._rate_last: dict = {}     # buyer → ts do último create
        self._stop = threading.Event()
        self._thread = None

        log.info(
            f"[OFFRAMP] init: rate={self.rate} fee={self.fee_pct}% "
            f"min={self.min_brn_sats/1e8:.8f}BRN max={self.max_brn_sats/1e8:.8f}BRN "
            f"minBTC={self.min_btc_sats}sats "
            f"cofreBTC={self.sender.get_address()[:16]}… "
            f"cofreBRN={self.treasury_brn[:16] if self.treasury_brn else '(vazio)'}…"
        )

    # ============================================================
    # CICLO DE VIDA
    # ============================================================
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="Offramp",
        )
        self._thread.start()
        log.info(f"[OFFRAMP] worker iniciado (poll={self.poll_interval_s}s)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        log.info("[OFFRAMP] worker parado")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._process_pending()
            except Exception as e:
                log.warning(f"[OFFRAMP] loop: {e}")
            if self._stop.wait(self.poll_interval_s):
                break

    def _process_pending(self) -> None:
        # Limpa _rate_last antigo (evita memory leak)
        now = int(time.time())
        with self._lock:
            for addr in list(self._rate_last.keys()):
                if now - self._rate_last[addr] > RATE_ENTRY_TTL_S:
                    del self._rate_last[addr]

        try:
            all_escrows = self.db.get_l2_escrows()
        except Exception as e:
            log.debug(f"[OFFRAMP] get_l2_escrows: {e}")
            return

        for e in all_escrows:
            try:
                if not isinstance(e, dict):
                    continue
                if e.get("direction") != "BRN_TO_BTC":
                    continue
                status = e.get("status")
                if status == "BRN_LOCKED":
                    self._send_btc(e)
                elif status == "BTC_SENT":
                    self._check_confirmation(e)
                elif status == "OPEN":
                    if int(e.get("expires_at", 0)) < now:
                        self._mark(e["escrow_id"], "EXPIRED", "ttl")
            except Exception as exc:
                log.warning(
                    f"[OFFRAMP] erro processando escrow "
                    f"{e.get('escrow_id', '?')}: {exc}"
                )

    # ============================================================
    # ENVIO DE BTC
    # ============================================================
    def _send_btc(self, escrow: dict) -> None:
        with self._lock:
            fresh = self.db.get_l2_escrow_by_id(escrow["escrow_id"])
            if not fresh or fresh.get("status") != "BRN_LOCKED":
                return

            brn_amount = int(fresh.get("brn_amount") or 0)
            if brn_amount <= 0:
                self._mark(fresh["escrow_id"], "FAILED", "brn_amount<=0")
                return

            gross_btc = brn_amount / self.rate
            fee_btc = gross_btc * self.fee_pct / 100.0
            net_btc = gross_btc - fee_btc
            net_sats = int(net_btc * 1e8)

            if net_sats < self.min_btc_sats:
                self._mark(
                    fresh["escrow_id"], "FAILED",
                    f"net {net_sats} < min {self.min_btc_sats}",
                )
                return

            dest = fresh.get("btc_dest", "")
            if not dest:
                self._mark(fresh["escrow_id"], "FAILED", "btc_dest vazio")
                return

            # Retry do broadcast (até 3 tentativas)
            last_err = ""
            result = None
            for attempt in range(1, BTC_SEND_RETRIES + 1):
                try:
                    r = self.sender.send(dest, net_sats, priority="halfHourFee")
                except Exception as e:
                    last_err = str(e)
                    log.warning(
                        f"[OFFRAMP] send tentativa {attempt}/{BTC_SEND_RETRIES} "
                        f"falhou: {e}"
                    )
                    if attempt < BTC_SEND_RETRIES:
                        time.sleep(BTC_SEND_RETRY_DELAY_S)
                    continue

                if r.get("ok"):
                    result = r
                    break
                last_err = r.get("msg", "broadcast falhou")
                log.warning(
                    f"[OFFRAMP] send tentativa {attempt}/{BTC_SEND_RETRIES} "
                    f"rejeitado: {last_err}"
                )
                if attempt < BTC_SEND_RETRIES:
                    time.sleep(BTC_SEND_RETRY_DELAY_S)

            if result is None:
                self._mark(fresh["escrow_id"], "FAILED", last_err or "send falhou")
                return

            fresh["status"] = "BTC_SENT"
            fresh["btc_txid"] = result["txid"]
            fresh["btc_sent_sats"] = net_sats
            fresh["btc_fee_sats"] = int(result.get("fee_sats") or 0)
            fresh["btc_sent_at"] = int(time.time())
            self.db.save_l2_escrow(fresh)
            log.info(
                f"[OFFRAMP] OK escrow={fresh['escrow_id']} "
                f"net={net_sats} sats txid={result['txid'][:16]}…"
            )

    def _check_confirmation(self, escrow: dict) -> None:
        txid = escrow.get("btc_txid", "")
        if not txid:
            return
        try:
            base = self.sender._api_candidates[0]
            r = requests.get(f"{base}/tx/{txid}/status", timeout=10)
            if r.status_code != 200:
                return
            st = r.json()
            if st.get("confirmed"):
                self._mark(escrow["escrow_id"], "RELEASED", "confirmed")
        except Exception as e:
            log.debug(f"[OFFRAMP] check_confirmation {txid[:16]}: {e}")

    def _mark(self, escrow_id: str, status: str, reason: str = "") -> None:
        try:
            e = self.db.get_l2_escrow_by_id(escrow_id)
            if not e:
                return
            e["status"] = status
            if reason:
                e["status_reason"] = reason
            e["updated_at"] = int(time.time())
            self.db.save_l2_escrow(e)
            log.info(f"[OFFRAMP] {escrow_id[:16]}… -> {status} ({reason})")
        except Exception as exc:
            log.warning(f"[OFFRAMP] _mark falhou: {exc}")

    # ============================================================
    # API PÚBLICA — COTAÇÃO
    # ============================================================
    def quote(self, brn_amount_sats: int) -> dict:
        if not isinstance(brn_amount_sats, int) or brn_amount_sats <= 0:
            raise OfframpError("brn_amount_sats deve ser int > 0")

        gross_btc = brn_amount_sats / self.rate
        fee_btc = gross_btc * self.fee_pct / 100.0
        net_btc = gross_btc - fee_btc
        net_sats = int(net_btc * 1e8)

        return {
            "brn_amount":    brn_amount_sats / 1e8,
            "brn_sats":      brn_amount_sats,
            "btc_gross":     round(gross_btc, 8),
            "fee_percent":   self.fee_pct,
            "btc_fee":       round(fee_btc, 8),
            "btc_net":       round(net_btc, 8),
            "btc_net_sats":  net_sats,
            "rate":          f"1 BTC = {self.rate} BRN",
            "min_brn":       self.min_brn_sats / 1e8,
            "max_brn":       self.max_brn_sats / 1e8,
            "min_btc_sats":  self.min_btc_sats,
            "cofre_btc":     self.sender.get_address(),
            "cofre_brn":     self.treasury_brn,
            "network":       self.sender.network,
        }

    # ============================================================
    # API PÚBLICA — CRIAÇÃO E CONTROLE
    # ============================================================
    def create_escrow(self, buyer_brn: str, brn_amount_sats: int,
                      btc_dest: str) -> dict:
        """Cria escrow OPEN. NÃO move BRN ainda (feito pelo server)."""
        if not buyer_brn or not buyer_brn.startswith("brn1"):
            return {"ok": False, "msg": "buyer_brn invalido"}
        if not isinstance(brn_amount_sats, int) or brn_amount_sats <= 0:
            return {"ok": False, "msg": "brn_amount_sats deve ser int > 0"}
        if brn_amount_sats < self.min_brn_sats:
            return {"ok": False,
                    "msg": f"abaixo do minimo ({self.min_brn_sats/1e8:.8f} BRN)"}
        if brn_amount_sats > self.max_brn_sats:
            return {"ok": False,
                    "msg": f"acima do maximo ({self.max_brn_sats/1e8:.8f} BRN)"}
        if not btc_dest or not isinstance(btc_dest, str):
            return {"ok": False, "msg": "btc_dest vazio"}

        # Rate-limit
        now = int(time.time())
        with self._lock:
            last = self._rate_last.get(buyer_brn, 0)
            if now - last < DEFAULT_RATE_S:
                faltam = DEFAULT_RATE_S - (now - last)
                return {"ok": False,
                        "msg": f"aguarde {faltam}s entre saques"}

        # Valida endereço BTC para a rede do cofre
        try:
            from bitcoinlib.keys import Address
            addr = Address(btc_dest, network=self.sender.network)
            btc_dest_clean = addr.address
        except Exception as e:
            return {"ok": False, "msg": f"endereco BTC invalido: {e}"}

        # Calcula net BTC
        gross_btc = brn_amount_sats / self.rate
        fee_btc = gross_btc * self.fee_pct / 100.0
        net_btc = gross_btc - fee_btc
        net_sats = int(net_btc * 1e8)
        if net_sats < self.min_btc_sats:
            return {"ok": False,
                    "msg": f"net BTC {net_sats} abaixo do minimo {self.min_btc_sats}"}
        if net_sats < DUST_SATS:
            return {"ok": False,
                    "msg": f"net BTC {net_sats} < dust threshold {DUST_SATS}"}

        escrow_id = "off_" + uuid.uuid4().hex[:16]
        now = int(time.time())
        escrow = {
            "escrow_id":         escrow_id,
            "direction":         "BRN_TO_BTC",
            "creator":           buyer_brn,
            "buyer":             buyer_brn,
            "brn_amount":        brn_amount_sats,
            "btc_dest":          btc_dest_clean,
            "btc_expected_sats": net_sats,
            "btc_address":       self.sender.get_address(),
            "btc_txid":          "",
            "status":            "OPEN",
            "created_at":        now,
            "expires_at":        now + self.ttl_s,
            "l2_hash":           "",
        }
        self.db.save_l2_escrow(escrow)

        with self._lock:
            self._rate_last[buyer_brn] = now

        return {"ok": True, "escrow": escrow}

    def mark_brn_locked(self, escrow_id: str, brn_txid: str) -> dict:
        """Chamado pelo server após a tx BRN ser aceita na mempool."""
        if not brn_txid or len(brn_txid) != 64:
            return {"ok": False, "msg": "brn_txid invalido"}
        with self._lock:
            e = self.db.get_l2_escrow_by_id(escrow_id)
            if not e:
                return {"ok": False, "msg": "escrow nao existe"}
            if e.get("direction") != "BRN_TO_BTC":
                return {"ok": False, "msg": "escrow nao e BRN_TO_BTC"}
            if e.get("status") != "OPEN":
                return {"ok": False, "msg": f"status {e.get('status')}"}
            e["status"] = "BRN_LOCKED"
            e["brn_txid"] = brn_txid
            e["brn_locked_at"] = int(time.time())
            self.db.save_l2_escrow(e)
            return {"ok": True}

    def cancel(self, escrow_id: str) -> dict:
        with self._lock:
            e = self.db.get_l2_escrow_by_id(escrow_id)
            if not e or e.get("direction") != "BRN_TO_BTC":
                return {"ok": False, "msg": "escrow nao existe"}
            if e.get("status") != "OPEN":
                return {"ok": False,
                        "msg": f"cancelamento so em OPEN (status {e.get('status')})"}
            e["status"] = "CANCELLED"
            e["updated_at"] = int(time.time())
            self.db.save_l2_escrow(e)
            return {"ok": True}

    # ============================================================
    # CONSULTAS
    # ============================================================
    def list_orders(self, buyer: str = None, status: str = None) -> list:
        try:
            all_escrows = self.db.get_l2_escrows()
        except Exception:
            return []
        out = []
        for e in all_escrows:
            if not isinstance(e, dict):
                continue
            if e.get("direction") != "BRN_TO_BTC":
                continue
            if buyer and e.get("buyer") != buyer:
                continue
            if status and e.get("status") != status:
                continue
            # Retorna cópia + status_desc amigável
            item = dict(e)
            item["status_desc"] = STATUS_DESCRIPTIONS.get(
                e.get("status", ""), e.get("status", "")
            )
            out.append(item)
        return out

    def stats(self) -> dict:
        orders = self.list_orders()
        out = {
            "total":        len(orders),
            "open":         0,
            "brn_locked":   0,
            "btc_sent":     0,
            "released":     0,
            "failed":       0,
            "expired":      0,
            "cancelled":    0,
            "cofre_btc":    self.sender.get_address(),
            "cofre_btc_sats": 0,
            "cofre_brn":    self.treasury_brn,
            "network":      self.sender.network,
            "rate":         f"1 BTC = {self.rate} BRN",
            "fee_percent":  self.fee_pct,
        }
        for o in orders:
            st = (o.get("status") or "").lower()
            if st in out:
                out[st] += 1
        try:
            out["cofre_btc_sats"] = self.sender.get_balance()
        except Exception:
            pass
        return out


# ============================================================
# AUTO-TESTE (sem rede, sem DB real)
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("Testando OfframpManager (dry-run, sem envio BTC)...")

    class FakeDB:
        def __init__(self):
            self.rows = {}
        def get_l2_escrows(self, status=None):
            vals = list(self.rows.values())
            if status:
                return [r for r in vals if r.get("status") == status]
            return vals
        def get_l2_escrow_by_id(self, eid):
            return self.rows.get(eid)
        def save_l2_escrow(self, d):
            self.rows[d["escrow_id"]] = dict(d)

    class FakeSender:
        network = "bitcoin"
        _api_candidates = ["https://mempool.space/api"]
        def get_address(self):
            return "bc1qfakecofre0000000000000000000000000000"
        def get_balance(self):
            return 1_000_000  # 0.01 BTC
        def send(self, dest, amount_sats, priority="halfHourFee"):
            return {
                "ok": True,
                "txid": "f" * 64,
                "fee_sats": 150,
                "from": self.get_address(),
                "to": dest,
                "amount_sats": amount_sats,
            }

    db = FakeDB()
    sender = FakeSender()

    off = OfframpManager(
        db=db,
        blockchain=object(),  # não usado no dry-run
        btc_sender=sender,
        rate_brn_per_btc=10000,
        fee_pct=2.0,
        min_brn_sats=1_000_000,      # 0.01 BRN
        max_brn_sats=100_000_000_000, # 1000 BRN
        min_btc_sats=5000,
        ttl_s=1800,
        treasury_brn="brn1faketreasury000000000000000000000",
    )

    # 1) Quote
    q = off.quote(10_000_000)  # 0.1 BRN
    print(f"  [info] quote: {q['brn_amount']} BRN -> {q['btc_net']} BTC "
          f"(fee {q['fee_percent']}%)")
    assert q["btc_net_sats"] > 0

    # 2) create_escrow
    r = off.create_escrow(
        "brn1fakebuyer000000000000000000000000000",
        10_000_000,  # 0.1 BRN
        "bc1qfakedest00000000000000000000000000000",
    )
    assert r["ok"], f"create_escrow falhou: {r}"
    escrow_id = r["escrow"]["escrow_id"]
    print(f"  [ok] create_escrow: {escrow_id}")

    # 3) marca BRN_LOCKED
    r2 = off.mark_brn_locked(escrow_id, "a" * 64)
    assert r2["ok"], f"mark_brn_locked falhou: {r2}"
    print("  [ok] mark_brn_locked")

    # 4) simula worker processando (envia BTC)
    off._process_pending()
    e = db.get_l2_escrow_by_id(escrow_id)
    assert e["status"] == "BTC_SENT", f"esperado BTC_SENT, veio {e['status']}"
    assert e["btc_txid"] == "f" * 64
    print(f"  [ok] _send_btc -> BTC_SENT (txid={e['btc_txid'][:16]}…)")

    # 5) rate-limit deve bloquear 2o create do mesmo buyer
    r3 = off.create_escrow(
        "brn1fakebuyer000000000000000000000000000",
        10_000_000,
        "bc1qfakedest00000000000000000000000000000",
    )
    assert not r3["ok"], "rate-limit deveria bloquear"
    print(f"  [ok] rate-limit: {r3['msg']}")

    # 6) list_orders
    orders = off.list_orders()
    assert len(orders) == 1
    assert orders[0]["status_desc"] == "BTC enviado — aguardando confirmação"
    print(f"  [ok] list_orders: {len(orders)} ordem, desc='{orders[0]['status_desc']}'")

    # 7) stats
    st = off.stats()
    assert st["total"] == 1
    assert st["btc_sent"] == 1
    print(f"  [ok] stats: {st}")

    print()
    print("=" * 60)
    print(" OfframpManager v1.1 OK.")
    print("=" * 60)
