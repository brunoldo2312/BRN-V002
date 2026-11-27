# db.py — Banco de Dados SQLite para o Nó BRN
# Versão: 9.9.0 | Data: 2026-11-27
# ============================================================
# v9.9.0 (2026-11-27) — P0-1 + P0-4 (relatório de escalabilidade):
#   [NEW] apply_block_atomic(block): UMA transação SQLite por bloco.
#         Antes, accept_block fazia add_block + N×apply_tx +
#         N×remove_mempool, gerando ~3N commits curtos (~1.500 em
#         bloco cheio). Agora: BEGIN IMMEDIATE -> bloco -> todas as
#         txs -> mempool -> tx_index -> COMMIT. Rollback desfaz tudo.
#   [NEW] Pragmas P0-4 em TODA conexão:
#           cache_size=-64000, mmap_size=268435456,
#           temp_store=MEMORY, wal_autocheckpoint=1000,
#           journal_size_limit=67108864, synchronous=NORMAL.
#   [NEW] Conexões de leitura tentam mode=ro (URI) com fallback.
#   [NEW] Índices: mempool(added_at), tx_index(height), utxos(height).
#   [KEEP] 100% da API v9.8.2: apply_tx/add_block/save_block/
#          remove_mempool individuais continuam funcionando (usados
#          por mine_block e por código legado).
#   [KEEP] _ConnAdapter, _FakeCursor, WAL, migração por inspeção,
#          peers.score, bootstrap_peers, mining_gate, sync_status,
#          L2 escrows, contracts, truncate_to, peers helpers.
# ============================================================
# v9.8.2 (2026-11-18) — FIX CRÍTICO: ORDEM DE CRIAÇÃO DOS ÍNDICES
#   [FIX] _create_tables NÃO cria mais idx_peers_score. Em DB antigo
#         (sem coluna score), o CREATE INDEX falhava com
#         "no such column: score" e derrubava o boot inteiro
#         (Blockchain.__init__ -> _init_database -> _create_tables).
#         Agora o índice é criado SÓ em _migrate_schema, DEPOIS do
#         ALTER TABLE peers ADD COLUMN score.
#   [FIX] Todos os CREATE INDEX em _create_tables estão envoltos em
#         try/except defensivo: se um índice falhar (schema muito
#         antigo), o nó continua em vez de morrer.
#   [KEEP] Todos os métodos v9.8.1 preservados (get_peer_score,
#          set_peer_score, bump_peer_score, count_utxos, get_utxos,
#          get_blocks_range, contar_peers, get_tx_by_txid, etc).
# ============================================================
# v9.8.1 (2026-11-18) — DESBLOQUEIA O P2P (get_peer_score):
#   [FIX CRÍTICO] get_peer_score()              → p2p_unified.py / p2p_auth.py
#   [FIX CRÍTICO] set_peer_score()              → (usado por rate limiting)
#   [FIX CRÍTICO] bump_peer_score(delta)        → (ajuste incremental)
#   [NEW]  Coluna 'score REAL DEFAULT 0' na tabela peers.
#   [NEW]  Migração adiciona 'score' em DBs antigos (ALTER TABLE).
#   [NEW]  upsert_peer(..., score=None) preserva score existente.
#   [NEW]  Índice idx_peers_score para ordenação rápida.
# ============================================================
# v9.8.0 (2026-11-18) — COMPLETA OS MÉTODOS QUE O SERVER.PY USA:
#   count_utxos / get_utxos / contar_peers / get_mempool_tx /
#   get_tx_by_txid / get_txs_for_address / get_blocks_range /
#   listar_peers(janela=...)  → todos adicionados.
# ============================================================
# v9.7.0 — COMPATIBILIDADE TOTAL COM blockchain.py 9.5.9 + l2_manager.py v1.2:
#   BLOCKS / UTXO / MEMPOOL / TX INDEX / CONTRATOS / L2 ESCROW /
#   META / COMPAT db.lock+db.conn / TRUNCATE recalcula nonces /
#   ChainDB + peers + bootstrap_peers + mining_gate + WAL.
# ============================================================
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import logging

logger = logging.getLogger("db")

__version__ = "9.9.0"


