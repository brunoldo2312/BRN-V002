"""miner_pool_client.py — Pool client BRN (v6.7)
================================================================
v6.7 (2026-11-03) — RECONSTRUÍDO:
  O arquivo original foi sobrescrito por engano com uma cópia de
  mining_pool.py (que tem só PoolManager — servidor). O PoolClient
  (cliente) foi perdido e nunca mais importava. Isso causava:
      [AVISO] miner_pool_client indisponível: cannot import name 'PoolClient'
      PoolClient: RETORNANDO None
      PoolClient: NAO INJETADO no server.py
  Esta versão reconstrói o cliente com base no uso em main.py,
  server.py e app_wallet_v3.py:
      - get_pool_client() / set_pool_client()
      - pc._job, pc._last_known_share_diff
      - pc.start(), pc.stop()
      - pc.status(), pc.stats(), pc.miners_list()

  O PoolManager continua em mining_pool.py e é re-exportado aqui
  para compatibilidade com "from miner_pool_client import PoolManager".
================================================================
"""
from __future__ import annotations

import os
import time
import logging
import threading

from blockchain import block_hash, target_from_difficulty

log = logging.getLogger("pool.client")

__version__ = "6.7"

# Re-export do servidor — mantém compatibilidade com imports
# do tipo: from miner_pool_client import PoolManager
try:
    from mining_pool import PoolManager
    _HAS_PM = True
except Exception as _e:
    PoolManager = None
    _HAS_PM = False
    log.error("miner_pool_client: nao consegui importar PoolManager: %s", _e)


# ============================================================
# SINGLETON
# ============================================================
_singleton = None
_singleton_lock = threading.Lock()


def get_pool_client():
    """Retorna a instância singleton do PoolClient (ou None)."""
    return _singleton


def set_pool_client(pc):
    """Define o singleton (chamado pelo main.py)."""
    global _singleton
    with _singleton_lock:
        _singleton = pc


