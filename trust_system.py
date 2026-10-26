"""
trust_system.py — Sistema de Reputação e Consenso do BRN (v4.1)
================================================================
v4.1 (2026-10-26):
  [FIX CRÍTICO] sync_scores_from_peer agora checa a promoção automática.
                Antes, um nó podia ter score 3000 e continuar como 'new'
                porque a promoção só era checada no update_score.
v4 (2026-10-25):
  [NEW] Salvamento automático a cada 30s em background.
  [NEW] get_top_validators() para a regra dos Top 5.
  [NEW] shutdown() para salvar no Ctrl+C.
================================================================
"""
import json
import time
import os
import threading
import logging
from typing import List, Dict

log = logging.getLogger("trust")

TRUST_DATA_FILE = "peer_trust_data.json"
TRUSTED_PEERS_FILE = "trusted_peers.json"
SAVE_INTERVAL_S = 30.0

class TrustConfig:
    SCORE_THRESHOLD = 100
    SCORE_PER_VALID_BLOCK = 5
    SCORE_PER_VALID_TX = 1
    PENALTY_INVALID_BLOCK = -50
    PENALTY_BEHAVIOR_SUSPICIOUS = -30
    SCORE_DECAY_HOURS = 72
    MAX_PEERS = 50
    TOP_VALIDATORS_COUNT = 5
    AUTO_PROMOTE = True


class PeerTrustSystem:
    def __init__(self):
        self.peers = {}
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._dirty = False
        
        self.load_data()
        
        self._saver_thread = threading.Thread(
            target=self._auto_save_loop,
            daemon=True,
            name="Trust-AutoSave"
        )
        self._saver_thread.start()

    def _atomic_write(self, filepath, data):
        tmp_path = f"{filepath}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, filepath)
        except Exception as e:
            log.error(f"Erro ao gravar {filepath}: {e}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _auto_save_loop(self):
        while not self._stop.wait(SAVE_INTERVAL_S):
            if self._dirty:
                log.debug("[Trust] Salvando automaticamente...")
                self.save_data()

    def load_data(self):
        with self.lock:
            if os.path.exists(TRUST_DATA_FILE):
                try:
                    with open(TRUST_DATA_FILE, "r", encoding="utf-8") as f:
                        self.peers = json.load(f)
                    log.info(f"✅ Dados de confiança carregados: {len(self.peers)} peers")
                except Exception as e:
                    log.error(f"⚠️ Erro ao carregar dados: {e}")
                    self.peers = {}

    def save_data(self):
        with self.lock:
            try:
                self._atomic_write(TRUST_DATA_FILE, self.peers)
                self._dirty = False
            except Exception as e:
                log.error(f"[Trust] Falha ao salvar: {e}")

    def shutdown(self):
        log.info("[Trust] Encerrando e salvando dados finais...")
        self._stop.set()
        self.save_data()
        if self._saver_thread and self._saver_thread.is_alive():
            self._saver_thread.join(timeout=3)

    # ============================================================
    # Manipulação de Dados
    # ============================================================
    def ensure_peer_entry(self, public_key_hex: str):
        with self.lock:
            if public_key_hex not in self.peers:
                self.peers[public_key_hex] = {
                    "score": 0,
                    "status": "new",
                    "first_seen": time.time(),
                    "last_seen": time.time(),
                    "valid_blocks": 0,
                    "invalid_blocks": 0,
                    "valid_txs": 0,
                    "total_interactions": 0,
                    "promoted_at": None,
                }
            self.peers[public_key_hex]["last_seen"] = time.time()
            self.peers[public_key_hex]["total_interactions"] += 1
            self._dirty = True
            return self.peers[public_key_hex]

    def update_score(self, public_key_hex: str, action: str):
        with self.lock:
            peer = self.ensure_peer_entry(public_key_hex)
            old_score = peer["score"]

            rules = {
                "valid_block": TrustConfig.SCORE_PER_VALID_BLOCK,
                "valid_transaction": TrustConfig.SCORE_PER_VALID_TX,
                "invalid_block": TrustConfig.PENALTY_INVALID_BLOCK,
                "invalid_transaction": -10,
                "suspicious_behavior": TrustConfig.PENALTY_BEHAVIOR_SUSPICIOUS,
                "heartbeat_ok": 1,
            }

            change = rules.get(action, 0)
            peer["score"] = max(-100, peer["score"] + change)

            if action == "valid_block":
                peer["valid_blocks"] += 1
            elif action == "invalid_block":
                peer["invalid_blocks"] += 1

            if TrustConfig.AUTO_PROMOTE and peer["status"] not in ("trusted", "banned"):
                self.check_auto_promotion(public_key_hex)

            if peer["score"] <= -50 and peer["status"] != "banned":
                peer["status"] = "banned"
                log.warning(f"🚫 PEER BANIDO: {public_key_hex[:12]}... (score baixo)")

            self._dirty = True
            return peer["score"]

    def check_auto_promotion(self, public_key_hex: str):
        with self.lock:
            peer = self.peers[public_key_hex]
            if peer["status"] in ("trusted", "banned"):
                return
            if peer["score"] >= TrustConfig.SCORE_THRESHOLD:
                peer["status"] = "trusted"
                peer["promoted_at"] = time.time()
                log.info(f"🎉 PEER PROMOVIDO: {public_key_hex[:12]}... | Score: {peer['score']:.1f}")

    def sync_scores_from_peer(self, remote_scores: dict):
        """[FIX v4.1] Agora promove o nó a trusted se o score passar de 100."""
        with self.lock:
            for pubkey, remote_score in remote_scores.items():
                if remote_score > 10000 or remote_score < -1000:
                    continue
                if pubkey in self.peers:
                    local_score = self.peers[pubkey]["score"]
                    # Média ponderada (60% local, 40% remoto)
                    new_score = (local_score * 0.6) + (remote_score * 0.4)
                    self.peers[pubkey]["score"] = new_score
                    
                    # [FIX] Checa se o nó merece ser promovido após a atualização
                    if TrustConfig.AUTO_PROMOTE and self.peers[pubkey]["status"] not in ("trusted", "banned"):
                        if new_score >= TrustConfig.SCORE_THRESHOLD:
                            self.peers[pubkey]["status"] = "trusted"
                            self.peers[pubkey]["promoted_at"] = time.time()
                            log.info(f"🎉 PEER PROMOVIDO (via sync): {pubkey[:12]}... | Score: {new_score:.1f}")
                else:
                    self.ensure_peer_entry(pubkey)
                    self.peers[pubkey]["score"] = remote_score
            self._dirty = True

    def get_top_validators(self, count: int = 5) -> List[str]:
        with self.lock:
            valid_peers = [(k, v) for k, v in self.peers.items() if v.get("status") != "banned"]
            valid_peers.sort(key=lambda x: x[1].get("score", 0), reverse=True)
            top_n = valid_peers[:count]
            return [k for k, _ in top_n]

    def is_trusted(self, public_key_hex: str) -> bool:
        with self.lock:
            peer = self.peers.get(public_key_hex)
            if not peer:
                return False
            if peer["status"] == "banned":
                return False
            return peer["status"] == "trusted"


# Instância global
trust_system = PeerTrustSystem()
