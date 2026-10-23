import json
import time
import os
import threading
import logging

# Arquivos de dados
TRUST_DATA_FILE = "peer_trust_data.json"
TRUSTED_PEERS_FILE = "trusted_peers.json"

log = logging.getLogger("trust")

# ⚙️ CONFIGURAÇÕES — Ajuste como quiser
class TrustConfig:
    SCORE_THRESHOLD = 100          # Pontuação mínima para entrar na lista branca
    SCORE_PER_VALID_BLOCK = 5      # +5 pontos por bloco válido enviado
    SCORE_PER_VALID_TX = 1         # +1 ponto por transação correta
    PENALTY_INVALID_BLOCK = -50    # -50 se enviar bloco inválido
    PENALTY_BEHAVIOR_SUSPICIOUS = -30  # -30 comportamento estranho
    SCORE_DECAY_HOURS = 72         # Perde confiança após 3 dias inativo
    MAX_PEERS = 50                 # Limite máximo de nós confiáveis na rede
    AUTO_PROMOTE = True            # ✅ Promove automaticamente quando atinge a pontuação

class PeerTrustSystem:
    def __init__(self):
        self.peers = {}  # public_key -> dados de confiança
        self.lock = threading.RLock() # [FIX] Lock para evitar race condition
        self.load_data()

    def _atomic_write(self, filepath, data):
        """[FIX] Gravação atômica para evitar corrupção de arquivo"""
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
                except:
                    pass

    def load_data(self):
        """Carrega dados de confiança salvos"""
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
        """Salva dados de confiança"""
        with self.lock:
            self._atomic_write(TRUST_DATA_FILE, self.peers)

    def ensure_peer_entry(self, public_key_hex: str):
        """Cria entrada do peer se não existe"""
        with self.lock:
            if public_key_hex not in self.peers:
                self.peers[public_key_hex] = {
                    "score": 0,
                    "status": "new",          # new → candidate → trusted → banned
                    "first_seen": time.time(),
                    "last_seen": time.time(),
                    "valid_blocks": 0,
                    "invalid_blocks": 0,
                    "valid_txs": 0,
                    "total_interactions": 0,
                    "promoted_at": None
                }
            # Atualizar último visto
            self.peers[public_key_hex]["last_seen"] = time.time()
            self.peers[public_key_hex]["total_interactions"] += 1
            return self.peers[public_key_hex]

    def update_score(self, public_key_hex: str, action: str):
        """Atualiza pontuação com base na ação"""
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
            peer["score"] = max(-100, peer["score"] + change)  # Mínimo -100

            # Contadores
            if action == "valid_block":
                peer["valid_blocks"] += 1
            elif action == "invalid_block":
                peer["invalid_blocks"] += 1

            log.info(f"📊 Peer {public_key_hex[:12]}...: {old_score} → {peer['score']} ({action})")

            # 🚀 VERIFICAR PROMOÇÃO AUTOMÁTICA
            if TrustConfig.AUTO_PROMOTE and peer["status"] not in ("trusted", "banned"):
                self.check_auto_promotion(public_key_hex)

            # ⚠️ Verificar banimento
            if peer["score"] <= -50 and peer["status"] != "banned":
                peer["status"] = "banned"
                log.warning(f"🚫 PEER BANIDO: {public_key_hex[:12]}... (pontuação muito baixa)")
                self.remove_from_trusted_list(public_key_hex) # [FIX] Remove da lista branca

            self.save_data()
            return peer["score"]

    def check_auto_promotion(self, public_key_hex: str):
        """Promove peer para lista branca automaticamente quando atinge a pontuação"""
        with self.lock:
            peer = self.peers[public_key_hex]
            
            # Já é confiável ou banido? Ignorar
            if peer["status"] in ["trusted", "banned"]:
                return

            # Atingiu pontuação necessária?
            if peer["score"] >= TrustConfig.SCORE_THRESHOLD:
                # Contar quantos confiáveis já temos
                trusted_count = sum(1 for p in self.peers.values() if p["status"] == "trusted")
                
                if trusted_count < TrustConfig.MAX_PEERS:
                    peer["status"] = "trusted"
                    peer["promoted_at"] = time.time()
                    self.add_to_trusted_list(public_key_hex)
                    log.info(f"🎉 PEER PROMOVIDO À CONFIÁVEL: {public_key_hex[:12]}... | Pontuação: {peer['score']}")
                else:
                    peer["status"] = "candidate"
                    log.info(f"⏳ Rede cheia — {public_key_hex[:12]}... é candidato mas lista branca está no limite")

    def add_to_trusted_list(self, public_key_hex: str):
        """Adiciona à lista branca permanente"""
        with self.lock:
            trusted_list = []
            if os.path.exists(TRUSTED_PEERS_FILE):
                try:
                    with open(TRUSTED_PEERS_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        trusted_list = data.get("trusted_peers", [])
                except Exception:
                    pass

            if public_key_hex not in trusted_list:
                trusted_list.append(public_key_hex)
                self._atomic_write(TRUSTED_PEERS_FILE, {"trusted_peers": trusted_list})

    def remove_from_trusted_list(self, public_key_hex: str):
        """[FIX] Remove da lista branca se for banido"""
        with self.lock:
            if not os.path.exists(TRUSTED_PEERS_FILE):
                return
            try:
                with open(TRUSTED_PEERS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    trusted_list = data.get("trusted_peers", [])
                
                if public_key_hex in trusted_list:
                    trusted_list.remove(public_key_hex)
                    self._atomic_write(TRUSTED_PEERS_FILE, {"trusted_peers": trusted_list})
            except Exception as e:
                log.error(f"Erro ao remover da lista branca: {e}")

    def is_trusted(self, public_key_hex: str) -> bool:
        """Verifica se peer está autorizado"""
        with self.lock:
            peer = self.peers.get(public_key_hex)
            if not peer:
                return False
            if peer["status"] == "banned":
                return False
            return peer["status"] == "trusted"

    def get_status(self, public_key_hex: str) -> dict:
        """Retorna status completo de um peer"""
        with self.lock:
            return self.peers.get(public_key_hex, {"status": "unknown", "score": 0})

    def cleanup_inactive(self):
        """Remove peers inativos há muito tempo"""
        now = time.time()
        removed = []
        with self.lock:
            for pubkey, peer in list(self.peers.items()):
                inactive_hours = (now - peer["last_seen"]) / 3600
                if inactive_hours > TrustConfig.SCORE_DECAY_HOURS and peer["status"] != "trusted":
                    del self.peers[pubkey]
                    removed.append(pubkey[:12])
            if removed:
                log.info(f"🧹 Limpeza: {len(removed)} peers inativos removidos")
                self.save_data()
        return removed


# Instância global
trust_system = PeerTrustSystem()