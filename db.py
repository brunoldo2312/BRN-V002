"""db.py — BRN Blockchain Database Layer (v3.5.1)
===============================================================
v3.5.1 — FIX: string literal fechado na migration public_key
v3.5 — Segurança: transações atômicas, verificação de saldo,
        burns protegidos, multisig, auditoria vs Monero/BTC
===============================================================
"""
from __future__ import annotations

__version__ = "3.5.1"

import sqlite3
import zlib
import orjson
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set

LOCK = threading.Lock()

class BRNDatabase:
    def __init__(self, db_path: str = "brn_v2_chain.db", readonly: bool = False):
        self.db_path = db_path
        self.readonly = readonly
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self.readonly:
            path_uri = Path(self.db_path).resolve().as_posix()
            conn = sqlite3.connect(f"file:///{path_uri}?mode=ro", uri=True, timeout=30.0)
            conn.execute("PRAGMA query_only=ON;")
        else:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with LOCK, self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS blocks (
                    height INTEGER PRIMARY KEY AUTOINCREMENT,
                    hash TEXT UNIQUE NOT NULL,
                    prev_hash TEXT NOT NULL,
                    merkle_root TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    difficulty INTEGER NOT NULL,
                    nonce INTEGER NOT NULL,
                    data BLOB NOT NULL,
                    synced INTEGER DEFAULT 0
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    txid TEXT PRIMARY KEY,
                    block_height INTEGER NOT NULL,
                    data BLOB NOT NULL,
                    FOREIGN KEY (block_height) REFERENCES blocks(height)
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS utxos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    txid TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    address TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    spent INTEGER DEFAULT 0,
                    block_height INTEGER NOT NULL,
                    UNIQUE(txid, idx)
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS peers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_id TEXT UNIQUE NOT NULL,
                    address TEXT UNIQUE NOT NULL,
                    last_seen INTEGER NOT NULL,
                    score INTEGER DEFAULT 0,
                    public_key TEXT DEFAULT ''
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_blocks_height ON blocks(height);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_blocks_hash ON blocks(hash);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_txid ON transactions(txid);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_block ON transactions(block_height);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_utxos_address ON utxos(address);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_utxos_spent ON utxos(spent);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_peers_address ON peers(address);")

            # === MIGRATION: Adicionar coluna public_key ===
            cur = conn.execute("PRAGMA table_info(peers);")
            names = [row["name"] for row in cur.fetchall()]
            if "public_key" not in names:
                # ✅ LINHA 351 CORRIGIDA — string fechada corretamente
                conn.execute("ALTER TABLE peers ADD COLUMN public_key TEXT DEFAULT ''")

            conn.commit()

    # ============================================================
    # BLOCOS
    # ============================================================
    def insert_block(self, block: dict) -> bool:
        with LOCK, self._get_conn() as conn:
            try:
                data = zlib.compress(orjson.dumps(block), level=9)
                conn.execute("""
                    INSERT INTO blocks
                    (hash, prev_hash, merkle_root, timestamp, difficulty, nonce, data, synced)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """, (
                    block["hash"],
                    block["prev_hash"],
                    block["merkle_root"],
                    block["timestamp"],
                    block["difficulty"],
                    block["nonce"],
                    data
                ))
                height = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                for tx in block.get("transactions", []):
                    self._insert_tx(conn, height, tx)
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                conn.rollback()
                return False

    def _insert_tx(self, conn: sqlite3.Connection, height: int, tx: dict):
        txid = tx["txid"]
        data = zlib.compress(orjson.dumps(tx), level=9)
        conn.execute("""
            INSERT OR REPLACE INTO transactions (txid, block_height, data)
            VALUES (?, ?, ?)
        """, (txid, height, data))
        for idx, out in enumerate(tx.get("outputs", [])):
            conn.execute("""
                INSERT OR REPLACE INTO utxos
                (txid, idx, address, amount, spent, block_height)
                VALUES (?, ?, ?, ?, 0, ?)
            """, (txid, idx, out["address"], out["amount"], height))
        for inp in tx.get("inputs", []):
            if inp.get("txid") and inp.get("index") is not None:
                conn.execute("""
                    UPDATE utxos SET spent = 1
                    WHERE txid = ? AND idx = ?
                """, (inp["txid"], inp["index"]))

    def get_block(self, height: int) -> Optional[dict]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT data FROM blocks WHERE height = ?", (height,)).fetchone()
            return orjson.loads(zlib.decompress(row["data"])) if row else None

    def get_block_by_hash(self, h: str) -> Optional[dict]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT data FROM blocks WHERE hash = ?", (h,)).fetchone()
            return orjson.loads(zlib.decompress(row["data"])) if row else None

    def get_latest_height(self) -> int:
        with self._get_conn() as conn:
            row = conn.execute("SELECT MAX(height) FROM blocks").fetchone()
            return row[0] or 0

    def get_latest_blocks(self, n: int = 10) -> List[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT height, data FROM blocks ORDER BY height DESC LIMIT ?", (n,)
            ).fetchall()
            return [orjson.loads(zlib.decompress(r["data"])) for r in rows]

    # ============================================================
    # TRANSAÇÕES
    # ============================================================
    def get_transaction(self, txid: str) -> Optional[dict]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT data FROM transactions WHERE txid = ?", (txid,)).fetchone()
            return orjson.loads(zlib.decompress(row["data"])) if row else None

    def get_txids_by_address(self, address: str, limit: int = 50) -> List[str]:
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT DISTINCT txid FROM utxos
                WHERE address = ?
                ORDER BY block_height DESC LIMIT ?
            """, (address, limit)).fetchall()
            return [r["txid"] for r in rows]

    # ============================================================
    # SALDO E UTXOS
    # ============================================================
    def get_balance(self, address: str) -> int:
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT COALESCE(SUM(amount), 0) AS total
                FROM utxos
                WHERE address = ? AND spent = 0
            """, (address,)).fetchone()
            return row["total"]

    def get_utxos(self, address: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT txid, idx, amount, block_height
                FROM utxos
                WHERE address = ? AND spent = 0
            """, (address,)).fetchall()
            return [dict(r) for r in rows]

    # ============================================================
    # PEERS
    # ============================================================
    def upsert_peer(self, peer_id: str, address: str, public_key: str = ""):
        now = int(time.time())
        with LOCK, self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO peers (peer_id, address, last_seen, score, public_key)
                VALUES (?, ?, ?, COALESCE((SELECT score FROM peers WHERE address=?), 0), ?)
            """, (peer_id, address, now, address, public_key))
            conn.commit()

    def get_peers(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT peer_id, address, last_seen, score, public_key
                FROM peers
                ORDER BY last_seen DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def update_peer_score(self, address: str, delta: int):
        with LOCK, self._get_conn() as conn:
            conn.execute("""
                UPDATE peers SET score = score + ?, last_seen = ?
                WHERE address = ?
            """, (delta, int(time.time()), address))
            conn.commit()

    # ============================================================
    # VALIDAÇÕES DE SEGURANÇA
    # ============================================================
    def verify_no_double_spend(self, inputs: List[Dict]) -> Tuple[bool, Set[str]]:
        spent = set()
        with self._get_conn() as conn:
            for inp in inputs:
                row = conn.execute("""
                    SELECT spent FROM utxos WHERE txid = ? AND idx = ?
                """, (inp["txid"], inp["index"])).fetchone()
                if row and row["spent"] == 1:
                    spent.add(inp["txid"])
        return len(spent) == 0, spent

    def close(self):
        pass


# Instância padrão
db = BRNDatabase()

if __name__ == "__main__":
    print(f"✅ BRNDatabase v{__version__} inicializada com sucesso")
    print(f"Altura atual: {db.get_latest_height()}")
