"""contracts/factory.py — Fábrica de escrows L2 BTC→BRN.

Cria ordens com limites BTC, cotação fixa, taxa e persistência via ChainDB.
A criação de um escrow não comprova depósito BTC nem autoriza crédito de BRN;
isso depende da validação on-chain e do fluxo seguro de liberação.
"""
from __future__ import annotations

import hashlib
import logging
import math
import time

from .escrow_btc_brn import (
    EscrowBTCtoBRN,
    generate_escrow_id,
    STATUS_OPEN,
    STATUS_BTC_DETECTED,
    STATUS_RELEASED,
    STATUS_EXPIRED,
    STATUS_CANCELLED,
    VALID_STATUSES,
)

log = logging.getLogger("contracts.factory")

DEFAULT_BRN_PER_BTC = 10_000
DEFAULT_FEE_PERCENT = 2.0
DEFAULT_MIN_BTC_SATS = 10_000
DEFAULT_MAX_BTC_SATS = 100_000_000
DEFAULT_BTC_ADDRESS = ""
DEFAULT_ESCROW_TTL_S = 24 * 3600


def _cfg(name: str, default):
    """Lê btc_config.py com fallback seguro."""
    try:
        import btc_config
        return getattr(btc_config, name, default)
    except Exception:
        return default


def _rate() -> float:
    try:
        value = float(_cfg("BRN_PER_BTC", DEFAULT_BRN_PER_BTC))
        return value if math.isfinite(value) and value > 0 else float(DEFAULT_BRN_PER_BTC)
    except (ValueError, TypeError, OverflowError):
        return float(DEFAULT_BRN_PER_BTC)


def _fee_pct() -> float:
    try:
        value = float(_cfg("FEE_PERCENT", DEFAULT_FEE_PERCENT))
        return value if math.isfinite(value) and 0 <= value <= 50 else float(DEFAULT_FEE_PERCENT)
    except (ValueError, TypeError, OverflowError):
        return float(DEFAULT_FEE_PERCENT)


def _min_sats() -> int:
    try:
        value = int(_cfg("MIN_BTC_SATS", DEFAULT_MIN_BTC_SATS))
        return value if value > 0 else DEFAULT_MIN_BTC_SATS
    except (ValueError, TypeError, OverflowError):
        return DEFAULT_MIN_BTC_SATS


def _max_sats() -> int:
    try:
        value = int(_cfg("MAX_BTC_SATS", DEFAULT_MAX_BTC_SATS))
        return value if value > 0 else DEFAULT_MAX_BTC_SATS
    except (ValueError, TypeError, OverflowError):
        return DEFAULT_MAX_BTC_SATS


def _btc_address() -> str:
    return str(_cfg("BTC_RECEIVE_ADDRESS", DEFAULT_BTC_ADDRESS) or "").strip()


def compute_brn_amounts(btc_sats: int) -> dict:
    """Calcula BRN em unidades mínimas; 1 BTC equivale a 1e8 satoshis."""
    if isinstance(btc_sats, bool) or not isinstance(btc_sats, int) or btc_sats <= 0:
        raise ValueError("btc_sats deve ser int > 0")
    rate = _rate()
    fee_pct = _fee_pct()
    # satoshis BTC × BRN/BTC = unidades mínimas de BRN.
    brn_gross = int(btc_sats * rate)
    fee = int(brn_gross * fee_pct / 100)
    brn_net = brn_gross - fee
    return {
        "brn_gross": brn_gross,
        "fee": fee,
        "brn_net": brn_net,
        "rate": rate,
        "fee_pct": fee_pct,
    }


