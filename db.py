"""db.py — Banco SQLite do BRN (v6.6)
================================================================
v6.6:
  [FIX] Adicionado PRAGMA busy_timeout=30000 (evita database is locked)
  [FIX] utxos_gastos_na_mempool agora tem lock (proteção de leitura)
  [FIX] all_mempool agora tem lock (evita leitura suja durante escrita)
================================================================
"""
import os
import time
import zlib
import sqlite3
import threading
import orjson
import json


class ChainDB:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        
        # [FIX v6.6] busy_timeout para evitar travamentos de escrita concorrente
        for p in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL",
                  "PRAGMA temp_store=MEMORY", "PRAGMA cache_size=-20000",
                  "PRAGMA mmap_size=268435456", "PRAGMA foreign_keys=ON",
                  "PRAGMA busy_timeout=30000"):
            self.conn.execute(p)
            
        self._schema()
        self._migrate_raw_txs()

    def _schema(self):
        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS blocks (
                height INTEGER PRIMARY KEY, hash TEXT UNIQUE NOT NULL,
                prev_hash TEXT NOT NULL, timestamp INTEGER NOT NULL,
                nonce INTEGER NOT NULL, merkle TEXT NOT NULL,
                difficulty INTEGER NOT NULL, raw BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_blocks_hash ON blocks(hash);
            CREATE INDEX IF NOT EXISTS idx_blocks_prev ON blocks(prev_hash);

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

            try:
                cols = self.conn.execute("PRAGMA table_info(peers)").fetchall()
                names = {c["name"] for c in cols}
                if "score" not in names:
                    self.conn.execute("ALTER TABLE peers ADD COLUMN score INTEGER DEFAULT 0")
                if "public_key" not in names:
                    self.conn.execute("ALTER TABLE peers ADD COLUMN public_key TEXT DEFAULT ''")
            except Exception:
                pass

            try:
                cols = self.conn.execute("PRAGMA table_info(transactions)").fetchall()
                names = {c["name"] for c in cols}
                if "raw" not in names:
                    self.conn.execute("ALTER TABLE transactions ADD COLUMN raw BLOB")
            except Exception:
                pass

    # ==================== BACKFILL AUTOMÁTICO ====================
    def _migrate_raw_txs(self):
        if os.environ.get("BRN_SKIP_RAW_BACKFILL", "0") == "1":
            return
        try:
            with self.lock:
                n_null = self.conn.execute("SELECT COUNT(*) AS c FROM transactions WHERE raw IS NULL").fetchone()["c"]
        except Exception:
            return
        if not n_null:
            return
        print(f"[db] backfill: {n_null} tx antigas sem raw — migrando...")
        try:
            with self.lock:
                rows = self.conn.execute("SELECT DISTINCT block_height FROM transactions WHERE raw IS NULL ORDER BY block_height").fetchall()
                heights = [r["block_height"] for r in rows]
        except Exception:
            return
        if not heights:
            return
        total = len(heights)
        done = 0
        t0 = time.time()
        for h in heights:
            try:
                blk = self.get_block(h)
                if not blk:
                    continue
                with self.lock:
                    self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
                    try:
                        for tx in blk["transactions"]:
                            blob = zlib.compress(orjson.dumps(tx), level=6)
                            self.conn.execute("UPDATE transactions SET raw=? WHERE txid=? AND raw IS NULL", (blob, tx["txid"]))
                        self.conn.execute("COMMIT")
                    except Exception:
                        self.conn.execute("ROLLBACK")
                        continue
            except Exception:
                continue
            done += 1
            if done % 500 == 0 or done == total:
                dt = time.time() - t0
                rate = done / dt if dt > 0 else 0.0
                print(f"[db] backfill: {done}/{total} blocos ({rate:.0f} blocos/s)")
        print(f"[db] backfill concluído: {done}/{total} blocos")

    # ==================== BLOCOS ====================
    def add_block(self, block):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
                blob = zlib.compress(orjson.dumps(block), level=6)
                self.conn.execute(
                    "INSERT INTO blocks(height,hash,prev_hash,timestamp,nonce,merkle,difficulty,raw) VALUES (?,?,?,?,?,?,?,?)",
                    (block["height"], block["hash"], block["prev_hash"], block["timestamp"], block["nonce"], block["merkle"], block["difficulty"], blob))
                for tx in block["transactions"]:
                    tx_blob = zlib.compress(orjson.dumps(tx), level=6)
                    self.conn.execute("INSERT OR REPLACE INTO transactions(txid,block_height,raw) VALUES (?,?,?)", (tx["txid"], block["height"], tx_blob))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def delete_blocks_above(self, height):
        with self.lock:
            n = self.conn.execute("DELETE FROM blocks WHERE height > ?", (height,)).rowcount
            self.conn.execute("DELETE FROM transactions WHERE block_height > ?", (height,))
            self.conn.execute("DELETE FROM utxos WHERE block_height > ?", (height,))
            return n

    def get_block(self, height):
        row = self.conn.execute("SELECT raw FROM blocks WHERE height=?", (height,)).fetchone()
        return orjson.loads(zlib.decompress(row["raw"])) if row else None

    def get_block_by_hash(self, h):
        row = self.conn.execute("SELECT raw FROM blocks WHERE hash=?", (h,)).fetchone()
        return orjson.loads(zlib.decompress(row["raw"])) if row else None

    def get_blocks_range(self, start, end):
        rows = self.conn.execute("SELECT raw FROM blocks WHERE height >= ? AND height < ? ORDER BY height", (start, end)).fetchall()
        return [orjson.loads(zlib.decompress(r["raw"])) for r in rows]

    def height(self):
        row = self.conn.execute("SELECT MAX(height) AS h FROM blocks").fetchone()
        return int(row["h"]) if row and row["h"] is not None else -1

    def tip(self):
        row = self.conn.execute("SELECT hash,prev_hash FROM blocks ORDER BY height DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def tip_hash(self):
        t = self.tip()
        return t["hash"] if t else "0" * 64

    def headers(self, start, limit=2000):
        rows = self.conn.execute("SELECT height,hash,prev_hash,timestamp,nonce,merkle,difficulty FROM blocks WHERE height>=? ORDER BY height LIMIT ?", (start, limit)).fetchall()
        return [dict(r) for r in rows]

    def latest_blocks(self, n=10):
        top = self.height()
        if top < 0: return []
        return [b for b in (self.get_block(h) for h in range(max(0, top - n + 1), top + 1)) if b]

    # ==================== TX LOOKUP ====================
    def get_tx_by_txid(self, txid: str):
        try:
            row = self.conn.execute("SELECT raw, block_height FROM transactions WHERE txid = ? LIMIT 1", (txid,)).fetchone()
            if not row or row["raw"] is None: return None, None
            return orjson.loads(zlib.decompress(row["raw"])), row["block_height"]
        except Exception:
            return None, None

    def get_tx_block_height(self, txid: str) -> int:
        try:
            row = self.conn.execute("SELECT block_height FROM transactions WHERE txid = ? LIMIT 1", (txid,)).fetchone()
            return int(row["block_height"]) if row else -1
        except Exception:
            return -1

    # ==================== HISTÓRICO POR ENDEREÇO ====================
    def get_txs_for_address(self, address: str, limit: int = 200) -> list:
        result = []
        seen_txids = set()

        with self.lock:
            try:
                rows = self.conn.execute("SELECT DISTINCT txid FROM utxos WHERE address = ? LIMIT ?", (address, limit)).fetchall()
                received_txids = {r["txid"] for r in rows}
            except Exception:
                received_txids = set()

            try:
                rows = self.conn.execute("SELECT DISTINCT spent_by FROM utxos WHERE address = ? AND spent_by IS NOT NULL LIMIT ?", (address, limit)).fetchall()
                sent_txids = {r["spent_by"] for r in rows if r["spent_by"]}
            except Exception:
                sent_txids = set()

            for txid_str in (received_txids | sent_txids):
                try:
                    row = self.conn.execute("SELECT raw, block_height FROM transactions WHERE txid = ?", (txid_str,)).fetchone()
                except Exception:
                    continue
                if not row or row["raw"] is None:
                    continue
                try:
                    tx = orjson.loads(zlib.decompress(row["raw"]))
                except Exception:
                    continue
                result.append({"tx": tx, "block_height": int(row["block_height"]), "is_mempool": False})
                seen_txids.add(txid_str)

            try:
                mp_rows = self.conn.execute("SELECT txid, raw FROM mempool").fetchall()
            except Exception:
                mp_rows = []

            for r in mp_rows:
                txid_str = r["txid"]
                if txid_str in seen_txids: continue
                try:
                    tx = orjson.loads(zlib.decompress(r["raw"]))
                except Exception:
                    continue

                touches = False
                for o in tx.get("outputs", []):
                    if o.get("address") == address:
                        touches = True
                        break

                if not touches:
                    for inp in tx.get("inputs", []):
                        if inp.get("txid") == "0" * 64: continue
                        try:
                            row_u = self.conn.execute("SELECT address FROM utxos WHERE txid = ? AND vout = ?", (inp.get("txid"), inp.get("vout"))).fetchone()
                        except Exception:
                            continue
                        if row_u and row_u["address"] == address:
                            touches = True
                            break

                if touches:
                    result.append({"tx": tx, "block_height": None, "is_mempool": True})

        result.sort(key=lambda x: (0 if x["is_mempool"] else 1, -(x["block_height"] or 0)))
        return result[:limit]

    # ==================== UTXO ====================
    def balance(self, address):
        row = self.conn.execute("SELECT COALESCE(SUM(amount),0) AS s FROM utxos WHERE address=? AND spent=0", (address,)).fetchone()
        return int(row["s"])

    def utxos_for(self, address):
        rows = self.conn.execute("SELECT txid,vout,amount,pubkey FROM utxos WHERE address=? AND spent=0 ORDER BY amount DESC", (address,)).fetchall()
        return [dict(r) for r in rows]

    def get_utxos(self, address):
        return self.utxos_for(address)

    def get_utxo(self, txid, vout):
        row = self.conn.execute("SELECT * FROM utxos WHERE txid=? AND vout=? AND spent=0", (txid, vout)).fetchone()
        return dict(row) if row else None

    def apply_tx(self, tx, height, coinbase=False):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
                if not coinbase:
                    for inp in tx["inputs"]:
                        self.conn.execute("UPDATE utxos SET spent=1, spent_by=? WHERE txid=? AND vout=? AND spent=0", (tx["txid"], inp["txid"], inp["vout"]))
                for i, out in enumerate(tx["outputs"]):
                    self.conn.execute("INSERT OR REPLACE INTO utxos(txid,vout,address,amount,pubkey,block_height,spent) VALUES (?,?,?,?,?,?,0)", (tx["txid"], i, out["address"], out["amount"], out.get("pubkey", ""), height))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def rollback_block(self, height):
        with self.lock:
            block = self.get_block(height)
            if not block: return
            self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
                for tx in reversed(block["transactions"]):
                    for i in range(len(tx["outputs"])):
                        self.conn.execute("DELETE FROM utxos WHERE txid=? AND vout=?", (tx["txid"], i))
                    if height > 0:
                        for inp in tx.get("inputs", []):
                            if inp["txid"] == "0" * 64: continue
                            self.conn.execute("UPDATE utxos SET spent=0, spent_by=NULL WHERE txid=? AND vout=?", (inp["txid"], inp["vout"]))
                    self.conn.execute("DELETE FROM transactions WHERE txid=?", (tx["txid"],))
                self.conn.execute("DELETE FROM blocks WHERE height=?", (height,))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # ==================== NONCE ====================
    def get_next_nonce(self, address: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM utxos WHERE address=? AND spent=1", (address,)).fetchone()
        return int(row["c"])

    def get_nonce_for_pubkey(self, pubkey: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM utxos WHERE pubkey=? AND spent=1", (pubkey,)).fetchone()
        return int(row["c"])

    # ==================== MEMPOOL ====================
    def add_mempool(self, tx, fee):
        with self.lock:
            try:
                self.conn.execute("INSERT INTO mempool(txid,raw,fee,received_at) VALUES (?,?,?,?)", (tx["txid"], zlib.compress(orjson.dumps(tx)), fee, time.time()))
            except sqlite3.IntegrityError:
                return False
            self._prune_mempool()
            return True

    def _prune_mempool(self, max_size=1000, ttl=86400):
        self.conn.execute("DELETE FROM mempool WHERE received_at < ?", (time.time() - ttl,))
        n = self.conn.execute("SELECT COUNT(*) AS c FROM mempool").fetchone()["c"]
        if n > max_size:
            self.conn.execute("DELETE FROM mempool WHERE txid IN (SELECT txid FROM mempool ORDER BY fee ASC, received_at ASC LIMIT ?)", (n - max_size,))

    def has_mempool(self, txid):
        return self.conn.execute("SELECT 1 FROM mempool WHERE txid=?", (txid,)).fetchone() is not None

    def get_mempool_tx(self, txid):
        row = self.conn.execute("SELECT raw FROM mempool WHERE txid=?", (txid,)).fetchone()
        return orjson.loads(zlib.decompress(row["raw"])) if row else None

    # [FIX v6.6] lock na leitura da mempool
    def all_mempool(self, limit=1000):
        with self.lock:
            rows = self.conn.execute("SELECT raw FROM mempool ORDER BY fee DESC LIMIT ?", (limit,)).fetchall()
            return [orjson.loads(zlib.decompress(r["raw"])) for r in rows]

    def remove_mempool(self, txid):
        with self.lock:
            self.conn.execute("DELETE FROM mempool WHERE txid=?", (txid,))

    def mempool_count(self):
        return self.conn.execute("SELECT COUNT(*) AS c FROM mempool").fetchone()["c"]

    def mempool_stats(self):
        row = self.conn.execute("SELECT COUNT(*) AS c, COALESCE(SUM(fee),0) AS sf FROM mempool").fetchone()
        fees = self.conn.execute("SELECT fee FROM mempool ORDER BY fee DESC LIMIT 100").fetchall()
        return {"count": row["c"], "sum_fee": row["sf"], "fees": [f["fee"] for f in fees]}

    # [FIX v6.6] lock na checagem de double-spend
    def utxos_gastos_na_mempool(self) -> set:
        gastos = set()
        with self.lock:
            try:
                rows = self.conn.execute("SELECT raw FROM mempool").fetchall()
                for r in rows:
                    try:
                        tx = orjson.loads(zlib.decompress(r["raw"]))
                        for inp in tx.get("inputs", []):
                            tid = inp.get("txid", "")
                            if tid and tid != "0" * 64:
                                gastos.add((tid, int(inp.get("vout", 0))))
                    except Exception:
                        continue
            except Exception:
                pass
        return gastos

    # ==================== L2 ====================
    def save_l2_escrow(self, escrow_dict):
        with self.lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO l2_escrows
                (escrow_id, creator, buyer, brn_amount, btc_expected_sats,
                 btc_address, btc_txid, status, created_at, expires_at,
                 l2_hash, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                escrow_dict['escrow_id'], escrow_dict['creator'],
                escrow_dict['buyer'], escrow_dict['brn_amount'],
                escrow_dict['btc_expected_sats'], escrow_dict['btc_address'],
                escrow_dict.get('btc_txid'), escrow_dict['status'],
                escrow_dict['created_at'], escrow_dict['expires_at'],
                escrow_dict['l2_hash'], json.dumps(escrow_dict)))

    def get_l2_escrows(self, status=None):
        if status:
            rows = self.conn.execute("SELECT raw_json FROM l2_escrows WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
        else:
            rows = self.conn.execute("SELECT raw_json FROM l2_escrows ORDER BY created_at DESC").fetchall()
        result = []
        for r in rows:
            try: result.append(json.loads(r["raw_json"]))
            except Exception: pass
        return result

    def get_l2_escrow_by_id(self, escrow_id):
        row = self.conn.execute("SELECT raw_json FROM l2_escrows WHERE escrow_id=?", (escrow_id,)).fetchone()
        if row:
            try: return json.loads(row["raw_json"])
            except Exception: return None
        return None

    def update_l2_escrow_status(self, escrow_id, status, btc_txid=None):
        with self.lock:
            escrow = self.get_l2_escrow_by_id(escrow_id)
            if not escrow: return
            escrow['status'] = status
            if btc_txid: escrow['btc_txid'] = btc_txid
            self.conn.execute("UPDATE l2_escrows SET status=?, btc_txid=?, raw_json=? WHERE escrow_id=?", (status, escrow.get('btc_txid'), json.dumps(escrow), escrow_id))

    def is_btc_txid_used(self, btc_txid):
        row = self.conn.execute("SELECT 1 FROM btc_used WHERE btc_txid=?", (btc_txid,)).fetchone()
        return row is not None

    def mark_btc_txid_used(self, btc_txid, escrow_id):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO btc_used (btc_txid, escrow_id, used_at) VALUES (?, ?, ?)", (btc_txid, escrow_id, int(time.time())))

    def save_l2_block(self, l2_block):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO l2_blocks (merkle_root, timestamp, count, raw_json) VALUES (?, ?, ?, ?)", (l2_block['merkle_root'], l2_block['timestamp'], l2_block.get('count', 1), json.dumps(l2_block)))

    def save_l2_tx(self, l2_tx):
        import hashlib
        with self.lock:
            txid = hashlib.sha256(json.dumps(l2_tx).encode()).hexdigest()[:16]
            self.conn.execute("INSERT OR REPLACE INTO l2_txs (txid, type, escrow_id, raw_json, timestamp) VALUES (?, ?, ?, ?, ?)", (txid, l2_tx['type'], l2_tx.get('escrow_id'), json.dumps(l2_tx), int(time.time())))
            return txid

    def get_l2_txs(self, limit=50):
        rows = self.conn.execute("SELECT raw_json FROM l2_txs ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r["raw_json"]) for r in rows]

    # ==================== ESTATÍSTICAS ====================
    def get_stats(self):
        return {
            "height": self.height(), "tip_hash": self.tip_hash(), "utxos": self.count_utxos(),
            "mempool": self.mempool_count(), "blocks": self.height() + 1, "contracts": self.contract_count(),
            "l2_escrows": self.conn.execute("SELECT COUNT(*) as c FROM l2_escrows").fetchone()["c"],
            "l2_txs": self.conn.execute("SELECT COUNT(*) as c FROM l2_txs").fetchone()["c"],
        }

    def prune_spent_utxos(self, keep_height=1000):
        cutoff = max(0, self.height() - keep_height)
        with self.lock:
            return self.conn.execute("DELETE FROM utxos WHERE spent=1 AND block_height < ?", (cutoff,)).rowcount

    def count_utxos(self):
        return self.conn.execute("SELECT COUNT(*) AS c FROM utxos WHERE spent=0").fetchone()["c"]

    def vacuum(self):
        with self.lock:
            self.conn.execute("VACUUM")

    # ==================== META ====================
    def set_meta(self, key, value):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, value))

    def get_meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # ==================== PEERS ====================
    def upsert_peer(self, node_id, address, genesis_hash, version="?", height=0, is_miner=False, public_key="", metadata=None):
        with self.lock:
            agora = int(time.time())
            existente = self.conn.execute("SELECT node_id FROM peers WHERE address=?", (address,)).fetchone()
            novo = existente is None
            self.conn.execute("""
                INSERT INTO peers (address, node_id, genesis_hash, version, height,
                                   is_miner, public_key, metadata, first_seen, last_seen, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(address) DO UPDATE SET
                    node_id=excluded.node_id, genesis_hash=excluded.genesis_hash,
                    version=excluded.version, height=excluded.height,
                    is_miner=excluded.is_miner, public_key=excluded.public_key,
                    metadata=excluded.metadata, last_seen=excluded.last_seen
            """, (address, node_id, genesis_hash, version, height, 1 if is_miner else 0, public_key, orjson.dumps(metadata or {}).decode(), agora, agora))
            if novo:
                self._log_event("peer_joined", address, f"node_id={node_id[:12]}")
            return novo

    def get_peer_score(self, address):
        row = self.conn.execute("SELECT score FROM peers WHERE address=?", (address,)).fetchone()
        return int(row["score"]) if row else 0

    def set_peer_score(self, address, score):
        with self.lock:
            self.conn.execute("UPDATE peers SET score=? WHERE address=?", (score, address))

    def add_peer_score(self, address, delta):
        with self.lock:
            self.conn.execute("UPDATE peers SET score = COALESCE(score, 0) + ? WHERE address=?", (delta, address))
            return self.get_peer_score(address)

    def listar_peers(self, apenas_ativos=True, janela=300):
        if apenas_ativos:
            cutoff = int(time.time()) - janela
            rows = self.conn.execute("SELECT * FROM peers WHERE last_seen >= ? ORDER BY last_seen DESC", (cutoff,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM peers ORDER BY last_seen DESC").fetchall()
        return [dict(r) for r in rows]

    def listar_peers_maus(self, score_min=-100):
        rows = self.conn.execute("SELECT * FROM peers WHERE score < ?", (score_min,)).fetchall()
        return [dict(r) for r in rows]

    def contar_peers(self, apenas_ativos=True, janela=300):
        if apenas_ativos:
            cutoff = int(time.time()) - janela
            row = self.conn.execute("SELECT COUNT(*) AS n FROM peers WHERE last_seen >= ?", (cutoff,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM peers").fetchone()
        return int(row["n"])

    def remover_peer(self, node_id):
        with self.lock:
            self.conn.execute("DELETE FROM peers WHERE node_id=?", (node_id,))

    def remover_peer_por_endereco(self, address):
        with self.lock:
            self.conn.execute("DELETE FROM peers WHERE address=?", (address,))

    def remover_peer_por_pubkey(self, pubkey: str) -> None:
        if not pubkey: return
        with self.lock:
            self.conn.execute("DELETE FROM peers WHERE public_key = ?", (pubkey,))

    def limpar_peers_inativos(self, janela=1800):
        with self.lock:
            self.conn.execute("DELETE FROM peers WHERE last_seen < ?", (int(time.time()) - janela,))

    # ==================== EVENTOS ====================
    def _log_event(self, tipo, peer="", details=""):
        with self.lock:
            self.conn.execute("INSERT INTO network_events (timestamp, event_type, peer_address, details) VALUES (?,?,?,?)", (int(time.time()), tipo, peer, details))

    def ultimos_eventos(self, n=50):
        rows = self.conn.execute("SELECT * FROM network_events ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    # ==================== CONTRATOS ====================
    def contract_insert(self, contract_id, owner, code, metadata=None):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO contracts(contract_id, owner, code, metadata, created_at) VALUES (?,?,?,?,?)", (contract_id, owner, orjson.dumps(code).decode(), orjson.dumps(metadata or {}).decode(), int(time.time())))
            self.conn.execute("INSERT OR REPLACE INTO contract_state(contract_id, state, updated_at) VALUES (?,?,?)", (contract_id, "{}", int(time.time())))

    def contract_get(self, contract_id):
        row = self.conn.execute("SELECT * FROM contracts WHERE contract_id=?", (contract_id,)).fetchone()
        if not row: return None
        d = dict(row)
        d["code"] = orjson.loads(d["code"])
        d["metadata"] = orjson.loads(d["metadata"] or "{}")
        return d

    def contract_list(self, owner=None, limit=100):
        if owner:
            rows = self.conn.execute("SELECT * FROM contracts WHERE owner=? ORDER BY created_at DESC LIMIT ?", (owner, limit)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM contracts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["code"] = orjson.loads(d["code"])
            d["metadata"] = orjson.loads(d["metadata"] or "{}")
            out.append(d)
        return out

    def contract_count(self):
        return self.conn.execute("SELECT COUNT(*) AS c FROM contracts").fetchone()["c"]

    def contract_delete(self, contract_id):
        with self.lock:
            self.conn.execute("DELETE FROM contracts WHERE contract_id=?", (contract_id,))
            self.conn.execute("DELETE FROM contract_state WHERE contract_id=?", (contract_id,))
            self.conn.execute("DELETE FROM contract_events WHERE contract_id=?", (contract_id,))

    def contract_get_state(self, contract_id):
        row = self.conn.execute("SELECT state FROM contract_state WHERE contract_id=?", (contract_id,)).fetchone()
        return orjson.loads(row["state"]) if row else {}

    def contract_set_state(self, contract_id, state):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO contract_state(contract_id, state, updated_at) VALUES (?,?,?)", (contract_id, orjson.dumps(state).decode(), int(time.time())))

    def contract_log_event(self, contract_id, event, data):
        with self.lock:
            self.conn.execute("INSERT INTO contract_events(contract_id, event, data, timestamp) VALUES (?,?,?,?)", (contract_id, event, orjson.dumps(data).decode() if data is not None else "null", int(time.time())))

    def contract_get_events(self, contract_id, limit=50):
        rows = self.conn.execute("SELECT * FROM contract_events WHERE contract_id=? ORDER BY id DESC LIMIT ?", (contract_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def contract_all_events(self, limit=200):
        rows = self.conn.execute("SELECT * FROM contract_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ==================== CONTRACT_SPEND ====================
    def contract_spend(self, from_addr: str, to_addr: str, amount: int):
        import hashlib
        import os as _os
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
                rows = self.conn.execute("SELECT txid, vout, amount FROM utxos WHERE address=? AND spent=0 ORDER BY amount DESC", (from_addr,)).fetchall()
                total = 0
                sacados = []
                for r in rows:
                    total += r["amount"]
                    sacados.append((r["txid"], r["vout"], r["amount"]))
                    if total >= amount: break
                if total < amount: raise ValueError(f"saldo insuficiente: {total} < {amount}")

                seed = (f"{from_addr}|{to_addr}|{amount}|{total}|{time.time_ns()}|{_os.urandom(8).hex()}").encode()
                txid_base = hashlib.sha256(seed).hexdigest()

                for txid, vout, _amt in sacados:
                    self.conn.execute("UPDATE utxos SET spent=1, spent_by=? WHERE txid=? AND vout=?", (txid_base, txid, vout))

                self.conn.execute("INSERT INTO utxos(txid, vout, address, amount, pubkey, block_height, spent) VALUES (?,?,?,?,?,?,0)", (txid_base, 0, to_addr, amount, "", self.height()))

                troco = total - amount
                if troco > 0:
                    self.conn.execute("INSERT INTO utxos(txid, vout, address, amount, pubkey, block_height, spent) VALUES (?,?,?,?,?,?,0)", (txid_base, 1, from_addr, troco, "", self.height()))

                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # ==================== CLOSE ====================
    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
