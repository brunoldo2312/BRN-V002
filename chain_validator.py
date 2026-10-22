"""
chain_validator.py — Auditoria e validação da cadeia BRN.

Compatível com blockchain.py v4:
- Block
- Blockchain
- UTXOSet
- txid()
- validate_transaction()

Formato esperado para transações comuns:
{
    "inputs": [
        {
            "txid": "<64 caracteres hex>",
            "vout": 0,
            "pubkey": "<hex>",
            "signature": "<hex>"
        }
    ],
    "outputs": [
        {"amount": 123, "address": "..."}
    ],
    "locktime": 0
}

Formato esperado para coinbase:
{
    "coinbase": true,
    "outputs": [
        {"amount": 5000000000, "address": "..."}
    ]
}

A coinbase deve ser a primeira transação do bloco. Sua soma de outputs
não pode exceder o subsídio do bloco mais as taxas das transações.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from blockchain import (
    HALVING_INTERVAL,
    INITIAL_SUBSIDY,
    MAX_BLOCK_SIZE,
    UTXOSet,
    txid,
    validate_transaction,
)

log = logging.getLogger("chain_validator")

ZERO_HASH = "0" * 64
DEFAULT_MAX_FUTURE_SECONDS = 2 * 60 * 60


class ChainValidationError(ValueError):
    """Erro indicando que a cadeia ou um bloco é inválido."""


def _canonical_json(value: Any) -> bytes:
    """Serialização determinística usada para medir e validar blocos."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _double_sha256_hex(data: bytes) -> str:
    return hashlib.sha256(hashlib.sha256(data).digest()).hexdigest()


def block_hash_from_fields(block: Any) -> str:
    """Recalcula o hash do bloco sem confiar no valor armazenado em block.hash."""
    content = {
        "index": block.index,
        "prev_hash": block.prev_hash,
        "transactions": block.transactions,
        "timestamp": block.timestamp,
        "nonce": block.nonce,
    }
    return _double_sha256_hex(_canonical_json(content))


def block_to_dict(block: Any) -> Dict[str, Any]:
    """Converte um bloco em dicionário para validações e serialização."""
    return {
        "index": block.index,
        "prev_hash": block.prev_hash,
        "transactions": block.transactions,
        "timestamp": block.timestamp,
        "nonce": block.nonce,
        "hash": block.hash,
    }


def block_size(block: Any) -> int:
    """Retorna o tamanho serializado do bloco em bytes."""
    return len(_canonical_json(block_to_dict(block)))


def block_subsidy(height: int) -> int:
    """Calcula o subsídio por altura, aplicando os halvings configurados."""
    if not isinstance(height, int) or height < 0:
        raise ChainValidationError("Altura do bloco inválida.")

    halvings = height // HALVING_INTERVAL
    if halvings >= 64:
        return 0

    return INITIAL_SUBSIDY >> halvings


def _is_nonnegative_int(value: Any) -> bool:
    # bool é subclasse de int em Python e não deve ser aceito como valor.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_output(output: Any) -> int:
    if not isinstance(output, dict):
        raise ChainValidationError("Output deve ser um objeto JSON.")

    amount = output.get("amount")
    if not _is_nonnegative_int(amount):
        raise ChainValidationError("Valor de output inválido.")

    if amount == 0:
        raise ChainValidationError("Outputs de valor zero não são permitidos.")

    return amount


def _validate_coinbase(tx: Any) -> int:
    if not isinstance(tx, dict) or tx.get("coinbase") is not True:
        raise ChainValidationError("Transação coinbase inválida.")

    # Evita que uma coinbase carregue inputs comuns, que poderiam gerar
    # ambiguidade nas regras de gasto e assinatura.
    if tx.get("inputs") not in (None, []):
        raise ChainValidationError("Coinbase não pode conter inputs comuns.")

    outputs = tx.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise ChainValidationError("Coinbase precisa conter pelo menos um output.")

    return sum(_validate_output(output) for output in outputs)


