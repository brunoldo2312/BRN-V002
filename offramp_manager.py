"""offramp_manager.py — Gerencia ordens BRN → BTC (OFFRAMP)
================================================================
v1.2 (2026-10-28):
  [FIX CRÍTICO] Fator 1e8 aplicado 2× no cálculo de sats BTC.
                brn_amount_sats / rate JÁ RETORNA SATOSHIS BTC,
                mas o código multiplicava por 1e8 de novo:
                  quote(10.000.000 BRN_sats)  # 0.1 BRN
                  → net_btc = 980  (interpretado como "980 BTC")
                  → net_sats = 980 * 1e8 = 98.000.000.000 sats
                  → valor real: 0.0000098 BTC (fator 1e8 de erro!)
                Agora usa variáveis explicitamente nomeadas
                (`*_btc_sats`) para tornar a unidade à prova de erro.
                Auto-teste foi reforçado com assertions sobre o
                valor em sats BTC (não só "não falhou").
v1.1 (2026-10-28):
  [FIX] _check_confirmation: import requests no topo.
  [FIX] _process_pending: try/except por escrow individual.
  [FIX] list_orders retorna cópia.
  [NEW] Retry de broadcast (3 tentativas).
  [NEW] Rate-limit com TTL para evitar memory leak.
================================================================
"""
from __future__ import annotations

import time
import uuid
import threading
import logging

import requests

log = logging.getLogger("offramp")

# Dust threshold Bitcoin
DUST_SATS = 546

# Rate-limit default
DEFAULT_RATE_S = 60

# TTL de entradas do rate-limit
RATE_ENTRY_TTL_S = 3600

# Retry do broadcast BTC
BTC_SEND_RETRIES = 3
BTC_SEND_RETRY_DELAY_S = 3.0

STATUS_DESCRIPTIONS = {
    "OPEN":       "Aguardando bloqueio de BRN",
    "BRN_LOCKED": "BRN bloqueado — enviando BTC",
    "BTC_SENT":   "BTC enviado — aguardando confirmação",
    "RELEASED":   "Concluído — BTC confirmado",
    "FAILED":     "Falhou — BRN será reembolsado",
    "EXPIRED":    "Expirado (TTL sem ação)",
    "CANCELLED":  "Cancelado pelo usuário",
}


class OfframpError(Exception):
    """Erro de validação ou regra de negócio do OFFRAMP."""
    pass