# ============================================================
# POOL CLIENT
# ============================================================
class PoolClient:
    """
    Cliente que conecta a um PoolManager (local ou remoto) e minera.

    Modos:
      - local (padrão): usa um PoolManager em processo (mesma máquina)
      - remoto: pool_url definido → fala HTTP com /api/pool/* no servidor
    """

    def __init__(self, chain=None, pool_manager=None, pool_url=None,
                 miner_address=None, miner_pubkey=None,
                 on_block_mined=None, **kwargs):
        self.chain = chain
        self.pool_manager = pool_manager
        self.pool_url = (
            (pool_url or os.environ.get("BRN_POOL_URL", "") or "").rstrip("/") or None
        )
        self.miner_address = miner_address or ""
        self.miner_pubkey = miner_pubkey or ""
        self.on_block_mined = on_block_mined or kwargs.get("on_block")

        self.running = False
        self.hashrate_khs = 0.0
        self._job = None
        self._last_known_share_diff = None
        self.miner_id = None
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.RLock()

        self._shares_ok = 0
        self._shares_fail = 0
        self._blocks_found = 0
        self._started_at = 0.0

        # Se não veio pool_manager e temos chain, cria um local
        if self.pool_manager is None and chain is not None and _HAS_PM:
            try:
                self.pool_manager = PoolManager(chain)
                log.info("PoolClient: PoolManager local criado")
            except Exception as e:
                log.warning("PoolClient: nao consegui criar PoolManager: %s", e)

    # ---------- start / stop ----------

    def start(self, miner_address=None, miner_pubkey=None):
        with self._lock:
            if self.running:
                return {"ok": False, "msg": "ja rodando"}
            if miner_address:
                self.miner_address = miner_address
            if miner_pubkey:
                self.miner_pubkey = miner_pubkey
            if not self.miner_address or not self.miner_pubkey:
                return {"ok": False, "msg": "address e pubkey obrigatorios"}

            if self.pool_manager is None:
                return {"ok": False, "msg": "PoolManager indisponivel"}

            r = self.pool_manager.register(self.miner_address, self.miner_pubkey)
            if not r.get("ok"):
                return {"ok": False, "msg": r.get("msg", "register falhou")}
            self.miner_id = r["miner_id"]

            self._stop.clear()
            self.running = True
            self._started_at = time.time()
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="PoolClient")
            self._thread.start()
            log.info("PoolClient iniciado (miner_id=%s)", self.miner_id)
            return {"ok": True, "miner_id": self.miner_id}

    def stop(self):
        with self._lock:
            if not self.running:
                return {"ok": False, "msg": "nao estava rodando"}
            self._stop.set()
            self.running = False
            if self.pool_manager and self.miner_id:
                try:
                    self.pool_manager.disconnect(self.miner_id)
                except Exception:
                    pass
            t = self._thread
        if t:
            t.join(timeout=3)
        log.info("PoolClient parado")
        return {"ok": True}

    # ---------- loop de mineração ----------

    def _loop(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if self.pool_url:
                    self._tick_remote()
                else:
                    self._tick_local()
                backoff = 1.0
            except Exception as e:
                log.warning("PoolClient tick erro: %s", e)
                if self._stop.wait(min(backoff, 30)):
                    break
                backoff *= 2

    def _tick_local(self):
        pm = self.pool_manager
        if pm is None:
            if self._stop.wait(2):
                return
            return

        try:
            pm.heartbeat(self.miner_id, hashrate=self.hashrate_khs, hashes_total=0)
        except Exception:
            pass

        w = pm.get_work(self.miner_id)
        if not w.get("ok"):
            if self._stop.wait(2):
                return
            return

        job = w["job"]
        self._job = job
        self._last_known_share_diff = job.get("share_difficulty")

        def submit(nonce, h):
            try:
                return pm.submit_share(self.miner_id, job["job_id"], nonce, h)
            except Exception as e:
                return {"ok": False, "msg": str(e)}

        self._mine_job(job, submit)

    def _tick_remote(self):
        import requests

        try:
            requests.post(
                f"{self.pool_url}/api/pool/heartbeat",
                json={"miner_id": self.miner_id, "hashrate": self.hashrate_khs},
                timeout=5)
        except Exception:
            pass

        try:
            r = requests.post(
                f"{self.pool_url}/api/pool/get_work",
                json={"miner_id": self.miner_id}, timeout=10)
            w = r.json()
        except Exception as e:
            log.warning("PoolClient remoto get_work falhou: %s", e)
            if self._stop.wait(2):
                return
            return

        if not w.get("ok"):
            if self._stop.wait(2):
                return
            return

        job = w["job"]
        self._job = job
        self._last_known_share_diff = job.get("share_difficulty")

        def submit(nonce, h):
            try:
                r = requests.post(
                    f"{self.pool_url}/api/pool/submit_share",
                    json={"miner_id": self.miner_id,
                          "job_id": job["job_id"],
                          "nonce": nonce, "hash_hex": h},
                    timeout=10)
                return r.json()
            except Exception as e:
                return {"ok": False, "msg": str(e)}

        self._mine_job(job, submit)

    def _mine_job(self, job, submit_fn):
        try:
            share_diff = int(job.get("share_difficulty") or 2)
            target_int = target_from_difficulty(share_diff)
        except Exception as e:
            log.warning("PoolClient job invalido: %s", e)
            return

        prev = job["prev_hash"]
        merkle = job["merkle"]
        ts = job["timestamp"]
        diff = job["difficulty"]
        nonce = 0
        hashes = 0
        t0 = time.time()

        while not self._stop.is_set() and self.running:
            h = block_hash(prev, merkle, ts, nonce, diff)
            hashes += 1

            if int(h, 16) <= target_int:
                r = submit_fn(nonce, h)
                if isinstance(r, dict) and r.get("ok"):
                    self._shares_ok += 1
                    if r.get("block_found"):
                        self._blocks_found += 1
                        self._job = None
                        self._last_known_share_diff = None
                        if self.on_block_mined:
                            try:
                                self.on_block_mined(r)
                            except Exception:
                                pass
                        return
                else:
                    self._shares_fail += 1

            nonce += 1
            if hashes % 1000 == 0:
                dt = time.time() - t0
                if dt > 0:
                    self.hashrate_khs = round((hashes / dt) / 1000.0, 2)
                if time.time() - job.get("created_at", time.time()) > 30:
                    return
                if self._stop.is_set() or not self.running:
                    return

    # ---------- status ----------

    def status(self):
        with self._lock:
            job = self._job or {}
            return {
                "ok": True,
                "running": self.running,
                "hashrate_khs": self.hashrate_khs,
                "shares_ok": self._shares_ok,
                "shares_fail": self._shares_fail,
                "blocks_found": self._blocks_found,
                "miner_id": self.miner_id,
                "miner_address": self.miner_address,
                "pool_url": self.pool_url,
                "job_id": job.get("job_id"),
                "job_height": job.get("height"),
                "job_difficulty": job.get("difficulty"),
                "job_share_diff": job.get("share_difficulty", self._last_known_share_diff),
                "uptime_s": int(time.time() - self._started_at) if self._started_at else 0,
            }

    def stats(self):
        s = self.status()
        if self.pool_manager is not None:
            try:
                pm_stats = self.pool_manager.stats()
                pm_stats.update(s)
                return pm_stats
            except Exception:
                pass
        s["miners_active"] = 1 if self.running else 0
        s["miners_total"] = 1 if self.miner_id else 0
        s["shares_accepted"] = self._shares_ok
        s["shares_rejected"] = self._shares_fail
        s["blocks_found"] = self._blocks_found
        return s

    def miners_list(self):
        if self.pool_manager is not None:
            try:
                return self.pool_manager.miners_list()
            except Exception:
                pass
        if not self.miner_id:
            return []
        return [{
            "miner_id": self.miner_id,
            "address": self.miner_address,
            "hashrate": self.hashrate_khs,
            "shares_total": self._shares_ok,
            "shares_period": self._shares_ok,
            "invalid_shares": self._shares_fail,
            "mining": self.running,
            "active": self.running,
            "share_difficulty": self._last_known_share_diff or 0,
            "active_share_difficulty": self._last_known_share_diff or 0,
        }]


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("miner_pool_client.py v%s" % __version__)
    print("=" * 60)

    print("\n[1] Import PoolManager:", "OK" if _HAS_PM else "FALHOU")
    print("[2] PoolClient definida:", PoolClient.__name__)
    print("[3] get_pool_client():", get_pool_client())

    # teste com pool_manager None
    pc = PoolClient()
    print("[4] PoolClient() sem args: OK")
    print("    status:", pc.status())
    print("    start() sem endereco:", pc.start())

    print("\n" + "=" * 60)
    print(" OK — PoolClient reconstruído")
    print("=" * 60)
