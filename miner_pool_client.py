"""miner_pool_client.py — Pool client BRN (v7.2)
================================================================
v7.2 (2026-11-12) — GATE DE MINERAÇÃO NO CLIENT:
  [NEW] _check_gates(): consulta /api/status e /health do pool e
        decide se pode minerar. Critérios:
          1. pool_height >= BRN_POOL_FAST_SYNC_UNTIL (default 6000)
          2. pool health: wallet_sk_installed == True
          3. pool health: validators_ready (quando exposto;
             senão assume OK para compat com server < v9.9.33)
        Retorna (ok, reason, detail).
  [NEW] start_mining() não minera imediatamente. Spawna
        _loop_gate_wait (30s). As threads de mineração
        (_loop_heartbeat, _loop_work, run/PoW) só sobem quando os
        gates passam. Se gates caem depois, as threads param
        via _mining_active.clear() e _loop_gate_wait re-spawna
        quando voltam.
  [NEW] status() inclui waiting_for_gates, gate_reason,
        gate_detail. /api/pool/local_status expõe isso direto
        (server copia o dict).
  [NEW] _http_get(): helper.
  [NEW] Env:
          BRN_POOL_FAST_SYNC_UNTIL (default 6000)
          BRN_POOL_GATE_POLL_INTERVAL (default 30s)
  [KEEP] v7.1: start_mining não-bloqueante, auto re-register,
        detecção de job desconhecido.
================================================================
"""
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
__version__ = "7.2"

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


