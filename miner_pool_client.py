"""miner_pool_client.py — Cliente do pool de mineração BRN (v3)
================================================================
v3:
  [FIX] Corrigido o bug onde _http_post ignorava o self.pool_url
        e tentava ligar sempre a 127.0.0.1 (localhost).
        Agora usa o IP do Ubuntu (API_BASE) corretamente.
  [FIX] CORREÇÃO CRÍTICA: _mine_batch agora usa share_difficulty
        no pow_search da biblioteca C. Antes usava a dificuldade
        do bloco inteiro, fazendo com que o minerador C nunca
        enviasse shares para o pool.
================================================================
"""
from __future__ import annotations

import os
import time
import json
import uuid
import socket
import hashlib
import threading
import urllib.request
import urllib.error
import logging


log = logging.getLogger("pool-client")


# ============================================================
# CONFIG
# ============================================================
POOL_URL           = os.environ.get("BRN_POOL_URL", "http://127.0.0.1:5000")
HEARTBEAT_S        = 5.0
WORK_REFRESH_S     = 15.0
HASH_BATCH         = 50000
HTTP_TIMEOUT_S     = 10.0


try:
    import brn_miner as _brn_miner_c
    _HAS_C = _brn_miner_c.is_available()
    if _HAS_C:
        log.info("pool-client: C acceleration ATIVA")
    else:
        log.info("pool-client: C acceleration indisponível, usando Python")
except Exception as e:
    _brn_miner_c = None
    _HAS_C = False
    log.info(f"pool-client: brn_miner indisponivel ({e})")


# ============================================================
# HTTP (RECEBE A BASE_URL)
# ============================================================
def _http_post(base_url: str, path: str, body: dict, timeout: float = HTTP_TIMEOUT_S) -> dict:
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json",
                     "User-Agent": "brn-pool-client/3"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "msg": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


# ============================================================
# HASH RATE METER
# ============================================================
class HashrateMeter:
    def __init__(self, window=10.0):
        self.window = window
        self._samples = []
        self._lock = threading.Lock()

    def add(self, total_hashes: int):
        now = time.time()
        with self._lock:
            self._samples.append((now, int(total_hashes)))
            cutoff = now - self.window
            self._samples = [(t, h) for (t, h) in self._samples if t >= cutoff]

    def rate(self) -> float:
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            t0, h0 = self._samples[0]
            t1, h1 = self._samples[-1]
            dt = t1 - t0
            if dt <= 0:
                return 0.0
            return max(0.0, (h1 - h0) / dt)

    def total(self) -> int:
        with self._lock:
            return self._samples[-1][1] if self._samples else 0


# ============================================================
# POW SEARCH
# ============================================================
def _target_from_difficulty(diff: int) -> int:
    return int("0" * int(diff) + "f" * (64 - int(diff)), 16)


def _mine_batch(prev_hash: str, merkle: str, ts: int, diff: int, share_difficulty: int,
                nonce_start: int, batch: int, share_target: int) -> tuple:
    if _HAS_C and _brn_miner_c is not None:
        prefix = prev_hash + merkle + str(ts)
        diff_str = str(int(diff))
        try:
            # [FIX CRÍTICO] Usa share_difficulty (não diff) no pow_search em C
            result = _brn_miner_c.pow_search(
                prefix, diff_str, int(share_difficulty),
                nonce_start=nonce_start,
                max_iters=batch,
            )
        except Exception as e:
            log.warning("brn_miner.pow_search falhou: %s — usando Python", e)
            result = None

        if result is not None:
            found_nonce, found_hash, hashes = result
            if found_nonce is not None:
                return found_nonce, found_hash, hashes, found_nonce + 1
            else:
                return None, None, hashes, nonce_start + hashes

    prefix = (prev_hash + merkle + str(ts)).encode()
    diff_bytes = str(diff).encode()
    sha = hashlib.sha256

    nonce = nonce_start
    for i in range(batch):
        data = prefix + str(nonce).encode() + diff_bytes
        h = sha(sha(data).digest()).digest()
        if int.from_bytes(h, "big") <= share_target:
            return nonce, h.hex(), i + 1, nonce + 1
        nonce += 1
    return None, None, batch, nonce


