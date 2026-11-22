"""mining_pool.py — Pool server do BRN (v7.4)
================================================================
v7.4 (2026-11-22) — CORREÇÕES CRÍTICAS (quebra total na v7.3):
  [FIX] make_coinbase com (address, pubkey, height, reward) na ORDEM
        certa. Antes: reward ia como height, height como reward, e
        pubkey = "0"*66 → coinbase queimada.
  [FIX] compute_merkle_root([t["txid"] for t in txs]) — a função
        recebe txids, não transações. Antes: TypeError imediato.
  [FIX] submit_share aceita job_id opcional e valida contra ele.
        job expirado → msg correta em vez de "hash invalido".
  [FIX] _generate_job NÃO é mais chamado a cada get_work(). Só
        regenera quando prev_hash/altura mudam ou após bloco aceito.
        Antes: cada heartbeat invalidava o trabalho dos outros.
  [FIX] Bloco submetido agora inclui validators_sig: [] e auth_tag
        (quando o chain tiver network_secret). Antes: rejeitado por
        accept_block em qualquer nó com validadores.
  [FIX] share_difficulty agora é de fato usada para aceitar shares.
        Antes: só aceitava share quando batia difficulty de bloco
        (nunca registrava share).
  [FIX] Address placeholder "brn1pooladdressplaceholder" (bech32
        inválido) não é mais usado. Job só é criado quando existe
        minerador registrado com endereço válido.
  [NEW] Filtro de mempool no _generate_job (validate_tx + conflito
        de inputs) para evitar blocos inválidos em race conditions.
================================================================
"""
from __future__ import annotations

import os
import time
import uuid
import logging
import threading
from typing import Optional

from blockchain import (
    block_hash, compute_merkle_root, make_coinbase,
    meets_difficulty,
)

log = logging.getLogger("pool")

JOB_MAX_AGE_S   = float(os.environ.get("BRN_POOL_JOB_MAX_AGE_S", "120"))
POOL_MAX_TX     = int(os.environ.get("BRN_POOL_MAX_TX", "100"))