def _http_get(url, timeout=10):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "msg": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


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
    def __init__(self, address: str, pubkey: str,
                 pool_url: str = None, miner_id: str = None):
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

        # [v7.2] Estado de gate
        self._mining_active = threading.Event()  # só minera quando setado
        self._gate_reason = "not_checked"
        self._gate_detail = {}
        self._mining_threads_spawned = False

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

    # --------------------------------------------------------
    # [v7.2] Gate
    # --------------------------------------------------------
    def _check_gates(self) -> tuple:
        """
        Consulta o pool e decide se pode minerar.

        Retorna (ok: bool, reason: str, detail: dict).
        """
        try:
            fast_sync_until = int(os.environ.get("BRN_POOL_FAST_SYNC_UNTIL", "6000"))
        except (ValueError, TypeError):
            fast_sync_until = 6000

        detail = {
            "pool_url": self.pool_url,
            "fast_sync_until": fast_sync_until,
            "pool_height": -1,
            "pool_sk_installed": None,
            "validators_ready": None,
            "fast_sync_active": None,
        }

        # 1. Altura + status do pool
        try:
            r = _http_get(f"{self.pool_url}/api/status", timeout=5.0)
            if not isinstance(r, dict):
                return False, "pool_status_invalid", detail
            if r.get("error"):
                return False, f"pool_status_error:{r['error']}", detail
            detail["pool_height"] = int(r.get("height", -1) or -1)
        except Exception as e:
            detail["error"] = str(e)
            return False, "pool_offline", detail

        # 2. Gate de altura (fast-sync)
        if fast_sync_until > 0 and detail["pool_height"] < fast_sync_until:
            return False, f"fast_sync_below_{fast_sync_until}", detail

        # 3. Consulta /health (SK + validadores)
        try:
            h = _http_get(f"{self.pool_url}/health", timeout=5.0)
            if isinstance(h, dict):
                detail["pool_sk_installed"] = bool(h.get("wallet_sk_installed", False))

                # sk_validation (main.py v13.9.17 expõe)
                sk_val = h.get("sk_validation") or {}
                if isinstance(sk_val, dict):
                    detail["fast_sync_active"] = bool(sk_val.get("fast_sync_active", False))
                    if sk_val.get("local_height") is not None:
                        # usa a altura mais atualizada
                        try:
                            lh = int(sk_val.get("local_height"))
                            if lh > detail["pool_height"]:
                                detail["pool_height"] = lh
                        except (ValueError, TypeError):
                            pass
                    if "validators_ready" in sk_val:
                        detail["validators_ready"] = bool(sk_val["validators_ready"])
                    if sk_val.get("validators_reason"):
                        detail["validators_reason"] = sk_val["validators_reason"]

                # validators (server v9.9.33 expõe)
                val = h.get("validators") or {}
                if isinstance(val, dict) and "validators_ready" in val:
                    detail["validators_ready"] = bool(val["validators_ready"])
                elif isinstance(val, dict) and "n_active" in val:
                    # fallback: se n_active > 0, considera OK
                    try:
                        detail["validators_ready"] = int(val.get("n_active", 0)) > 0
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            # /health indisponível: não bloqueia (compat com server antigo)
            detail["health_error"] = str(e)

        # 4. Gate de SK
        if detail["pool_sk_installed"] is False:
            return False, "pool_no_sk", detail

        # 5. Gate de validadores (só se explicitamente False)
        if detail["validators_ready"] is False:
            return False, "validators_not_synced", detail

        return True, "gates_ok", detail

    # --------------------------------------------------------
    # Ciclo de vida
    # --------------------------------------------------------
    def start_mining(self) -> tuple:
        """
        [v7.2] Não minera imediatamente. Spawna _loop_gate_wait.
        As threads de mineração só sobem quando os gates passam.
        """
        if self._running:
            return False, "ja rodando"

        self._stop.clear()
        self._mining_active.clear()
        self._running = True
        self.started_at = time.time()
        self._mining_threads_spawned = False
        self._gate_reason = "checking"

        threading.Thread(target=self._loop_gate_wait, daemon=True,
                         name="Pool-Gate").start()

        log.info("pool client: iniciado (miner_id=%s) — aguardando gates",
                 self.miner_id)
        return True, "iniciado (aguardando gates)"

    def stop_mining(self) -> tuple:
        if not self._running:
            return False, "nao estava rodando"
        self._stop.set()
        self._mining_active.clear()
        self._running = False
        try:
            _http_post(f"{self.pool_url}/api/pool/disconnect",
                       {"miner_id": self.miner_id})
        except Exception:
            pass
        log.info("pool client: parado")
        return True, "parado"

    def is_running(self) -> bool:
        return self._running

    def is_mining(self) -> bool:
        """[v7.2] True só quando gates passaram E thread de PoW está viva."""
        return self._running and self._mining_active.is_set()

    # --------------------------------------------------------
    # Loop de gate: espera gates → spawna mineração
    # --------------------------------------------------------
    def _loop_gate_wait(self):
        """
        Re-checa gates a cada POLL_GATE_S. Quando passam, spawna as
        threads de mineração (uma vez). Se caem, para as threads
        via _mining_active.clear() e marca como não-spawned para
        re-spawnar quando voltarem.
        """
        try:
            poll_gate_s = float(os.environ.get("BRN_POOL_GATE_POLL_INTERVAL", "30"))
        except (ValueError, TypeError):
            poll_gate_s = 30.0
        poll_gate_s = max(5.0, poll_gate_s)

        gate_ok_prev = False

        # Primeira checagem imediata
        while not self._stop.is_set():
            try:
                ok, reason, detail = self._check_gates()
                self._gate_reason = reason
                self._gate_detail = detail

                if ok and not gate_ok_prev:
                    log.info("pool client: ✅ gates OK (%s) — iniciando mineração",
                             reason)
                    self._mining_active.set()
                    if not self._mining_threads_spawned:
                        self._spawn_mining_threads()
                        self._mining_threads_spawned = True
                    gate_ok_prev = True
                elif ok and gate_ok_prev:
                    # Continua OK, sem log
                    self._mining_active.set()
                elif not ok:
                    if gate_ok_prev:
                        log.warning("pool client: ⚠️ gates caíram (%s) — "
                                    "parando mineração", reason)
                        self._mining_active.clear()
                        self._mining_threads_spawned = False
                    # Loga a primeira vez e a cada 10 polls
                    elif self._gate_reason != "not_checked":
                        pass  # já logado
                    gate_ok_prev = False
            except Exception as e:
                log.warning("pool client: gate check falhou: %s", e)

            if self._stop.wait(poll_gate_s):
                break

    def _spawn_mining_threads(self):
        """
        Spawna _loop_heartbeat, _loop_work e a thread `run` (PoW).
        Chamado quando gates passam pela primeira vez.
        """
        threading.Thread(target=self._loop_heartbeat, daemon=True,
                         name="Pool-HB").start()
        threading.Thread(target=self._loop_work, daemon=True,
                         name="Pool-Work").start()
        if not self.is_alive():
            try:
                self.start()
            except RuntimeError:
                # já foi startado antes; spawna um novo wrapper
                threading.Thread(target=self.run, daemon=True,
                                 name="Pool-PoW").start()

    # --------------------------------------------------------
    # Loops internos
    # --------------------------------------------------------
    def _loop_heartbeat(self):
        # Registra ao iniciar
        if not self._mining_active.is_set():
            return
        self._reregister()

        while not self._stop.wait(5.0):
            if not self._mining_active.is_set():
                return
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
            if not self._mining_active.is_set():
                return
            try:
                self._fetch_work()
            except Exception:
                pass

    def _fetch_work(self):
        r = _http_post(f"{self.pool_url}/api/pool/work",
                       {"miner_id": self.miner_id})
        if not r.get("ok"):
            self.last_error = r.get("msg", "work falhou")
            if "desconhecido" in self.last_error.lower():
                self._reregister()
            return
        job = r.get("job") or {}
        if "share_difficulty" not in job:
            job["share_difficulty"] = job.get("share_diff", 4)
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

    # --------------------------------------------------------
    # PoW
    # --------------------------------------------------------
    def run(self):
        # Espera o primeiro job
        for _ in range(10):
            if self._stop.is_set() or not self._mining_active.is_set():
                return
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
            if not self._mining_active.is_set():
                # Gates caíram; sai. _loop_gate_wait vai re-spawnar
                # quando voltarem.
                log.info("pool client: PoW pausado (gates caíram)")
                return

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
                if self._stop.is_set() or not self._mining_active.is_set():
                    break
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
                        if ("job expirado" in self.last_error.lower()
                                or "desconhecido" in self.last_error.lower()):
                            self._fetch_work()
                            break
                nonce += 1

            self.hashes_total += batch
            self.hashrate_meter.add(self.hashes_total)

    # --------------------------------------------------------
    # Status
    # --------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            job = self._job or {}
            sdiff = job.get("share_difficulty", 4)
            mining_active = self._mining_active.is_set()
            return {
                "ok": True,
                "running": self._running,
                "mining_active": mining_active,
                "waiting_for_gates": self._running and not mining_active,
                "gate_reason": self._gate_reason,
                "gate_detail": self._gate_detail,
                "miner_id": self.miner_id,
                "address": self.address,
                "hashrate": round(self.hashrate_meter.rate(), 2),
                "hashrate_khs": round(self.hashrate_meter.rate() / 1000, 3),
                "hashes_total": self.hashes_total,
                "shares_submitted": self.shares_submitted,
                "shares_accepted": self.shares_accepted,
                "shares_rejected": self.shares_rejected,
                "blocks_found": self.blocks_found,
                "uptime_s": (int(time.time() - self.started_at)
                             if self.started_at else 0),
                "last_error": self.last_error,
                "job_id": job.get("job_id"),
                "job_height": job.get("height"),
                "pool_url": self.pool_url,
                "share_difficulty": sdiff,
                "active_share_difficulty": sdiff,
                "job_share_diff": sdiff,
            }


try:
    from mining_pool import PoolManager
except Exception:
    PoolManager = None