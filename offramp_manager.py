"""offramp_manager.py — Gerencia ordens BRN → BTC (OFFRAMP)
================================================================
v1.0 (2026-10-28):
  [NEW] Classe OfframpManager. Worker em thread que processa escrows
        com status BRN_LOCKED, envia BTC via BTCSender e atualiza estado.
  [NEW] Fluxo:
        OPEN → BRN_LOCKED → BTC_SENT → RELEASED
                              ↘ FAILED
        OPEN → EXPIRED / CANCELLED
  [SECURITY] Verifica valor mínimo/máximo. Rate-limita por buyer.
             Reutiliza a tabela l2_escrows com campo `direction`.
================================================================
"""
from __future__ import annotations

import time
import uuid
import threading
import logging

log = logging.getLogger("offramp")

# Limites de taxa BTC (dust threshold Bitcoin é 546)
DUST_SATS = 546

# Rate-limit: 1 saque a cada N segundos por endereço
DEFAULT_RATE_S = 60


class OfframpError(Exception):
    pass


class OfframpManager:
    def __init__(self, db, blockchain, btc_sender,
                 rate_brn_per_btc: float, fee_pct: float,
                 min_brn_sats: int, max_brn_sats: int,
                 min_btc_sats: int = 5000,
                 ttl_s: int = 1800,
                 treasury_brn: str = ""):
        if btc_sender is None:
            raise OfframpError("BTCSender obrigatório (WIF ausente?)")

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

        self._lock = threading.RLock()
        self._rate_last: dict = {}  # buyer -> ts do último create
        self._stop = threading.Event()
        self._thread = None

        log.info(
            f"[OFFRAMP] init: rate={self.rate} fee={self.fee_pct}% "
            f"min={self.min_brn_sats/1e8:.8f}BRN max={self.max_brn_sats/1e8:.8f}BRN "
            f"minBTC={self.min_btc_sats}sats cofreBTC={self.sender.get_address()[:16]}…"
        )

    # ============================================================
    # CICLO DE VIDA
    # ============================================================
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="Offramp",
        )
        self._thread.start()
        log.info("[OFFRAMP] worker iniciado")

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        log.info("[OFFRAMP] worker parado")

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._process_pending()
            except Exception as e:
                log.warning(f"[OFFRAMP] loop: {e}")
            self._stop.wait(10)

    def _process_pending(self):
        try:
            all_escrows = self.db.get_l2_escrows()
        except Exception as e:
            log.debug(f"[OFFRAMP] get_l2_escrows: {e}")
            return

        for e in all_escrows:
            if not isinstance(e, dict):
                continue
            if e.get("direction") != "BRN_TO_BTC":
                continue
            if e.get("status") == "BRN_LOCKED":
                self._send_btc(e)
            elif e.get("status") == "BTC_SENT":
                self._check_confirmation(e)
            elif e.get("status") == "OPEN":
                # Se ficou OPEN > ttl, expira
                if int(e.get("expires_at", 0)) < int(time.time()):
                    self._mark(e["escrow_id"], "EXPIRED", "ttl")

    # ============================================================
    # ENVIO
    # ============================================================
    def _send_btc(self, escrow: dict):
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
                self._mark(fresh["escrow_id"], "FAILED",
                           f"net {net_sats} < min {self.min_btc_sats}")
                return

            dest = fresh.get("btc_dest", "")
            if not dest:
                self._mark(fresh["escrow_id"], "FAILED", "btc_dest vazio")
                return

            try:
                r = self.sender.send(dest, net_sats, priority="halfHourFee")
            except Exception as e:
                log.exception(f"[OFFRAMP] send: {e}")
                self._mark(fresh["escrow_id"], "FAILED", str(e))
                return

            if not r.get("ok"):
                self._mark(fresh["escrow_id"], "FAILED",
                           r.get("msg", "broadcast falhou"))
                return

            fresh["status"] = "BTC_SENT"
            fresh["btc_txid"] = r["txid"]
            fresh["btc_sent_sats"] = net_sats
            fresh["btc_fee_sats"] = int(r.get("fee_sats") or 0)
            fresh["btc_sent_at"] = int(time.time())
            self.db.save_l2_escrow(fresh)
            log.info(
                f"[OFFRAMP] OK escrow={fresh['escrow_id']} "
                f"net={net_sats} sats txid={r['txid'][:16]}…"
            )

    def _check_confirmation(self, escrow: dict):
        txid = escrow.get("btc_txid", "")
        if not txid:
            return
        try:
            base = (self.sender._api_candidates[0])
            r = requests.get(f"{base}/tx/{txid}/status", timeout=10)
            if r.status_code != 200:
                return
            st = r.json()
            if st.get("confirmed"):
                self._mark(escrow["escrow_id"], "RELEASED", "confirmed")
        except Exception:
            pass

    def _mark(self, escrow_id: str, status: str, reason: str = ""):
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
    # API PÚBLICA
    # ============================================================
    def quote(self, brn_amount_sats: int) -> dict:
        if brn_amount_sats <= 0:
            raise OfframpError("brn_amount invalido")
        gross_btc = brn_amount_sats / self.rate
        fee_btc = gross_btc * self.fee_pct / 100.0
        net_btc = gross_btc - fee_btc
        net_sats = int(net_btc * 1e8)
        return {
            "brn_amount": brn_amount_sats / 1e8,
            "brn_sats": brn_amount_sats,
            "btc_gross": round(gross_btc, 8),
            "fee_percent": self.fee_pct,
            "btc_fee": round(fee_btc, 8),
            "btc_net": round(net_btc, 8),
            "btc_net_sats": net_sats,
            "rate": f"1 BTC = {self.rate} BRN",
            "min_brn": self.min_brn_sats / 1e8,
            "max_brn": self.max_brn_sats / 1e8,
            "min_btc_sats": self.min_btc_sats,
            "cofre_btc": self.sender.get_address(),
            "cofre_brn": self.treasury_brn,
            "network": self.sender.network,
        }

    def create_escrow(self, buyer_brn: str, brn_amount_sats: int,
                      btc_dest: str) -> dict:
        """Cria escrow OPEN. NÃO bloqueia BRN ainda (feito pelo server)."""
        if not buyer_brn or not buyer_brn.startswith("brn1"):
            return {"ok": False, "msg": "buyer_brn invalido"}
        if brn_amount_sats < self.min_brn_sats:
            return {"ok": False, "msg": f"abaixo do minimo ({self.min_brn_sats/1e8:.8f} BRN)"}
        if brn_amount_sats > self.max_brn_sats:
            return {"ok": False, "msg": f"acima do maximo ({self.max_brn_sats/1e8:.8f} BRN)"}
        if not btc_dest:
            return {"ok": False, "msg": "btc_dest vazio"}

        # Rate-limit
        now = int(time.time())
        with self._lock:
            last = self._rate_last.get(buyer_brn, 0)
            if now - last < 60:
                return {"ok": False,
                        "msg": f"aguarde {60 - (now - last)}s entre saques"}

        # Valida endereço BTC
        try:
            from bitcoinlib.keys import Address
            addr = Address(btc_dest, network=self.sender.network)
            btc_dest_clean = addr.address
        except Exception as e:
            return {"ok": False, "msg": f"endereco BTC invalido: {e}"}

        gross_btc = brn_amount_sats / self.rate
        fee_btc = gross_btc * self.fee_pct / 100.0
        net_sats = int((gross_btc - fee_btc) * 1e8)
        if net_sats < self.min_btc_sats:
            return {"ok": False,
                    "msg": f"net BTC {net_sats} abaixo do minimo {self.min_btc_sats}"}

        escrow_id = "off_" + uuid.uuid4().hex[:16]
        now = int(time.time())
        escrow = {
            "escrow_id":        escrow_id,
            "direction":        "BRN_TO_BTC",
            "creator":          buyer_brn,
            "buyer":            buyer_brn,
            "brn_amount":       brn_amount_sats,
            "btc_dest":         btc_dest_clean,
            "btc_expected_sats": net_sats,
            "btc_address":      self.sender.get_address(),
            "btc_txid":         "",
            "status":           "OPEN",
            "created_at":       now,
            "expires_at":       now + self.ttl_s,
            "l2_hash":          "",
        }
        self.db.save_l2_escrow(escrow)

        with self._lock:
            self._rate_last[buyer_brn] = now

        return {"ok": True, "escrow": escrow}

    def mark_brn_locked(self, escrow_id: str, brn_txid: str) -> dict:
        """Chamado pelo server após a tx BRN ser aceita na mempool."""
        with self._lock:
            e = self.db.get_l2_escrow_by_id(escrow_id)
            if not e:
                return {"ok": False, "msg": "escrow nao existe"}
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
            if e.get("status") not in ("OPEN",):
                return {"ok": False,
                        "msg": f"cancelamento so em OPEN (status {e.get('status')})"}
            e["status"] = "CANCELLED"
            e["updated_at"] = int(time.time())
            self.db.save_l2_escrow(e)
            return {"ok": True}

    def list_orders(self, buyer: str = None, status: str = None) -> list:
        try:
            all_escrows = self.db.get_l2_escrows()
        except Exception:
            return []
        out = []
        for e in all_escrows:
            if e.get("direction") != "BRN_TO_BTC":
                continue
            if buyer and e.get("buyer") != buyer:
                continue
            if status and e.get("status") != status:
                continue
            out.append(e)
        return out

    def stats(self) -> dict:
        orders = self.list_orders()
        out = {
            "total": len(orders),
            "open": 0, "brn_locked": 0, "btc_sent": 0,
            "released": 0, "failed": 0, "expired": 0, "cancelled": 0,
            "cofre_btc": self.sender.get_address(),
            "cofre_btc_sats": 0,
            "cofre_brn": self.treasury_brn,
            "network": self.sender.network,
        }
        for o in orders:
            st = o.get("status", "").lower()
            if st in out:
                out[st] += 1
        try:
            out["cofre_btc_sats"] = self.sender.get_balance()
        except Exception:
            pass
        return out