def _validate_regular_tx_shape(tx: Any) -> Tuple[List[dict], List[dict], int]:
    if not isinstance(tx, dict):
        raise ChainValidationError("Transação deve ser um objeto JSON.")

    if tx.get("coinbase") is True:
        raise ChainValidationError("Coinbase não pode ser validada como transação comum.")

    inputs = tx.get("inputs")
    outputs = tx.get("outputs")

    if not isinstance(inputs, list) or not inputs:
        raise ChainValidationError("Transação comum precisa de inputs.")

    if not isinstance(outputs, list) or not outputs:
        raise ChainValidationError("Transação comum precisa de outputs.")

    seen_inputs = set()
    for inp in inputs:
        if not isinstance(inp, dict):
            raise ChainValidationError("Input deve ser um objeto JSON.")

        prev_txid = inp.get("txid")
        vout = inp.get("vout")

        if not isinstance(prev_txid, str) or len(prev_txid) != 64:
            raise ChainValidationError("txid de input inválido.")

        try:
            bytes.fromhex(prev_txid)
        except ValueError as exc:
            raise ChainValidationError("txid de input não é hexadecimal.") from exc

        if not _is_nonnegative_int(vout):
            raise ChainValidationError("Índice vout inválido.")

        outpoint = (prev_txid.lower(), vout)
        if outpoint in seen_inputs:
            raise ChainValidationError("A transação repete o mesmo input.")
        seen_inputs.add(outpoint)

        for field in ("pubkey", "signature"):
            value = inp.get(field)
            if not isinstance(value, str) or not value:
                raise ChainValidationError(f"Campo {field} ausente ou inválido.")
            try:
                bytes.fromhex(value)
            except ValueError as exc:
                raise ChainValidationError(
                    f"Campo {field} não é hexadecimal."
                ) from exc

    output_total = sum(_validate_output(output) for output in outputs)
    return inputs, outputs, output_total


