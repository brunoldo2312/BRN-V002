"""mining_pool.py — Pool de mineração BRN (v6.3)
================================================================
v6.3 (2026-11-04):
  [FIX CRÍTICO] _assemble_and_accept_block agora coleta assinaturas
                via chain._collect_signatures(block) ANTES de chamar
                chain.accept_block(block). Sem isso, o bloco saía
                com validators_sig=[] e era rejeitado por
                _validate_validators_sig com "sem assinaturas
                válidas" — pool em loop infinito (bloco achado mas
                nunca aceito, altura travada).
                chain._collect_signatures já faz: assina localmente
                + chama _sign_collector (peers) + dedup por pubkey
                + filtra por valid_set. Mesma função usada por
                mine_block_interruptible, então comportamento é
                idêntico entre miner local e pool.
                Fallback: se _collect_signatures não existir
                (blockchain.py antigo), chama _sign_collector puro.
v6.2 (2026-11-03):
  [NEW] PoolClient — cliente de mineração que faltava. Roda em
        thread daemon, minera jobs do PoolManager local (modo
        local) ou de um pool remoto (modo HTTP, se BRN_POOL_URL
        setado), e expõe os atributos que app_wallet_v3.py espera:
          _job: dict do último job (job_id, height, difficulty,
                share_difficulty)
          _last_known_share_diff: int
          running: bool
          hashrate_khs: float
        Métodos: start(), stop(), status().
        Singleton: get_pool_client() / reset_pool_client().
        Fecha o gap em que server.py chamava "/api/pool/start_local"
        e recebia "PoolClient nao injetado".
  [NEW] Suporte a BRN_POOL_URL para mineração remota (HTTP).
        Se vazio, usa o PoolManager local (mesmo processo).
v6.1 (2026-10-27):
  [FIX CRÍTICO] Unifica a fonte de reputação no consenso.
                Antes:
                  - pool usava trust_system.py      (peer_trust_data.json)
                  - consenso usava trust_network.py (trust_network.json)
                Resultado: pool pontuava em uma fonte que o consenso
                NÃO lia → multi-sig sempre em modo bootstrap, e
                reputação de mineradores do pool nunca afetava a
                lista de validadores.
                Agora:
                  - register(): registra o minerador em chain.trust_net
                  - _assemble_and_accept_block(): credita o bloco via
                    chain.trust_net.record_block_validated()
v6 (2026-10-25):
  [FIX CRÍTICO] Proteção contra UNIQUE constraint failed.
v5 (2026-10-23):
  [NEW] Vardiff: dificuldade variável por minerador.
================================================================
"""
from __future__ import annotations

import hashlib
import json as _json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Optional

from blockchain import block_hash, compute_merkle_root, meets_difficulty, txid as calc_txid

log = logging.getLogger("pool")

SHARE_DIFF_OFFSET   = 1
MINER_TIMEOUT_S     = 60
JOB_REFRESH_S       = 20
MAX_MINERS          = 50
MAX_RECENT_JOBS     = 5

# ============================================================
# VARDIFF — Dificuldade variável por minerador
# ============================================================
VARDIFF_TARGET_S        = 15.0
VARDIFF_MIN_DIFF        = 2
VARDIFF_WINDOW_S        = 120.0
VARDIFF_FAST_RATIO      = 0.5
VARDIFF_SLOW_RATIO      = 2.0
VARDIFF_MAX_STEP        = 1
VARDIFF_MAX_SAMPLES     = 100
VARDIFF_MIN_SAMPLES     = 3

# ============================================================
# POOLCLIENT — parâmetros do cliente de mineração (v6.2)
# ============================================================
POOLCLIENT_JOB_MAX_S        = 30.0
POOLCLIENT_HEARTBEAT_S      = 10.0
POOLCLIENT_RETRY_S          = 5.0
POOLCLIENT_HASH_BATCH       = 2000


class MinerState:
    __slots__ = (
        "miner_id", "address", "pubkey",
        "shares_period", "shares_total",
        "hashes_total", "hashrate",
        "last_seen", "connected_at", "mining",
        "share_difficulty", "active_share_difficulty", "share_times",
    )
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
        self.share_difficulty = VARDIFF_MIN_DIFF
        self.active_share_difficulty = VARDIFF_MIN_DIFF
        self.share_times = []


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
    try:
        return int(h_hex, 16) <= _target(diff)
    except Exception:
        return False