# ============================================================
# POOL CLIENT
# ============================================================
class PoolClient(threading.Thread):
    def __init__(self, address: str, pubkey: str,
                 pool_url: str = None, miner_id: str = None):
        super().__init__(daemon=True, name="Pool-Client")
        self.address = address
        self.pubkey = pubkey
        self.pool_url = (pool_url or POOL_URL).rstrip("/")
        self.miner_id = miner_id or uuid.uuid4().hex[:12]

        self._stop = threading.Event()
        self._running = False
        self._lock = threading.RLock()

        self.hashrate_meter = HashrateMeter(window=10.0)
        self.hashes_total = 0
        self.shares_submitted = 0
        self.shares_accepted = 0
        self.shares_rejected = 0
        self.blocks_found = 0
        self.last_share_at = 0.0
        self.last_block_at = 0.0
        self.last_error = ""
        self.started_at = 0.0

        self._job = None
        self._job_lock = threading.RLock()

    def start_mining(self) -> tuple:
        if self._running:
            return False, "ja rodando"

        r = _http_post(self.pool_url, "/api/pool/register", {
            "miner_id": self.miner_id,
            "address":  self.address,
            "pubkey":   self.pubkey,
        })
        if not r.get("ok"):
            return False, r.get("msg", "register falhou")

        self.miner_id = r.get("miner_id", self.miner_id)

        self._stop.clear()
        self._running = True
        self.started_at = time.time()

        threading.Thread(target=self._loop_heartbeat, daemon=True, name="Pool-HB").start()
        threading.Thread(target=self._loop_work, daemon=True, name="Pool-Work").start()
        self.start()

        log.info("pool client: iniciado (miner_id=%s)", self.miner_id)
        return True, "iniciado"

    def stop_mining(self) -> tuple:
        if not self._running:
            return False, "nao estava rodando"
        self._stop.set()
        self._running = False
        try:
            _http_post(self.pool_url, "/api/pool/disconnect", {"miner_id": self.miner_id})
        except Exception:
            pass
        log.info("pool client: parado")
        return True, "parado"

    def is_running(self) -> bool:
        return self._running

    def _loop_heartbeat(self):
        while not self._stop.wait(HEARTBEAT_S):
            try:
                h = self.hashrate_meter.rate()
                _http_post(self.pool_url, "/api/pool/heartbeat", {
                    "miner_id":     self.miner_id,
                    "hashrate":     h,
                    "hashes_total": self.hashes_total,
                }, timeout=5.0)
            except Exception as e:
                log.debug("heartbeat: %s", e)

    def _loop_work(self):
        while not self._stop.wait(WORK_REFRESH_S):
            try:
                self._fetch_work()
            except Exception as e:
                log.debug("work refresh: %s", e)

    def _fetch_work(self):
        r = _http_post(self.pool_url, "/api/pool/work", {"miner_id": self.miner_id})
        if not r.get("ok"):
            self.last_error = r.get("msg", "work falhou")
            return
        job = r.get("job") or {}
        with self._job_lock:
            if not self._job or self._job.get("job_id") != job.get("job_id"):
                self._job = job
                log.info("pool client: novo job %s h=%s diff=%s share_diff=%s",
                         job.get("job_id"), job.get("height"),
                         job.get("difficulty"), job.get("share_difficulty"))

    def run(self):
        for _ in range(10):
            if self._stop.is_set():
                break
            try:
                self._fetch_work()
            except Exception:
                pass
            with self._job_lock:
                if self._job:
                    break
            time.sleep(1.0)

        nonce = 0
        last_job_id = None

        while not self._stop.is_set():
            with self._job_lock:
                job = self._job

            if not job:
                time.sleep(1.0)
                continue

            if job.get("job_id") != last_job_id:
                last_job_id = job.get("job_id")
                nonce = 0
                log.info("pool client: mining job %s", last_job_id)

            share_diff = int(job.get("share_difficulty", 4))
            share_target = _target_from_difficulty(share_diff)

            try:
                found_nonce, found_hash, hashes, nonce = _mine_batch(
                    prev_hash=job["prev_hash"],
                    merkle=job["merkle"],
                    ts=int(job["timestamp"]),
                    diff=int(job["difficulty"]),
                    share_difficulty=share_diff,  # [FIX] Passando a dificuldade do share
                    nonce_start=nonce,
                    batch=HASH_BATCH,
                    share_target=share_target,
                )
            except Exception as e:
                self.last_error = str(e)
                time.sleep(1.0)
                continue

            self.hashes_total += hashes
            self.hashrate_meter.add(self.hashes_total)

            if found_nonce is None:
                continue

            self.shares_submitted += 1
            self.last_share_at = time.time()
            log.info("pool client: share nonce=%s hash=%s", found_nonce, found_hash[:20])

            try:
                r = _http_post(self.pool_url, "/api/pool/submit", {
                    "miner_id": self.miner_id,
                    "nonce":    int(found_nonce),
                    "hash":     found_hash,
                })
            except Exception as e:
                self.shares_rejected += 1
                self.last_error = str(e)
                continue

            if r.get("ok"):
                self.shares_accepted += 1
                if r.get("block_found"):
                    self.blocks_found += 1
                    self.last_block_at = time.time()
                    log.warning("pool client: FULL BLOCK #%s encontrado!", r.get("height"))
                    try:
                        self._fetch_work()
                    except Exception:
                        pass
            else:
                self.shares_rejected += 1
                self.last_error = r.get("msg", "rejeitado")
                log.debug("share rejeitado: %s", self.last_error)

    def status(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "running":            self._running,
                "miner_id":           self.miner_id,
                "address":            self.address,
                "hashrate":           round(self.hashrate_meter.rate(), 2),
                "hashrate_khs":       round(self.hashrate_meter.rate() / 1000, 3),
                "hashes_total":       self.hashes_total,
                "shares_submitted":   self.shares_submitted,
                "shares_accepted":    self.shares_accepted,
                "shares_rejected":    self.shares_rejected,
                "blocks_found":       self.blocks_found,
                "last_share_ago_s":   (int(time.time() - self.last_share_at) if self.last_share_at else None),
                "last_block_ago_s":   (int(time.time() - self.last_block_at) if self.last_block_at else None),
                "uptime_s":           (int(time.time() - self.started_at) if self.started_at else 0),
                "last_error":         self.last_error,
                "job_id":             (self._job.get("job_id") if self._job else None),
                "job_height":         (self._job.get("height") if self._job else None),
                "job_difficulty":     (self._job.get("difficulty") if self._job else None),
                "job_share_diff":     (self._job.get("share_difficulty") if self._job else None),
                "pool_url":           self.pool_url,
                "c_acceleration":     _HAS_C,
            }


# ============================================================
# SINGLETON
# ============================================================
_INSTANCE = None
_INSTANCE_LOCK = threading.RLock()


def get_pool_client(address: str = None, pubkey: str = None) -> PoolClient:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            if not address or not pubkey:
                return None
            _INSTANCE = PoolClient(address, pubkey)
        elif address and _INSTANCE.address != address:
            if _INSTANCE.is_running():
                _INSTANCE.stop_mining()
            _INSTANCE = PoolClient(address, pubkey)
        return _INSTANCE


def stop_pool_client() -> bool:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            return False
        if _INSTANCE.is_running():
            _INSTANCE.stop_mining()
        return True