class PoolManager:
    def __init__(self, chain, on_job_change=None):
        self.chain = chain
        self.on_job_change = on_job_change
        self.miners: dict = {}
        self.lock = threading.RLock()
        self.current_job: Optional[dict] = None
        # Sem job inicial: só gera quando um minerador válido se registrar.

    # ------------------------------------------------------------------
    # Job lifecycle
    # ------------------------------------------------------------------
    def _job_is_stale(self) -> bool:
        if self.current_job is None:
            return True
        try:
            tip = self.chain.db.tip_hash()
            expected_height = self.chain.db.height() + 1
        except Exception:
            return True
        if self.current_job.get("prev_hash") != tip:
            return True
        if self.current_job.get("height") != expected_height:
            return True
        if (time.time() - self.current_job.get("created_at", 0)) > JOB_MAX_AGE_S:
            return True
        return False

    def _select_mempool_txs(self) -> list:
        """Filtra txs que passam validação + sem conflito entre si."""
        try:
            raw = self.chain.db.all_mempool(limit=POOL_MAX_TX) or []
        except Exception:
            return []
        selected = []
        gastos = set()
        for t in raw:
            try:
                ok, _ = self.chain.validate_tx(t, from_mempool=True)
                if not ok:
                    continue
                conflito = False
                for inp in t.get("inputs", []):
                    key = (inp["txid"], int(inp["vout"]))
                    if key in gastos:
                        conflito = True
                        break
                if conflito:
                    continue
                for inp in t.get("inputs", []):
                    gastos.add((inp["txid"], int(inp["vout"])))
                selected.append(t)
            except Exception:
                continue
        return selected

    def _generate_job(self, miner_address: str, miner_pubkey: str) -> dict:
        """Lock do chamador é obrigatório. Levanta ValueError se inválido."""
        if not miner_address or not str(miner_address).startswith("brn1"):
            raise ValueError(f"address invalido: {miner_address!r}")
        if not miner_pubkey:
            raise ValueError("pubkey obrigatoria")

        height = self.chain.db.height() + 1
        prev_hash = self.chain.db.tip_hash()
        reward = self.chain.current_reward(height)
        diff = self.chain.current_difficulty()

        # Ordem correta: (address, pubkey_hex, height, reward)
        coinbase = make_coinbase(miner_address, miner_pubkey, height, reward)

        txs = [coinbase] + self._select_mempool_txs()

        # compute_merkle_root espera LISTA DE TXIDS (strings).
        merkle = compute_merkle_root([t["txid"] for t in txs])

        job = {
            "job_id":            uuid.uuid4().hex[:16],
            "height":            height,
            "prev_hash":         prev_hash,
            "merkle":            merkle,
            "timestamp":         int(time.time()),
            "difficulty":        diff,
            "share_difficulty":  max(1, diff - 1),
            "txs":               txs,
            "miner_address":     miner_address,
            "miner_pubkey":      miner_pubkey,
            "created_at":        time.time(),
        }
        self.current_job = job
        return job

    def _ensure_job(self, miner_address: str, miner_pubkey: str) -> dict:
        if self._job_is_stale():
            return self._generate_job(miner_address, miner_pubkey)
        return self.current_job

    # ------------------------------------------------------------------
    # Registro
    # ------------------------------------------------------------------
    def register(self, address: str, pubkey: str,
                 miner_id: Optional[str] = None) -> dict:
        if not address or not str(address).startswith("brn1"):
            return {"ok": False, "msg": "address invalido"}
        if not pubkey:
            return {"ok": False, "msg": "pubkey obrigatoria"}
        with self.lock:
            if not miner_id:
                miner_id = uuid.uuid4().hex[:12]
            self.miners[miner_id] = {
                "address":     address,
                "pubkey":      pubkey,
                "last_seen":   time.time(),
                "hashrate":    0.0,
                "hashes_total": 0,
            }
            if self.current_job is None or self._job_is_stale():
                try:
                    self._generate_job(address, pubkey)
                except ValueError as e:
                    log.warning(f"pool: nao gerou job ainda: {e}")
            return {"ok": True, "miner_id": miner_id}

    def get_work(self, miner_id: str) -> dict:
        with self.lock:
            miner = self.miners.get(miner_id)
            if miner is None:
                return {"ok": False, "msg": "miner desconhecido"}
            miner["last_seen"] = time.time()
            try:
                job = self._ensure_job(miner["address"], miner["pubkey"])
            except ValueError as e:
                return {"ok": False, "msg": str(e)}
            return {"ok": True, "job": job}

    # ------------------------------------------------------------------
    # Shares / blocos
    # ------------------------------------------------------------------
    def submit_share(self, miner_id: str, nonce: int, hash_hex: str,
                     job_id: Optional[str] = None) -> dict:
        with self.lock:
            miner = self.miners.get(miner_id)
            if miner is None:
                return {"ok": False, "msg": "miner desconhecido"}
            miner["last_seen"] = time.time()

            job = self.current_job
            if not job:
                return {"ok": False, "msg": "sem job ativo"}

            # Rejeita early se o minerador está minerando job antigo.
            if job_id is not None and job_id != job.get("job_id"):
                return {"ok": False, "msg": "job expirado"}

            try:
                expected = block_hash(
                    job["prev_hash"], job["merkle"], job["timestamp"],
                    int(nonce), job["difficulty"],
                )
            except Exception as e:
                return {"ok": False, "msg": f"hash calc falhou: {e}"}
            if expected != hash_hex:
                return {"ok": False, "msg": "hash invalido"}

            # Share: aceita contra share_difficulty.
            share_diff = int(job.get("share_difficulty", 1))
            if not meets_difficulty(hash_hex, share_diff):
                return {"ok": False, "msg": "share abaixo da dificuldade"}

            # Bloco: se também bate a dificuldade do bloco, tenta aceitar.
            if meets_difficulty(hash_hex, job["difficulty"]):
                if self._job_is_stale():
                    return {"ok": False, "msg": "job expirado"}
                block = self._build_block_from_job(job, int(nonce), hash_hex)
                try:
                    ok, msg = self.chain.accept_block(block)
                except Exception as e:
                    log.exception("pool: accept_block lancou")
                    ok, msg = False, str(e)

                if ok:
                    try:
                        self._generate_job(
                            job["miner_address"], job["miner_pubkey"])
                    except ValueError:
                        self.current_job = None
                    return {
                        "ok": True,
                        "block_found": True,
                        "msg": "bloco aceito",
                        "height": job["height"],
                    }

                log.warning(f"pool: bloco rejeitado: {msg}")
                try:
                    self._generate_job(
                        job["miner_address"], job["miner_pubkey"])
                except ValueError:
                    self.current_job = None
                return {"ok": False, "msg": f"bloco rejeitado: {msg}"}

            # Share válida mas não é bloco.
            return {"ok": True, "block_found": False}

    def _build_block_from_job(self, job: dict, nonce: int,
                              hash_hex: str) -> dict:
        """
        Monta bloco com todos os campos que validate_block exige.
        validators_sig fica vazio (o nó local pode enriquecer depois,
        ou operar em modo bootstrap).
        """
        block = {
            "height":         job["height"],
            "prev_hash":      job["prev_hash"],
            "merkle":         job["merkle"],
            "timestamp":      job["timestamp"],
            "nonce":          nonce,
            "difficulty":     job["difficulty"],
            "hash":           hash_hex,
            "transactions":   job["txs"],
            "validators_sig": [],
        }
        try:
            attach = getattr(self.chain, "_attach_auth_tag", None)
            if callable(attach):
                tagged = attach(dict(block))
                if isinstance(tagged, dict) and tagged.get("auth_tag"):
                    block["auth_tag"] = tagged["auth_tag"]
        except Exception:
            pass
        return block

    # ------------------------------------------------------------------
    # Heartbeat / disconnect / stats
    # ------------------------------------------------------------------
    def heartbeat(self, miner_id: str, hashrate: float = 0.0,
                  hashes_total: int = 0) -> dict:
        with self.lock:
            miner = self.miners.get(miner_id)
            if miner is None:
                return {"ok": False, "msg": "miner desconhecido"}
            miner["last_seen"]    = time.time()
            miner["hashrate"]     = float(hashrate)
            miner["hashes_total"] = int(hashes_total)
            return {"ok": True}

    def disconnect(self, miner_id: str) -> dict:
        with self.lock:
            self.miners.pop(miner_id, None)
            return {"ok": True}

    def stats(self) -> dict:
        with self.lock:
            total_hashrate = sum(m.get("hashrate", 0.0)
                                 for m in self.miners.values())
            job = self.current_job or {}
            return {
                "ok":              True,
                "miners":          len(self.miners),
                "height":          self.chain.db.height(),
                "job_id":          job.get("job_id"),
                "job_height":      job.get("height"),
                "total_hashrate":  round(total_hashrate, 2),
            }

    def miners_list(self) -> list:
        with self.lock:
            return [
                {
                    "miner_id":     mid,
                    "address":      m.get("address"),
                    "hashrate":     round(m.get("hashrate", 0.0), 2),
                    "last_seen":    m.get("last_seen"),
                    "hashes_total": m.get("hashes_total", 0),
                }
                for mid, m in self.miners.items()
            ]