class ChainValidator:
    """
    Validador independente da cadeia.

    O validador constrói um UTXO temporário durante a auditoria e não altera
    o UTXOSet pertencente ao objeto Blockchain.
    """

    def __init__(
        self,
        difficulty: Optional[int] = None,
        max_block_size: int = MAX_BLOCK_SIZE,
        max_future_seconds: int = DEFAULT_MAX_FUTURE_SECONDS,
        check_future_timestamps: bool = True,
    ) -> None:
        if difficulty is not None and (
            not isinstance(difficulty, int) or difficulty < 0
        ):
            raise ValueError("difficulty deve ser um inteiro não negativo.")

        if not isinstance(max_block_size, int) or max_block_size <= 0:
            raise ValueError("max_block_size deve ser um inteiro positivo.")

        if not isinstance(max_future_seconds, int) or max_future_seconds < 0:
            raise ValueError("max_future_seconds deve ser não negativo.")

        self.difficulty = difficulty
        self.max_block_size = max_block_size
        self.max_future_seconds = max_future_seconds
        self.check_future_timestamps = check_future_timestamps

    def validate_block_header(
        self,
        block: Any,
        expected_height: int,
        expected_prev_hash: str,
        previous_timestamp: Optional[int] = None,
        now: Optional[int] = None,
        check_pow: bool = True,
    ) -> None:
        if not isinstance(getattr(block, "index", None), int):
            raise ChainValidationError("Índice do bloco inválido.")

        if block.index != expected_height:
            raise ChainValidationError(
                f"Altura incorreta: esperado {expected_height}, recebido {block.index}."
            )

        if block.prev_hash != expected_prev_hash:
            raise ChainValidationError("prev_hash não corresponde ao bloco anterior.")

        if not isinstance(block.timestamp, int) or block.timestamp < 0:
            raise ChainValidationError("Timestamp do bloco inválido.")

        if previous_timestamp is not None and block.timestamp < previous_timestamp:
            raise ChainValidationError(
                "Timestamp do bloco anterior ao timestamp do bloco precedente."
            )

        if self.check_future_timestamps:
            current_time = int(time.time()) if now is None else now
            if block.timestamp > current_time + self.max_future_seconds:
                raise ChainValidationError("Timestamp do bloco está muito no futuro.")

        if not isinstance(block.nonce, int) or block.nonce < 0:
            raise ChainValidationError("Nonce inválido.")

        if not isinstance(block.transactions, list):
            raise ChainValidationError("Lista de transações do bloco inválida.")

        if block_size(block) > self.max_block_size:
            raise ChainValidationError("Bloco excede o limite de tamanho.")

        computed_hash = block_hash_from_fields(block)
        if not isinstance(block.hash, str) or block.hash != computed_hash:
            raise ChainValidationError("Hash armazenado não corresponde ao conteúdo.")

        if check_pow and self.difficulty is not None:
            target_prefix = "0" * self.difficulty
            if not computed_hash.startswith(target_prefix):
                raise ChainValidationError("Prova de trabalho inválida.")

    def _validate_genesis(self, block: Any) -> None:
        """
        Valida o formato de gênese usado por blockchain.py v4.

        O código original cria um bloco gênese especial, sem mineração e com
        uma transação informativa {"info": "BRN Genesis Block"}.
        """
        if block.index != 0:
            raise ChainValidationError("O primeiro bloco deve ter índice zero.")

        if block.prev_hash != ZERO_HASH:
            raise ChainValidationError("prev_hash do gênese inválido.")

        if not isinstance(block.transactions, list) or not block.transactions:
            raise ChainValidationError("Bloco gênese sem transações.")

        if block.timestamp < 0 or not isinstance(block.timestamp, int):
            raise ChainValidationError("Timestamp do gênese inválido.")

        if block.nonce != 0:
            raise ChainValidationError("Nonce inesperado no bloco gênese.")

        computed_hash = block_hash_from_fields(block)
        if block.hash != computed_hash:
            raise ChainValidationError("Hash do bloco gênese inválido.")

    def _apply_regular_transaction(self, tx: dict, utxo_set: UTXOSet) -> int:
        inputs, _outputs, output_total = _validate_regular_tx_shape(tx)

        # Verifica que os inputs existem antes de chamar a validação
        # criptográfica. Também soma o valor dos inputs para calcular taxas.
        input_total = 0
        for inp in inputs:
            prev_out = utxo_set.get(inp["txid"], inp["vout"])
            if not isinstance(prev_out, dict):
                raise ChainValidationError(
                    f"UTXO ausente: {inp['txid']}:{inp['vout']}."
                )

            amount = prev_out.get("amount")
            if not _is_nonnegative_int(amount) or amount == 0:
                raise ChainValidationError("Valor inválido no UTXO anterior.")

            input_total += amount

        if output_total > input_total:
            raise ChainValidationError("Outputs excedem o total dos inputs.")

        # validate_transaction usa a implementação de assinatura do módulo
        # crypto e verifica a transação contra o UTXOSet.
        if not validate_transaction(tx, utxo_set):
            raise ChainValidationError("Transação ou assinatura inválida.")

        # Consome os inputs só depois de validar toda a transação.
        for inp in inputs:
            utxo_set.spend(inp["txid"], inp["vout"])

        # Os outputs são adicionados usando o txid definido pelo blockchain.py.
        utxo_set.add(tx)

        return input_total - output_total

    def validate_block(
        self,
        block: Any,
        expected_height: int,
        expected_prev_hash: str,
        utxo_set: UTXOSet,
        previous_timestamp: Optional[int] = None,
        now: Optional[int] = None,
        check_pow: bool = True,
    ) -> int:
        """
        Valida um bloco e atualiza o UTXOSet recebido.

        Retorna a quantidade de taxas de transação do bloco.
        A alteração do UTXOSet deve ocorrer sobre uma cópia temporária se o
        chamador precisar garantir rollback em caso de falha posterior.
        """
        self.validate_block_header(
            block=block,
            expected_height=expected_height,
            expected_prev_hash=expected_prev_hash,
            previous_timestamp=previous_timestamp,
            now=now,
            check_pow=check_pow,
        )

        if not block.transactions:
            raise ChainValidationError("Bloco sem transações.")

        coinbase_flags = [
            isinstance(tx, dict) and tx.get("coinbase") is True
            for tx in block.transactions
        ]

        if any(coinbase_flags[1:]):
            raise ChainValidationError("Coinbase só pode aparecer uma vez e primeiro.")

        if not coinbase_flags[0]:
            raise ChainValidationError("A primeira transação deve ser coinbase.")

        fees = 0
        block_txids = set()

        # Transações comuns são aplicadas em ordem: outputs de transações
        # anteriores do mesmo bloco podem ser gastas depois, mas não antes.
        for tx in block.transactions[1:]:
            tid = txid(tx)
            if tid in block_txids:
                raise ChainValidationError("txid duplicado no mesmo bloco.")
            block_txids.add(tid)

            fees += self._apply_regular_transaction(tx, utxo_set)

        coinbase_total = _validate_coinbase(block.transactions[0])
        max_reward = block_subsidy(expected_height) + fees

        if coinbase_total > max_reward:
            raise ChainValidationError(
                f"Recompensa coinbase excede o permitido: "
                f"{coinbase_total} > {max_reward}."
            )

        coinbase_txid = txid(block.transactions[0])
        if coinbase_txid in block_txids:
            raise ChainValidationError("txid da coinbase duplicado no bloco.")

        # Registra a coinbase após as transações comuns para impedir que ela
        # seja gasta dentro do próprio bloco.
        utxo_set.add(block.transactions[0])

        return fees

    def validate_chain(
        self,
        chain_or_blockchain: Any,
        difficulty: Optional[int] = None,
        check_pow: bool = True,
    ) -> bool:
        """
        Valida toda a cadeia e retorna True se for válida.

        Aceita:
        - um objeto Blockchain com atributo .chain;
        - diretamente uma lista de blocos.

        Lança ChainValidationError em caso de inconsistência.
        """
        chain = getattr(chain_or_blockchain, "chain", chain_or_blockchain)

        if not isinstance(chain, list) or not chain:
            raise ChainValidationError("A cadeia está vazia ou possui formato inválido.")

        effective_difficulty = self.difficulty
        if difficulty is not None:
            if not isinstance(difficulty, int) or difficulty < 0:
                raise ChainValidationError("Dificuldade inválida.")
            effective_difficulty = difficulty

        validator = ChainValidator(
            difficulty=effective_difficulty,
            max_block_size=self.max_block_size,
            max_future_seconds=self.max_future_seconds,
            check_future_timestamps=self.check_future_timestamps,
        )

        validator._validate_genesis(chain[0])

        temp_utxo = UTXOSet()

        # O gênese do blockchain.py v4 é informativo, não cria UTXOs.
        for height in range(1, len(chain)):
            block = chain[height]
            previous = chain[height - 1]

            validator.validate_block(
                block=block,
                expected_height=height,
                expected_prev_hash=previous.hash,
                utxo_set=temp_utxo,
                previous_timestamp=previous.timestamp,
                check_pow=check_pow,
            )

        return True

    def check_chain(
        self,
        chain_or_blockchain: Any,
        difficulty: Optional[int] = None,
        check_pow: bool = True,
    ) -> Tuple[bool, Optional[str]]:
        """Versão conveniente que retorna (válida, motivo), sem propagar erro."""
        try:
            self.validate_chain(
                chain_or_blockchain,
                difficulty=difficulty,
                check_pow=check_pow,
            )
            return True, None
        except Exception as exc:
            return False, str(exc)

    def audit_blockchain(
        self,
        blockchain: Any,
        check_pow: bool = True,
    ) -> Dict[str, Any]:
        """Retorna um relatório simples de auditoria."""
        difficulty = getattr(blockchain, "difficulty", self.difficulty)
        valid, error = self.check_chain(
            blockchain,
            difficulty=difficulty,
            check_pow=check_pow,
        )

        chain = getattr(blockchain, "chain", [])
        return {
            "valid": valid,
            "height": len(chain) - 1 if chain else None,
            "blocks_checked": len(chain),
            "difficulty": difficulty,
            "error": error,
        }


