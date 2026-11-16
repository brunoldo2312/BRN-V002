import time
import uuid
import logging
import threading
from blockchain import block_hash, compute_merkle_root, make_coinbase, meets_difficulty

log = logging.getLogger("pool")

class PoolManager:
    def __init__(self, chain):
        self.chain = chain
        self.miners = {}
        self.lock = threading.RLock()
        self.current_job = None
        # Endereço inicial placeholder (só será usado até o primeiro minerador se registrar)
        self._generate_job("brn1pooladdressplaceholder")

    def _generate_job(self, miner_address="brn1pooladdressplaceholder"):
        with self.lock:
            height = self.chain.db.height() + 1
            prev_hash = self.chain.db.tip_hash()
            reward = self.chain.current_reward(height)
            # [FIX CRÍTICO] Usa o endereço real do minerador no coinbase
            coinbase = make_coinbase(miner_address, "0"*66, reward, height)
            txs = [coinbase] + self.chain.db.all_mempool(limit=100)
            merkle = compute_merkle_root(txs)
            diff = self.chain.current_difficulty()
            self.current_job = {
                "job_id": uuid.uuid4().hex[:16],
                "height": height,
                "prev_hash": prev_hash,
                "merkle": merkle,
                "timestamp": int(time.time()),
                "difficulty": diff,
                "share_difficulty": max(1, diff - 1),
                "txs": txs,
                "miner_address": miner_address
            }

    def register(self, address, pubkey, miner_id=None):
        with self.lock:
            if not miner_id:
                miner_id = uuid.uuid4().hex[:12]
            self.miners[miner_id] = {"address": address, "pubkey": pubkey, "last_seen": time.time()}
            return {"ok": True, "miner_id": miner_id}

    def get_work(self, miner_id):
        with self.lock:
            if miner_id not in self.miners:
                return {"ok": False, "msg": "miner desconhecido"}
            self.miners[miner_id]["last_seen"] = time.time()
            
            # [FIX] Gera o job com o endereço de quem pediu
            miner_addr = self.miners[miner_id]["address"]
            self._generate_job(miner_addr)
            
            return {"ok": True, "job": self.current_job}

    def submit_share(self, miner_id, nonce, hash_hex):
        with self.lock:
            if miner_id not in self.miners:
                return {"ok": False, "msg": "miner desconhecido"}
            job = self.current_job
            if not job:
                return {"ok": False, "msg": "sem job ativo"}
            
            expected = block_hash(job["prev_hash"], job["merkle"], job["timestamp"], nonce, job["difficulty"])
            if expected != hash_hex:
                return {"ok": False, "msg": "hash invalido"}
            
            if meets_difficulty(hash_hex, job["difficulty"]):
                block = {
                    "height": job["height"],
                    "prev_hash": job["prev_hash"],
                    "merkle": job["merkle"],
                    "timestamp": job["timestamp"],
                    "nonce": nonce,
                    "difficulty": job["difficulty"],
                    "hash": hash_hex,
                    "transactions": job["txs"]
                }
                try:
                    ok, msg = self.chain.accept_block(block)
                    if ok:
                        # Gera o próximo job para o mesmo minerador
                        self._generate_job(job.get("miner_address", "brn1pooladdressplaceholder"))
                        return {"ok": True, "block_found": True, "msg": "bloco aceito"}
                    else:
                        log.warning(f"Bloco minerado rejeitado: {msg}")
                        return {"ok": False, "msg": f"bloco rejeitado: {msg}"}
                except Exception as e:
                    return {"ok": False, "msg": str(e)}
            return {"ok": True, "block_found": False}

    def heartbeat(self, miner_id, hashrate, hashes_total):
        with self.lock:
            if miner_id in self.miners:
                self.miners[miner_id]["last_seen"] = time.time()
                self.miners[miner_id]["hashrate"] = hashrate
                return {"ok": True}
            return {"ok": False, "msg": "miner desconhecido"}

    def disconnect(self, miner_id):
        with self.lock:
            self.miners.pop(miner_id, None)
            return {"ok": True}

    def stats(self):
        with self.lock:
            return {"ok": True, "miners": len(self.miners), "height": self.chain.db.height()}

    def miners_list(self):
        with self.lock:
            return list(self.miners.values())
