"""miner_pool_client.py — Pool client BRN (v7.5)
================================================================
v7.5 (2026-11-22) — DEFAULTS SEGUROS E RACE-FREE:

  [BREAKING] pool_url SEM default. Requer BRN_POOL_URL ou argumento
             explícito. O default antigo apontava para o Render
             (brn-v002.onrender.com) e quebrava silenciosamente
             instalações self-hosted.
  [BREAKING] BRN_POOL_FAST_SYNC_UNTIL default agora 0 (desligado).
             Antes era 4084 hardcoded — após a rede passar de 4084,
             todos os mineradores travavam em "fast_sync_below_4084"
             para sempre sem explicação.
  [FIX] hashrate agora é amostrado a cada 250 hashes dentro do batch.
        Antes, só a cada 5000 → UI ficava em 0 kH/s por ~50s em
        máquinas lentas.
  [FIX] hashes_total conta por iteração, não += batch no fim. Antes,
        um stop no meio do batch inflava 5k hashes fantasmas.
  [FIX] _loop_gate_wait ganhou guard (_gate_thread) — stop/start
        rápido não duplica mais a thread de gate.
  [FIX] _spawn_mining_threads usa flags por tipo de thread; no fall
        de gate, limpa apenas o que morreu (HB, Work, PoW).
  [FIX] _fetch_work reage a "job expirado" e "desconhecido" →
        dispara _reregister() automaticamente.
  [FIX] run() faz self._running = False ao sair → is_alive() correto.
  [FIX] _gate_reason inicial = "starting" em vez de "not_checked".
  [KEEP] API pública (get_pool_client, set_pool_client,
         stop_pool_client, PoolClient) idêntica.
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
__version__ = "7.5"

HASHRATE_SAMPLE_EVERY = int(os.environ.get("BRN_POOL_HASHRATE_SAMPLE", "250"))
POW_BATCH_SIZE = int(os.environ.get("BRN_POOL_BATCH", "5000"))

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
    def __init__(self, window=5.0):
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
        # [FIX v7.5] sem default Render. Exige configuração explícita.
        self.pool_url = (pool_url or os.environ.get("BRN_POOL_URL", "")).rstrip("/")
        self.miner_id = miner_id or uuid.uuid4().hex[:12]

        self._stop = threading.Event()
        self._running = False
        self._lock = threading.RLock()

        self._mining_active = threading.Event()
        self._gate_reason = "starting"
        self._gate_detail = {}
        self._gate_thread: threading.Thread | None = None

        # [FIX v7.5] flags por tipo de thread
        self._hb_thread: threading.Thread | None = None
        self._work_thread: threading.Thread | None = None
        self._pow_thread: threading.Thread | None = None

        self.hashrate_meter = HashrateMeter(window=5.0)
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

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------
    def _check_gates(self) -> tuple:
        if not self.pool_url:
            return False, "pool_url_not_configured", {
                "pool_url": "",
                "hint": "defina BRN_POOL_URL (ex: http://127.0.0.1:8080)",
            }

        try:
            # [FIX v7.5] default 0 → desligado. Só ativa se o operador
            # quiser forçar. Evita deadlock eterno após altura 4084.
            fast_sync_until = int(os.environ.get("BRN_POOL_FAST_SYNC_UNTIL", "0"))
        except (ValueError, TypeError):
            fast_sync_until = 0

        detail = {
            "pool_url": self.pool_url,
            "fast_sync_until": fast_sync_until,
            "pool_height": -1,
            "pool_sk_installed": None,
            "validators_ready": None,
            "fast_sync_active": None,
        }

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

        if fast_sync_until > 0 and detail["pool_height"] < fast_sync_until:
            return False, f"fast_sync_below_{fast_sync_until}", detail

        try:
            h = _http_get(f"{self.pool_url}/health", timeout=5.0)
            if isinstance(h, dict):
                detail["pool_sk_installed"] = bool(h.get("wallet_sk_installed", False))

                sk_val = h.get("sk_validation") or {}
                if isinstance(sk_val, dict):
                    detail["fast_sync_active"] = bool(sk_val.get("fast_sync_active", False))
                    if sk_val.get("local_height") is not None:
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

                val = h.get("validators") or {}
                if isinstance(val, dict) and "validators_ready" in val:
                    detail["validators_ready"] = bool(val["validators_ready"])
                elif isinstance(val, dict) and "n_active" in val:
                    try:
                        detail["validators_ready"] = int(val.get("n_active", 0)) > 0
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            detail["health_error"] = str(e)

        if detail["pool_sk_installed"] is False:
            return False, "pool_no_sk", detail

        if detail["validators_ready"] is False:
            return False, "validators_not_synced", detail

        return True, "gates_ok", detail

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------
    def start_mining(self) -> tuple:
        if self._running:
            return False, "ja rodando"

        if not self.pool_url:
            msg = ("BRN_POOL_URL nao configurada — defina a URL do pool "
                   "(ex: http://127.0.0.1:8080 ou https://seu-pool.example)")
            self.last_error = msg
            log.error("pool client: %s", msg)
            return False, msg

        self._stop.clear()
        self._mining_active.clear()
        self._running = True
        self.started_at = time.time()
        self._gate_reason = "starting"
        self._gate_detail = {}
        self._hb_thread = None
        self._work_thread = None
        self._pow_thread = None

        # [FIX v7.5] guard contra gate thread duplicada
        with self._lock:
            if self._gate_thread is None or not self._gate_thread.is_alive():
                self._gate_thread = threading.Thread(
                    target=self._loop_gate_wait, daemon=True,
                    name="Pool-Gate")
                self._gate_thread.start()

        log.info("pool client: iniciado (miner_id=%s, pool=%s) — aguardando gates",
                 self.miner_id, self.pool_url)
        return True, "iniciado (aguardando gates)"

    def stop_mining(self) -> tuple:
        if not self._running:
            return False, "nao estava rodando"
        self._stop.set()
        self._mining_active.clear()
        self._running = False
        try:
            _http_post(f"{self.pool_url}/api/pool/disconnect",
                       {"miner_id": self.miner_id}, timeout=3.0)
        except Exception:
            pass
        log.info("pool client: parado")
        return True, "parado"

    def is_running(self) -> bool:
        return self._running

    def is_mining(self) -> bool:
        return self._running and self._mining_active.is_set()

    # ------------------------------------------------------------------
    # Gate loop
    # ------------------------------------------------------------------
    def _loop_gate_wait(self):
        try:
            poll_gate_s = float(os.environ.get("BRN_POOL_GATE_POLL_INTERVAL", "30"))
        except (ValueError, TypeError):
            poll_gate_s = 30.0
        poll_gate_s = max(5.0, poll_gate_s)

        gate_ok_prev = False

        while not self._stop.is_set():
            try:
                ok, reason, detail = self._check_gates()
                self._gate_reason = reason
                self._gate_detail = detail

                if ok and not gate_ok_prev:
                    log.info("pool client: gates OK (%s) — iniciando mineracao",
                             reason)
                    self._mining_active.set()
                    self._spawn_mining_threads()
                    gate_ok_prev = True
                elif ok and gate_ok_prev:
                    self._mining_active.set()
                elif not ok:
                    if gate_ok_prev:
                        log.warning("pool client: gates cairam (%s) — "
                                    "parando mineracao", reason)
                        self._mining_active.clear()
                        self._reset_dead_threads()
                    gate_ok_prev = False
            except Exception as e:
                log.warning("pool client: gate check falhou: %s", e)

            if self._stop.wait(poll_gate_s):
                break

    # ------------------------------------------------------------------
    # Worker threads
    # ------------------------------------------------------------------
    def _reset_dead_threads(self):
        """[FIX v7.5] limpa flags apenas para threads que já morreram."""
        with self._lock:
            for attr in ("_hb_thread", "_work_thread", "_pow_thread"):
                t = getattr(self, attr)
                if t is not None and not t.is_alive():
                    setattr(self, attr, None)

    def _spawn_mining_threads(self):
        with self._lock:
            # heartbeat
            if self._hb_thread is None or not self._hb_thread.is_alive():
                self._hb_thread = threading.Thread(
                    target=self._loop_heartbeat, daemon=True,
                    name="Pool-HB")
                self._hb_thread.start()

            # fetch de work
            if self._work_thread is None or not self._work_thread.is_alive():
                self._work_thread = threading.Thread(
                    target=self._loop_work, daemon=True,
                    name="Pool-Work")
                self._work_thread.start()

            # PoW
            if self._pow_thread is None or not self._pow_thread.is_alive():
                # self (PoolClient) é Thread; se já foi iniciado e morreu,
                # não pode ser re-startado — usa Thread alvo.
                if not self.is_alive() and self.ident is None:
                    self._pow_thread = self
                    try:
                        self.start()
                    except RuntimeError:
                        self._pow_thread = threading.Thread(
                            target=self.run, daemon=True, name="Pool-PoW")
                        self._pow_thread.start()
                else:
                    self._pow_thread = threading.Thread(
                        target=self.run, daemon=True, name="Pool-PoW")
                    self._pow_thread.start()

    # ------------------------------------------------------------------
    # Heartbeat / Work
    # ------------------------------------------------------------------
    def _loop_heartbeat(self):
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
            low = self.last_error.lower()
            if "desconhecido" in low or "expirado" in low:
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
                new_id = r.get("miner_id", self.miner_id)
                if new_id != self.miner_id:
                    log.info("pool client: miner_id atualizado %s -> %s",
                             self.miner_id, new_id)
                    self.miner_id = new_id
                self._fetch_work()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # PoW loop
    # ------------------------------------------------------------------
    def run(self):
        # Espera o primeiro work chegar
        for _ in range(10):
            if self._stop.is_set() or not self._mining_active.is_set():
                self._running = False
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
        sampled_at = self.hashes_total

        while not self._stop.is_set():
            if not self._mining_active.is_set():
                log.info("pool client: PoW pausado (gates caíram)")
                # [FIX v7.5] não zera _running aqui: _loop_gate_wait
                # gerencia. run() só sai.
                return

            with self._job_lock:
                job = self._job

            if not job:
                time.sleep(1.0)
                continue

            if job.get("job_id") != last_job_id:
                last_job_id = job.get("job_id")
                nonce = 0
                sampled_at = self.hashes_total

            share_diff = int(job.get("share_difficulty", 4))
            diff = int(job.get("difficulty", 5))
            target_int = target_from_difficulty(share_diff)

            prev = job["prev_hash"]
            merkle = job["merkle"]
            ts = int(job["timestamp"])

            for _i in range(POW_BATCH_SIZE):
                if self._stop.is_set() or not self._mining_active.is_set():
                    break

                h = block_hash(prev, merkle, ts, nonce, diff)
                self.hashes_total += 1

                # [FIX v7.5] amostra a cada HASHRATE_SAMPLE_EVERY hashes
                # para o hashrate fluir em sub-segundo.
                if self.hashes_total - sampled_at >= HASHRATE_SAMPLE_EVERY:
                    self.hashrate_meter.add(self.hashes_total)
                    sampled_at = self.hashes_total

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
                        low = self.last_error.lower()
                        if "job expirado" in low or "desconhecido" in low:
                            self._fetch_work()
                            break
                nonce += 1

            # flush final do batch
            self.hashrate_meter.add(self.hashes_total)
            sampled_at = self.hashes_total

        # saída limpa
        self._running = False
        log.info("pool client: run() encerrado")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            job = self._job or {}
            sdiff = job.get("share_difficulty", 4)
            mining_active = self._mining_active.is_set()
            rate = self.hashrate_meter.rate()
            return {
                "ok": True,
                "running": self._running,
                "mining_active": mining_active,
                "waiting_for_gates": self._running and not mining_active,
                "gate_reason": self._gate_reason,
                "gate_detail": self._gate_detail,
                "miner_id": self.miner_id,
                "address": self.address,
                "hashrate": round(rate, 2),
                "hashrate_khs": round(rate / 1000, 3),
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