def validate_chain(
    chain_or_blockchain: Any,
    difficulty: Optional[int] = None,
    check_pow: bool = True,
) -> bool:
    """Atalho funcional para validar uma cadeia."""
    validator = ChainValidator(difficulty=difficulty)
    return validator.validate_chain(
        chain_or_blockchain,
        difficulty=difficulty,
        check_pow=check_pow,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Audita uma cadeia BRN serializada em JSON."
    )
    parser.add_argument(
        "json_file",
        help="Arquivo JSON contendo uma lista de blocos.",
    )
    parser.add_argument(
        "--difficulty",
        type=int,
        default=4,
        help="Número de zeros hexadecimais exigidos no início do hash.",
    )
    parser.add_argument(
        "--skip-pow",
        action="store_true",
        help="Pula a checagem de prova de trabalho.",
    )
    args = parser.parse_args()

    try:
        with open(args.json_file, "r", encoding="utf-8") as file:
            raw_chain = json.load(file)

        # O CLI espera os campos usados pela classe Block.
        from blockchain import Block

        blocks = []
        for item in raw_chain:
            block = Block(
                index=item["index"],
                prev_hash=item["prev_hash"],
                transactions=item["transactions"],
                timestamp=item["timestamp"],
            )
            block.nonce = item["nonce"]
            block.hash = item["hash"]
            blocks.append(block)

        validator = ChainValidator(difficulty=args.difficulty)
        report = validator.audit_blockchain(
            type("BlockchainView", (), {
                "chain": blocks,
                "difficulty": args.difficulty,
            })(),
            check_pow=not args.skip_pow,
        )

        print(json.dumps(report, indent=2, ensure_ascii=False))
        raise SystemExit(0 if report["valid"] else 1)

    except Exception as exc:
        print(json.dumps({
            "valid": False,
            "error": str(exc),
        }, indent=2, ensure_ascii=False))
        raise SystemExit(1)
