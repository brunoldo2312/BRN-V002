"""Validação estrutural compatível com o blockchain.py atual do BRN.

Este módulo não usa o modelo UTXOSet da antiga blockchain v4. A auditoria
confere a sequência persistida, hashes, Merkle, dificuldade e tags de rede.
A validação completa de transações/UTXOs acontece no Blockchain.validate_block
quando cada bloco é recebido, usando o estado de consenso daquele momento.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional



@dataclass
class ChainVerificationResult:
    valid: bool
    blocks_checked: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"{self.blocks_checked} blocos verificados"

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "blocks_checked": self.blocks_checked,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "summary": self.summary(),
        }


class ChainValidationError(ValueError):
    """Erro indicando inconsistência estrutural na cadeia."""


def _fail(result: ChainVerificationResult, message: str) -> ChainVerificationResult:
    result.valid = False
    result.errors.append(message)
    return result


def verify_chain(chain: Any) -> ChainVerificationResult:
    """Audita blocos armazenados no ChainDB, sem depender de chain.chain."""
    # Importação tardia evita ciclo quando blockchain.py conecta este módulo.
    import blockchain as brn_chain

    result = ChainVerificationResult(valid=True)
    db = getattr(chain, "db", None)
    if db is None or not callable(getattr(db, "height", None)) or not callable(
        getattr(db, "get_block", None)
    ):
        return _fail(result, "Objeto Blockchain sem ChainDB compatível.")

    try:
        tip_height = int(db.height())
        if tip_height < 0:
            return _fail(result, "Banco não contém blocos.")

        previous = None
        for height in range(tip_height + 1):
            block = db.get_block(height)
            if not isinstance(block, dict):
                return _fail(result, f"Bloco #{height} ausente ou inválido.")

            result.blocks_checked += 1
            if block.get("height") != height:
                return _fail(result, f"Altura inconsistente no bloco #{height}.")

            required = ("hash", "prev_hash", "timestamp", "nonce", "merkle", "difficulty", "transactions")
            if any(k not in block for k in required):
                return _fail(result, f"Campos obrigatórios ausentes no bloco #{height}.")

            if not isinstance(block["transactions"], list) or not block["transactions"]:
                return _fail(result, f"Bloco #{height} sem transações.")

            if not isinstance(block["timestamp"], int) or block["timestamp"] < 0:
                return _fail(result, f"Timestamp inválido no bloco #{height}.")
            if not isinstance(block["nonce"], int) or block["nonce"] < 0:
                return _fail(result, f"Nonce inválido no bloco #{height}.")

            difficulty = block["difficulty"]
            if not isinstance(difficulty, int) or not 1 <= difficulty <= 64:
                return _fail(result, f"Dificuldade inválida no bloco #{height}.")

            expected_prev = brn_chain.GENESIS_PREV if height == 0 else previous["hash"]
            if block["prev_hash"] != expected_prev:
                return _fail(result, f"prev_hash inválido no bloco #{height}.")

            if height > 0 and block["timestamp"] < previous["timestamp"]:
                return _fail(result, f"Timestamp retrocede no bloco #{height}.")

            txids = []
            for index, tx in enumerate(block["transactions"]):
                if not isinstance(tx, dict):
                    return _fail(result, f"Transação {index} inválida no bloco #{height}.")
                tid = tx.get("txid")
                if not isinstance(tid, str) or tid != brn_chain.txid(tx):
                    return _fail(result, f"txid inválido no bloco #{height}, transação {index}.")
                txids.append(tid)

            expected_merkle = brn_chain.compute_merkle_root(txids)
            if block["merkle"] != expected_merkle:
                return _fail(result, f"Merkle inválido no bloco #{height}.")

            expected_hash = brn_chain.block_hash(
                block["prev_hash"], block["merkle"], block["timestamp"],
                block["nonce"], difficulty,
            )
            if block["hash"] != expected_hash:
                return _fail(result, f"Hash inválido no bloco #{height}.")

            if not brn_chain.meets_difficulty(block["hash"], difficulty):
                return _fail(result, f"Prova de trabalho inválida no bloco #{height}.")

            if height == 0 and getattr(chain, "genesis_expected_hash", None):
                if block["hash"] != chain.genesis_expected_hash:
                    return _fail(result, "Hash do gênese diferente do esperado.")

            if getattr(chain, "require_auth_tag", False):
                secret = getattr(chain, "network_secret", "")
                if height > brn_chain.AUTH_TAG_START_HEIGHT:
                    tag = block.get("auth_tag", "")
                    if not secret or not brn_chain.verify_auth_tag(height, block["hash"], tag, secret):
                        return _fail(result, f"auth_tag inválida no bloco #{height}.")

            previous = block

        # A auditoria histórica é estrutural. A validação de assinaturas e
        # gastos UTXO é feita no ingresso do bloco, com o estado de consenso.
        result.warnings.append(
            "Auditoria estrutural concluída; transações/UTXOs históricos não foram reaplicados."
        )
        return result
    except Exception as exc:
        return _fail(result, f"Falha durante auditoria: {type(exc).__name__}: {exc}")


def validate_chain(chain_or_blockchain: Any, difficulty: Optional[int] = None,
                   check_pow: bool = True) -> bool:
    """Compatibilidade com chamadas antigas; audita o formato atual do BRN."""
    result = verify_chain(chain_or_blockchain)
    if not result.valid:
        raise ChainValidationError("; ".join(result.errors))
    return True


class ChainValidator:
    """Interface de compatibilidade para ferramentas que usam ChainValidator."""

    def __init__(self, difficulty: Optional[int] = None, **_kwargs):
        self.difficulty = difficulty

    def validate_chain(self, chain_or_blockchain: Any, difficulty: Optional[int] = None,
                       check_pow: bool = True) -> bool:
        return validate_chain(chain_or_blockchain, difficulty, check_pow)

    def check_chain(self, chain_or_blockchain: Any, difficulty: Optional[int] = None,
                    check_pow: bool = True):
        try:
            self.validate_chain(chain_or_blockchain, difficulty, check_pow)
            return True, None
        except Exception as exc:
            return False, str(exc)

    def audit_blockchain(self, blockchain: Any, check_pow: bool = True) -> dict:
        return verify_chain(blockchain).to_dict()
