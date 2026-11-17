# db.py — Banco de Dados SQLite para o Nó BRN
# Versão: 9.6.3 | Data: 2026-11-17
# ============================================================
# Correções v9.6.3:
#   • [FIX CRÍTICO] Adicionado get_meta / set_meta / delete_meta /
#     get_all_meta — blockchain.py faz db.get_meta() e quebrava com
#     "'ChainDB' object has no attribute 'get_meta'"
#   • [KEEP] v9.6.2: ChainDB, tabela blocks, VACUUM fora da transação,
#     truncate_to() real, bootstrap_peers, close() resetando singleton
# ============================================================
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import logging

logger = logging.getLogger("db")

class Database:
    _instance: Optional["Database"] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, db_path: str = None):
        if getattr(self, "_initialized", False):
            return
        self.db_path = self._resolve_path(db_path)
        self._write_lock = threading.Lock()
        self._initialized = True
        self._init_database()

    def _resolve_path(self, db_path: str = None) -> str:
        base_dir = Path(__file__).resolve().parent
        if db_path:
            path = Path(db_path)
            if path.is_absolute():
                return str(path)
            return str(base_dir / db_path)
        return str(base_dir / os.environ.get("BRN_DB", "brn_v2_chain.db"))

    @contextmanager
    def _get_connection(self, write: bool = False):
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
        )
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE;")
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            conn.close()

    def _vacuum(self):
        """VACUUM tem que rodar SEM transação aberta."""
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 30000;")
            conn.execute("VACUUM")
        finally:
            conn.close()

    def _init_database(self):
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._get_connection(write=True) as conn:
            self._create_tables(conn)
            self._migrate_schema(conn)
        logger.info("[DB] Inicializado em: %s", self.db_path)

    def _create_tables(self, conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                height INTEGER PRIMARY KEY,
                hash TEXT NOT NULL DEFAULT '',
                prev_hash TEXT NOT NULL DEFAULT '',
                timestamp REAL NOT NULL DEFAULT 0,
                data TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS peers (
                node_id TEXT PRIMARY KEY,
                address TEXT NOT NULL UNIQUE,
                genesis_hash TEXT NOT NULL DEFAULT '',
                version TEXT NOT NULL DEFAULT '',
                height INTEGER NOT NULL DEFAULT 0,
                is_miner INTEGER NOT NULL DEFAULT 0,
                public_key TEXT NOT NULL DEFAULT '',
                last_seen REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bootstrap_peers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL UNIQUE,
                priority INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_status (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mining_gate (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                gate_open INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                opened_at REAL,
                closed_at REAL,
                updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_peers_height ON peers(height);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_peers_last_seen ON peers(last_seen DESC);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_blocks_hash ON blocks(hash);")
        conn.execute("INSERT OR IGNORE INTO mining_gate (id) VALUES (1)")

    def _migrate_schema(self, conn: sqlite3.Connection):
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "blocks" not in tables:
            logger.info("[DB MIGRATION] Criando tabela blocks (faltava no DB antigo)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS blocks (
                    height INTEGER PRIMARY KEY,
                    hash TEXT NOT NULL DEFAULT '',
                    prev_hash TEXT NOT NULL DEFAULT '',
                    timestamp REAL NOT NULL DEFAULT 0,
                    data TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_blocks_hash ON blocks(hash);")

        cursor = conn.execute("PRAGMA table_info(peers)")
        columns = [row[1] for row in cursor.fetchall()]
        if "genesis_hash" not in columns:
            logger.info("[DB MIGRATION] Adicionando coluna genesis_hash")
            conn.execute("ALTER TABLE peers ADD COLUMN genesis_hash TEXT NOT NULL DEFAULT ''")
        if "version" not in columns:
            logger.info("[DB MIGRATION] Adicionando coluna version")
            conn.execute("ALTER TABLE peers ADD COLUMN version TEXT NOT NULL DEFAULT ''")
        if "public_key" not in columns:
            logger.info("[DB MIGRATION] Adicionando coluna public_key")
            conn.execute("ALTER TABLE peers ADD COLUMN public_key TEXT NOT NULL DEFAULT ''")

    # ============================================================
    # BLOCKS
    # ============================================================
    def save_block(self, height: int, hash_: str = "", prev_hash: str = "",
                    timestamp: float = 0, data: str = "{}") -> None:
        with self._write_lock, self._get_connection(write=True) as conn:
            conn.execute("""
                INSERT INTO blocks (height, hash, prev_hash, timestamp, data)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(height) DO UPDATE SET
                    hash = excluded.hash,
                    prev_hash = excluded.prev_hash,
                    timestamp = excluded.timestamp,
                    data = excluded.data
            """, (int(height), hash_, prev_hash, float(timestamp or 0), data))

    def get_block(self, height: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM blocks WHERE height = ?", (int(height),)).fetchone()
            return dict(row) if row else None

    def list_blocks(self, start: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM blocks WHERE height >= ? ORDER BY height ASC LIMIT ?",
                (int(start), int(limit)))
            return [dict(r) for r in cur.fetchall()]

    # ============================================================
    # PEERS
    # ============================================================
    def upsert_peer(self, node_id: str, address: str, genesis_hash: str = "",
                    version: str = "", height: int = 0, is_miner: bool = False,
                    public_key: str = "") -> None:
        now = time.time()
        with self._write_lock, self._get_connection(write=True) as conn:
            conn.execute("""
                INSERT INTO peers (
                    node_id, address, genesis_hash, version,
                    height, is_miner, public_key, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    address = excluded.address,
                    genesis_hash = excluded.genesis_hash,
                    version = excluded.version,
                    height = excluded.height,
                    is_miner = excluded.is_miner,
                    public_key = excluded.public_key,
                    last_seen = excluded.last_seen
            """, (node_id, address, genesis_hash, version,
                  height, int(is_miner), public_key, now))

    def listar_peers(self, apenas_ativos: bool = True, max_age_s: int = 300) -> List[Dict[str, Any]]:
        now = time.time()
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM peers ORDER BY last_seen DESC")
            peers = []
            for row in cursor.fetchall():
                peer = dict(row)
                peer["is_miner"] = bool(peer["is_miner"])
                peer["age_s"] = int(now - peer["last_seen"]) if peer["last_seen"] > 0 else -1
                if apenas_ativos and peer["age_s"] > max_age_s:
                    continue
                peers.append(peer)
            return peers

    def remover_peer(self, node_id: str) -> bool:
        with self._write_lock, self._get_connection(write=True) as conn:
            cur = conn.execute("DELETE FROM peers WHERE node_id = ?", (node_id,))
            return cur.rowcount > 0

    def limpar_peers_antigos(self, max_age_s: int = 900) -> int:
        limite = time.time() - max_age_s
        with self._write_lock, self._get_connection(write=True) as conn:
            cur = conn.execute("DELETE FROM peers WHERE last_seen < ?", (limite,))
            count = cur.rowcount
            if count > 0:
                logger.info("[DB] Removidos %d peers inativos (>%ds)", count, max_age_s)
            return count

    # ============================================================
    # BOOTSTRAP PEERS
    # ============================================================
    def add_bootstrap_peer(self, address: str, priority: int = 0) -> None:
        with self._write_lock, self._get_connection(write=True) as conn:
            conn.execute("""
                INSERT INTO bootstrap_peers (address, priority, enabled)
                VALUES (?, ?, 1)
                ON CONFLICT(address) DO UPDATE SET priority = excluded.priority, enabled = 1
            """, (address, int(priority)))

    def listar_bootstrap_peers(self, only_enabled: bool = True) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            q = "SELECT * FROM bootstrap_peers"
            if only_enabled:
                q += " WHERE enabled = 1"
            q += " ORDER BY priority DESC, id ASC"
            return [dict(r) for r in conn.execute(q).fetchall()]

    def remover_bootstrap_peer(self, address: str) -> bool:
        with self._write_lock, self._get_connection(write=True) as conn:
            cur = conn.execute("DELETE FROM bootstrap_peers WHERE address = ?", (address,))
            return cur.rowcount > 0

    def limpar_bootstrap_peers(self) -> int:
        with self._write_lock, self._get_connection(write=True) as conn:
            cur = conn.execute("DELETE FROM bootstrap_peers")
            return cur.rowcount

    # ============================================================
    # SYNC STATUS
    # ============================================================
    def set_sync_value(self, key: str, value: str):
        now = time.time()
        with self._write_lock, self._get_connection(write=True) as conn:
            conn.execute("""
                INSERT INTO sync_status (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?
            """, (key, value, now, value, now))

    def get_sync_value(self, key: str, default: str = None) -> Optional[str]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT value FROM sync_status WHERE key = ?", (key,))
            row = cur.fetchone()
            return row[0] if row else default

    # ============================================================
    # META — [FIX v9.6.3] É O QUE O blockchain.py USA
    # Usa a mesma tabela sync_status, só com nome que o blockchain espera.
    # Aceita qualquer tipo: str / int / float / bool / dict / list
    # ============================================================
    def set_meta(self, key: str, value: Any) -> None:
        import json as _json
        if isinstance(value, (dict, list)):
            v = _json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            v = "1" if value else "0"
        elif value is None:
            v = ""
        else:
            v = str(value)
        self.set_sync_value(str(key), v)

    def get_meta(self, key: str, default: Any = None) -> Any:
        import json as _json
        v = self.get_sync_value(str(key), None)
        if v is None:
            return default
        # Tenta devolver no tipo original: JSON / int / float / bool
        s = v.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return _json.loads(s)
            except Exception:
                return v
        if s in ("1", "0") and default is True or default is False:
            return s == "1"
        try:
            if "." in s:
                return float(s)
            return int(s)
        except (ValueError, TypeError):
            return v

    def delete_meta(self, key: str) -> bool:
        with self._write_lock, self._get_connection(write=True) as conn:
            cur = conn.execute("DELETE FROM sync_status WHERE key = ?", (str(key),))
            return cur.rowcount > 0

    def get_all_meta(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT key, value FROM sync_status").fetchall()
            return {k: self.get_meta(k) for k, _v in rows for k in [k]}

    # Aliases — caso blockchain.py use outro nome
    def get_metadata(self, key: str, default: Any = None) -> Any:
        return self.get_meta(key, default)

    def set_metadata(self, key: str, value: Any) -> None:
        return self.set_meta(key, value)

    # ============================================================
    # MINING GATE
    # ============================================================
    def abrir_mining_gate(self, reason: str = ""):
        now = time.time()
        with self._write_lock, self._get_connection(write=True) as conn:
            conn.execute("""
                UPDATE mining_gate
                SET gate_open = 1, reason = ?, opened_at = ?, updated_at = ?, closed_at = NULL
                WHERE id = 1 AND gate_open = 0
            """, (reason, now, now))
            if conn.total_changes > 0:
                logger.info("[MINING-GATE] ✅ ABERTO — %s", reason)

    def fechar_mining_gate(self, reason: str = ""):
        now = time.time()
        with self._write_lock, self._get_connection(write=True) as conn:
            conn.execute("""
                UPDATE mining_gate
                SET gate_open = 0, reason = ?, closed_at = ?, updated_at = ?
                WHERE id = 1 AND gate_open = 1
            """, (reason, now, now))
            if conn.total_changes > 0:
                logger.info("[MINING-GATE] 🔒 FECHADO — %s", reason)

    def get_mining_gate(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM mining_gate WHERE id = 1").fetchone()
            if row:
                d = dict(row)
                d["gate_open"] = bool(d["gate_open"])
                return d
            return {"gate_open": False, "reason": "", "opened_at": None,
                    "closed_at": None, "updated_at": 0}

    # ============================================================
    # TRUNCATE / PRUNE (Fast-Sync & HTTP Re-Sync)
    # ============================================================
    def height(self) -> int:
        with self._get_connection() as conn:
            row = conn.execute("SELECT MAX(height) FROM blocks").fetchone()
            return int(row[0]) if row and row[0] is not None else -1

    def truncate_to(self, target_height: int) -> bool:
        if target_height < 0:
            logger.error("[DB] truncate_to: altura alvo inválida: %d", target_height)
            return False
        current = self.height()
        if current <= 0:
            logger.warning("[DB] truncate_to: DB vazio — nada a fazer")
            return True
        if target_height >= current:
            logger.info("[DB] truncate_to: %d >= %d — nada a remover", target_height, current)
            return True
        logger.warning("[DB] TRUNCANDO blocos > %d (atual: %d)", target_height, current)
        try:
            with self._write_lock, self._get_connection(write=True) as conn:
                conn.execute("DELETE FROM blocks WHERE height > ?", (target_height,))
                removed = conn.total_changes
            self._vacuum()
            logger.info("[DB] Removidos %d blocos. Nova altura: %d", removed, self.height())
            return True
        except Exception as e:
            logger.error("[DB] truncate_to falhou: %s", e)
            return False

    def remove_blocks_above(self, target_height: int) -> bool:
        return self.truncate_to(target_height)

    def prune_above(self, target_height: int) -> bool:
        return self.truncate_to(target_height)

    def reset_to_height(self, target_height: int) -> bool:
        return self.truncate_to(target_height)

    # ============================================================
    # FECHAMENTO
    # ============================================================
    def close(self):
        Database._instance = None
        self._initialized = False
        logger.info("[DB] instância fechada")

# ============================================================
# COMPATIBILIDADE — ChainDB
# main.py / blockchain.py / version.py usam:
#   from db import ChainDB
# ============================================================
class ChainDB(Database):
    """ChainDB é o Database. Mesma classe, nome que o main.py espera."""

    def add_block(self, height: int, hash_: str = "", prev_hash: str = "",
                  timestamp: float = 0, data: str = "{}"):
        return self.save_block(height, hash_, prev_hash, timestamp, data)

    def get_block_by_height(self, height: int):
        return self.get_block(height)

    def get_last_block(self):
        h = self.height()
        return self.get_block(h) if h >= 0 else None

    def get_height(self) -> int:
        return self.height()

    def get_chain_height(self) -> int:
        return self.height()

# Aliases para qualquer import antigo
DB = Database
ChainDatabase = Database

def get_db(db_path: str = None) -> ChainDB:
    return ChainDB(db_path)

def get_chain_db(db_path: str = None) -> ChainDB:
    return ChainDB(db_path)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    db = get_db()
    print(f"✅ Banco inicializado em: {db.db_path}")
    print(f"📊 Altura atual: {db.height()}")
    print(f"👥 Peers ativos: {len(db.listar_peers())}")
    print(f"🔓 Mining Gate: {'ABERTO' if db.get_mining_gate()['gate_open'] else 'FECHADO'}")
    print(f"🌐 Bootstrap peers: {len(db.listar_bootstrap_peers())}")
