"""contracts/escrow_btc_brn.py — Contrato L2 BTC → BRN
================================================================
Representa uma ordem de câmbio BTC → BRN.

Ciclo de vida:
    OPEN          → criado pelo seller, esperando depósito BTC
    BTC_DETECTED  → BTC chegou, aguardando inclusão em bloco
    RELEASED      → BRN minerado e creditado ao buyer
    EXPIRED       → passou de expires_at sem receber BTC
    CANCELLED     → cancelado pelo creator antes do BTC chegar

API:
    e = EscrowBTCtoBRN(...)           # cria em memória
    e.to_dict() / EscrowBTCtoBRN.from_dict(d)
    e.is_expired(now) / e.can_release()

Uso (via factory):
    from contracts.factory import ContractFactory
    f = ContractFactory(db)
    e = f.create_btc_to_brn("BRN_TREASURY", 100_000, "brn1q...")
================================================================
"""
from __future__ import annotations

import hashlib
import time


# ============================================================
# ESTADOS
# ============================================================
STATUS_OPEN = "OPEN"
STATUS_BTC_DETECTED = "BTC_DETECTED"
STATUS_RELEASED = "RELEASED"
STATUS_EXPIRED = "EXPIRED"
STATUS_CANCELLED = "CANCELLED"

VALID_STATUSES = {
    STATUS_OPEN,
    STATUS_BTC_DETECTED,
    STATUS_RELEASED,
    STATUS_EXPIRED,
    STATUS_CANCELLED,
}