# ------------------------------------------------------------
# Cursor fake para db.conn.execute() usado em cumulative_work()
# ------------------------------------------------------------
class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _ConnAdapter:
    """db.conn.execute(...) abre conexão curta, executa e fecha."""

    def __init__(self, db):
        self._db = db

    def execute(self, sql, params=()):
        with self._db._get_connection() as conn:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
            out = []
            for r in rows:
                try:
                    out.append(dict(r))
                except Exception:
                    out.append(r)
            return _FakeCursor(out)


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
        # blockchain.py usa self.db.lock
        self.lock = self._write_lock
        # blockchain.py usa self.db.conn.execute(...)
        self.conn = _ConnAdapter(self)
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

    # ============================================================
    # [P0-4] Conexão com pragmas + mode=ro para leitura
    # ============================================================
    @contextmanager
    def _get_connection(self, write: bool = False):
        """Abre uma conexão SQLite com os pragmas P0-4.

        Leitura: tenta mode=ro (URI). Se o arquivo ainda não existir ou
        o SQLite recusar (ex.: WAL ativo e -wal ainda vazio em alguns
        builds), cai no modo normal sem quebrar chamadores.
        Escrita: modo normal + BEGIN IMMEDIATE explícito.
        """
        conn = None
        if not write:
            try:
                uri = f"file:{Path(self.db_path).as_posix()}?mode=ro"
                conn = sqlite3.connect(uri, uri=True, timeout=30.0,
                                       isolation_level=None)
                # Verifica se o modo ro foi de fato aplicado; se o SQLite
                # abriu RW (fallback interno), segue normal.
                conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            except Exception:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                conn = None
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30.0,
                                   isolation_level=None)

        conn.row_factory = sqlite3.Row

        # [P0-4] Pragmas de sessão
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA busy_timeout = 30000;")
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA cache_size = -64000;")          # ~64 MB
            conn.execute("PRAGMA mmap_size = 268435456;")        # 256 MB
            conn.execute("PRAGMA temp_store = MEMORY;")
            conn.execute("PRAGMA wal_autocheckpoint = 1000;")
            conn.execute("PRAGMA journal_size_limit = 67108864;")  # 64 MB
        except Exception as e:
            logger.debug("[DB] pragma falhou (%s): %s", "ro" if not write else "rw", e)

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
            try:
                conn.close()
            except Exception:
                pass

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
            # [v9.8.2] Cria TODOS os índices APÓS migração, garantindo
            # que as colunas existem. try/except por índice: um falhando
            # não derruba o boot.
            self._create_indexes(conn)
        logger.info("[DB] Inicializado em: %s", self.db_path)

    def _create_tables(self, conn: sqlite3.Connection):
        # [v9.8.2] SEM CREATE INDEX aqui. Todos os índices são criados
        # em _create_indexes(), chamado APÓS _migrate_schema().
        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                height INTEGER PRIMARY KEY,
                hash TEXT NOT NULL DEFAULT '',
                prev_hash TEXT NOT NULL DEFAULT '',
                timestamp REAL NOT NULL DEFAULT 0,
                nonce INTEGER NOT NULL DEFAULT 0,
                merkle TEXT NOT NULL DEFAULT '',
                difficulty INTEGER NOT NULL DEFAULT 1,
                cumulative_work INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS utxos (
                txid TEXT NOT NULL,
                vout INTEGER NOT NULL,
                address TEXT NOT NULL DEFAULT '',
                amount INTEGER NOT NULL DEFAULT 0,
                pubkey TEXT NOT NULL DEFAULT '',
                height INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (txid, vout)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tx_index (
                txid TEXT PRIMARY KEY,
                height INTEGER NOT NULL DEFAULT -1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mempool (
                txid TEXT PRIMARY KEY,
                tx TEXT NOT NULL DEFAULT '{}',
                fee INTEGER NOT NULL DEFAULT 0,
                added_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nonces (
                pubkey TEXT PRIMARY KEY,
                nonce INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contracts (
                contract_id TEXT PRIMARY KEY,
                data TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS l2_escrows (
                escrow_id TEXT PRIMARY KEY,
                creator TEXT NOT NULL DEFAULT '',
                buyer TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'OPEN',
                btc_address TEXT NOT NULL DEFAULT '',
                btc_txid TEXT NOT NULL DEFAULT '',
                btc_amount_sats INTEGER NOT NULL DEFAULT 0,
                brn_amount_sats INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL DEFAULT 0,
                expires_at INTEGER NOT NULL DEFAULT 0,
                l2_hash TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS btc_txids_used (
                btc_txid TEXT PRIMARY KEY,
                escrow_id TEXT NOT NULL DEFAULT '',
                used_at INTEGER NOT NULL DEFAULT 0
            )
        """)
        # [v9.8.1] peers com coluna 'score'
        conn.execute("""
            CREATE TABLE IF NOT EXISTS peers (
                node_id TEXT PRIMARY KEY,
                address TEXT NOT NULL UNIQUE,
                genesis_hash TEXT NOT NULL DEFAULT '',
                version TEXT NOT NULL DEFAULT '',
                height INTEGER NOT NULL DEFAULT 0,
                is_miner INTEGER NOT NULL DEFAULT 0,
                public_key TEXT NOT NULL DEFAULT '',
                score REAL NOT NULL DEFAULT 0,
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
        conn.execute("INSERT OR IGNORE INTO mining_gate (id) VALUES (1)")

    def _migrate_schema(self, conn: sqlite3.Connection):
        """Adiciona colunas/tabelas novas em DBs antigos.

        IMPORTANTE: NÃO cria índices aqui — isso é feito em
        _create_indexes() após esta função, quando TODAS as colunas
        já existem.
        """
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        # DB antigo tinha blocks com outro formato -> recria se não tiver coluna data
        if "blocks" in tables:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(blocks)").fetchall()]
            if "data" not in cols:
                logger.info("[DB MIGRATION] Recriando tabela blocks no formato novo")
                conn.execute("DROP TABLE IF EXISTS blocks")
                conn.execute("""
                    CREATE TABLE blocks (
                        height INTEGER PRIMARY KEY,
                        hash TEXT NOT NULL DEFAULT '',
                        prev_hash TEXT NOT NULL DEFAULT '',
                        timestamp REAL NOT NULL DEFAULT 0,
                        nonce INTEGER NOT NULL DEFAULT 0,
                        merkle TEXT NOT NULL DEFAULT '',
                        difficulty INTEGER NOT NULL DEFAULT 1,
                        cumulative_work INTEGER NOT NULL DEFAULT 0,
                        data TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
                    )
                """)
        for name, ddl in [
            ("utxos", """CREATE TABLE IF NOT EXISTS utxos (
                txid TEXT NOT NULL, vout INTEGER NOT NULL, address TEXT NOT NULL DEFAULT '',
                amount INTEGER NOT NULL DEFAULT 0, pubkey TEXT NOT NULL DEFAULT '',
                height INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (txid, vout))"""),
            ("tx_index", "CREATE TABLE IF NOT EXISTS tx_index (txid TEXT PRIMARY KEY, height INTEGER NOT NULL DEFAULT -1)"),
            ("mempool", """CREATE TABLE IF NOT EXISTS mempool (
                txid TEXT PRIMARY KEY, tx TEXT NOT NULL DEFAULT '{}',
                fee INTEGER NOT NULL DEFAULT 0, added_at REAL NOT NULL DEFAULT (strftime('%s','now')))"""),
            ("nonces", "CREATE TABLE IF NOT EXISTS nonces (pubkey TEXT PRIMARY KEY, nonce INTEGER NOT NULL DEFAULT 0)"),
            ("contracts", "CREATE TABLE IF NOT EXISTS contracts (contract_id TEXT PRIMARY KEY, data TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL DEFAULT (strftime('%s','now')))"),
            ("l2_escrows", """CREATE TABLE IF NOT EXISTS l2_escrows (
                escrow_id TEXT PRIMARY KEY, creator TEXT NOT NULL DEFAULT '',
                buyer TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'OPEN',
                btc_address TEXT NOT NULL DEFAULT '', btc_txid TEXT NOT NULL DEFAULT '',
                btc_amount_sats INTEGER NOT NULL DEFAULT 0, brn_amount_sats INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL DEFAULT 0, expires_at INTEGER NOT NULL DEFAULT 0,
                l2_hash TEXT NOT NULL DEFAULT '')"""),
            ("btc_txids_used", "CREATE TABLE IF NOT EXISTS btc_txids_used (btc_txid TEXT PRIMARY KEY, escrow_id TEXT NOT NULL DEFAULT '', used_at INTEGER NOT NULL DEFAULT 0)"),
        ]:
            if name not in tables:
                logger.info("[DB MIGRATION] Criando tabela %s", name)
                conn.execute(ddl)
        # peers: colunas novas (ALTER TABLE) — precisa vir ANTES dos índices
        if "peers" in tables:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(peers)").fetchall()]
            for col in ("genesis_hash", "version", "public_key"):
                if col not in cols:
                    logger.info("[DB MIGRATION] Adicionando coluna peers.%s", col)
                    conn.execute(f"ALTER TABLE peers ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            if "score" not in cols:
                logger.info("[DB MIGRATION] Adicionando coluna peers.score")
                conn.execute("ALTER TABLE peers ADD COLUMN score REAL NOT NULL DEFAULT 0")

    def _create_indexes(self, conn: sqlite3.Connection):
        """Cria TODOS os índices. Chamado APÓS _migrate_schema().

        [v9.8.2] Cada CREATE INDEX tem try/except individual: se um
        falhar (DB de schema muito antigo sem alguma coluna), só loga
        warning e continua. Diferente de _create_tables, que precisa
        suceder tudo, os índices são otimização — não bloqueiam boot.

        [v9.9.0 - P0-4] Adicionados índices que faltavam:
          - mempool(added_at): a montagem do bloco faz ORDER BY added_at
          - tx_index(height): truncate_to e reorg escaneiam por altura
          - utxos(height): reorg/prune usam DELETE ... WHERE height > ?
        """
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_peers_height ON peers(height);",
            "CREATE INDEX IF NOT EXISTS idx_peers_last_seen ON peers(last_seen DESC);",
            "CREATE INDEX IF NOT EXISTS idx_peers_score ON peers(score DESC);",
            "CREATE INDEX IF NOT EXISTS idx_blocks_hash ON blocks(hash);",
            "CREATE INDEX IF NOT EXISTS idx_utxos_address ON utxos(address);",
            "CREATE INDEX IF NOT EXISTS idx_utxos_height ON utxos(height);",
            "CREATE INDEX IF NOT EXISTS idx_tx_index_height ON tx_index(height);",
            "CREATE INDEX IF NOT EXISTS idx_mempool_added_at ON mempool(added_at);",
            "CREATE INDEX IF NOT EXISTS idx_l2_status ON l2_escrows(status);",
            "CREATE INDEX IF NOT EXISTS idx_l2_buyer ON l2_escrows(buyer);",
        ]
        for ddl in indexes:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError as e:
                logger.warning("[DB] Falha ao criar índice (%s) — ignorando: %s",
                               ddl.split("idx_")[1].split(" ")[0] if "idx_" in ddl else "?", e)

    # ============================================================
    # BLOCKS — formato que blockchain.py espera (dict completo)
    # ============================================================
    def add_block(self, block: dict) -> None:
        """blockchain.py chama add_block(block_dict)."""
        if isinstance(block, dict) and "height" in block:
            self.save_block(block)
            return
        raise TypeError("add_block espera dict de bloco com 'height'")

    def save_block(self, block_or_height, hash_: str = "", prev_hash: str = "",
                    timestamp: float = 0, data: str = "{}") -> None:
        # Aceita os 2 formatos: save_block(dict) ou save_block(height, hash, ...)
        if isinstance(block_or_height, dict):
            block = block_or_height
            height = int(block["height"])
            hash_v = block.get("hash", "")
            prev_v = block.get("prev_hash", "")
            ts_v = block.get("timestamp", 0)
            nonce_v = block.get("nonce", 0)
            merkle_v = block.get("merkle", "")
            diff_v = block.get("difficulty", 1)
            work_v = block.get("cumulative_work", 0)
            data_v = json.dumps(block, ensure_ascii=False)
        else:
            height = int(block_or_height)
            hash_v, prev_v, ts_v = hash_, prev_hash, timestamp
            nonce_v, merkle_v, diff_v, work_v = 0, "", 1, 0
            data_v = data if isinstance(data, str) else json.dumps(data)
        with self._write_lock, self._get_connection(write=True) as conn:
            conn.execute("""
                INSERT INTO blocks (height, hash, prev_hash, timestamp, nonce, merkle, difficulty, cumulative_work, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(height) DO UPDATE SET
                    hash=excluded.hash, prev_hash=excluded.prev_hash,
                    timestamp=excluded.timestamp, nonce=excluded.nonce,
                    merkle=excluded.merkle, difficulty=excluded.difficulty,
                    cumulative_work=excluded.cumulative_work, data=excluded.data
            """, (height, hash_v, prev_v, float(ts_v or 0), int(nonce_v or 0),
                  merkle_v, int(diff_v or 1), int(work_v or 0), data_v))

    def _row_to_block(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        d = dict(row)
        raw = d.get("data") or "{}"
        try:
            block = json.loads(raw)
        except Exception:
            block = {}
        block.setdefault("height", d.get("height"))
        block.setdefault("hash", d.get("hash", ""))
        block.setdefault("prev_hash", d.get("prev_hash", ""))
        block.setdefault("timestamp", d.get("timestamp", 0))
        block.setdefault("nonce", d.get("nonce", 0))
        block.setdefault("merkle", d.get("merkle", ""))
        block.setdefault("difficulty", d.get("difficulty", 1))
        block.setdefault("cumulative_work", d.get("cumulative_work", 0))
        block.setdefault("transactions", [])
        block.setdefault("validators_sig", [])
        return block

    def get_block(self, height: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM blocks WHERE height = ?", (int(height),)).fetchone()
            return self._row_to_block(row)

    def get_block_by_hash(self, h: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM blocks WHERE hash = ?", (h,)).fetchone()
            return self._row_to_block(row)

    def get_block_by_height(self, height: int):
        return self.get_block(height)

    def get_last_block(self):
        h = self.height()
        return self.get_block(h) if h >= 0 else None

    def list_blocks(self, start: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM blocks WHERE height >= ? ORDER BY height ASC LIMIT ?",
                (int(start), int(limit)))
            return [b for b in (self._row_to_block(r) for r in cur.fetchall()) if b]

    def get_blocks_range(self, start: int, end: int) -> List[Dict[str, Any]]:
        """Blocos entre start (inclusive) e end (EXCLUSIVO), em ordem crescente.

        [v9.8.0] server.py::/api/blocks e /api/latest chamam
        get_blocks_range(start, end).
        """
        try:
            start = int(start)
            end = int(end)
        except (ValueError, TypeError):
            return []
        if start < 0:
            start = 0
        if end <= start:
            return []
        return self.list_blocks(start, end - start)

    def tip_hash(self) -> str:
        b = self.get_last_block()
        return b.get("hash", "0" * 64) if b else "0" * 64

    def height(self) -> int:
        with self._get_connection() as conn:
            row = conn.execute("SELECT MAX(height) FROM blocks").fetchone()
            return int(row[0]) if row and row[0] is not None else -1

    def get_height(self) -> int:
        return self.height()

    def get_chain_height(self) -> int:
        return self.height()

    # ============================================================
    # [P0-1] apply_block_atomic — UMA transação por bloco
    # ============================================================
    def apply_block_atomic(self, block: dict) -> None:
        """Aplica o bloco inteiro numa ÚNICA transação SQLite.

        Substitui a sequência antiga em blockchain.py::accept_block:
            add_block(block)                      # 1 commit
            for tx: apply_tx(tx, h, coinbase=...) # N commits
                    remove_mempool(tx['txid'])    # N commits

        por:
            BEGIN IMMEDIATE
              INSERT blocks                (1)
              INSERT tx_index / DELETE utxos / INSERT utxos / nonces (por tx)
              DELETE mempool (por tx não-coinbase)
            COMMIT                          (1)

        Rollback desfaz tudo se algo falhar — fecha a janela em que o
        bloco estava gravado mas as txs não tinham sido aplicadas.

        Preserva EXATAMENTE a semântica do apply_tx individual:
          - Não-coinbase: apaga UTXOs gastos e incrementa nonce do
            pubkey do input.
          - Sempre: INSERT OR REPLACE de UTXOs criados.
          - Sempre: UPSERT em tx_index.
          - Não-coinbase: remove da mempool.

        O bloco NÃO é salvo em `save_block`; é inserido direto com
        `data = json.dumps(block)` (mesmo formato de `save_block(dict)`).
        """
        height = int(block["height"])
        data_v = json.dumps(block, ensure_ascii=False)
        txs = block.get("transactions") or []

        with self._write_lock, self._get_connection(write=True) as conn:
            # 1) Bloco
            conn.execute("""
                INSERT INTO blocks
                (height, hash, prev_hash, timestamp, nonce, merkle,
                 difficulty, cumulative_work, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(height) DO UPDATE SET
                    hash=excluded.hash, prev_hash=excluded.prev_hash,
                    timestamp=excluded.timestamp, nonce=excluded.nonce,
                    merkle=excluded.merkle, difficulty=excluded.difficulty,
                    cumulative_work=excluded.cumulative_work,
                    data=excluded.data
            """, (
                height,
                block.get("hash", ""),
                block.get("prev_hash", ""),
                float(block.get("timestamp", 0) or 0),
                int(block.get("nonce", 0) or 0),
                block.get("merkle", ""),
                int(block.get("difficulty", 1) or 1),
                int(block.get("cumulative_work", 0) or 0),
                data_v,
            ))

            # 2) Cada tx
            for i, tx in enumerate(txs):
                coinbase = (i == 0)
                txid = tx.get("txid", "")

                # 2a) tx_index (sempre)
                if txid:
                    conn.execute(
                        "INSERT INTO tx_index (txid, height) VALUES (?, ?) "
                        "ON CONFLICT(txid) DO UPDATE SET height = excluded.height",
                        (txid, height),
                    )

                # 2b) Consome UTXOs + incrementa nonces (não-coinbase)
                if not coinbase:
                    for inp in (tx.get("inputs") or []):
                        conn.execute(
                            "DELETE FROM utxos WHERE txid = ? AND vout = ?",
                            (inp.get("txid", ""), int(inp.get("vout", 0))),
                        )
                        pk = inp.get("pubkey", "")
                        if pk:
                            conn.execute(
                                "INSERT INTO nonces (pubkey, nonce) VALUES (?, 1) "
                                "ON CONFLICT(pubkey) DO UPDATE SET nonce = nonce + 1",
                                (pk,),
                            )

                # 2c) Cria UTXOs das saídas (sempre)
                for vout, out in enumerate(tx.get("outputs") or []):
                    conn.execute("""
                        INSERT INTO utxos (txid, vout, address, amount, pubkey, height)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(txid, vout) DO UPDATE SET
                            address=excluded.address, amount=excluded.amount,
                            pubkey=excluded.pubkey, height=excluded.height
                    """, (
                        txid, vout,
                        out.get("address", ""),
                        int(out.get("amount", 0) or 0),
                        out.get("pubkey", "") or "",
                        height,
                    ))

                # 2d) Remove da mempool (não-coinbase)
                if not coinbase and txid:
                    conn.execute("DELETE FROM mempool WHERE txid = ?", (txid,))

        logger.debug("[DB] apply_block_atomic #%d: %d tx numa transação",
                     height, len(txs))

    # ============================================================
    # UTXO / NONCE / TX INDEX — usado por validate_tx / apply_tx
    # ============================================================
    def get_utxo(self, txid: str, vout: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM utxos WHERE txid = ? AND vout = ?", (txid, int(vout))).fetchone()
            return dict(row) if row else None

    def get_utxos(self, address: str) -> List[Dict[str, Any]]:
        """Lista todos os UTXOs de um endereço, ordenados por amount DESC.

        [v9.8.0] server.py::/api/transfer e /api/saldo chamam
        get_utxos(address).
        """
        if not address:
            return []
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT txid, vout, address, amount, pubkey, height "
                "FROM utxos WHERE address = ? "
                "ORDER BY amount DESC",
                (address,))
            return [dict(r) for r in cur.fetchall()]

    def count_utxos(self) -> int:
        """Número total de UTXOs ativos (usado em /api/status).

        [v9.8.0] server.py::/api/status chama chain.db.count_utxos().
        """
        with self._get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM utxos").fetchone()
            return int(row[0]) if row else 0

    def get_nonce_for_pubkey(self, pubkey: str) -> int:
        with self._get_connection() as conn:
            row = conn.execute("SELECT nonce FROM nonces WHERE pubkey = ?", (pubkey,)).fetchone()
            return int(row[0]) if row else 0

    def get_tx_block_height(self, txid_str: str) -> int:
        with self._get_connection() as conn:
            row = conn.execute("SELECT height FROM tx_index WHERE txid = ?", (txid_str,)).fetchone()
            return int(row[0]) if row else -1

    def get_tx_by_txid(self, txid_str: str) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
        """Retorna (tx_dict, block_height) ou (None, None) se não encontrado.

        [v9.8.0] server.py::/api/tx-status e /api/verificar-recebimento
        chamam get_tx_by_txid(txid).
        """
        if not txid_str:
            return None, None
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT height FROM tx_index WHERE txid = ?", (txid_str,)).fetchone()
            if not row:
                return None, None
            height = int(row[0])
        blk = self.get_block(height)
        if not blk:
            return None, None
        for tx in blk.get("transactions") or []:
            if tx.get("txid") == txid_str:
                return tx, height
        return None, None

    def get_mempool_tx(self, txid_str: str) -> Optional[Dict[str, Any]]:
        """Retorna o tx dict da mempool, ou None.

        [v9.8.0] server.py::/api/verificar-recebimento e /api/tx-status
        consultam a mempool antes de tx_index.
        """
        if not txid_str:
            return None
        with self._get_connection() as conn:
            row = conn.execute("SELECT tx FROM mempool WHERE txid = ?", (txid_str,)).fetchone()
            if not row:
                return None
            try:
                return json.loads(row[0])
            except Exception:
                return None

    def get_txs_for_address(self, address: str, limit: int = 200) -> List[Dict[str, Any]]:
        """Lista txs confirmadas + mempool envolvendo `address`.

        Retorna lista de dicts {tx, block_height, is_mempool, timestamp},
        ordenada da mais recente para a mais antiga.

        [v9.8.0] server.py::/api/minhas-txs chama get_txs_for_address.
        """
        if not address:
            return []
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            limit = 200
        limit = max(1, min(limit, 5000))

        out: List[Dict[str, Any]] = []

        # 1) mempool primeiro (mais recente por definição)
        try:
            for tx in self.all_mempool(limit=5000):
                if self._tx_involves_address(tx, address, tx_cache=None):
                    out.append({
                        "tx": tx,
                        "block_height": None,
                        "is_mempool": True,
                        "timestamp": int(tx.get("timestamp", 0) or 0),
                    })
        except Exception as e:
            logger.warning("[DB] get_txs_for_address(mempool): %s", e)

        # 2) confirmadas — escaneia blocos do mais recente pro mais antigo,
        #    montando cache de txid → tx conforme avança (pra resolver inputs)
        h = self.height()
        if h >= 0:
            tx_cache: Dict[str, Dict[str, Any]] = {}
            for height in range(h, max(-1, h - 5000), -1):
                blk = self.get_block(height)
                if not blk:
                    continue
                for tx in (blk.get("transactions") or []):
                    txid = tx.get("txid", "")
                    if not txid:
                        continue
                    tx_cache[txid] = tx
                    if self._tx_involves_address(tx, address, tx_cache=tx_cache):
                        out.append({
                            "tx": tx,
                            "block_height": height,
                            "is_mempool": False,
                            "timestamp": int(tx.get("timestamp", 0) or 0),
                        })
                if len(out) >= limit * 2:
                    break

        out.sort(key=lambda x: (
            -(x["block_height"] if x["block_height"] is not None else 10**9),
            -x["timestamp"],
        ))
        return out[:limit]

    def _tx_involves_address(self, tx: dict, address: str,
                              tx_cache: Optional[Dict[str, Dict[str, Any]]]) -> bool:
        """Helper: True se address aparece em outputs OU é dono de input."""
        for o in (tx.get("outputs") or []):
            if o.get("address") == address:
                return True
        if tx_cache is not None:
            for inp in (tx.get("inputs") or []):
                ref_txid = inp.get("txid", "")
                if ref_txid == "0" * 64 or not ref_txid:
                    continue
                ref_tx = tx_cache.get(ref_txid)
                if not ref_tx:
                    continue
                try:
                    vout = int(inp.get("vout", 0))
                except (ValueError, TypeError):
                    continue
                outputs = ref_tx.get("outputs") or []
                if 0 <= vout < len(outputs) and outputs[vout].get("address") == address:
                    return True
        return False

    def apply_tx(self, tx: dict, height: int, coinbase: bool = False) -> None:
        """Aplica tx: gasta inputs, cria outputs, registra tx_index, avança nonce.

        [KEEP] Mantido para mine_block e código legado. Para blocos
        completos, prefira apply_block_atomic().
        """
        txid = tx.get("txid", "")
        with self._write_lock, self._get_connection(write=True) as conn:
            if txid:
                conn.execute(
                    "INSERT INTO tx_index (txid, height) VALUES (?, ?) "
                    "ON CONFLICT(txid) DO UPDATE SET height = excluded.height",
                    (txid, int(height)))
            if not coinbase:
                for inp in (tx.get("inputs") or []):
                    conn.execute("DELETE FROM utxos WHERE txid = ? AND vout = ?",
                                 (inp.get("txid", ""), int(inp.get("vout", 0))))
                    pk = inp.get("pubkey", "")
                    if pk:
                        conn.execute(
                            "INSERT INTO nonces (pubkey, nonce) VALUES (?, 1) "
                            "ON CONFLICT(pubkey) DO UPDATE SET nonce = nonce + 1", (pk,))
            for vout, out in enumerate(tx.get("outputs") or []):
                conn.execute("""
                    INSERT INTO utxos (txid, vout, address, amount, pubkey, height)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(txid, vout) DO UPDATE SET
                        address=excluded.address, amount=excluded.amount,
                        pubkey=excluded.pubkey, height=excluded.height
                """, (txid, vout, out.get("address", ""), int(out.get("amount", 0)),
                      out.get("pubkey", ""), int(height)))

    # ============================================================
    # MEMPOOL
    # ============================================================
    def add_mempool(self, tx: dict, fee: int = 0) -> bool:
        try:
            txid = tx.get("txid", "")
            if not txid:
                return False
            with self._write_lock, self._get_connection(write=True) as conn:
                conn.execute("""
                    INSERT INTO mempool (txid, tx, fee) VALUES (?, ?, ?)
                    ON CONFLICT(txid) DO UPDATE SET tx=excluded.tx, fee=excluded.fee
                """, (txid, json.dumps(tx, ensure_ascii=False), int(fee or 0)))
            return True
        except Exception as e:
            logger.warning("[DB] add_mempool falhou: %s", e)
            return False

    def has_mempool(self, txid: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute("SELECT 1 FROM mempool WHERE txid = ?", (txid,)).fetchone()
            return row is not None

    def mempool_count(self) -> int:
        with self._get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM mempool").fetchone()
            return int(row[0]) if row else 0

    def mempool_stats(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(fee),0) FROM mempool").fetchone()
            fees = [r[0] for r in conn.execute("SELECT fee FROM mempool").fetchall()]
            return {"count": int(row[0]) if row else 0, "fees": fees}

    def all_mempool(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT tx FROM mempool ORDER BY added_at ASC LIMIT ?", (int(limit),))
            out = []
            for r in cur.fetchall():
                try:
                    out.append(json.loads(r[0]))
                except Exception:
                    continue
            return out

    def remove_mempool(self, txid: str) -> bool:
        with self._write_lock, self._get_connection(write=True) as conn:
            cur = conn.execute("DELETE FROM mempool WHERE txid = ?", (txid,))
            return cur.rowcount > 0

    def utxos_gastos_na_mempool(self) -> set:
        gastos = set()
        for tx in self.all_mempool(limit=5000):
            for inp in (tx.get("inputs") or []):
                try:
                    gastos.add((inp["txid"], int(inp["vout"])))
                except Exception:
                    continue
        return gastos

    # ============================================================
    # CONTRATOS
    # ============================================================
    def contract_get(self, contract_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT data FROM contracts WHERE contract_id = ?", (contract_id,)).fetchone()
            if not row:
                return None
            try:
                return json.loads(row[0])
            except Exception:
                return None

    def contract_put(self, contract_id: str, data: dict) -> None:
        with self._write_lock, self._get_connection(write=True) as conn:
            conn.execute("""
                INSERT INTO contracts (contract_id, data) VALUES (?, ?)
                ON CONFLICT(contract_id) DO UPDATE SET data = excluded.data
            """, (contract_id, json.dumps(data or {}, ensure_ascii=False)))

    def contract_list(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            out = []
            for r in conn.execute("SELECT contract_id, data FROM contracts").fetchall():
                try:
                    out.append(json.loads(r[1]))
                except Exception:
                    out.append({"contract_id": r[0]})
            return out

    # ============================================================
    # L2 ESCROW (BTC -> BRN)
    # ============================================================
    def _ensure_l2_tables(self):
        with self._get_connection(write=True) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS l2_escrows (
                    escrow_id TEXT PRIMARY KEY,
                    creator TEXT NOT NULL DEFAULT '',
                    buyer TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    btc_address TEXT NOT NULL DEFAULT '',
                    btc_txid TEXT NOT NULL DEFAULT '',
                    btc_amount_sats INTEGER NOT NULL DEFAULT 0,
                    brn_amount_sats INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL DEFAULT 0,
                    expires_at INTEGER NOT NULL DEFAULT 0,
                    l2_hash TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS btc_txids_used (
                    btc_txid TEXT PRIMARY KEY,
                    escrow_id TEXT NOT NULL DEFAULT '',
                    used_at INTEGER NOT NULL DEFAULT 0
                )
            """)

    def save_l2_escrow(self, d: dict) -> None:
        self._ensure_l2_tables()
        with self._write_lock, self._get_connection(write=True) as conn:
            conn.execute("""
                INSERT INTO l2_escrows
                (escrow_id, creator, buyer, status, btc_address, btc_txid,
                 btc_amount_sats, brn_amount_sats, created_at, expires_at, l2_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(escrow_id) DO UPDATE SET
                    creator=excluded.creator, buyer=excluded.buyer,
                    status=excluded.status, btc_address=excluded.btc_address,
                    btc_txid=excluded.btc_txid,
                    btc_amount_sats=excluded.btc_amount_sats,
                    brn_amount_sats=excluded.brn_amount_sats,
                    created_at=excluded.created_at, expires_at=excluded.expires_at,
                    l2_hash=excluded.l2_hash
            """, (d.get("escrow_id", ""), d.get("creator", ""), d.get("buyer", ""),
                  d.get("status", "OPEN"), d.get("btc_address", ""), d.get("btc_txid", ""),
                  int(d.get("btc_amount_sats") or 0), int(d.get("brn_amount_sats") or 0),
                  int(d.get("created_at") or 0), int(d.get("expires_at") or 0),
                  d.get("l2_hash", "")))

    def get_l2_escrow_by_id(self, escrow_id: str):
        self._ensure_l2_tables()
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM l2_escrows WHERE escrow_id = ?", (escrow_id,)).fetchone()
            return dict(row) if row else None

    def get_l2_escrows(self, status: str | None = None):
        self._ensure_l2_tables()
        with self._get_connection() as conn:
            if status:
                cur = conn.execute(
                    "SELECT * FROM l2_escrows WHERE status = ? ORDER BY created_at DESC", (status,))
            else:
                cur = conn.execute("SELECT * FROM l2_escrows ORDER BY created_at DESC")
            return [dict(r) for r in cur.fetchall()]

    def update_l2_escrow_status(self, escrow_id: str, status: str, btc_txid: str | None = None):
        self._ensure_l2_tables()
        with self._write_lock, self._get_connection(write=True) as conn:
            if btc_txid is not None:
                conn.execute(
                    "UPDATE l2_escrows SET status = ?, btc_txid = ? WHERE escrow_id = ?",
                    (status, btc_txid, escrow_id))
            else:
                conn.execute(
                    "UPDATE l2_escrows SET status = ? WHERE escrow_id = ?",
                    (status, escrow_id))
            return conn.total_changes > 0

    def is_btc_txid_used(self, btc_txid: str) -> bool:
        if not btc_txid:
            return False
        self._ensure_l2_tables()
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM btc_txids_used WHERE btc_txid = ?", (btc_txid,)).fetchone()
            return row is not None

    def mark_btc_txid_used(self, btc_txid: str, escrow_id: str = "") -> bool:
        """Marca txid como usado. Retorna False se JÁ estava usado (anti-replay)."""
        if not btc_txid:
            return False
        self._ensure_l2_tables()
        try:
            with self._write_lock, self._get_connection(write=True) as conn:
                conn.execute(
                    "INSERT INTO btc_txids_used (btc_txid, escrow_id, used_at) VALUES (?, ?, ?)",
                    (btc_txid, escrow_id, int(time.time())))
            return True
        except Exception:
            return False

    # ============================================================
    # PEERS
    # ============================================================
    def upsert_peer(self, node_id: str, address: str, genesis_hash: str = "",
                    version: str = "", height: int = 0, is_miner: bool = False,
                    public_key: str = "", score: Optional[float] = None) -> None:
        """Insere ou atualiza um peer.

        [v9.8.1] score=None preserva o score atual (se o peer já existir)
        ou usa 0.0 (se novo). Assim, o upsert periódico do Peer-Healer não
        zera scores acumulados por get_peer_score/bump_peer_score.
        """
        now = time.time()
        with self._write_lock, self._get_connection(write=True) as conn:
            if score is None:
                conn.execute("""
                    INSERT INTO peers (node_id, address, genesis_hash, version, height, is_miner, public_key, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(node_id) DO UPDATE SET
                        address=excluded.address, genesis_hash=excluded.genesis_hash,
                        version=excluded.version, height=excluded.height,
                        is_miner=excluded.is_miner, public_key=excluded.public_key,
                        last_seen=excluded.last_seen
                """, (node_id, address, genesis_hash, version, height, int(is_miner), public_key, now))
            else:
                try:
                    score_v = float(score)
                except (ValueError, TypeError):
                    score_v = 0.0
                conn.execute("""
                    INSERT INTO peers (node_id, address, genesis_hash, version, height, is_miner, public_key, score, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(node_id) DO UPDATE SET
                        address=excluded.address, genesis_hash=excluded.genesis_hash,
                        version=excluded.version, height=excluded.height,
                        is_miner=excluded.is_miner, public_key=excluded.public_key,
                        score=excluded.score, last_seen=excluded.last_seen
                """, (node_id, address, genesis_hash, version, height, int(is_miner),
                      public_key, score_v, now))

    def listar_peers(self, apenas_ativos: bool = True, max_age_s: int = 300,
                     janela: Optional[int] = None) -> List[Dict[str, Any]]:
        """Lista peers.

        [v9.8.0] Aceita kwarg 'janela' como alias de max_age_s (auto.py,
        auto_trust.py, auto_trust_daemon.py chamam janela=1800).

        [v9.8.0] Também filtra corretamente quando apenas_ativos=True e
        peer.age_s < 0 (nunca visto).
        """
        if janela is not None:
            try:
                max_age_s = int(janela)
            except (ValueError, TypeError):
                pass
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM peers ORDER BY last_seen DESC")
            peers = []
            for row in cursor.fetchall():
                peer = dict(row)
                peer["is_miner"] = bool(peer["is_miner"])
                peer["age_s"] = int(now - peer["last_seen"]) if peer["last_seen"] > 0 else -1
                if apenas_ativos:
                    if peer["age_s"] < 0:
                        continue
                    if peer["age_s"] > max_age_s:
                        continue
                peers.append(peer)
            return peers

    def contar_peers(self, apenas_ativos: bool = True,
                     max_age_s: int = 300) -> int:
        """Número de peers.

        [v9.8.0] server.py::/api/status e /api/sync-info chamam
        contar_peers(apenas_ativos=True/False).
        """
        try:
            return len(self.listar_peers(
                apenas_ativos=bool(apenas_ativos), max_age_s=int(max_age_s)))
        except Exception:
            return 0

    def get_peer_score(self, identifier: str) -> float:
        """Score de confiança do peer (usado pelo P2P e rate limiting).

        [v9.8.1] p2p_unified.py e p2p_auth.py chamam
        db.get_peer_score(addr_or_pubkey) no handshake. Sem isto, TODA
        conexão P2P de entrada era rejeitada com AttributeError, o nó
        ficava isolado e o mining gate nunca abria.

        Aceita node_id (hash do pubkey), public_key ou address (com ou
        sem :porta). Retorna 0.0 se o peer for desconhecido.
        """
        if not identifier:
            return 0.0
        ident = str(identifier).strip()
        candidates = [ident]
        if ":" in ident:
            candidates.append(ident.rsplit(":", 1)[0])
        try:
            with self._get_connection() as conn:
                for c in candidates:
                    row = conn.execute(
                        "SELECT COALESCE(score, 0) FROM peers "
                        "WHERE node_id = ? OR public_key = ? OR address = ? "
                        "LIMIT 1",
                        (c, c, c)).fetchone()
                    if row:
                        return float(row[0])
        except Exception as e:
            logger.debug("[DB] get_peer_score(%s): %s", ident, e)
        return 0.0

    def set_peer_score(self, identifier: str, score: float) -> bool:
        """Seta score absoluto. Aceita node_id/public_key/address."""
        if not identifier:
            return False
        try:
            score = float(score)
        except (ValueError, TypeError):
            return False
        ident = str(identifier).strip()
        candidates = [ident]
        if ":" in ident:
            candidates.append(ident.rsplit(":", 1)[0])
        try:
            with self._write_lock, self._get_connection(write=True) as conn:
                for c in candidates:
                    cur = conn.execute(
                        "UPDATE peers SET score = ? "
                        "WHERE node_id = ? OR public_key = ? OR address = ?",
                        (score, c, c, c))
                    if cur.rowcount > 0:
                        return True
        except Exception as e:
            logger.warning("[DB] set_peer_score(%s): %s", ident, e)
        return False

    def bump_peer_score(self, identifier: str, delta: float) -> float:
        """Adiciona delta ao score atual. Retorna o novo valor (ou 0 se ausente)."""
        try:
            delta = float(delta)
        except (ValueError, TypeError):
            return 0.0
        current = self.get_peer_score(identifier)
        new_score = current + delta
        self.set_peer_score(identifier, new_score)
        return new_score

    def remover_peer(self, node_id: str) -> bool:
        with self._write_lock, self._get_connection(write=True) as conn:
            cur = conn.execute("DELETE FROM peers WHERE node_id = ?", (node_id,))
            return cur.rowcount > 0

    def limpar_peers_antigos(self, max_age_s: int = 900) -> int:
        limite = time.time() - max_age_s
        with self._write_lock, self._get_connection(write=True) as conn:
            cur = conn.execute("DELETE FROM peers WHERE last_seen < ?", (limite,))
            return cur.rowcount

    # ============================================================
    # BOOTSTRAP PEERS
    # ============================================================
    def add_bootstrap_peer(self, address: str, priority: int = 0) -> None:
        with self._write_lock, self._get_connection(write=True) as conn:
            conn.execute("""
                INSERT INTO bootstrap_peers (address, priority, enabled) VALUES (?, ?, 1)
                ON CONFLICT(address) DO UPDATE SET priority=excluded.priority, enabled=1
            """, (address, int(priority)))

    def listar_bootstrap_peers(self, only_enabled: bool = True) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
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
    # SYNC STATUS + META
    # ============================================================
    def set_sync_value(self, key: str, value: str):
        now = time.time()
        with self._write_lock, self._get_connection(write=True) as conn:
            conn.execute("""
                INSERT INTO sync_status (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?
            """, (key, value, now, value, now))

    def get_sync_value(self, key: str, default: str = None) -> Optional[str]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT value FROM sync_status WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        if isinstance(value, (dict, list)):
            v = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            v = "1" if value else "0"
        elif value is None:
            v = ""
        else:
            v = str(value)
        self.set_sync_value(str(key), v)

    def get_meta(self, key: str, default: Any = None) -> Any:
        # IMPORTANTE: blockchain.py faz json.loads(db.get_meta("fila_mineracao"))
        # então devolve a string crua, NÃO tenta converter.
        v = self.get_sync_value(str(key), None)
        return v if v is not None else default

    def delete_meta(self, key: str) -> bool:
        with self._write_lock, self._get_connection(write=True) as conn:
            cur = conn.execute("DELETE FROM sync_status WHERE key = ?", (str(key),))
            return cur.rowcount > 0

    def get_all_meta(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            return {r[0]: r[1] for r in conn.execute("SELECT key, value FROM sync_status").fetchall()}

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
                UPDATE mining_gate SET gate_open=1, reason=?, opened_at=?, updated_at=?, closed_at=NULL
                WHERE id=1 AND gate_open=0
            """, (reason, now, now))
            if conn.total_changes > 0:
                logger.info("[MINING-GATE] ✅ ABERTO — %s", reason)

    def fechar_mining_gate(self, reason: str = ""):
        now = time.time()
        with self._write_lock, self._get_connection(write=True) as conn:
            conn.execute("""
                UPDATE mining_gate SET gate_open=0, reason=?, closed_at=?, updated_at=?
                WHERE id=1 AND gate_open=1
            """, (reason, now, now))
            if conn.total_changes > 0:
                logger.info("[MINING-GATE] 🔒 FECHADO — %s", reason)

    def get_mining_gate(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM mining_gate WHERE id = 1").fetchone()
            if row:
                d = dict(row)
                d["gate_open"] = bool(d["gate_open"])
                return d
            return {"gate_open": False, "reason": "", "opened_at": None,
                    "closed_at": None, "updated_at": 0}

    # ============================================================
    # TRUNCATE / PRUNE
    # ============================================================
    def truncate_to(self, target_height: int) -> bool:
        if target_height < 0:
            return False
        current = self.height()
        if current <= 0:
            return True
        if target_height >= current:
            return True
        try:
            with self._write_lock, self._get_connection(write=True) as conn:
                conn.execute("DELETE FROM blocks WHERE height > ?", (target_height,))
                conn.execute("DELETE FROM utxos WHERE height > ?", (target_height,))
                conn.execute("DELETE FROM tx_index WHERE height > ?", (target_height,))
                conn.execute("DELETE FROM nonces")
                rows = conn.execute("SELECT data FROM blocks ORDER BY height ASC").fetchall()
                counts = {}
                for (raw,) in rows:
                    try:
                        blk = json.loads(raw or "{}")
                    except Exception:
                        continue
                    for idx, tx in enumerate(blk.get("transactions") or []):
                        if idx == 0:
                            continue  # coinbase não avança nonce
                        for inp in (tx.get("inputs") or []):
                            pk = inp.get("pubkey", "")
                            if pk:
                                counts[pk] = counts.get(pk, 0) + 1
                for pk, n in counts.items():
                    conn.execute("INSERT INTO nonces (pubkey, nonce) VALUES (?, ?)", (pk, n))
                removed = conn.total_changes
            self._vacuum()
            logger.info("[DB] Truncado > %d. Nova altura: %d (removidos ~%d)",
                        target_height, self.height(), removed)
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

    def close(self):
        # [v9.9.0 - P0-4] PRAGMA optimize antes de fechar para que o
        # SQLite consolide estatísticas (equivalente a ANALYZE parcial).
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            try:
                conn.execute("PRAGMA optimize;")
            finally:
                conn.close()
        except Exception:
            pass
        Database._instance = None
        self._initialized = False
        logger.info("[DB] instância fechada")


# ============================================================
# COMPATIBILIDADE — ChainDB
# ============================================================
class ChainDB(Database):
    """ChainDB é o Database. Nome que main.py / blockchain.py esperam."""


DB = Database
ChainDatabase = Database


def get_db(db_path: str = None) -> ChainDB:
    return ChainDB(db_path)


def get_chain_db(db_path: str = None) -> ChainDB:
    return ChainDB(db_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    db = get_db()
    print(f"✅ Banco: {db.db_path} | Altura: {db.height()} | tip: {db.tip_hash()[:16]}...")
    print(f"👥 Peers: {len(db.listar_peers())} | Mempool: {db.mempool_count()} | L2 OPEN: {len(db.get_l2_escrows(status='OPEN'))}")
    print(f"💰 UTXOs: {db.count_utxos()} | Peers ativos: {db.contar_peers(apenas_ativos=True)}")
    print(f"📦 Blocos 0..4: {len(db.get_blocks_range(0, 5))}")
    print(f"🎯 get_peer_score('test'): {db.get_peer_score('test')}")