"""mining_pool.py — Pool de mineração BRN (v4.1)
================================================================
v4.1:
  [FIX] A fila de mineração e o estado da Fase (1 ou 2) agora são
        lidos diretamente da blockchain (chain.fila_mineracao e
        chain.rede_descentralizada). Antes o pool mantinha uma fila
        paralela e causava divergência entre o Painel e a Carteira.
v4:
  [NEW] Integração da lógica de Fases (Bootstrap 20 mineradores).
  [NEW] Fila de espera visual mantida em memória.
  [NEW] Transição automática para Fase 2 (Descentralizada).
  [KEEP] Compatibilidade total com server.py e blockchain.py.
================================================================
"""
from __future__ import annotations

import time
import uuid
import threading
import logging
from typing import Optional
from blockchain import block_hash, compute_merkle_root, meets_difficulty, txid as calc_txid

log = logging.getLogger("pool")

SHARE_DIFF_OFFSET   = 1
MINER_TIMEOUT_S     = 60
JOB_REFRESH_S       = 20
MAX_MINERS          = 50
MAX_RECENT_JOBS     = 5

class MinerState:
    __slots__ = ("miner_id", "address", "pubkey", "shares_period", "shares_total", 
                 "hashes_total", "hashrate", "last_seen", "connected_at", "mining")
    def __init__(self, miner_id, address, pubkey):
        self.miner_id = miner_id
        self.address = address
        self.pubkey = pubkey
        self.shares_period = 0
        self.shares_total = 0
        self.hashes_total = 0
        self.hashrate = 0.0
        self.last_seen = time.time()
        self.connected_at = time.time()
        self.mining = False

class Job:
    __slots__ = ("job_id", "height", "prev_hash", "merkle", "timestamp", "difficulty", 
                 "share_difficulty", "transactions", "distribution", "created_at")
    def __init__(self, height, prev_hash, merkle, timestamp, difficulty, share_difficulty, transactions, distribution):
        self.job_id = uuid.uuid4().hex[:16]
        self.height = height
        self.prev_hash = prev_hash
        self.merkle = merkle
        self.timestamp = timestamp
        self.difficulty = difficulty
        self.share_difficulty = share_difficulty
        self.transactions = transactions
        self.distribution = distribution
        self.created_at = time.time()

def _target(diff: int) -> int:
    return int("0" * int(diff) + "f" * (64 - int(diff)), 16)

def _meets(h_hex: str, diff: int) -> bool:
    try: return int(h_hex, 16) <= _target(diff)
    except Exception: return False

def _make_pool_coinbase(height: int, reward: int, distribution: dict) -> dict:
    if not distribution:
        return {"txid": "", "inputs": [{"txid": "0" * 64, "vout": 0xFFFFFFFF, "pubkey": "", "signature": ""}], "outputs": [{"address": "brn1poolfallbackxxxxxxxxxxxxxxxxxxx", "amount": reward, "pubkey": "00" * 33}], "timestamp": int(time.time()), "locktime": 0, "height": height, "nonce": 0}
    total_weight = sum(d["weight"] for d in distribution.values()) or 1.0
    outputs = []
    for addr, info in distribution.items():
        amt = int(reward * (info["weight"] / total_weight))
        if amt <= 0: continue
        outputs.append({"address": addr, "amount": amt, "pubkey": info["pubkey"] or ("00" * 33)})
    if not outputs:
        first = max(distribution.items(), key=lambda kv: kv[1]["weight"])
        outputs = [{"address": first[0], "amount": reward, "pubkey": first[1]["pubkey"]}]
    dif = reward - sum(o["amount"] for o in outputs)
    if dif > 0: outputs[0]["amount"] += dif
    cb = {"txid": "", "inputs": [{"txid": "0" * 64, "vout": 0xFFFFFFFF, "pubkey": "", "signature": ""}], "outputs": outputs, "timestamp": int(time.time()), "locktime": 0, "height": int(height), "nonce": 0}
    cb["txid"] = calc_txid(cb)
    return cb