class ContractFactory:
    """Cria, consulta e atualiza escrows usando a interface L2 do ChainDB."""

    def __init__(self, db):
        if db is None:
            raise ValueError("db obrigatorio")
        self.db = db

    def _validate_buyer(self, buyer: str) -> None:
        if not isinstance(buyer, str) or not buyer:
            raise ValueError("buyer obrigatorio")
        if not buyer.startswith("brn1"):
            raise ValueError("buyer deve ser endereco brn1...")
        if not 20 <= len(buyer) <= 128:
            raise ValueError("buyer com tamanho invalido")

    def _validate_btc_amount(self, sats: int) -> int:
        if isinstance(sats, bool) or not isinstance(sats, int) or sats <= 0:
            raise ValueError("btc_amount_sats deve ser int > 0")
        min_s, max_s = _min_sats(), _max_sats()
        if min_s >= max_s:
            raise ValueError("configuracao MIN_BTC_SATS/MAX_BTC_SATS invalida")
        if sats < min_s:
            raise ValueError(f"BTC abaixo do minimo ({sats} < {min_s} sats)")
        if sats > max_s:
            raise ValueError(f"BTC acima do maximo ({sats} > {max_s} sats)")
        return sats

    def create_btc_to_brn(
        self,
        creator_wallet: str,
        btc_amount_sats: int,
        buyer_wallet: str,
        ttl_s: int = DEFAULT_ESCROW_TTL_S,
    ) -> EscrowBTCtoBRN:
        """Cria e persiste um escrow OPEN. Não credita BRN neste estágio."""
        self._validate_buyer(buyer_wallet)
        self._validate_btc_amount(btc_amount_sats)
        if isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or ttl_s <= 0:
            raise ValueError("ttl_s deve ser int > 0")

        amounts = compute_brn_amounts(btc_amount_sats)
        brn_net = amounts["brn_net"]
        if brn_net <= 0:
            raise ValueError("brn_amount calculado <= 0")

        btc_address = _btc_address()
        if not btc_address or "SEU_ENDERECO" in btc_address or btc_address.startswith("bc1qSEU_"):
            log.warning("BTC_RECEIVE_ADDRESS nao configurado: configure btc_config.py antes de usar L2")

        now = int(time.time())
        escrow_id = generate_escrow_id()
        expires_at = now + ttl_s
        l2_hash = hashlib.sha256(
            f"{escrow_id}:{creator_wallet or 'BRN_TREASURY'}:{buyer_wallet}:"
            f"{brn_net}:{btc_amount_sats}:{btc_address}:{now}:{expires_at}".encode("utf-8")
        ).hexdigest()

        escrow = EscrowBTCtoBRN(
            escrow_id=escrow_id,
            creator=creator_wallet or "BRN_TREASURY",
            buyer=buyer_wallet,
            brn_amount=brn_net,
            btc_expected_sats=btc_amount_sats,
            btc_address=btc_address,
            created_at=now,
            expires_at=expires_at,
            l2_hash=l2_hash,
            status=STATUS_OPEN,
        )
        try:
            self.db.save_l2_escrow(escrow.to_dict())
        except Exception as exc:
            log.exception("Falha ao salvar escrow")
            raise RuntimeError(f"falha ao salvar escrow: {exc}") from exc

        log.info(
            "[FACTORY] escrow %s criado — btc=%.8f brn=%.8f buyer=%s...",
            escrow_id, btc_amount_sats / 1e8, brn_net / 1e8, buyer_wallet[:16],
        )
        return escrow

    def list_by_buyer(self, buyer: str, status: str | None = "OPEN") -> list:
        if not isinstance(buyer, str) or not buyer.startswith("brn1"):
            return []
        try:
            rows = self.db.get_l2_escrows(status=status) if status and status != "ALL" else self.db.get_l2_escrows()
        except Exception as exc:
            log.warning("list_by_buyer: %s", exc)
            return []
        return [r for r in rows if r.get("buyer") == buyer]

    def list_by_creator(self, creator: str, status: str | None = None) -> list:
        if not creator:
            return []
        try:
            rows = self.db.get_l2_escrows(status=status) if status and status != "ALL" else self.db.get_l2_escrows()
        except Exception as exc:
            log.warning("list_by_creator: %s", exc)
            return []
        return [r for r in rows if r.get("creator") == creator]

    def get(self, escrow_id: str) -> EscrowBTCtoBRN | None:
        try:
            data = self.db.get_l2_escrow_by_id(escrow_id)
            return EscrowBTCtoBRN.from_dict(data) if data else None
        except Exception as exc:
            log.warning("get(%s): %s", escrow_id, exc)
            return None

    def cancel(self, escrow_id: str) -> dict:
        """Cancela apenas escrows OPEN."""
        escrow = self.get(escrow_id)
        if not escrow:
            return {"ok": False, "msg": "escrow nao existe"}
        if not escrow.can_cancel():
            return {"ok": False, "msg": f"nao pode cancelar em estado {escrow.status}"}
        try:
            self.db.update_l2_escrow_status(escrow_id, STATUS_CANCELLED)
            return {"ok": True, "escrow_id": escrow_id, "status": STATUS_CANCELLED}
        except Exception as exc:
            log.exception("Falha ao cancelar escrow %s", escrow_id)
            return {"ok": False, "msg": str(exc)}

    def expire_overdue(self, limit: int = 500) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit deve ser int >= 0")
        try:
            rows = self.db.get_l2_escrows(status=STATUS_OPEN)
        except Exception as exc:
            log.warning("expire_overdue: %s", exc)
            return 0
        now = int(time.time())
        count = 0
        for row in rows[:limit]:
            try:
                if int(row.get("expires_at", 0)) <= now:
                    self.db.update_l2_escrow_status(row["escrow_id"], STATUS_EXPIRED)
                    count += 1
            except Exception as exc:
                log.warning("Falha ao expirar escrow: %s", exc)
        if count:
            log.info("[FACTORY] %d escrow(s) expirado(s)", count)
        return count

    def stats(self) -> dict:
        out = {
            "open": 0, "btc_detected": 0, "released": 0,
            "expired": 0, "cancelled": 0, "total": 0,
        }
        try:
            for status in VALID_STATUSES:
                rows = self.db.get_l2_escrows(status=status)
                key = "btc_detected" if status == STATUS_BTC_DETECTED else status.lower()
                out[key] = len(rows)
                out["total"] += len(rows)
        except Exception as exc:
            log.warning("stats: %s", exc)
        out["rate"] = _rate()
        out["fee_pct"] = _fee_pct()
        out["btc_address"] = _btc_address()
        return out


