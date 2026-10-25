"""
trust_system.py — Sistema de Reputação e Consenso do BRN (v3)
================================================================
v3 (2026-10-24):
  [NEW] Adicionado sync_scores_from_peer() para consenso global de reputação.
  [NEW] Adicionado get_top_validators() para a regra dos "Top 5 Validadores".
  [NEW] Trava de segurança (lock) em todas as operações.
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

class TrustConfig:
    SCORE_THRESHOLD = 100          
    SCORE_PER_VALID_BLOCK = 5      
    SCORE_PER_VALID_TX = 1         
    PENALTY_INVALID_BLOCK = -50    
    PENALTY_BEHAVIOR_SUSPICIOUS = -30  
    SCORE_DECAY_HOURS = 72         
    MAX_PEERS = 50                 
    TOP_VALIDATORS_COUNT = 5       # [NOVO] Regra dos 5 validadores
    AUTO_PROMOTE = True            

class PeerTrustSystem:
    def __init__(self):
        self.peers = {}  
        self.lock = threading.RLock()
        self.load_data()

    def _atomic_write(self, filepath, data):
        """Gravação atômica para evitar corrupção se o PC desligar."""
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
                try: os.remove(tmp_path)
                except: pass

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
            self._atomic_write(TRUST_DATA_FILE, self.peers)

    def ensure_peer_entry(self, public_key_hex: str):
        """Cria entrada do peer se não existe e atualiza o last_seen."""
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
                    "promoted_at": None
                }
            self.peers[public_key_hex]["last_seen"] = time.time()
            self.peers[public_key_hex]["total_interactions"] += 1
            return self.peers[public_key_hex]

    def update_score(self, public_key_hex: str, action: str):
        """Atualiza pontuação com base na ação (bloco válido, tx, etc)."""
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

            if action == "valid_block": peer["valid_blocks"] += 1
            elif action == "invalid_block": peer["invalid_blocks"] += 1

            # 🚀 VERIFICAR PROMOÇÃO AUTOMÁTICA
            if TrustConfig.AUTO_PROMOTE and peer["status"] not in ("trusted", "banned"):
                self.check_auto_promotion(public_key_hex)

            # ⚠️ Verificar banimento
            if peer["score"] <= -50 and peer["status"] != "banned":
                peer["status"] = "banned"
                log.warning(f"🚫 PEER BANIDO: {public_key_hex[:12]}... (pontuação muito baixa)")

            self.save_data()
            return peer["score"]

    def check_auto_promotion(self, public_key_hex: str):
        with self.lock:
            peer = self.peers[public_key_hex]
            if peer["status"] in ["trusted", "banned"]: return

            if peer["score"] >= TrustConfig.SCORE_THRESHOLD:
                peer["status"] = "trusted"
                peer["promoted_at"] = time.time()
                log.info(f"🎉 PEER PROMOVIDO: {public_key_hex[:12]}... | Score: {peer['score']}")

    # ============================================================
    # [NOVO] Sincronização Global de Reputação (Consenso)
    # ============================================================
    def sync_scores_from_peer(self, remote_scores: dict):
        """
        Recebe os scores de outro nó e faz a média para chegar a um consenso.
        Isso evita que um único nó malicioso mude a nota de todo mundo.
        """
        with self.lock:
            for pubkey, remote_score in remote_scores.items():
                # Ignora scores absurdos (proteção)
                if remote_score > 10000 or remote_score < -1000:
                    continue
                    
                if pubkey in self.peers:
                    local_score = self.peers[pubkey]["score"]
                    # Média ponderada (dá um pouco mais de peso para o local)
                    new_score = (local_score * 0.6) + (remote_score * 0.4)
                    self.peers[pubkey]["score"] = new_score
                else:
                    # Se não conheço o nó, crio uma entrada com o score remoto
                    self.ensure_peer_entry(pubkey)
                    self.peers[pubkey]["score"] = remote_score
            self.save_data()

    # ============================================================
    # [NOVO] Obter os Top Validadores
    # ============================================================
    def get_top_validators(self, count: int = 5) -> List[str]:
        """Retorna a lista das pubkeys dos Top N nós por reputação."""
        with self.lock:
            # Filtra apenas os não banidos
            valid_peers = [(k, v) for k, v in self.peers.items() if v.get("status") != "banned"]
            # Ordena do maior score para o menor
            valid_peers.sort(key=lambda x: x[1].get("score", 0), reverse=True)
            # Pega os Top N
            top_n = valid_peers[:count]
            return [k for k, v in top_n]

    def is_trusted(self, public_key_hex: str) -> bool:
        with self.lock:
            peer = self.peers.get(public_key_hex)
            if not peer: return False
            if peer["status"] == "banned": return False
            return peer["status"] == "trusted"

# Instância global
trust_system = PeerTrustSystem()