class PoolManager:
    def __init__(self, chain):
        self.chain = chain
        self.lock = threading.RLock()
        self.miners = {}
        self.current_job = None
        self.recent_jobs = {}
        self.started_at = time.time()
        self.blocks_found = 0
        self.total_reward_paid = 0
        self.last_block_at = 0.0
        self.last_block_height = -1
        self.rejected_shares = 0
        self.accepted_shares = 0
        
        # Estado da Fase (Bootstrap) - Sincronizado com a blockchain
        self.phase = 2 if self.chain.rede_descentralizada else 1
        self.phase2_threshold = 20
        self.miner_queue = list(self.chain.fila_mineracao.keys())

        self._stop = threading.Event()
        threading.Thread(target=self._maintenance_loop, daemon=True, name="Pool-Maint").start()
        log.info("PoolManager iniciado — Sincronizado com a Blockchain")

    def register(self, address: str, pubkey: str, miner_id: Optional[str] = None) -> dict:
        if not address or not address.startswith("brn1"): return {"ok": False, "msg": "address invalido"}
        if not pubkey: return {"ok": False, "msg": "pubkey obrigatoria"}
        with self.lock:
            if len(self.miners) >= MAX_MINERS and miner_id not in self.miners: return {"ok": False, "msg": "pool cheio"}
            if not miner_id: miner_id = uuid.uuid4().hex[:12]
            m = self.miners.get(miner_id)
            if m is None:
                m = MinerState(miner_id, address, pubkey)
                self.miners[miner_id] = m
                log.info("pool: %s registado (addr=%s)", miner_id, address[:16])
            else:
                m.address = address; m.pubkey = pubkey; m.last_seen = time.time()

            # [FIX v4.1] Lê o estado real da blockchain
            self.phase = 2 if self.chain.rede_descentralizada else 1
            self.miner_queue = list(self.chain.fila_mineracao.keys())
            
        return {"ok": True, "miner_id": miner_id}

    def heartbeat(self, miner_id: str, hashrate: float = 0.0, hashes_total: int = 0) -> dict:
        with self.lock:
            m = self.miners.get(miner_id)
            if m is None: return {"ok": False, "msg": "miner desconhecido"}
            m.last_seen = time.time()
            m.hashrate = float(hashrate or 0.0)
            if hashes_total > m.hashes_total: m.hashes_total = int(hashes_total)
        return {"ok": True}

    def disconnect(self, miner_id: str) -> dict:
        with self.lock:
            m = self.miners.pop(miner_id, None)
        return {"ok": True}

    def get_work(self, miner_id: str) -> dict:
        with self.lock:
            m = self.miners.get(miner_id)
            if m is None: return {"ok": False, "msg": "miner desconhecido"}
            m.last_seen = time.time(); m.mining = True
            job = self.current_job
            if job is None or self._job_is_stale(job): job = self._build_job_locked()
        return {"ok": True, "job": {"job_id": job.job_id, "height": job.height, "prev_hash": job.prev_hash, "merkle": job.merkle, "timestamp": job.timestamp, "difficulty": job.difficulty, "share_difficulty": job.share_difficulty, "coinbase_txid": job.transactions[0]["txid"], "n_txs": len(job.transactions), "created_at": job.created_at}}

    def _job_is_stale(self, job: Job) -> bool:
        if time.time() - job.created_at > JOB_REFRESH_S: return True
        try:
            tip = self.chain.db.tip_hash()
            if tip != job.prev_hash: return True
        except Exception: return True
        return False

    def _build_job_locked(self) -> Job:
        h = self.chain.db.height() + 1
        reward = self.chain.current_reward(h)
        diff = self.chain.current_difficulty()
        share_diff = max(2, diff - SHARE_DIFF_OFFSET)
        prev_hash = self.chain.db.tip_hash()
        
        distribution = {}
        for mid, m in self.miners.items():
            if (time.time() - m.last_seen) > MINER_TIMEOUT_S: continue
            if not m.address or not m.pubkey: continue
            w = float(m.shares_period) + 1.0
            distribution[m.address] = {"pubkey": m.pubkey, "weight": w}

        # Se estiver na Fase 2, a recompensa é 100% para quem encontrar.
        cb = _make_pool_coinbase(h, reward, distribution)
        
        try: mempool_txs = self.chain.db.all_mempool(limit=200)
        except Exception: mempool_txs = []
        transactions = [cb] + mempool_txs
        merkle = compute_merkle_root([t["txid"] for t in transactions])
        
        job = Job(height=h, prev_hash=prev_hash, merkle=merkle, timestamp=int(time.time()), difficulty=diff, share_difficulty=share_diff, transactions=transactions, distribution=distribution)
        self.current_job = job
        self.recent_jobs[job.job_id] = job
        if len(self.recent_jobs) > MAX_RECENT_JOBS:
            oldest = sorted(self.recent_jobs.items(), key=lambda kv: kv[1].created_at)[:1]
            for k, _ in oldest: self.recent_jobs.pop(k, None)
        log.info("pool: novo job %s (h=%d diff=%d share_diff=%d miners=%d)", job.job_id, h, diff, share_diff, len(distribution))
        return job

    def submit_share(self, miner_id: str, nonce: int, hash_hex: str = "") -> dict:
        with self.lock:
            m = self.miners.get(miner_id)
            if m is None: return {"ok": False, "msg": "miner desconhecido"}
            m.last_seen = time.time()
            job = self.current_job
            if job is None: return {"ok": False, "msg": "sem job ativo"}
            
            candidates = [job]
            for j in self.recent_jobs.values():
                if j.job_id != job.job_id: candidates.append(j)
            
            h = None; matched_job = None
            for cand in candidates:
                try: h2 = block_hash(cand.prev_hash, cand.merkle, cand.timestamp, int(nonce), cand.difficulty)
                except Exception: continue
                if _meets(h2, cand.share_difficulty):
                    h = h2; matched_job = cand; break
                    
            if matched_job is None:
                self.rejected_shares += 1; return {"ok": False, "msg": "share nao bate share_difficulty de nenhum job"}
            
            job = matched_job
            if hash_hex and hash_hex != h:
                self.rejected_shares += 1; return {"ok": False, "msg": "hash nao corresponde ao nonce"}
            
            m.shares_period += 1; m.shares_total += 1; self.accepted_shares += 1
            block_found = False; new_height = None
            
            if _meets(h, job.difficulty):
                log.warning("pool: FULL BLOCK encontrado por %s (nonce=%s, h=%d)", miner_id, nonce, job.height)
                try:
                    ok = self._assemble_and_accept_block(job, int(nonce), h)
                    if ok:
                        block_found = True; new_height = job.height
                        self.blocks_found += 1; self.last_block_at = time.time(); self.last_block_height = job.height
                        for mm in self.miners.values(): mm.shares_period = 0
                        self.current_job = None
                    else: log.warning("pool: accept_block falhou")
                except Exception as e: log.exception("pool: erro aceitando bloco: %s", e)
        return {"ok": True, "share_id": uuid.uuid4().hex[:12], "block_found": block_found, "height": new_height, "job_id": job.job_id, "share_difficulty": job.share_difficulty}

    def _assemble_and_accept_block(self, job: Job, nonce: int, hash_hex: str) -> bool:
        cb = job.transactions[0]
        for out in cb["outputs"]:
            if out["address"] not in job.distribution:
                log.error(f"[POOL] Bloco rejeitado: endereço suspeito no coinbase: {out['address']}")
                return False
        
        block = {"height": int(job.height), "hash": str(hash_hex), "prev_hash": str(job.prev_hash), "timestamp": int(job.timestamp), "nonce": int(nonce), "merkle": str(job.merkle), "difficulty": int(job.difficulty), "transactions": job.transactions}
        try: self.chain._attach_auth_tag(block)
        except Exception: pass
        ok, msg = self.chain.accept_block(block)
        if not ok:
            log.warning("pool: accept_block: %s", msg); return False
        try:
            cb = block["transactions"][0]
            reward = sum(int(o["amount"]) for o in cb.get("outputs", []))
            self.total_reward_paid += int(reward)
        except Exception: pass
        return True

    def stats(self) -> dict:
        with self.lock:
            # [FIX v4.1] Lê o estado real da blockchain antes de gerar o status
            self.phase = 2 if self.chain.rede_descentralizada else 1
            self.miner_queue = list(self.chain.fila_mineracao.keys())

            now = time.time()
            active = [m for m in self.miners.values() if now - m.last_seen < MINER_TIMEOUT_S]
            total_hashrate = sum(m.hashrate for m in active)
            total_shares = sum(m.shares_period for m in active)
            current_job = self.current_job
            return {"ok": True, "uptime_s": int(now - self.started_at), "miners_active": len(active), "miners_total": len(self.miners), "hashrate_total": round(total_hashrate, 2), "shares_period": int(total_shares), "shares_accepted": self.accepted_shares, "shares_rejected": self.rejected_shares, "blocks_found": self.blocks_found, "total_reward_paid": self.total_reward_paid, "last_block_height": self.last_block_height, "last_block_ago_s": (int(now - self.last_block_at) if self.last_block_at else None), "job_active": current_job is not None, "job_id": (current_job.job_id if current_job else None), "job_height": (current_job.height if current_job else None), "job_difficulty": (current_job.difficulty if current_job else None), "job_share_difficulty": (current_job.share_difficulty if current_job else None), "job_created_ago_s": (int(now - current_job.created_at) if current_job else None), "recent_jobs_cached": len(self.recent_jobs), "phase": self.phase, "miner_queue_len": len(self.miner_queue)}

    def miners_list(self) -> list:
        with self.lock:
            now = time.time(); out = []
            for m in self.miners.values():
                out.append({"miner_id": m.miner_id, "address": m.address, "shares_period": m.shares_period, "shares_total": m.shares_total, "hashrate": round(m.hashrate, 2), "connected_s": int(now - m.connected_at), "last_seen_s": int(now - m.last_seen), "mining": m.mining, "active": (now - m.last_seen) < MINER_TIMEOUT_S})
            out.sort(key=lambda x: x["hashrate"], reverse=True); return out

    def miner_status(self, miner_id: str) -> dict:
        with self.lock:
            m = self.miners.get(miner_id)
            if m is None: return {"ok": False, "msg": "miner desconhecido"}
            total_shares = sum(x.shares_period for x in self.miners.values())
            reward = self.chain.current_reward(self.chain.db.height() + 1)
            est_reward = 0
            if total_shares > 0: est_reward = int(reward * (m.shares_period / total_shares))
            return {"ok": True, "miner_id": m.miner_id, "address": m.address, "hashrate": round(m.hashrate, 2), "shares_period": m.shares_period, "shares_total": m.shares_total, "hashes_total": m.hashes_total, "connected_s": int(time.time() - m.connected_at), "last_seen_s": int(time.time() - m.last_seen), "mining": m.mining, "est_reward_sats": est_reward, "pool_total_shares": int(total_shares)}

    def _maintenance_loop(self):
        while not self._stop.is_set():
            try:
                time.sleep(5)
                with self.lock:
                    now = time.time()
                    gone = [mid for mid, m in self.miners.items() if now - m.last_seen > MINER_TIMEOUT_S * 3]
                    for mid in gone:
                        m = self.miners.pop(mid, None)
                        log.info("pool: miner %s removido (timeout)", mid)
                    if (self.current_job is None or self._job_is_stale(self.current_job)):
                        if self.miners: self._build_job_locked()
            except Exception as e: log.warning("pool maint: %s", e)

    def stop(self):
        self._stop.set()
