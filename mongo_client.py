"""miner_pool_client.py — Pool client BRN (v6.9)"""
from __future__ import annotations
import os
import time
import json
import uuid
import logging
import threading
import urllib.request
import urllib.error

from blockchain import block_hash, target_from_difficulty

log = logging.getLogger("pool.client")
__version__ = "6.9"

_INSTANCE = None
_INSTANCE_LOCK = threading.RLock()

def get_pool_client(address: str = None, pubkey: str = None) -> 'PoolClient':
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is not None:
            return _INSTANCE
        if not address or not pubkey:
            return None
        _INSTANCE = PoolClient(address, pubkey)
        return _INSTANCE

def set_pool_client(pc):
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = pc

def stop_pool_client() -> bool:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            return False
        if _INSTANCE.is_running():
            _INSTANCE.stop_mining()
        return True

def _http_post(url, data, timeout=10):
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
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

class HashrateMeter:
    def __init__(self, window=10.0):
        self.window = window
        self._samples = []
        self._lock = threading.Lock()

    def add(self, total_hashes):
        now = time.time()
        with self._lock:
            self._samples.append((now, int(total_hashes)))
            cutoff = now - self.window
            self._samples = [(t, h) for (t, h) in self._samples if t >= cutoff]

    def rate(self):
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            t0, h0 = self._samples[0]
            t1, h1 = self._samples[-1]
            dt = t1 - t0
            if dt <= 0:
                return 0.0
            return max(0.0, (h1 - h0) / dt)

class PoolClient(threading.Thread):
    def __init__(self, address: str, pubkey: str, pool_url: str = None, miner_id: str = None):
        super().__init__(daemon=True, name="Pool-Client")
        global _INSTANCE
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = self

        self.address = address
        self.pubkey = pubkey
        default_url = "http://127.0.0.1:5000"
        self.pool_url = (pool_url or os.environ.get("BRN_POOL_URL", default_url)).rstrip("/")
        self.miner_id = miner_id or uuid.uuid4().hex[:12]

        self._stop = threading.Event()
        self._running = False
        self._lock = threading.RLock()

        self.hashrate_meter = HashrateMeter()
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

        r = _http_post(f"{self.pool_url}/api/pool/register", {
            "miner_id": self.miner_id,
            "address":  self.address,
            "pubkey":   self.pubkey,
        })
        
        if not r.get("ok"):
            msg = r.get("msg", "register falhou")
            if "já registrado" not in msg.lower():
                log.error("PoolClient: falha ao registrar: %s", msg)
                return False, msg

        self.miner_id = r.get("miner_id", self.miner_id)

        self._stop.clear()
        self._running = True
        self.started_at = time.time()

        threading.Thread(target=self._loop_heartbeat, daemon=True, name="Pool-HB").start()
        threading.Thread(target=self._loop_work, daemon=True, name="Pool-Work").start()
        
        if not self.is_alive():
            self.start()

        log.info("pool client: iniciado (miner_id=%s)", self.miner_id)
        return True, "iniciado"

    def stop_mining(self) -> tuple:
        if not self._running:
            return False, "nao estava rodando"
        self._stop.set()
        self._running = False
        try:
            _http_post(f"{self.pool_url}/api/pool/disconnect", {"miner_id": self.miner_id})
        except Exception:
            pass
        log.info("pool client: parado")
        return True, "parado"

    def is_running(self) -> bool:
        return self._running

    def _loop_heartbeat(self):
        while not self._stop.wait(5.0):
            try:
                h = self.hashrate_meter.rate()
                r = _http_post(f"{self.pool_url}/api/pool/heartbeat", {
                    "miner_id":     self.miner_id,
                    "hashrate":     h,
                    "hashes_total": self.hashes_total,
                }, timeout=5.0)
                if not r.get("ok"):
                    msg = (r.get("msg") or "").lower()
                    if "desconhecido" in msg:
                        self._reregister()
            except Exception:
                pass

    def _loop_work(self):
        while not self._stop.wait(15.0):
            try:
                self._fetch_work()
            except Exception:
                pass

    def _fetch_work(self):
        r = _http_post(f"{self.pool_url}/api/pool/work", {"miner_id": self.miner_id})
        if not r.get("ok"):
            self.last_error = r.get("msg", "work falhou")
            return
        job = r.get("job") or {}
        with self._job_lock:
            if self._job is None or self._job.get("job_id") != job.get("job_id"):
                self._job = job

    def _reregister(self):
        try:
            r = _http_post(f"{self.pool_url}/api/pool/register", {
                "miner_id": self.miner_id,
                "address":  self.address,
                "pubkey":   self.pubkey,
            })
            if r.get("ok"):
                self.miner_id = r.get("miner_id", self.miner_id)
                self._fetch_work()
        except Exception:
            pass

    def run(self):
        for _ in range(10):
            if self._stop.is_set(): break
            try: self._fetch_work()
            except Exception: pass
            with self._job_lock:
                if self._job: break
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

            share_diff = int(job.get("share_difficulty", 4))
            diff = int(job.get("difficulty", 5))
            target_int = target_from_difficulty(share_diff)

            prev = job["prev_hash"]
            merkle = job["merkle"]
            ts = int(job["timestamp"])

            batch = 50000
            for i in range(batch):
                if self._stop.is_set() or not self._running: break
                h = block_hash(prev, merkle, ts, nonce, diff)
                
                if int(h, 16) <= target_int:
                    self.shares_submitted += 1
                    self.last_share_at = time.time()
                    
                    r = _http_post(f"{self.pool_url}/api/pool/submit", {
                        "miner_id": self.miner_id,
                        "job_id":   job.get("job_id"),
                        "nonce":    int(nonce),
                        "hash":     h,
                    })
                    
                    if r.get("ok"):
                        self.shares_accepted += 1
                        if r.get("block_found"):
                            self.blocks_found += 1
                            self.last_block_at = time.time()
                            self._fetch_work()
                            break
                    else:
                        self.shares_rejected += 1
                        self.last_error = r.get("msg", "rejeitado")
                        if "job expirado" in self.last_error.lower() or "desconhecido" in self.last_error.lower():
                            self._fetch_work()
                            break
                nonce += 1

            self.hashes_total += batch
            self.hashrate_meter.add(self.hashes_total)

    def status(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "running": self._running,
                "miner_id": self.miner_id,
                "address": self.address,
                "hashrate": round(self.hashrate_meter.rate(), 2),
                "hashrate_khs": round(self.hashrate_meter.rate() / 1000, 3),
                "hashes_total": self.hashes_total,
                "shares_submitted": self.shares_submitted,
                "shares_accepted": self.shares_accepted,
                "shares_rejected": self.shares_rejected,
                "blocks_found": self.blocks_found,
                "uptime_s": (int(time.time() - self.started_at) if self.started_at else 0),
                "last_error": self.last_error,
                "job_id": (self._job.get("job_id") if self._job else None),
                "job_height": (self._job.get("height") if self._job else None),
                "pool_url": self.pool_url,
            }

try:
    from mining_pool import PoolManager
except Exception:
    PoolManager = None