def _make_pool_coinbase(height: int, reward: int, distribution: dict) -> dict:
    if not distribution:
        return {
            "txid": "",
            "inputs": [{"txid": "0" * 64, "vout": 0xFFFFFFFF, "pubkey": "", "signature": ""}],
            "outputs": [{"address": "brn1poolfallbackxxxxxxxxxxxxxxxxxxx", "amount": reward,
                         "pubkey": "00" * 33}],
            "timestamp": int(time.time()), "locktime": 0, "height": height, "nonce": 0,
        }
    total_weight = sum(d["weight"] for d in distribution.values()) or 1.0
    outputs = []
    for addr, info in distribution.items():
        amt = int(reward * (info["weight"] / total_weight))
        if amt <= 0:
            continue
        outputs.append({"address": addr, "amount": amt,
                        "pubkey": info["pubkey"] or ("00" * 33)})
    if not outputs:
        first = max(distribution.items(), key=lambda kv: kv[1]["weight"])
        outputs = [{"address": first[0], "amount": reward, "pubkey": first[1]["pubkey"]}]
    dif = reward - sum(o["amount"] for o in outputs)
    if dif > 0:
        outputs[0]["amount"] += dif
    cb = {
        "txid": "",
        "inputs": [{"txid": "0" * 64, "vout": 0xFFFFFFFF, "pubkey": "", "signature": ""}],
        "outputs": outputs,
        "timestamp": int(time.time()),
        "locktime": 0,
        "height": int(height),
        "nonce": 0,
    }
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

        self.phase = 2 if self.chain.rede_descentralizada else 1
        self.phase2_threshold = 20
        self.miner_queue = list(self.chain.fila_mineracao.keys())

        # [v6.1] Informa qual fonte de trust está ativa
        _trust = getattr(self.chain, "trust_net", None)
        if _trust is not None:
            log.info("PoolManager: usando chain.trust_net (unificado com consenso)")
        else:
            try:
                from trust_system import trust_system as _legacy  # noqa: F401
                log.warning("PoolManager: chain.trust_net ausente — usando "
                            "trust_system LEGADO (repare: main.py deve criar "
                            "TrustNetwork e chamar chain.set_trust_net)")
            except Exception:
                log.warning("PoolManager: sem trust network — reputação de "
                            "mineradores desabilitada neste pool")

        self._stop = threading.Event()
        threading.Thread(target=self._maintenance_loop, daemon=True, name="Pool-Maint").start()
        log.info("PoolManager iniciado — Blockchain + Vardiff + Trust unificado + Multi-sig")

    # ============================================================
    # [v6.1] Helpers de integração com o trust network
    # ============================================================
    def _register_in_trust(self, miner_id: str, address: str, pubkey: str) -> None:
        trust = getattr(self.chain, "trust_net", None)
        if trust is not None:
            try:
                trust.register_node(miner_id, address, pubkey)
                return
            except Exception as e:
                log.warning("[Pool] register_node falhou em trust_net: %s", e)
                return
        try:
            from trust_system import trust_system as _legacy
            _legacy.ensure_peer_entry(pubkey)
        except Exception:
            pass

    def _credit_miner_in_trust(self, cb: dict, height: int) -> None:
        output_pubkeys = {o.get("pubkey") for o in cb.get("outputs", []) if o.get("pubkey")}
        if not output_pubkeys:
            return

        trust = getattr(self.chain, "trust_net", None)
        if trust is not None:
            for mid, m in self.miners.items():
                if m.pubkey in output_pubkeys:
                    try:
                        trust.record_block_validated(mid, height)
                        log.info("[Trust] %s creditado por bloco #%d",
                                 m.pubkey[:8], height)
                    except Exception as e:
                        log.warning("[Trust] record_block_validated falhou "
                                    "para %s: %s", mid, e)
                    return
            return

        try:
            from trust_system import trust_system as _legacy
            for mid, m in self.miners.items():
                if m.pubkey in output_pubkeys:
                    _legacy.update_score(m.pubkey, "valid_block")
                    log.info("[Trust-legacy] %s creditado por bloco #%d",
                             m.pubkey[:8], height)
                    return
        except Exception:
            pass

    # ============================================================
    # Registro de mineradores
    # ============================================================
    def register(self, address: str, pubkey: str, miner_id: Optional[str] = None) -> dict:
        if not address or not address.startswith("brn1"):
            return {"ok": False, "msg": "address invalido"}
        if not pubkey:
            return {"ok": False, "msg": "pubkey obrigatoria"}
        with self.lock:
            if len(self.miners) >= MAX_MINERS and miner_id not in self.miners:
                return {"ok": False, "msg": "pool cheio"}
            if not miner_id:
                miner_id = uuid.uuid4().hex[:12]
            m = self.miners.get(miner_id)
            if m is None:
                m = MinerState(miner_id, address, pubkey)
                self.miners[miner_id] = m
                log.info("pool: %s registado (addr=%s)", miner_id, address[:16])
            else:
                m.address = address
                m.pubkey = pubkey
                m.last_seen = time.time()

            self.phase = 2 if self.chain.rede_descentralizada else 1
            self.miner_queue = list(self.chain.fila_mineracao.keys())

            self._register_in_trust(miner_id, address, pubkey)

        return {"ok": True, "miner_id": miner_id}

    def heartbeat(self, miner_id: str, hashrate: float = 0.0, hashes_total: int = 0) -> dict:
        with self.lock:
            m = self.miners.get(miner_id)
            if m is None:
                return {"ok": False, "msg": "miner desconhecido"}
            m.last_seen = time.time()
            m.hashrate = float(hashrate or 0.0)
            if hashes_total > m.hashes_total:
                m.hashes_total = int(hashes_total)
        return {"ok": True}

    def disconnect(self, miner_id: str) -> dict:
        with self.lock:
            self.miners.pop(miner_id, None)
        return {"ok": True}

    # ============================================================
    # VARDIFF
    # ============================================================
    def _apply_vardiff(self, m: MinerState, job: Job) -> None:
        now = time.time()
        cutoff = now - VARDIFF_WINDOW_S
        m.share_times = [t for t in m.share_times if t >= cutoff]

        if len(m.share_times) < VARDIFF_MIN_SAMPLES:
            return

        intervals = [m.share_times[i + 1] - m.share_times[i]
                     for i in range(len(m.share_times) - 1)]
        if not intervals:
            return
        avg_interval = sum(intervals) / len(intervals)

        max_diff = max(VARDIFF_MIN_DIFF, int(job.difficulty) - 1)
        changed = False

        if avg_interval < VARDIFF_TARGET_S * VARDIFF_FAST_RATIO:
            new_diff = min(m.share_difficulty + VARDIFF_MAX_STEP, max_diff)
            if new_diff > m.share_difficulty:
                m.share_difficulty = new_diff
                changed = True
                log.info("vardiff: %s diff -> %d (rápido avg=%.1fs n=%d)",
                         m.miner_id, new_diff, avg_interval, len(m.share_times))
        elif avg_interval > VARDIFF_TARGET_S * VARDIFF_SLOW_RATIO:
            new_diff = max(m.share_difficulty - VARDIFF_MAX_STEP, VARDIFF_MIN_DIFF)
            if new_diff < m.share_difficulty:
                m.share_difficulty = new_diff
                changed = True
                log.info("vardiff: %s diff -> %d (lento avg=%.1fs n=%d)",
                         m.miner_id, new_diff, avg_interval, len(m.share_times))

        if changed:
            m.share_times = []

    def get_work(self, miner_id: str) -> dict:
        with self.lock:
            m = self.miners.get(miner_id)
            if m is None:
                return {"ok": False, "msg": "miner desconhecido"}
            m.last_seen = time.time()
            m.mining = True
            job = self.current_job
            if job is None or self._job_is_stale(job):
                job = self._build_job_locked()

            self._apply_vardiff(m, job)
            effective_diff = min(
                m.share_difficulty,
                max(VARDIFF_MIN_DIFF, int(job.difficulty) - 1),
            )
            m.share_difficulty = effective_diff
            m.active_share_difficulty = effective_diff

        return {"ok": True, "job": {
            "job_id": job.job_id,
            "height": job.height,
            "prev_hash": job.prev_hash,
            "merkle": job.merkle,
            "timestamp": job.timestamp,
            "difficulty": job.difficulty,
            "share_difficulty": effective_diff,
            "coinbase_txid": job.transactions[0]["txid"],
            "n_txs": len(job.transactions),
            "created_at": job.created_at,
        }}

    def _job_is_stale(self, job: Job) -> bool:
        if time.time() - job.created_at > JOB_REFRESH_S:
            return True
        try:
            tip = self.chain.db.tip_hash()
            if tip != job.prev_hash:
                return True
        except Exception:
            return True
        return False

    def _build_job_locked(self) -> Job:
        h = self.chain.db.height() + 1
        reward = self.chain.current_reward(h)
        diff = self.chain.current_difficulty()
        share_diff = max(2, diff - SHARE_DIFF_OFFSET)
        prev_hash = self.chain.db.tip_hash()

        distribution = {}
        for mid, m in self.miners.items():
            if (time.time() - m.last_seen) > MINER_TIMEOUT_S:
                continue
            if not m.address or not m.pubkey:
                continue
            w = float(m.shares_period) + 1.0
            distribution[m.address] = {"pubkey": m.pubkey, "weight": w}

        cb = _make_pool_coinbase(h, reward, distribution)

        try:
            mempool_txs = self.chain.db.all_mempool(limit=200)
        except Exception:
            mempool_txs = []
        transactions = [cb] + mempool_txs
        merkle = compute_merkle_root([t["txid"] for t in transactions])

        job = Job(
            height=h, prev_hash=prev_hash, merkle=merkle,
            timestamp=int(time.time()), difficulty=diff,
            share_difficulty=share_diff,
            transactions=transactions, distribution=distribution,
        )
        self.current_job = job
        self.recent_jobs[job.job_id] = job
        if len(self.recent_jobs) > MAX_RECENT_JOBS:
            oldest = sorted(self.recent_jobs.items(), key=lambda kv: kv[1].created_at)[:1]
            for k, _ in oldest:
                self.recent_jobs.pop(k, None)
        log.info("pool: novo job %s (h=%d diff=%d share_diff=%d miners=%d)",
                 job.job_id, h, diff, share_diff, len(distribution))
        return job

    def submit_share(self, miner_id: str, nonce: int, hash_hex: str = "") -> dict:
        with self.lock:
            m = self.miners.get(miner_id)
            if m is None:
                return {"ok": False, "msg": "miner desconhecido"}
            m.last_seen = time.time()
            job = self.current_job
            if job is None:
                return {"ok": False, "msg": "sem job ativo"}

            miner_share_diff = max(
                int(m.active_share_difficulty or VARDIFF_MIN_DIFF),
                VARDIFF_MIN_DIFF,
            )

            candidates = [job]
            for j in self.recent_jobs.values():
                if j.job_id != job.job_id:
                    candidates.append(j)

            h = None
            matched_job = None
            for cand in candidates:
                try:
                    h2 = block_hash(cand.prev_hash, cand.merkle,
                                    cand.timestamp, int(nonce), cand.difficulty)
                except Exception:
                    continue
                if _meets(h2, miner_share_diff):
                    h = h2
                    matched_job = cand
                    break

            if matched_job is None:
                self.rejected_shares += 1
                return {"ok": False, "msg": "share nao bate share_difficulty"}

            job = matched_job
            if hash_hex and hash_hex != h:
                self.rejected_shares += 1
                return {"ok": False, "msg": "hash nao corresponde ao nonce"}

            m.shares_period += 1
            m.shares_total += 1
            self.accepted_shares += 1

            m.share_times.append(time.time())
            if len(m.share_times) > VARDIFF_MAX_SAMPLES:
                m.share_times = m.share_times[-VARDIFF_MAX_SAMPLES:]

            block_found = False
            new_height = None

            if _meets(h, job.difficulty):
                log.warning("pool: FULL BLOCK encontrado por %s (nonce=%s, h=%d)",
                            miner_id, nonce, job.height)
                try:
                    ok = self._assemble_and_accept_block(job, int(nonce), h)
                    if ok:
                        block_found = True
                        new_height = job.height
                        self.blocks_found += 1
                        self.last_block_at = time.time()
                        self.last_block_height = job.height
                        for mm in self.miners.values():
                            mm.shares_period = 0
                            mm.share_times = []
                        self.current_job = None
                    else:
                        log.warning("pool: accept_block falhou — forcando novo job")
                        self.current_job = None
                except Exception as e:
                    log.exception("pool: erro aceitando bloco: %s", e)

        return {
            "ok": True,
            "share_id": uuid.uuid4().hex[:12],
            "block_found": block_found,
            "height": new_height,
            "job_id": job.job_id,
            "share_difficulty": miner_share_diff,
        }

    def _assemble_and_accept_block(self, job: Job, nonce: int, hash_hex: str) -> bool:
        cb = job.transactions[0]
        for out in cb["outputs"]:
            if out["address"] not in job.distribution:
                log.error("[POOL] Bloco rejeitado: endereço suspeito no coinbase: %s",
                          out["address"])
                return False

        try:
            existing = self.chain.db.get_block(job.height)
            if existing:
                log.warning("[POOL] Bloco #%d já existe. Ignorando duplicata.", job.height)
                return False
        except Exception:
            pass

        block = {
            "height": int(job.height),
            "hash": str(hash_hex),
            "prev_hash": str(job.prev_hash),
            "timestamp": int(job.timestamp),
            "nonce": int(nonce),
            "merkle": str(job.merkle),
            "difficulty": int(job.difficulty),
            "transactions": job.transactions,
            "validators_sig": [],
        }
        try:
            self.chain._attach_auth_tag(block)
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════
        # [v6.3] FIX CRÍTICO: coletar assinaturas ANTES do accept_block
        # ═══════════════════════════════════════════════════════════
        # blockchain.accept_block NÃO preenche validators_sig — ele
        # apenas VALIDA. Sem essa coleta, o bloco do pool sai com 0
        # sigs e é rejeitado por _validate_validators_sig com
        # "sem assinaturas válidas", causando loop infinito (bloco
        # achado, aceito, altura travada).
        #
        # chain._collect_signatures é a MESMA função usada por
        # mine_block_interruptible. Ela faz:
        #   1. Assina localmente com chain._local_validator_sk
        #   2. Chama chain._sign_collector (que pede aos peers)
        #   3. Deduplica por pubkey
        #   4. Filtra por valid_set (só aceita sigs de validadores
        #      ativos no trust_network local)
        # Então o comportamento é idêntico ao miner local — sem
        # divergência.
        _collector = getattr(self.chain, "_collect_signatures", None)
        if callable(_collector):
            try:
                log.info("[POOL] coletando sigs para bloco #%d",
                         block.get("height", -1))
                _t0 = time.time()
                _sigs = _collector(block)
                _dt_ms = (time.time() - _t0) * 1000
                if _sigs:
                    block["validators_sig"] = _sigs
                    log.info("[POOL] %d sig(s) coletada(s) em %.0fms",
                             len(_sigs), _dt_ms)
                else:
                    log.warning("[POOL] _collect_signatures retornou 0 sig(s) "
                                "em %.0fms para #%d",
                                _dt_ms, block.get("height", -1))
            except Exception as _e:
                log.exception("[POOL] _collect_signatures falhou: %s", _e)
        else:
            # Fallback: blockchain.py muito antigo — tenta _sign_collector puro
            # (perde a assinatura local do origin, mas ao menos pega dos peers)
            _sc = getattr(self.chain, "_sign_collector", None)
            if _sc is not None:
                try:
                    log.info("[POOL] fallback: usando _sign_collector puro")
                    _sigs = _sc(block)
                    if _sigs:
                        block["validators_sig"] = _sigs
                        log.info("[POOL] fallback: %d sig(s) via _sign_collector",
                                 len(_sigs))
                    else:
                        log.warning("[POOL] fallback _sign_collector retornou 0 sig(s)")
                except Exception as _e:
                    log.exception("[POOL] fallback _sign_collector falhou: %s", _e)
            else:
                log.warning("[POOL] chain._collect_signatures E _sign_collector "
                            "ausentes — bloco sairá SEM assinaturas e será "
                            "rejeitado. Atualize blockchain.py ou instale "
                            "brn_multisig.")
        # ═══════════════════════════════════════════════════════════

        ok, msg = self.chain.accept_block(block)
        if not ok:
            log.warning("pool: accept_block: %s", msg)
            return False

        self._credit_miner_in_trust(cb, job.height)

        try:
            cb = block["transactions"][0]
            reward = sum(int(o["amount"]) for o in cb.get("outputs", []))
            self.total_reward_paid += int(reward)
        except Exception:
            pass
        return True

    def stats(self) -> dict:
        with self.lock:
            self.phase = 2 if self.chain.rede_descentralizada else 1
            self.miner_queue = list(self.chain.fila_mineracao.keys())

            now = time.time()
            active = [m for m in self.miners.values()
                      if now - m.last_seen < MINER_TIMEOUT_S]
            total_hashrate = sum(m.hashrate for m in active)
            total_shares = sum(m.shares_period for m in active)
            current_job = self.current_job
            return {
                "ok": True,
                "uptime_s": int(now - self.started_at),
                "miners_active": len(active),
                "miners_total": len(self.miners),
                "hashrate_total": round(total_hashrate, 2),
                "shares_period": int(total_shares),
                "shares_accepted": self.accepted_shares,
                "shares_rejected": self.rejected_shares,
                "blocks_found": self.blocks_found,
                "total_reward_paid": self.total_reward_paid,
                "last_block_height": self.last_block_height,
                "last_block_ago_s": (int(now - self.last_block_at) if self.last_block_at else None),
                "job_active": current_job is not None,
                "job_id": (current_job.job_id if current_job else None),
                "job_height": (current_job.height if current_job else None),
                "job_difficulty": (current_job.difficulty if current_job else None),
                "job_share_difficulty": (current_job.share_difficulty if current_job else None),
                "job_created_ago_s": (int(now - current_job.created_at) if current_job else None),
                "recent_jobs_cached": len(self.recent_jobs),
                "phase": self.phase,
                "miner_queue_len": len(self.miner_queue),
                "vardiff_target_s": VARDIFF_TARGET_S,
                "vardiff_miners": sum(1 for m in active if m.share_difficulty > VARDIFF_MIN_DIFF),
            }

    def miners_list(self) -> list:
        with self.lock:
            now = time.time()
            out = []
            for m in self.miners.values():
                out.append({
                    "miner_id": m.miner_id,
                    "address": m.address,
                    "shares_period": m.shares_period,
                    "shares_total": m.shares_total,
                    "hashrate": round(m.hashrate, 2),
                    "connected_s": int(now - m.connected_at),
                    "last_seen_s": int(now - m.last_seen),
                    "mining": m.mining,
                    "active": (now - m.last_seen) < MINER_TIMEOUT_S,
                    "share_difficulty": int(m.share_difficulty),
                    "active_share_difficulty": int(m.active_share_difficulty),
                })
            out.sort(key=lambda x: x["hashrate"], reverse=True)
            return out

    def miner_status(self, miner_id: str) -> dict:
        with self.lock:
            m = self.miners.get(miner_id)
            if m is None:
                return {"ok": False, "msg": "miner desconhecido"}
            total_shares = sum(x.shares_period for x in self.miners.values())
            reward = self.chain.current_reward(self.chain.db.height() + 1)
            est_reward = 0
            if total_shares > 0:
                est_reward = int(reward * (m.shares_period / total_shares))
            return {
                "ok": True,
                "miner_id": m.miner_id,
                "address": m.address,
                "hashrate": round(m.hashrate, 2),
                "shares_period": m.shares_period,
                "shares_total": m.shares_total,
                "hashes_total": m.hashes_total,
                "connected_s": int(time.time() - m.connected_at),
                "last_seen_s": int(time.time() - m.last_seen),
                "mining": m.mining,
                "est_reward_sats": est_reward,
                "pool_total_shares": int(total_shares),
                "share_difficulty": int(m.share_difficulty),
                "active_share_difficulty": int(m.active_share_difficulty),
                "share_rate_n": len(m.share_times),
            }

    def _maintenance_loop(self):
        while not self._stop.is_set():
            try:
                time.sleep(5)
                with self.lock:
                    now = time.time()
                    gone = [mid for mid, m in self.miners.items()
                            if now - m.last_seen > MINER_TIMEOUT_S * 3]
                    for mid in gone:
                        self.miners.pop(mid, None)
                        log.info("pool: miner %s removido (timeout)", mid)
                    if (self.current_job is None or self._job_is_stale(self.current_job)):
                        if self.miners:
                            self._build_job_locked()
            except Exception as e:
                log.warning("pool maint: %s", e)

    def stop(self):
        self._stop.set()


