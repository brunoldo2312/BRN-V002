"""db.py — Banco SQLite do BRN (v7.6 - Consenso Atômico & Hardened)
================================================================
v7.6 (2026-11-17) — MELHORIAS DE CONSENSO E ATOMICIDADE:
  [NEW] commit_block(block): grava bloco, txs, UTXOs e limpa mempool
        em uma única transação atômica com checagem estrita de continuidade.
  [FIX] Checagem rowcount == 1 no gasto de UTXO (bloqueia double-spend).
  [FIX] Substituído INSERT OR REPLACE por INSERT puro em blocks,
        transactions e utxos para evitar sobrescrita silenciosa em forks.
  [NEW] Coluna chainwork (hex 64 chars) em blocks com migração automática
        para suportar regra Heaviest Chain (maior trabalho acumulado).
  [KEEP] v7.5: Writer/Reader separados, WAL, fila de escrita, busy_timeout 30s.
================================================================
"""
from __future__ import annotations

__version__ = "7.6"

import os
import time
import zlib
import sqlite3
import threading
import queue
import concurrent.futures
import orjson
import json
import logging
import pathlib
import hashlib

log = logging.getLogger("db")

_ALLOW_CONTRACT_SPEND = os.environ.get("BRN_ALLOW_CONTRACT_SPEND", "0") == "1"

_BATCH_MAX_OPS      = int(os.environ.get("BRN_DB_BATCH_MAX", "50"))
_BATCH_MAX_WAIT_MS  = int(os.environ.get("BRN_DB_BATCH_WAIT_MS", "100"))
_QUEUE_MAX          = int(os.environ.get("BRN_DB_QUEUE_MAX", "2000"))
_QUEUE_PUT_TIMEOUT  = float(os.environ.get("BRN_DB_QUEUE_PUT_TIMEOUT", "5.0"))
_FUTURE_TIMEOUT     = float(os.environ.get("BRN_DB_FUTURE_TIMEOUT", "60.0"))


def _format_chainwork(work_int: int) -> str:
    """Formata trabalho acumulado como hex de 64 chars para ordenação lexicográfica."""
    return f"{max(0, int(work_int)):064x}"


class _WriteOp:
    __slots__ = ("fn", "args", "kwargs", "batch_safe", "future", "_pending_result")

    def __init__(self, fn, args, kwargs, batch_safe):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.batch_safe = batch_safe
        self.future = concurrent.futures.Future()
        self._pending_result = None