class OfframpManager:
    """
    Gerencia o ciclo de vida completo de ordens BRN → BTC.

    Estados:
        OPEN → BRN_LOCKED → BTC_SENT → RELEASED
                ↘ FAILED / EXPIRED / CANCELLED

    Unidades (IMPORTANTE):
        - *brn_sats*  = unidades mínimas de BRN (1 BRN = 1e8 sats)
        - *btc_sats*  = unidades mínimas de BTC (1 BTC = 1e8 sats)
        - rate        = "1 BTC = X BRN" (X inteiro, ex: 10000)

        Portanto:
            1 BRN_sat = 1/rate  BTC_sats
            N BRN_sats = N / rate  BTC_sats

        O NUNCA aplicamos 1e8 após dividir por `rate`, porque a
        divisão já entrega o valor em satoshis BTC.
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
        if rate_brn_per_btc <= 0:
            raise OfframpError(f"rate_brn_per_btc invalido: {rate_brn_per_btc}")
        if not (0 <= fee_pct < 100):
            raise OfframpError(f"fee_pct invalido: {fee_pct}")

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
        self._rate_last: dict = {}
        self._stop = threading.Event()
        self._thread = None

        log.info(
            f"[OFFRAMP] init: rate={self.rate} fee={self.fee_pct}% "
            f"minBRN={self.min_brn_sats/1e8:.8f} maxBRN={self.max_brn_sats/1e8:.8f} "
            f"minBTC={self.min_btc_sats}sats "
            f"cofreBTC={self.sender.get_address()[:16]}… "
            f"cofreBRN={self.treasury_brn[:16] if self.treasury_brn else '(vazio)'}…"
        )

    # ============================================================
    # CONVERSÃO (unidade única de verdade)
    # ============================================================
    def _brn_sats_to_btc_sats(self, brn_amount_sats: int) -> dict:
        """
        Conversão canônica BRN_sats → BTC_sats.

        Retorna dict:
            gross_btc_sats : valor bruto em satoshis BTC
            fee_btc_sats   : taxa em satoshis BTC
            net_btc_sats   : valor líquido em satoshis BTC
            net_btc        : valor líquido em BTC unidades (para display)
            fee_btc        : taxa em BTC unidades
            gross_btc      : bruto em BTC unidades

        NOTA: `brn_amount_sats / rate` JÁ RETORNA SATOSHIS BTC.
        Nunca multiplique por 1e8 aqui — use 1e8 só para converter
        sats → BTC unidades (display).
        """
        if brn_amount_sats <= 0:
            raise OfframpError("brn_amount_sats deve ser > 0")

        gross_btc_sats = brn_amount_sats / self.rate
        fee_btc_sats = gross_btc_sats * self.fee_pct / 100.0
        net_btc_sats_f = gross_btc_sats - fee_btc_sats
        net_btc_sats = int(net_btc_sats_f)

        # Arredondamento defensivo: se por algum motivo virar negativo
        # (rate/fee absurdos), força 0 e avisa.
        if net_btc_sats < 0:
            log.warning(
                f"[OFFRAMP] net_btc_sats negativo ({net_btc_sats}) — "
                f"brn={brn_amount_sats} rate={self.rate} fee={self.fee_pct}%"
            )
            net_btc_sats = 0

        return {
            "gross_btc_sats": int(gross_btc_sats),
            "fee_btc_sats":   int(fee_btc_sats),
            "net_btc_sats":   net_btc_sats,
            "gross_btc":      round(gross_btc_sats / 1e8, 8),
            "fee_btc":        round(fee_btc_sats / 1e8, 8),
            "net_btc":        round(net_btc_sats / 1e8, 8),
        }

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

            brn_amount_sats = int(fresh.get("brn_amount") or 0)
            if brn_amount_sats <= 0:
                self._mark(fresh["escrow_id"], "FAILED", "brn_amount<=0")
                return

            try:
                amounts = self._brn_sats_to_btc_sats(brn_amount_sats)
            except OfframpError as e:
                self._mark(fresh["escrow_id"], "FAILED", str(e))
                return

            net_btc_sats = amounts["net_btc_sats"]

            if net_btc_sats < self.min_btc_sats:
                self._mark(
                    fresh["escrow_id"], "FAILED",
                    f"net {net_btc_sats} sats < min {self.min_btc_sats}",
                )
                return
            if net_btc_sats < DUST_SATS:
                self._mark(
                    fresh["escrow_id"], "FAILED",
                    f"net {net_btc_sats} sats < dust {DUST_SATS}",
                )
                return

            dest = fresh.get("btc_dest", "")
            if not dest:
                self._mark(fresh["escrow_id"], "FAILED", "btc_dest vazio")
                return

            # Retry do broadcast
            last_err = ""
            result = None
            for attempt in range(1, BTC_SEND_RETRIES + 1):
                try:
                    r = self.sender.send(dest, net_btc_sats, priority="halfHourFee")
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
                self._mark(
                    fresh["escrow_id"], "FAILED",
                    last_err or "send falhou",
                )
                return

            fresh["status"] = "BTC_SENT"
            fresh["btc_txid"] = result["txid"]
            fresh["btc_sent_sats"] = net_btc_sats
            fresh["btc_fee_sats"] = int(result.get("fee_sats") or 0)
            fresh["btc_sent_at"] = int(time.time())
            self.db.save_l2_escrow(fresh)
            log.info(
                f"[OFFRAMP] OK escrow={fresh['escrow_id']} "
                f"net={net_btc_sats} sats ({net_btc_sats/1e8:.8f} BTC) "
                f"txid={result['txid'][:16]}…"
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
    # COTAÇÃO
    # ============================================================
    def quote(self, brn_amount_sats: int) -> dict:
        if not isinstance(brn_amount_sats, int) or brn_amount_sats <= 0:
            raise OfframpError("brn_amount_sats deve ser int > 0")

        amounts = self._brn_sats_to_btc_sats(brn_amount_sats)

        return {
            "brn_amount":    brn_amount_sats / 1e8,
            "brn_sats":      brn_amount_sats,
            "btc_gross":     amounts["gross_btc"],
            "fee_percent":   self.fee_pct,
            "btc_fee":       amounts["fee_btc"],
            "btc_net":       amounts["net_btc"],
            "btc_net_sats":  amounts["net_btc_sats"],
            "rate":          f"1 BTC = {self.rate} BRN",
            "min_brn":       self.min_brn_sats / 1e8,
            "max_brn":       self.max_brn_sats / 1e8,
            "min_btc_sats":  self.min_btc_sats,
            "cofre_btc":     self.sender.get_address(),
            "cofre_brn":     self.treasury_brn,
            "network":       self.sender.network,
        }

    # ============================================================
    # CRIAÇÃO E CONTROLE
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

        # Valida endereço BTC
        try:
            from bitcoinlib.keys import Address
            addr = Address(btc_dest, network=self.sender.network)
            btc_dest_clean = addr.address
        except Exception as e:
            return {"ok": False, "msg": f"endereco BTC invalido: {e}"}

        # Calcula valores em sats BTC
        try:
            amounts = self._brn_sats_to_btc_sats(brn_amount_sats)
        except OfframpError as e:
            return {"ok": False, "msg": str(e)}

        net_btc_sats = amounts["net_btc_sats"]
        if net_btc_sats < self.min_btc_sats:
            return {"ok": False,
                    "msg": (f"net BTC {net_btc_sats} sats abaixo do minimo "
                            f"{self.min_btc_sats}")}
        if net_btc_sats < DUST_SATS:
            return {"ok": False,
                    "msg": f"net BTC {net_btc_sats} sats < dust {DUST_SATS}"}

        escrow_id = "off_" + uuid.uuid4().hex[:16]
        now = int(time.time())
        escrow = {
            "escrow_id":         escrow_id,
            "direction":         "BRN_TO_BTC",
            "creator":           buyer_brn,
            "buyer":             buyer_brn,
            "brn_amount":        brn_amount_sats,
            "btc_dest":          btc_dest_clean,
            "btc_expected_sats": net_btc_sats,
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
            item = dict(e)
            item["status_desc"] = STATUS_DESCRIPTIONS.get(
                e.get("status", ""), e.get("status", "")
            )
            out.append(item)
        return out

    def stats(self) -> dict:
        orders = self.list_orders()
        out = {
            "total":          len(orders),
            "open":           0,
            "brn_locked":     0,
            "btc_sent":       0,
            "released":       0,
            "failed":         0,
            "expired":        0,
            "cancelled":      0,
            "cofre_btc":      self.sender.get_address(),
            "cofre_btc_sats": 0,
            "cofre_brn":      self.treasury_brn,
            "network":        self.sender.network,
            "rate":           f"1 BTC = {self.rate} BRN",
            "fee_percent":    self.fee_pct,
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

    print("Testando OfframpManager v1.2 (dry-run, sem envio BTC)...")

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
            # REGISTRA o que foi enviado para o teste verificar
            FakeSender.last_send = {
                "dest": dest, "amount_sats": amount_sats,
            }
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

    # rate=10000 → 1 BTC = 10.000 BRN
    # 5 BRN = 500_000_000 brn_sats → 50.000 sats BTC (0.0005 BTC)
    # 50.000 sats > min_btc_sats=5000 ✓
    off = OfframpManager(
        db=db,
        blockchain=object(),
        btc_sender=sender,
        rate_brn_per_btc=10000,
        fee_pct=2.0,
        min_brn_sats=100_000_000,       # 1 BRN
        max_brn_sats=100_000_000_000,   # 1000 BRN
        min_btc_sats=5000,
        ttl_s=1800,
        treasury_brn="brn1faketreasury000000000000000000000",
    )

    failures = []
    def check(name, cond, detail=""):
        if cond:
            print(f"  [ok]   {name}")
        else:
            print(f"  [FAIL] {name} — {detail}")
            failures.append(name)

    # ---- 1) Quote: 5 BRN deve dar 0.0005 BTC (50.000 sats) ----
    q = off.quote(500_000_000)  # 5 BRN
    check("quote.brn_amount == 5.0", q["brn_amount"] == 5.0, f"got {q['brn_amount']}")
    check("quote.btc_gross ≈ 0.0005",
          abs(q["btc_gross"] - 0.0005) < 1e-10, f"got {q['btc_gross']}")
    check("quote.btc_fee ≈ 0.00001",
          abs(q["btc_fee"] - 0.00001) < 1e-10, f"got {q['btc_fee']}")
    check("quote.btc_net ≈ 0.00049",
          abs(q["btc_net"] - 0.00049) < 1e-10, f"got {q['btc_net']}")
    check("quote.btc_net_sats == 49000",
          q["btc_net_sats"] == 49000, f"got {q['btc_net_sats']}")

    # ---- 2) Quote: 0.1 BRN é MENOS que 5000 sats → será rejeitado em create ----
    q_small = off.quote(10_000_000)  # 0.1 BRN
    check("quote(0.1 BRN).btc_net_sats == 980",
          q_small["btc_net_sats"] == 980, f"got {q_small['btc_net_sats']}")
    check("quote(0.1 BRN).btc_net < min_btc_sats",
          q_small["btc_net_sats"] < off.min_btc_sats,
          f"{q_small['btc_net_sats']} < {off.min_btc_sats}? NÃO!")

    # ---- 3) create_escrow com 0.1 BRN deve REJEITAR ----
    r_small = off.create_escrow(
        "brn1fakebuyer000000000000000000000000000",
        10_000_000,  # 0.1 BRN
        "bc1qfakedest00000000000000000000000000000",
    )
    check("create_escrow(0.1 BRN) é rejeitado",
          not r_small["ok"], f"deveria rejeitar; passou com {r_small}")

    # ---- 4) create_escrow com 5 BRN deve PASSAR ----
    r_ok = off.create_escrow(
        "brn1fakebuyer000000000000000000000000000",
        500_000_000,  # 5 BRN
        "bc1qfakedest00000000000000000000000000000",
    )
    check("create_escrow(5 BRN) é aceito",
          r_ok["ok"], f"deveria aceitar; erro={r_ok.get('msg')}")
    if r_ok["ok"]:
        escrow = r_ok["escrow"]
        check("escrow.btc_expected_sats == 49000",
              escrow["btc_expected_sats"] == 49000,
              f"got {escrow['btc_expected_sats']}")
        escrow_id = escrow["escrow_id"]

        # ---- 5) mark_brn_locked ----
        r2 = off.mark_brn_locked(escrow_id, "a" * 64)
        check("mark_brn_locked OK", r2["ok"])

        # ---- 6) _process_pending envia BTC com valor CORRETO ----
        FakeSender.last_send = None
        off._process_pending()
        e_after = db.get_l2_escrow_by_id(escrow_id)
        check("escrow virou BTC_SENT",
              e_after["status"] == "BTC_SENT",
              f"got {e_after['status']}")
        check("btc_sent_sats == 49000 (não 98 bilhões)",
              e_after.get("btc_sent_sats") == 49000,
              f"got {e_after.get('btc_sent_sats')}")
        check("sender.send chamado com amount=49000",
              FakeSender.last_send and FakeSender.last_send["amount_sats"] == 49000,
              f"got {FakeSender.last_send}")

        # ---- 7) rate-limit ----
        r3 = off.create_escrow(
            "brn1fakebuyer000000000000000000000000000",
            500_000_000,
            "bc1qfakedest00000000000000000000000000000",
        )
        check("rate-limit bloqueia 2o create",
              not r3["ok"], f"deveria bloquear; passou com {r3}")

    # ---- 8) list_orders com status_desc ----
    orders = off.list_orders()
    check("list_orders retorna 1 ordem", len(orders) == 1)
    if orders:
        check("status_desc amigável",
              orders[0]["status_desc"] == "BTC enviado — aguardando confirmação",
              f"got {orders[0]['status_desc']}")

    # ---- 9) stats ----
    st = off.stats()
    check("stats.total == 1", st["total"] == 1)
    check("stats.btc_sent == 1", st["btc_sent"] == 1)

    print()
    print("=" * 60)
    if failures:
        print(f" FALHAS: {len(failures)} teste(s)")
        for f in failures:
            print(f"   - {f}")
        raise SystemExit(1)
    print(" OfframpManager v1.2 OK — todos os testes passaram.")
    print("=" * 60)
