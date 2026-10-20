"""l2_manager.py — Gerencia ordens L2 BTC→BRN (v1.1)
================================================================
v1.1:
  [FIX] CRÍTICO: apply_btc_deposit() acessava escrow["btc_expected_sats"]
        mas _format_order() retorna "btc_amount_sats".
        KeyError impedia TODA aplicação de depósito BTC.
  [FIX] CRÍTICO: l2_tx usava escrow["brn_amount"] (decimal string)
        em vez de escrow["brn_amount_sats"] (int).
        Convertia a string para int — corrompia o valor creditado.
  [NEW] _format_order() agora expõe também btc_expected_sats
        (alias) para compatibilidade com código antigo.
v1.0:
  Versão inicial.
================================================================
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal, ROUND_DOWN

from contracts.factory import (
    ContractFactory,
    compute_brn_amounts,
    _rate, _fee_pct, _min_sats, _max_sats, _btc_address,
)
from contracts.escrow_btc_brn import (
    STATUS_OPEN, STATUS_BTC_DETECTED, STATUS_RELEASED,
    STATUS_EXPIRED, STATUS_CANCELLED, VALID_STATUSES,
)

log = logging.getLogger("l2_manager")

SAT = Decimal(10 ** 8)

STATUS_DESCRIPTIONS = {
    STATUS_OPEN:         "Aguardando BTC no endereco",
    STATUS_BTC_DETECTED: "BTC confirmado, aguardando mineracao",
    STATUS_RELEASED:     "Minerado — BRN creditado",
    STATUS_EXPIRED:      "Expirado (24h)",
    STATUS_CANCELLED:    "Cancelado pelo creator",
}


# ============================================================
# ERROS
# ============================================================
class L2Error(Exception):
    """Erro de validação / regra de negócio do L2."""
    pass


# ============================================================
# HELPERS
# ============================================================
def sats_to_coin(sats: int) -> Decimal:
    """Converte satoshis → valor decimal (8 casas)."""
    return (Decimal(int(sats)) / SAT).quantize(Decimal("0.00000001"))


def coin_to_sats(amount) -> int:
    """Converte valor decimal → satoshis (int)."""
    try:
        return int((Decimal(str(amount)) * SAT).quantize(Decimal("1"),
                                                          rounding=ROUND_DOWN))
    except Exception:
        return 0


def _default_btc_address() -> str:
    a = _btc_address()
    if not a or a.startswith("bc1qSEU_"):
        return ""
    return a


# ============================================================
# L2Manager
# ============================================================
class L2Manager:

    def __init__(self, db, blockchain=None):
        self.db = db
        self.blockchain = blockchain
        self.factory = ContractFactory(db)

    # ============================================================
    # CRIAÇÃO DE ORDEM
    # ============================================================
    def create_order(self,
                     btc_amount_sats: int,
                     buyer_brn_address: str,
                     creator_wallet: str = "BRN_TREASURY") -> dict:
        """
        Cria uma ordem BTC → BRN.

        Retorna dict com:
            escrow_id, btc_address, btc_amount, btc_amount_sats,
            brn_amount, brn_amount_sats, buyer, status, expires_at,
            l2_hash, instructions
        """
        if not isinstance(btc_amount_sats, int):
            raise L2Error("btc_amount_sats deve ser int (satoshis)")
        if btc_amount_sats <= 0:
            raise L2Error("btc_amount_sats deve ser > 0")
        if not buyer_brn_address or not buyer_brn_address.startswith("brn1"):
            raise L2Error("Endereco BRN invalido (deve comecar com brn1)")

        # cria via factory (valida min/max internamente)
        try:
            escrow = self.factory.create_btc_to_brn(
                creator_wallet=creator_wallet,
                btc_amount_sats=btc_amount_sats,
                buyer_wallet=buyer_brn_address,
            )
        except ValueError as e:
            raise L2Error(str(e))
        except Exception as e:
            log.exception("create_btc_to_brn falhou")
            raise L2Error(f"falha ao criar ordem: {e}")

        # monta resposta
        btc_addr = escrow.btc_address or _default_btc_address()
        instructions = (
            f"Envie exatamente {sats_to_coin(btc_amount_sats)} BTC para "
            f"{btc_addr} em ate 24h. Apos {_confirmations_required()} "
            f"confirmacoes, o BRN sera creditado automaticamente."
        )

        return {
            "escrow_id":         escrow.escrow_id,
            "btc_address":       btc_addr,
            "btc_amount":        str(sats_to_coin(btc_amount_sats)),
            "btc_amount_sats":   btc_amount_sats,
            "brn_amount":        str(sats_to_coin(escrow.brn_amount)),
            "brn_amount_sats":   escrow.brn_amount,
            "buyer":             buyer_brn_address,
            "creator":           creator_wallet,
            "status":            escrow.status,
            "status_desc":       STATUS_DESCRIPTIONS.get(escrow.status,
                                                          escrow.status),
            "created_at":        escrow.created_at,
            "expires_at":        escrow.expires_at,
            "l2_hash":           escrow.l2_hash,
            "instructions":      instructions,
        }

    # ============================================================
    # CONSULTAS
    # ============================================================
    def get_order(self, escrow_id: str) -> dict | None:
        if not escrow_id:
            return None
        try:
            row = self.db.get_l2_escrow_by_id(escrow_id)
        except Exception as e:
            log.warning(f"get_l2_escrow_by_id({escrow_id}): {e}")
            return None
        if not row:
            return None

        return self._format_order(row)

    def list_orders(self,
                    buyer: str | None = None,
                    status: str | None = "OPEN",
                    limit: int = 100) -> list:
        """
        Lista ordens. Filtra por buyer e/ou status.
        status="ALL" ou None → todos os status.
        """
        try:
            if status and status != "ALL":
                rows = self.db.get_l2_escrows(status=status)
            else:
                rows = self.db.get_l2_escrows()
        except Exception as e:
            log.warning(f"get_l2_escrows: {e}")
            return []

        out = []
        for r in rows:
            if buyer and r.get("buyer") != buyer:
                continue
            out.append(self._format_order(r))
            if len(out) >= limit:
                break
        return out

    # ============================================================
    # COTAÇÃO
    # ============================================================
    def quote(self, btc_amount_sats: int) -> dict:
        """
        Retorna cotação para um valor em sats.
        Não persiste nada.
        """
        if not isinstance(btc_amount_sats, int) or btc_amount_sats <= 0:
            raise L2Error("btc_amount_sats deve ser int > 0")

        min_s = _min_sats()
        max_s = _max_sats()
        if btc_amount_sats < min_s:
            raise L2Error(
                f"BTC minimo: {sats_to_coin(min_s)} BTC")
        if btc_amount_sats > max_s:
            raise L2Error(
                f"BTC maximo: {sats_to_coin(max_s)} BTC")

        amounts = compute_brn_amounts(btc_amount_sats)
        return {
            "btc_amount":  str(sats_to_coin(btc_amount_sats)),
            "btc_sats":    btc_amount_sats,
            "brn_gross":   str(sats_to_coin(amounts["brn_gross"])),
            "fee_percent": amounts["fee_pct"],
            "fee_brn":     str(sats_to_coin(amounts["fee"])),
            "brn_net":     str(sats_to_coin(amounts["brn_net"])),
            "brn_net_sats": amounts["brn_net"],
            "rate":        f"1 BTC = {amounts['rate']} BRN",
            "min_btc":     str(sats_to_coin(min_s)),
            "max_btc":     str(sats_to_coin(max_s)),
            "btc_address": _default_btc_address(),
            "min_confirmations": _confirmations_required(),
        }

    # ============================================================
    # ESTATÍSTICAS
    # ============================================================
    def stats(self) -> dict:
        out = {
            "open":         0,
            "btc_detected": 0,
            "released":     0,
            "expired":      0,
            "cancelled":    0,
            "total":        0,
            "btc_address":  _default_btc_address(),
            "rate":         _rate(),
            "fee_percent":  _fee_pct(),
        }
        try:
            for st in VALID_STATUSES:
                rows = self.db.get_l2_escrows(status=st)
                n = len(rows) if rows else 0
                if st == STATUS_OPEN:
                    out["open"] = n
                elif st == STATUS_BTC_DETECTED:
                    out["btc_detected"] = n
                elif st == STATUS_RELEASED:
                    out["released"] = n
                elif st == STATUS_EXPIRED:
                    out["expired"] = n
                elif st == STATUS_CANCELLED:
                    out["cancelled"] = n
                out["total"] += n
        except Exception as e:
            log.warning(f"stats: {e}")
            out["error"] = str(e)
        return out

    # ============================================================
    # AÇÕES
    # ============================================================
    def cancel_order(self, escrow_id: str) -> dict:
        """Cancela uma ordem OPEN."""
        return self.factory.cancel(escrow_id)

    def expire_overdue(self, limit: int = 500) -> int:
        """Marca como EXPIRED os escrows vencidos. Retorna quantos."""
        return self.factory.expire_overdue(limit=limit)

    # ============================================================
    # APLICAÇÃO DE DEPÓSITO BTC (chamado pelo btc_watcher)
    # ============================================================
    def apply_btc_deposit(self,
                          escrow_id: str,
                          btc_txid: str,
                          btc_amount_sats: int) -> dict:
        """
        Aplica um depósito BTC detectado.

        IMPORTANTE: `btc_amount_sats` deve ser o valor REALMENTE recebido
        (verificado on-chain pelo caller — btc_watcher ou server).
        Nunca confie em "btc_amount_sats esperado" — sempre o real.

        Fluxo:
          1. Valida escrow (existe, OPEN, não expirado, valor suficiente)
          2. Chama blockchain.add_l2_transaction()
             → blockchain vai marcar btc_txid_used
             → blockchain vai criar coinbase L2 e jogar na mempool

        Retorna {"ok": bool, "txid": str, "msg": str}.

        [FIX v1.1] Duas correções críticas:
          - escrow["btc_expected_sats"] → escrow["btc_amount_sats"]
          - escrow["brn_amount"]       → escrow["brn_amount_sats"]
        Ambos estavam mascarados porque o auto-teste só roda com
        blockchain=None e não chega até aqui.
        """
        if self.blockchain is None:
            return {"ok": False, "msg": "blockchain nao injetada"}

        escrow = self.get_order(escrow_id)
        if not escrow:
            return {"ok": False, "msg": "escrow nao existe"}

        if escrow["status"] != STATUS_OPEN:
            return {"ok": False,
                    "msg": f"escrow em estado {escrow['status']}"}

        if int(escrow["expires_at"]) < int(time.time()):
            self.db.update_l2_escrow_status(escrow_id, STATUS_EXPIRED)
            return {"ok": False, "msg": "escrow expirado"}

        # [FIX v1.1] Chave correta: btc_amount_sats (era btc_expected_sats)
        expected = int(escrow["btc_amount_sats"])

        # tolerância de 1% (fee)
        if int(btc_amount_sats) < int(expected * 0.99):
            return {"ok": False,
                    "msg": f"BTC insuficiente ({btc_amount_sats} < {expected})"}

        # anti-replay
        try:
            if self.db.is_btc_txid_used(btc_txid):
                return {"ok": False, "msg": "btc_txid ja utilizado"}
        except Exception:
            pass

        # [FIX v1.1] Escolhe a chave certa (brn_amount_sats, não brn_amount)
        brn_amount_sats = int(escrow["brn_amount_sats"])

        # monta l2_tx para blockchain.add_l2_transaction
        l2_tx = {
            "escrow_id":   escrow_id,
            "buyer":       escrow["buyer"],
            "brn_amount":  brn_amount_sats,
            "btc_txid":    btc_txid,
            "btc_address": escrow.get("btc_address", ""),
            "l2_hash":     escrow.get("l2_hash", ""),
            "timestamp":   int(time.time()),
            "pubkey":      "00" * 33,
        }

        try:
            txid = self.blockchain.add_l2_transaction(l2_tx)
        except Exception as e:
            log.exception("add_l2_transaction falhou")
            return {"ok": False, "msg": str(e)}

        return {
            "ok":   True,
            "txid": txid,
            "msg":  f"L2 tx enviada para mempool (txid={txid[:16]}...)",
        }

    # ============================================================
    # FORMAT
    # ============================================================
    @staticmethod
    def _format_order(row: dict) -> dict:
        status = row.get("status", "")

        # [FIX v1.1] Aceita os dois formatos de chave que podem aparecer
        # em escrows antigos salvos no DB.
        btc_sats = int(
            row.get("btc_amount_sats")
            or row.get("btc_expected_sats")
            or 0
        )
        brn_sats = int(
            row.get("brn_amount_sats")
            or row.get("brn_amount")
            or 0
        )

        return {
            "escrow_id":         row.get("escrow_id", ""),
            "creator":           row.get("creator", ""),
            "buyer":             row.get("buyer", ""),
            "status":            status,
            "status_desc":       STATUS_DESCRIPTIONS.get(status, status),
            "btc_address":       row.get("btc_address", ""),
            "btc_txid":          row.get("btc_txid", ""),
            "btc_amount":        str(sats_to_coin(btc_sats)),
            "btc_amount_sats":   btc_sats,
            # alias retroativo — alguns callers antigos procuram este nome
            "btc_expected_sats": btc_sats,
            "brn_amount":        str(sats_to_coin(brn_sats)),
            "brn_amount_sats":   brn_sats,
            "created_at":        int(row.get("created_at", 0) or 0),
            "expires_at":        int(row.get("expires_at", 0) or 0),
            "l2_hash":           row.get("l2_hash", ""),
            "released":          status == STATUS_RELEASED,
        }


# ============================================================
# HELPERS INTERNOS
# ============================================================
def _confirmations_required() -> int:
    try:
        import btc_config
        return int(getattr(btc_config, "BTC_MIN_CONFIRMATIONS", 3))
    except Exception:
        return 3


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    class FakeDB:
        def __init__(self):
            self.rows = {}
            self.used_txids = set()
        def save_l2_escrow(self, d):
            self.rows[d["escrow_id"]] = d
        def get_l2_escrow_by_id(self, eid):
            return self.rows.get(eid)
        def get_l2_escrows(self, status=None):
            vals = list(self.rows.values())
            if status:
                return [r for r in vals if r.get("status") == status]
            return vals
        def update_l2_escrow_status(self, eid, status, btc_txid=None):
            if eid in self.rows:
                self.rows[eid]["status"] = status
                if btc_txid:
                    self.rows[eid]["btc_txid"] = btc_txid
        def is_btc_txid_used(self, txid):
            return txid in self.used_txids

    class FakeBlockchain:
        """Mock mínimo para testar apply_btc_deposit sem tocar em blockchain real."""
        def __init__(self):
            self.calls = []
        def add_l2_transaction(self, l2_tx):
            self.calls.append(l2_tx)
            return "txid_fake_" + str(len(self.calls)).zfill(8)

    print("Testando L2Manager v1.1...")

    db = FakeDB()
    bc = FakeBlockchain()
    l2 = L2Manager(db, blockchain=bc)

    # ---------- 1. Cotação ----------
    q = l2.quote(100_000)
    print(f"  [info] quote: {q['btc_amount']} BTC → "
          f"{q['brn_net']} BRN (fee {q['fee_percent']}%)")
    assert Decimal(q["brn_net"]) > 0

    # ---------- 2. Cria ordem ----------
    buyer = "brn1qtestaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    o = l2.create_order(100_000, buyer)
    assert o["status"] == STATUS_OPEN
    assert o["buyer"] == buyer
    assert o["escrow_id"].startswith("esc_")
    print(f"  [ok] create_order: {o['escrow_id']}")

    # ---------- 3. Consulta ----------
    o2 = l2.get_order(o["escrow_id"])
    assert o2 is not None
    assert o2["escrow_id"] == o["escrow_id"]
    assert o2["btc_amount_sats"] == 100_000
    assert o2["btc_expected_sats"] == 100_000  # alias
    print("  [ok] get_order (com alias btc_expected_sats)")

    # ---------- 4. Lista ----------
    lst = l2.list_orders(buyer=buyer, status="OPEN")
    assert len(lst) == 1
    print(f"  [ok] list_orders: {len(lst)} aberta(s)")

    # ---------- 5. Estatísticas ----------
    s = l2.stats()
    print(f"  [info] stats: open={s['open']} total={s['total']} rate={s['rate']}")
    assert s["open"] == 1

    # ---------- 6. Cancelamento ----------
    r = l2.cancel_order(o["escrow_id"])
    assert r["ok"]
    print("  [ok] cancel_order")

    # ---------- 7. Validações ----------
    for bad_btc, bad_buyer in [(-1, buyer), (100_000, "not_brn")]:
        try:
            l2.create_order(bad_btc, bad_buyer)
            raise AssertionError("deveria rejeitar")
        except L2Error:
            pass
    print("  [ok] validações de entrada")

    # ---------- 8. Depósito sem blockchain ----------
    l2_nobc = L2Manager(db, blockchain=None)
    o3 = l2.create_order(100_000, buyer)
    r = l2_nobc.apply_btc_deposit(o3["escrow_id"], "txid_fake", 100_000)
    assert not r["ok"]
    print("  [ok] apply_btc_deposit rejeita sem blockchain")

    # ---------- 9. [CRÍTICO] Depósito COM blockchain ----------
    o4 = l2.create_order(100_000, buyer)
    r = l2.apply_btc_deposit(o4["escrow_id"], "txid_real_001", 100_000)
    assert r["ok"], f"esperado ok=True, obteve {r}"
    assert r["txid"].startswith("txid_fake_")
    assert len(bc.calls) == 1
    l2_tx = bc.calls[0]
    print(f"  [ok] apply_btc_deposit: {r['txid'][:16]}...")
    print(f"       l2_tx.brn_amount = {l2_tx['brn_amount']} (int, não string)")
    assert isinstance(l2_tx["brn_amount"], int), \
        f"brn_amount deve ser int, recebeu {type(l2_tx['brn_amount'])}"
    assert l2_tx["brn_amount"] > 0
    assert l2_tx["btc_txid"] == "txid_real_001"

    # ---------- 10. [CRÍTICO] Rejeita valor insuficiente ----------
    o5 = l2.create_order(100_000, buyer)
    r = l2.apply_btc_deposit(o5["escrow_id"], "txid_low_002", 50_000)
    assert not r["ok"]
    assert "insuficiente" in r["msg"].lower()
    print(f"  [ok] rejeita BTC insuficiente: {r['msg']}")

    # ---------- 11. [CRÍTICO] Rejeita escrow já pago ----------
    # Marca o escrow como BTC_DETECTED e tenta pagar de novo
    db.update_l2_escrow_status(o4["escrow_id"], STATUS_BTC_DETECTED)
    r = l2.apply_btc_deposit(o4["escrow_id"], "txid_dup_003", 100_000)
    assert not r["ok"]
    assert "estado" in r["msg"].lower() or "BTC_DETECTED" in r["msg"]
    print(f"  [ok] rejeita escrow já processado: {r['msg']}")

    print("\n" + "=" * 60)
    print(" L2Manager v1.1 OK — bugs críticos resolvidos.")
    print("=" * 60)