# ============================================================
# CLASSE
# ============================================================
class EscrowBTCtoBRN:
    """Ordem de câmbio BTC→BRN.

    Campos:
        escrow_id          : ID único (esc_<hex>)
        creator            : endereço BRN que criou a ordem
        buyer               : endereço BRN que receberá os BRN
        brn_amount          : quantidade de BRN (unidades mínimas)
        btc_expected_sats   : quantidade de BTC (satoshis) esperada
        btc_address         : endereço BTC de recebimento
        btc_txid            : txid da transação BTC recebida (se houver)
        status              : estado atual do escrow
        created_at          : timestamp (unix)
        expires_at          : timestamp de expiração
        l2_hash             : hash determinístico do escrow
    """

    def __init__(
        self,
        escrow_id: str,
        creator: str,
        buyer: str,
        brn_amount: int,
        btc_expected_sats: int,
        btc_address: str,
        created_at: int,
        expires_at: int,
        l2_hash: str = "",
        status: str = STATUS_OPEN,
        btc_txid: str = "",
    ):
        if not escrow_id:
            raise ValueError("escrow_id obrigatorio")
        if not buyer or not buyer.startswith("brn1"):
            raise ValueError("buyer deve ser endereco brn1...")
        if isinstance(brn_amount, bool) or int(brn_amount) <= 0:
            raise ValueError("brn_amount deve ser > 0")
        if isinstance(btc_expected_sats, bool) or int(btc_expected_sats) <= 0:
            raise ValueError("btc_expected_sats deve ser > 0")
        if status not in VALID_STATUSES:
            raise ValueError(f"status invalido: {status}")
        if int(expires_at) < int(created_at):
            raise ValueError("expires_at nao pode ser anterior a created_at")

        self.escrow_id = str(escrow_id)
        self.creator = str(creator or "")
        self.buyer = str(buyer)
        self.brn_amount = int(brn_amount)
        self.btc_expected_sats = int(btc_expected_sats)
        self.btc_address = str(btc_address or "")
        self.btc_txid = str(btc_txid or "")
        self.status = status
        self.created_at = int(created_at)
        self.expires_at = int(expires_at)
        self.l2_hash = str(l2_hash or self._compute_hash())

    # ============================================================
    # HASH
    # ============================================================
    def _compute_hash(self) -> str:
        raw = (
            f"{self.escrow_id}:{self.creator}:{self.buyer}:"
            f"{self.brn_amount}:{self.btc_expected_sats}:"
            f"{self.btc_address}:{self.created_at}:{self.expires_at}"
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    # ============================================================
    # ESTADO
    # ============================================================
    def is_expired(self, now: int | None = None) -> bool:
        n = int(now if now is not None else time.time())
        return n >= self.expires_at and self.status == STATUS_OPEN

    def is_open(self) -> bool:
        return self.status == STATUS_OPEN

    def is_detected(self) -> bool:
        return self.status == STATUS_BTC_DETECTED

    def is_released(self) -> bool:
        return self.status == STATUS_RELEASED

    def is_terminal(self) -> bool:
        return self.status in (STATUS_RELEASED, STATUS_EXPIRED, STATUS_CANCELLED)

    def can_release(self) -> bool:
        """Indica se a ordem está no estado anterior à liberação.

        Isto não confirma um depósito BTC. A confirmação, valor, endereço,
        número de confirmações e proteção contra reutilização do txid devem
        ser verificados pelo watcher/factory antes de creditar BRN.
        """
        return self.status == STATUS_BTC_DETECTED

    def can_cancel(self) -> bool:
        """Pode ser cancelado enquanto está OPEN."""
        return self.status == STATUS_OPEN

    def seconds_until_expiry(self, now: int | None = None) -> int:
        n = int(now if now is not None else time.time())
        return max(0, self.expires_at - n)

    # ============================================================
    # MUTAÇÕES (in-place — não persistem no DB)
    # ============================================================
    def mark_detected(self, btc_txid: str) -> None:
        if self.status != STATUS_OPEN:
            raise ValueError(f"mark_detected em estado {self.status}")
        txid = str(btc_txid or "").strip()
        if not txid:
            raise ValueError("btc_txid obrigatorio")
        self.status = STATUS_BTC_DETECTED
        self.btc_txid = txid

    def mark_released(self) -> None:
        if self.status != STATUS_BTC_DETECTED:
            raise ValueError(f"mark_released em estado {self.status}")
        self.status = STATUS_RELEASED

    def mark_expired(self) -> None:
        if self.status not in (STATUS_OPEN, STATUS_BTC_DETECTED):
            raise ValueError(f"mark_expired em estado {self.status}")
        self.status = STATUS_EXPIRED

    def mark_cancelled(self) -> None:
        if not self.can_cancel():
            raise ValueError(f"mark_cancelled em estado {self.status}")
        self.status = STATUS_CANCELLED

    # ============================================================
    # SERIALIZAÇÃO
    # ============================================================
    def to_dict(self) -> dict:
        return {
            "escrow_id": self.escrow_id,
            "creator": self.creator,
            "buyer": self.buyer,
            "brn_amount": self.brn_amount,
            "btc_expected_sats": self.btc_expected_sats,
            "btc_address": self.btc_address,
            "btc_txid": self.btc_txid,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "l2_hash": self.l2_hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EscrowBTCtoBRN":
        return cls(
            escrow_id=d["escrow_id"],
            creator=d.get("creator", ""),
            buyer=d["buyer"],
            brn_amount=d["brn_amount"],
            btc_expected_sats=d["btc_expected_sats"],
            btc_address=d.get("btc_address", ""),
            btc_txid=d.get("btc_txid", ""),
            status=d.get("status", STATUS_OPEN),
            created_at=d["created_at"],
            expires_at=d["expires_at"],
            l2_hash=d.get("l2_hash", ""),
        )

    def __repr__(self) -> str:
        return (
            f"<EscrowBTCtoBRN {self.escrow_id[:12]}... "
            f"status={self.status} "
            f"btc={self.btc_expected_sats / 1e8:.8f} "
            f"brn={self.brn_amount / 1e8:.8f}>"
        )

    def __eq__(self, other) -> bool:
        if not isinstance(other, EscrowBTCtoBRN):
            return NotImplemented
        return self.escrow_id == other.escrow_id

    def __hash__(self) -> int:
        return hash(self.escrow_id)


# ============================================================
# HELPERS
# ============================================================
def generate_escrow_id() -> str:
    """Gera ID único de escrow: esc_<16 hex>."""
    import secrets

    return "esc_" + secrets.token_hex(8)


def create_new(
    creator: str,
    buyer: str,
    brn_amount: int,
    btc_expected_sats: int,
    btc_address: str,
    ttl_s: int = 24 * 3600,
) -> EscrowBTCtoBRN:
    """Cria um escrow em estado OPEN (ttl_s padrão de 24 horas)."""
    ttl = int(ttl_s)
    if ttl <= 0:
        raise ValueError("ttl_s deve ser > 0")
    now = int(time.time())
    return EscrowBTCtoBRN(
        escrow_id=generate_escrow_id(),
        creator=creator,
        buyer=buyer,
        brn_amount=brn_amount,
        btc_expected_sats=btc_expected_sats,
        btc_address=btc_address,
        created_at=now,
        expires_at=now + ttl,
    )


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    print("Testando EscrowBTCtoBRN...")

    e = create_new(
        creator="BRN_TREASURY",
        buyer="brn1qtestaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        brn_amount=100_000,
        btc_expected_sats=10_000,
        btc_address="bc1qtest",
    )
    assert e.status == STATUS_OPEN
    assert e.escrow_id.startswith("esc_")
    assert e.is_open()
    assert not e.is_terminal()
    assert e.can_cancel()
    assert not e.can_release()
    print(f"  [ok] criado: {e}")

    d = e.to_dict()
    assert d["status"] == STATUS_OPEN
    e2 = EscrowBTCtoBRN.from_dict(d)
    assert e2 == e
    assert e2.to_dict() == d
    print("  [ok] to_dict/from_dict")

    e.mark_detected("deadbeef" * 8)
    assert e.status == STATUS_BTC_DETECTED
    assert e.is_detected()
    assert e.can_release()
    assert not e.can_cancel()
    print("  [ok] mark_detected")

    e.mark_released()
    assert e.is_released()
    assert e.is_terminal()
    print("  [ok] mark_released")

    e3 = EscrowBTCtoBRN(
        escrow_id=generate_escrow_id(),
        creator="X",
        buyer="brn1qtestbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        brn_amount=1,
        btc_expected_sats=1,
        btc_address="bc1qtest",
        created_at=100,
        expires_at=101,
    )
    assert e3.is_expired(now=101)
    e3.mark_expired()
    assert e3.status == STATUS_EXPIRED
    print("  [ok] mark_expired")

    e4 = create_new(
        creator="X",
        buyer="brn1qtestccccccccccccccccccccccccccccccc",
        brn_amount=1,
        btc_expected_sats=1,
        btc_address="bc1qtest",
    )
    e4.mark_cancelled()
    assert e4.status == STATUS_CANCELLED
    assert e4.is_terminal()
    print("  [ok] mark_cancelled")

    for bad in (
        dict(buyer="not_brn", brn_amount=1, btc_expected_sats=1),
        dict(buyer="brn1qtest", brn_amount=0, btc_expected_sats=1),
        dict(buyer="brn1qtest", brn_amount=1, btc_expected_sats=0),
    ):
        try:
            create_new(creator="X", btc_address="x", **bad)
            raise AssertionError(f"deveria rejeitar: {bad}")
        except ValueError:
            pass
    print("  [ok] validacoes de entrada")

    e5 = create_new(
        creator="X",
        buyer="brn1qtestddddddddddddddddddddddddddddddd",
        brn_amount=1,
        btc_expected_sats=1,
        btc_address="x",
    )
    try:
        e5.mark_released()
        raise AssertionError("deveria rejeitar mark_released em OPEN")
    except ValueError:
        pass
    try:
        e5.mark_detected("")
        raise AssertionError("deveria rejeitar txid vazio")
    except ValueError:
        pass
    print("  [ok] transicoes invalidas rejeitadas")

    print("EscrowBTCtoBRN OK.")