# ══════════════════════════════════════════════════════════════
# PoolClient — cliente de mineração (v6.2, inalterado em v6.3)
# ══════════════════════════════════════════════════════════════
class PoolClient:
    def __init__(self, address: str, pubkey: str,
                 pool_manager: Optional[PoolManager] = None,
                 pool_url: str = ""):
        self.address = (address or "").strip()
        self.pubkey = (pubkey or "").strip()
        self.pm = pool_manager
        self.pool_url = (pool_url or "").strip().rstrip("/")

        self.running = False
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._miner_id: Optional[str] = None
        self._job: dict = {}
        self._last_known_share_diff: Optional[int] = None
        self._hashes_total: int = 0
        self._hashrate: float = 0.0
        self._shares_submitted: int = 0
        self._shares_accepted: int = 0
        self._blocks_found: int = 0
        self._last_error: str = ""
        self._last_heartbeat: float = 0.0
        self._hashes_at_last_hb: int = 0

    def start(self) -> tuple:
        if self.running:
            return False, "PoolClient já está rodando"
        if not self.address or not self.pubkey:
            return False, "address e pubkey obrigatórios"
        if self.pm is None and not self.pool_url:
            return False, "sem PoolManager local e BRN_POOL_URL vazio"

        self._stop_evt.clear()
        self.running = True
        self._thread = threading.Thread(
            target=self._mining_loop, name="PoolClient", daemon=True,
        )
        self._thread.start()
        log.info("PoolClient: iniciado (addr=%s, mode=%s)",
                 self.address[:16],
                 "local" if self.pm else f"remoto {self.pool_url}")
        return True, "iniciado"

    def stop(self) -> tuple:
        if not self.running and (self._thread is None or not self._thread.is_alive()):
            return False, "PoolClient não está rodando"
        self._stop_evt.set()
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        try:
            if self._miner_id and self.pm:
                self.pm.disconnect(self._miner_id)
        except Exception:
            pass
        log.info("PoolClient: parado (%d shares, %d blocos)",
                 self._shares_accepted, self._blocks_found)
        return True, "parado"

    def status(self) -> dict:
        return {
            "ok": True,
            "running": self.running,
            "mode": "local" if self.pm else "remoto",
            "pool_url": self.pool_url,
            "miner_id": self._miner_id,
            "address": self.address,
            "hashrate_khs": round(self._hashrate / 1000.0, 2),
            "hashrate_hs": round(self._hashrate, 1),
            "hashes_total": self._hashes_total,
            "shares_submitted": self._shares_submitted,
            "shares_accepted": self._shares_accepted,
            "blocks_found": self._blocks_found,
            "job_id": self._job.get("job_id"),
            "job_height": self._job.get("height"),
            "job_difficulty": self._job.get("difficulty"),
            "job_share_diff": self._last_known_share_diff,
            "last_error": self._last_error,
        }

    def _mining_loop(self):
        try:
            if not self._register():
                log.error("PoolClient: register falhou, encerrando")
                return
            while self.running and not self._stop_evt.is_set():
                try:
                    job = self._get_work()
                    if not job:
                        self._sleep_retry()
                        continue

                    nonce, hash_hex, hashes = self._mine(job)
                    if nonce is None:
                        continue

                    self._submit(nonce, hash_hex)
                    self._maybe_heartbeat()

                except Exception as e:
                    self._last_error = str(e)
                    log.exception("PoolClient: erro no loop: %s", e)
                    self._sleep_retry()
        finally:
            self.running = False
            if self._miner_id and self.pm:
                try:
                    self.pm.disconnect(self._miner_id)
                except Exception:
                    pass
            log.info("PoolClient: thread encerrada")

    def _sleep_retry(self):
        if self._stop_evt.wait(POOLCLIENT_RETRY_S):
            pass

    def _register(self) -> bool:
        try:
            if self.pm is not None:
                r = self.pm.register(self.address, self.pubkey)
            else:
                r = self._http_post("/api/pool/register", {
                    "address": self.address, "pubkey": self.pubkey,
                })
            if not r.get("ok"):
                self._last_error = r.get("msg", "register falhou")
                return False
            self._miner_id = r.get("miner_id")
            log.info("PoolClient: registrado como %s", self._miner_id)
            return True
        except Exception as e:
            self._last_error = str(e)
            log.warning("PoolClient: register exception: %s", e)
            return False

    def _get_work(self) -> Optional[dict]:
        try:
            if self.pm is not None:
                r = self.pm.get_work(self._miner_id)
            else:
                r = self._http_post("/api/pool/getwork", {"miner_id": self._miner_id})
            if not r.get("ok"):
                self._last_error = r.get("msg", "get_work falhou")
                return None
            job = r.get("job") or {}
            if not job or not job.get("job_id"):
                return None
            self._job = dict(job)
            self._last_known_share_diff = int(job.get("share_difficulty") or 2)
            return self._job
        except Exception as e:
            self._last_error = str(e)
            log.warning("PoolClient: get_work exception: %s", e)
            return None

    def _submit(self, nonce: int, hash_hex: str) -> None:
        try:
            self._shares_submitted += 1
            if self.pm is not None:
                r = self.pm.submit_share(self._miner_id, nonce, hash_hex)
            else:
                r = self._http_post("/api/pool/submit", {
                    "miner_id": self._miner_id,
                    "nonce": int(nonce),
                    "hash": hash_hex,
                })
            if r.get("ok"):
                self._shares_accepted += 1
                if r.get("block_found"):
                    self._blocks_found += 1
                    log.warning("PoolClient: BLOCO encontrado! h=%s", r.get("height"))
            else:
                log.debug("PoolClient: share rejeitada: %s", r.get("msg"))
        except Exception as e:
            self._last_error = str(e)
            log.warning("PoolClient: submit exception: %s", e)

    def _maybe_heartbeat(self) -> None:
        now = time.time()
        if now - self._last_heartbeat < POOLCLIENT_HEARTBEAT_S:
            return
        try:
            if self.pm is not None:
                self.pm.heartbeat(self._miner_id, self._hashrate, self._hashes_total)
            else:
                self._http_post("/api/pool/heartbeat", {
                    "miner_id": self._miner_id,
                    "hashrate": float(self._hashrate),
                    "hashes_total": int(self._hashes_total),
                })
            self._last_heartbeat = now
            self._hashes_at_last_hb = self._hashes_total
        except Exception as e:
            log.debug("PoolClient: heartbeat exception: %s", e)

    def _mine(self, job: dict) -> tuple:
        try:
            prev = job["prev_hash"]
            merkle = job["merkle"]
            ts = int(job["timestamp"])
            diff = int(job.get("share_difficulty") or 2)
            target = _target(diff)
            prefix = f"{prev}{merkle}{ts}".encode()
            diff_bytes = str(int(job["difficulty"])).encode()

            nonce = 0
            hashes = 0
            t0 = time.time()

            while self.running and not self._stop_evt.is_set():
                for _ in range(POOLCLIENT_HASH_BATCH):
                    data = prefix + str(nonce).encode() + diff_bytes
                    h = hashlib.sha256(hashlib.sha256(data).digest()).digest()
                    hashes += 1
                    if int.from_bytes(h, "big") <= target:
                        dt = time.time() - t0
                        self._hashes_total += hashes
                        if dt > 0:
                            self._hashrate = hashes / dt
                        return nonce, h.hex(), hashes
                    nonce += 1

                dt = time.time() - t0
                if dt > 0:
                    self._hashrate = hashes / dt

                if dt > POOLCLIENT_JOB_MAX_S:
                    self._hashes_total += hashes
                    log.debug("PoolClient: job expirou (%.1fs, %d hashes) — renovando",
                              dt, hashes)
                    return None, None, hashes

            self._hashes_total += hashes
            return None, None, hashes
        except Exception as e:
            self._last_error = str(e)
            log.warning("PoolClient: mine exception: %s", e)
            return None, None, 0

    def _http_post(self, path: str, body: dict, timeout: float = 10.0) -> dict:
        if not self.pool_url:
            return {"ok": False, "msg": "pool_url vazio"}
        url = self.pool_url + path
        try:
            req = urllib.request.Request(
                url,
                data=_json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": "brn-poolclient/6.3"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return {"ok": False, "msg": f"HTTP {e.code}"}
        except urllib.error.URLError as e:
            return {"ok": False, "msg": f"conn: {e.reason}"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}


# ══════════════════════════════════════════════════════════════
# Singleton
# ══════════════════════════════════════════════════════════════
_pool_client: Optional[PoolClient] = None
_pool_client_lock = threading.Lock()


def get_pool_client(
    pool_manager: Optional[PoolManager] = None,
    address: str = "",
    pubkey: str = "",
    pool_url: str = "",
) -> Optional[PoolClient]:
    global _pool_client
    with _pool_client_lock:
        if _pool_client is None:
            if not address or not pubkey:
                return None
            if pool_manager is None and not pool_url:
                return None
            _pool_client = PoolClient(
                address=address,
                pubkey=pubkey,
                pool_manager=pool_manager,
                pool_url=pool_url,
            )
        return _pool_client


def reset_pool_client() -> None:
    global _pool_client
    with _pool_client_lock:
        if _pool_client is not None and _pool_client.running:
            try:
                _pool_client.stop()
            except Exception:
                pass
        _pool_client = None