if __name__ == "__main__":
    class FakeDB:
        def __init__(self):
            self.rows = {}
        def save_l2_escrow(self, data):
            self.rows[data["escrow_id"]] = dict(data)
        def get_l2_escrow_by_id(self, escrow_id):
            return self.rows.get(escrow_id)
        def get_l2_escrows(self, status=None):
            values = list(self.rows.values())
            return [r for r in values if r.get("status") == status] if status else values
        def update_l2_escrow_status(self, escrow_id, status, btc_txid=None):
            if escrow_id in self.rows:
                self.rows[escrow_id]["status"] = status
                if btc_txid:
                    self.rows[escrow_id]["btc_txid"] = btc_txid

    print("Testando ContractFactory...")
    db = FakeDB()
    factory = ContractFactory(db)
    buyer = "brn1qtestaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    quote = compute_brn_amounts(100_000)
    assert quote["brn_gross"] > quote["brn_net"] > 0
    assert quote["fee"] > 0

    escrow = factory.create_btc_to_brn("BRN_TREASURY", 100_000, buyer)
    assert escrow.status == STATUS_OPEN
    assert factory.get(escrow.escrow_id).to_dict() == escrow.to_dict()
    assert len(factory.list_by_buyer(buyer, "OPEN")) == 1
    assert factory.cancel(escrow.escrow_id)["ok"]
    assert factory.get(escrow.escrow_id).status == STATUS_CANCELLED
    assert factory.stats()["cancelled"] == 1

    try:
        factory.create_btc_to_brn("BRN_TREASURY", 100_000, buyer, ttl_s=-1)
        raise AssertionError("ttl negativo deveria ser rejeitado")
    except ValueError:
        pass
    for amount, address in ((0, buyer), (-1, buyer), (100_000, "not_brn")):
        try:
            factory.create_btc_to_brn("BRN_TREASURY", amount, address)
            raise AssertionError("entrada invalida deveria ser rejeitada")
        except ValueError:
            pass

    expiring = factory.create_btc_to_brn("BRN_TREASURY", 100_000, buyer, ttl_s=1)
    db.rows[expiring.escrow_id]["expires_at"] = int(time.time()) - 1
    assert factory.expire_overdue() == 1
    assert factory.get(expiring.escrow_id).status == STATUS_EXPIRED
    print("ContractFactory OK.")