class ChainDB:
    def __init__(self, path: str):
        self.path = path

        # ─── Conexão do WRITER ───
        self.conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()

        # ─── Conexão do READER (separada e read-only) ───
        path_uri = pathlib.Path(path).resolve().as_posix()
        try:
            self.read_conn = sqlite3.connect(
                f"file:///{path_uri}?mode=ro", check_same_thread=False,
                isolation_level=None, timeout=30.0, uri=True,
            )
        except sqlite3.OperationalError:
            self.read_conn = sqlite3.connect(
                path, check_same_thread=False, isolation_level=None,
                timeout=30.0,
            )
        self.read_conn.row_factory = sqlite3.Row
        self.read_lock = threading.RLock()

        common_pragmas = (
            "PRAGMA temp_store=MEMORY",
            "PRAGMA cache_size=-64000",
            "PRAGMA mmap_size=268435456",
            "PRAGMA foreign_keys=ON",
            "PRAGMA busy_timeout=30000",
            "PRAGMA wal_autocheckpoint=1000",
            "PRAGMA journal_size_limit=67108864",
        )

        self.conn.execute("PRAGMA journal_mode=WAL")

        for p in common_pragmas:
            self.conn.execute(p)
            try:
                self.read_conn.execute(p)
            except sqlite3.OperationalError:
                pass

        self.conn.execute("PRAGMA synchronous=FULL")
        try:
            self.read_conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass

        # Schema + migrations rodam ANTES do Writer thread começar
        self._schema()
        self._migrate_raw_txs()
        self._migrate_chainwork()

        # Writer Queue
        self._write_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="DB-Writer",
        )
        self._writer_thread.start()

    # ============================================================
    # FILA DE ESCRITA
    # ============================================================
    def _enqueue(self, fn, *args, batch_safe: bool = False, **kwargs):
        op = _WriteOp(fn, args, kwargs, batch_safe)
        try:
            self._write_queue.put(op, timeout=_QUEUE_PUT_TIMEOUT)
        except queue.Full:
            raise RuntimeError(
                f"DB write queue cheia ({_QUEUE_MAX}); sistema sobrecarregado."
            )
        return op.future.result(timeout=_FUTURE_TIMEOUT)

    def _writer_loop(self) -> None:
        while True:
            try:
                first = self._write_queue.get()
            except Exception:
                continue
            if first is None:
                return
            batch = [first]
            deadline = time.time() + (_BATCH_MAX_WAIT_MS / 1000.0)
            while len(batch) < _BATCH_MAX_OPS:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    op = self._write_queue.get(timeout=remaining)
                    if op is None:
                        batch.append(None)
                        break
                    batch.append(op)
                except queue.Empty:
                    break
            shutdown = None in batch
            ops = [op for op in batch if op is not None]
            try:
                self._execute_ops(ops)
            except Exception as e:
                log.error(f"DB writer falhou: {e}")
            if shutdown:
                return

    def _execute_ops(self, ops: list) -> None:
        batch = []
        for op in ops:
            if op.batch_safe:
                batch.append(op)
            else:
                if batch:
                    self._flush_batch(batch)
                    batch = []
                self._exec_one(op)
        if batch:
            self._flush_batch(batch)

    def _flush_batch(self, batch: list) -> None:
        pending: list[tuple] = []
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for op in batch:
                try:
                    result = op.fn(*op.args, **op.kwargs)
                    pending.append((op, result, None))
                except Exception as e:
                    pending.append((op, None, e))
                    raise
            self.conn.execute("COMMIT")
            for op, result, exc in pending:
                if exc is not None:
                    op.future.set_exception(exc)
                else:
                    op.future.set_result(result)
        except Exception as e:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass

            done_before = len(pending)
            total = len(batch)
            log.warning(
                f"[db] batch de {total} ops desfeito por ROLLBACK "
                f"({done_before} executadas antes da falha): {e}"
            )
            for op, _result, _exc in pending:
                if not op.future.done():
                    op.future.set_exception(e)
            pending_ids = {id(op) for op, _, _ in pending}
            for op in batch:
                if id(op) not in pending_ids and not op.future.done():
                    op.future.set_exception(e)

    def _exec_one(self, op: _WriteOp) -> None:
        try:
            result = op.fn(*op.args, **op.kwargs)
            op.future.set_result(result)
        except Exception as e:
            op.future.set_exception(e)

    # ============================================================
    # SCHEMA & MIGRAÇÕES
    # ============================================================
    def _schema(self):
        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS blocks (
                height INTEGER PRIMARY KEY, hash TEXT UNIQUE NOT NULL,
                prev_hash TEXT NOT NULL, timestamp INTEGER NOT NULL,
                nonce INTEGER NOT NULL, merkle TEXT NOT NULL,
                difficulty INTEGER NOT NULL,
                chainwork TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000',
                raw BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_blocks_hash ON blocks(hash);
            CREATE INDEX IF NOT EXISTS idx_blocks_prev ON blocks(prev_hash);
            CREATE INDEX IF NOT EXISTS idx_blocks_work ON blocks(chainwork DESC);

            CREATE TABLE IF NOT EXISTS transactions (
                txid TEXT PRIMARY KEY, block_height INTEGER NOT NULL,
                raw BLOB
            );
            CREATE INDEX IF NOT EXISTS idx_tx_block ON transactions(block_height);

            CREATE TABLE IF NOT EXISTS utxos (
                txid TEXT NOT NULL, vout INTEGER NOT NULL,
                address TEXT NOT NULL, amount INTEGER NOT NULL,
                pubkey TEXT NOT NULL, block_height INTEGER NOT NULL,
                spent INTEGER NOT NULL DEFAULT 0, spent_by TEXT,
                PRIMARY KEY (txid, vout)
            );
            CREATE INDEX IF NOT EXISTS idx_utxo_addr ON utxos(address, spent);
            CREATE INDEX IF NOT EXISTS idx_utxo_spent ON utxos(spent);
            CREATE INDEX IF NOT EXISTS idx_utxo_h ON utxos(block_height);

            CREATE TABLE IF NOT EXISTS mempool (
                txid TEXT PRIMARY KEY, raw BLOB NOT NULL,
                fee INTEGER NOT NULL, received_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mp_fee ON mempool(fee DESC);
            CREATE INDEX IF NOT EXISTS idx_mp_time ON mempool(received_at);

            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS peers (
                address TEXT PRIMARY KEY NOT NULL, node_id TEXT NOT NULL,
                genesis_hash TEXT NOT NULL, version TEXT,
                height INTEGER DEFAULT 0, is_miner INTEGER DEFAULT 0,
                public_key TEXT, metadata TEXT,
                first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
                score INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_peers_last_seen ON peers(last_seen);
            CREATE INDEX IF NOT EXISTS idx_peers_genesis ON peers(genesis_hash);
            CREATE INDEX IF NOT EXISTS idx_peers_node_id ON peers(node_id);
            CREATE INDEX IF NOT EXISTS idx_peers_pubkey ON peers(public_key);

            CREATE TABLE IF NOT EXISTS network_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL, event_type TEXT NOT NULL,
                peer_address TEXT, details TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON network_events(timestamp DESC);

            CREATE TABLE IF NOT EXISTS contracts (
                contract_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                code TEXT NOT NULL, metadata TEXT, created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_contracts_owner ON contracts(owner);
            CREATE INDEX IF NOT EXISTS idx_contracts_created ON contracts(created_at DESC);

            CREATE TABLE IF NOT EXISTS contract_state (
                contract_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contract_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id TEXT NOT NULL, event TEXT NOT NULL,
                data TEXT, timestamp INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_contract_events_id
            ON contract_events(contract_id, timestamp DESC);

            CREATE TABLE IF NOT EXISTS l2_escrows (
                escrow_id TEXT PRIMARY KEY, creator TEXT, buyer TEXT,
                brn_amount INTEGER, btc_expected_sats INTEGER,
                btc_address TEXT, btc_txid TEXT, status TEXT,
                created_at INTEGER, expires_at INTEGER, l2_hash TEXT,
                raw_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_l2_status ON l2_escrows(status);
            CREATE INDEX IF NOT EXISTS idx_l2_buyer ON l2_escrows(buyer);

            CREATE TABLE IF NOT EXISTS btc_used (
                btc_txid TEXT PRIMARY KEY, escrow_id TEXT, used_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS l2_blocks (
                merkle_root TEXT PRIMARY KEY, timestamp INTEGER,
                count INTEGER, raw_json TEXT
            );

            CREATE TABLE IF NOT EXISTS l2_txs (
                txid TEXT PRIMARY KEY, type TEXT, escrow_id TEXT,
                raw_json TEXT, timestamp INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_l2tx_escrow ON l2_txs(escrow_id);
            """)

        # Migrations inline
        try:
            cols = self.conn.execute("PRAGMA table_info(blocks)").fetchall()
            names = {c["name"] for c in cols}
            if "chainwork" not in names:
                self.conn.execute(
                    "ALTER TABLE blocks ADD COLUMN chainwork TEXT NOT NULL "
                    "DEFAULT '0000000000000000000000000000000000000000000000000000000000000000'"
                )
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_blocks_work ON blocks(chainwork DESC)")
        except Exception:
            pass

        try:
            cols = self.conn.execute("PRAGMA table_info(peers)").fetchall()
            names = {c["name"] for c in cols}
            if "score" not in names:
                self.conn.execute("ALTER TABLE peers ADD COLUMN score INTEGER DEFAULT 0")
            if "public_key" not in names:
                self.conn.execute("ALTER TABLE peers ADD COLUMN public_key TEXT DEFAULT ''
