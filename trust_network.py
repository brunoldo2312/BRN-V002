"""trust_network.py — Rede de confiança e reputação do BRN (v2)
================================================================
v2 (2026-10-19):
  [FIX] RLock em todas as operações (thread-safe com P2P + miner + HTTP).
  [FIX] get_validator_list() não dá KeyError com validators órfãos.
  [FIX] record_share_submitted() rastreia valid_shares e invalid_shares.
  [FIX] list_all_nodes() com precedência de operadores correta.
  [FIX] Saves em disco BATCHED — flag dirty + flush() a cada 30s.
  [NEW] flush() público e shutdown() para save no Ctrl+C.
  [NEW] marcar_nó_online() / marcar_nó_offline() com nomes PT-BR.
  [NEW] Compatível com PoW: NÃO interfere no consenso (reputação só).
================================================================
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any

log = logging.getLogger("trust_network")

# ============================================================
# CONSTANTES DO SISTEMA
# ============================================================
INITIAL_TRUST_SCORE = 100.0
MAX_TRUST_SCORE = 1000.0
MIN_TRUST_SCORE = 0.0
TRUST_THRESHOLD_VALIDATOR = 600.0
TRUST_THRESHOLD_ACTIVE = 200.0

GAIN_BLOCK_VALIDATED = 2.0
GAIN_ONLINE_HOUR = 0.1
GAIN_SHARE_SUBMITTED = 0.05
GAIN_REWARD_DISTRIBUTED = 5.0

PENALTY_INVALID_BLOCK = -50.0
PENALTY_OFFLINE_DROP = -2.0
PENALTY_LIE_DETECTED = -200.0
PENALTY_REORG_CAUSED = -100.0
PENALTY_SUSPICIOUS_RATE = -10.0

TRUST_DECAY_DAILY = 0.5
DECAY_INTERVAL_SECONDS = 86400

MAX_VALIDATORS = 21
VALIDATOR_ROTATION_BLOCKS = 100
DEFAULT_DATA_FILE = "trust_network.json"

# Intervalo de flush em disco (segundos)
FLUSH_INTERVAL_S = 30.0


# ============================================================
# ESTRUTURA DE DADOS DO NÓ
# ============================================================
@dataclass
class NodeRecord:
    node_id: str
    address: str
    public_key: str
    peer_addr: str = ""
    
    trust_score: float = INITIAL_TRUST_SCORE
    trust_level: str = "new"
    
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    connected: bool = False
    total_online_hours: float = 0.0
    last_connect_time: Optional[float] = None
    
    blocks_validated: int = 0
    blocks_proposed: int = 0
    shares_submitted: int = 0
    valid_shares: int = 0
    invalid_shares: int = 0
    rewards_distributed: int = 0
    
    penalties: List[Dict] = field(default_factory=list)
    total_penalties: int = 0
    banned: bool = False
    ban_reason: str = ""
    
    is_validator: bool = False
    validator_since: Optional[float] = None
    votes_received: int = 0
    
    uptime_score: float = 100.0
    success_rate: float = 100.0

    def update_trust_level(self) -> None:
        if self.banned:
            self.trust_level = "banned"
            self.is_validator = False
        elif self.trust_score >= TRUST_THRESHOLD_VALIDATOR:
            self.trust_level = "validator"
        elif self.trust_score >= 400:
            self.trust_level = "trusted"
        elif self.trust_score >= TRUST_THRESHOLD_ACTIVE:
            self.trust_level = "active"
        else:
            self.trust_level = "new"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NodeRecord":
        campos = {
            "node_id", "address", "public_key", "peer_addr",
            "trust_score", "trust_level",
            "first_seen", "last_seen", "connected",
            "total_online_hours", "last_connect_time",
            "blocks_validated", "blocks_proposed",
            "shares_submitted", "valid_shares", "invalid_shares",
            "rewards_distributed", "penalties", "total_penalties",
            "banned", "ban_reason", "is_validator", "validator_since",
            "votes_received", "uptime_score", "success_rate",
        }
        return cls(**{k: v for k, v in data.items() if k in campos})


# ============================================================
# TRUST NETWORK (thread-safe)
# ============================================================
class TrustNetwork:
    """Reputação de nós. NÃO interfere no consenso (PoW continua)."""

    def __init__(self, data_file: str = DEFAULT_DATA_FILE):
        self.data_file = Path(data_file)
        self.nodes: Dict[str, NodeRecord] = {}
        self.active_validators: Set[str] = set()
        self.last_decay_time: float = time.time()
        self._last_rotation_height: int = 0

        # [FIX] Thread safety
        self._lock = threading.RLock()
        self._dirty = False
        self._last_flush = 0.0

        self._load()
        log.info("[TrustNetwork] Inicializada — %d nós, %d validadores",
                 len(self.nodes), len(self.active_validators))

    # ========================================================
    # PERSISTÊNCIA (BATCHED)
    # ========================================================
    def _mark_dirty(self) -> None:
        self._dirty = True
        agora = time.time()
        if agora - self._last_flush > FLUSH_INTERVAL_S:
            self.flush()

    def flush(self) -> None:
        """Força save em disco agora (chamado a cada 30s)."""
        with self._lock:
            if not self._dirty:
                return
            try:
                data = {
                    "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
                    "last_decay_time": self.last_decay_time,
                    "active_validators": list(self.active_validators),
                    "last_rotation_height": self._last_rotation_height,
                    "saved_at": time.time(),
                }
                with open(self.data_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                self._dirty = False
                self._last_flush = time.time()
                log.debug("[TrustNetwork] flush OK (%d nós)", len(self.nodes))
            except Exception as e:
                log.error("[TrustNetwork] flush falhou: %s", e)

    def shutdown(self) -> None:
        """Chamado no Ctrl+C — força save final."""
        self.flush()

    def _load(self) -> None:
        if not self.data_file.exists():
            log.info("[TrustNetwork] Arquivo não existe — iniciando vazio")
            return
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                self.nodes = {
                    nid: NodeRecord.from_dict(nd)
                    for nid, nd in data.get("nodes", {}).items()
                }
                self.last_decay_time = data.get("last_decay_time", time.time())
                self.active_validators = set(data.get("active_validators", []))
                self._last_rotation_height = int(data.get("last_rotation_height", 0))
            log.info("[TrustNetwork] Carregada — %d nós, %d validadores",
                     len(self.nodes), len(self.active_validators))
        except Exception as e:
            log.error("[TrustNetwork] Load falhou: %s — iniciando vazio", e)

    # ========================================================
    # REGISTRO E CONEXÃO
    # ========================================================
    def register_node(self, node_id: str, address: str, public_key: str,
                      peer_addr: str = "") -> NodeRecord:
        with self._lock:
            if node_id in self.nodes:
                n = self.nodes[node_id]
                n.last_seen = time.time()
                if peer_addr:
                    n.peer_addr = peer_addr
                return n

            n = NodeRecord(
                node_id=node_id,
                address=address,
                public_key=public_key,
                peer_addr=peer_addr,
            )
            self.nodes[node_id] = n
            self._mark_dirty()
            log.info("[TrustNetwork] ✅ Novo nó: %s (%s)",
                     node_id[:12] + "…", address[:12] + "…")
            return n

    def mark_connected(self, node_id: str) -> None:
        with self._lock:
            n = self.nodes.get(node_id)
            if not n:
                return
            n.connected = True
            n.last_connect_time = time.time()
            n.last_seen = time.time()
            self._adjust_score(node_id, GAIN_ONLINE_HOUR * 2, "conexão")

    def mark_disconnected(self, node_id: str, unexpected: bool = False) -> None:
        with self._lock:
            n = self.nodes.get(node_id)
            if not n or not n.connected:
                return
            if n.last_connect_time:
                h = (time.time() - n.last_connect_time) / 3600.0
                n.total_online_hours += h
                gain = GAIN_ONLINE_HOUR * h
                if gain > 0:
                    self._adjust_score(node_id, gain, f"online {h:.1f}h")
            n.connected = False
            n.last_seen = time.time()
            if unexpected:
                self._adjust_score(node_id, PENALTY_OFFLINE_DROP, "drop inesperado")
            self._update_uptime(node_id)
            self._mark_dirty()

    def heartbeat(self, node_id: str) -> None:
        with self._lock:
            n = self.nodes.get(node_id)
            if not n:
                return
            n.last_seen = time.time()
            if n.connected:
                self._adjust_score(node_id, GAIN_ONLINE_HOUR / 10, "heartbeat")
            self._apply_decay()

    # ========================================================
    # EVENTOS (chamados pelo P2P / miner)
    # ========================================================
    def record_block_validated(self, node_id: str, block_height: int) -> None:
        with self._lock:
            n = self.nodes.get(node_id)
            if not n:
                return
            n.blocks_validated += 1
            self._adjust_score(node_id, GAIN_BLOCK_VALIDATED,
                               f"validou #{block_height}")

    def record_block_rejected(self, node_id: str, block_height: int,
                              reason: str = "") -> None:
        with self._lock:
            n = self.nodes.get(node_id)
            if not n:
                return
            n.total_penalties += 1
            n.penalties.append({
                "time": time.time(),
                "reason": f"bloco #{block_height} rejeitado: {reason}",
                "severity": "high",
                "amount": PENALTY_INVALID_BLOCK,
            })
            self._adjust_score(node_id, PENALTY_INVALID_BLOCK,
                               f"bloco inválido #{block_height}")
            if n.trust_score < MIN_TRUST_SCORE:
                self.ban_node(node_id, "pontuação mínima atingida")

    def record_share_submitted(self, node_id: str, valid: bool = True) -> None:
        with self._lock:
            n = self.nodes.get(node_id)
            if not n:
                return
            n.shares_submitted += 1
            if valid:
                n.valid_shares += 1
                self._adjust_score(node_id, GAIN_SHARE_SUBMITTED, "share válida")
            else:
                n.invalid_shares += 1
                self._adjust_score(node_id, PENALTY_SUSPICIOUS_RATE, "share inválida")
            # [FIX] success_rate agora usa contadores corretos
            if n.shares_submitted > 0:
                n.success_rate = round(
                    (n.valid_shares / n.shares_submitted) * 100.0, 1
                )

    def penalize(self, node_id: str, reason: str,
                 severity: str = "medium",
                 auto_ban_threshold: Optional[float] = None) -> float:
        penalties = {
            "low": -5.0, "medium": -50.0,
            "high": -200.0, "critical": -1000.0,
        }
        amount = penalties.get(severity, -50.0)
        with self._lock:
            n = self.nodes.get(node_id)
            if not n:
                return 0.0
            n.penalties.append({
                "time": time.time(),
                "reason": reason,
                "severity": severity,
                "amount": amount,
            })
            n.total_penalties += 1
            new_score = self._adjust_score(node_id, amount, reason)
            if auto_ban_threshold and new_score < auto_ban_threshold:
                self.ban_node(node_id, f"score {new_score:.1f} < {auto_ban_threshold}")
            return amount

    def ban_node(self, node_id: str, reason: str) -> bool:
        with self._lock:
            n = self.nodes.get(node_id)
            if not n:
                return False
            n.banned = True
            n.ban_reason = reason
            n.is_validator = False
            self.active_validators.discard(node_id)
            log.warning("[TrustNetwork] 🚫 %s banido: %s",
                        node_id[:12] + "…", reason)
            self._mark_dirty()
            return True

    # ========================================================
    # SCORE INTERNO
    # ========================================================
    def _adjust_score(self, node_id: str, delta: float, reason: str = "") -> float:
        n = self.nodes.get(node_id)
        if not n or n.banned:
            return 0.0
        old = n.trust_score
        n.trust_score = max(MIN_TRUST_SCORE, min(MAX_TRUST_SCORE, old + delta))
        n.update_trust_level()
        if abs(delta) > 0.1:
            log.debug("[TrustNetwork] %s %.1f→%.1f (%+0.1f) %s",
                      node_id[:12] + "…", old, n.trust_score, delta, reason)
            self._mark_dirty()
        return n.trust_score

    def _update_uptime(self, node_id: str) -> None:
        n = self.nodes.get(node_id)
        if not n:
            return
        recent = [
            p for p in n.penalties
            if "drop" in p.get("reason", "")
            and time.time() - p.get("time", 0) < 86400 * 7
        ]
        n.uptime_score = max(0.0, 100.0 - len(recent) * 5.0)

    def _apply_decay(self) -> None:
        now = time.time()
        if now - self.last_decay_time < DECAY_INTERVAL_SECONDS:
            return
        self.last_decay_time = now
        for node_id, n in self.nodes.items():
            if n.banned or n.connected:
                continue
            dias = (now - n.last_seen) / 86400.0
            if dias >= 1:
                fator = 1.0 - (TRUST_DECAY_DAILY / 100.0)
                n.trust_score = max(MIN_TRUST_SCORE, n.trust_score * fator)
                n.update_trust_level()
        log.info("[TrustNetwork] decaimento aplicado")
        self._mark_dirty()

    # ========================================================
    # VALIDADORES (INFORMATIVO — NÃO SUBSTITUI PoW)
    # ========================================================
    def select_validators(self, current_block_height: int) -> List[str]:
        """
        Retorna candidatos a validador ordenados por confiança.
        NOTA: no BRN atual (PoW), isto é apenas INFORMATIVO.
        Não bloqueia mineração nem consenso.
        """
        with self._lock:
            if (current_block_height - self._last_rotation_height
                    < VALIDATOR_ROTATION_BLOCKS):
                return list(self.active_validators)
            self._last_rotation_height = current_block_height
            candidates = [
                n for n in self.nodes.values()
                if not n.banned and n.trust_score >= TRUST_THRESHOLD_VALIDATOR
            ]
            if not candidates:
                self.active_validators.clear()
                self._mark_dirty()
                return []
            candidates.sort(key=lambda n: (-n.trust_score, -n.total_online_hours))
            selected = candidates[:MAX_VALIDATORS]
            ids = [n.node_id for n in selected]
            self.active_validators = set(ids)
            for n in self.nodes.values():
                n.is_validator = (n.node_id in ids)
                if n.is_validator and not n.validator_since:
                    n.validator_since = time.time()
            log.info("[TrustNetwork] validadores: %d/%d",
                     len(ids), len(candidates))
            self._mark_dirty()
            return ids

    def is_validator(self, node_id: str) -> bool:
        with self._lock:
            return node_id in self.active_validators

    def get_validator_list(self) -> List[Dict]:
        with self._lock:
            # [FIX] Filtra órfãos ANTES do sort
            ids = [nid for nid in self.active_validators if nid in self.nodes]
            return [
                {
                    "node_id": nid,
                    "address": self.nodes[nid].address,
                    "trust_score": round(self.nodes[nid].trust_score, 1),
                    "trust_level": self.nodes[nid].trust_level,
                    "uptime_score": round(self.nodes[nid].uptime_score, 1),
                    "blocks_validated": self.nodes[nid].blocks_validated,
                }
                for nid in sorted(ids,
                                  key=lambda x: -self.nodes[x].trust_score)
            ]

    # ========================================================
    # CONSULTAS / API
    # ========================================================
    def get_node_info(self, node_id: str) -> Optional[Dict]:
        with self._lock:
            n = self.nodes.get(node_id)
            if not n:
                return None
            return {
                **n.to_dict(),
                "is_validator": node_id in self.active_validators,
                "threshold_validator": TRUST_THRESHOLD_VALIDATOR,
                "percentile": self._get_percentile(node_id),
            }

    def list_all_nodes(self, min_level: str = "new") -> List[Dict]:
        with self._lock:
            levels = ["new", "active", "trusted", "validator", "banned"]
            if min_level not in levels:
                min_idx = 0
            else:
                min_idx = levels.index(min_level)
            out = []
            for n in sorted(self.nodes.values(), key=lambda x: -x.trust_score):
                # [FIX] Precedência com parênteses explícitos
                if (not n.banned or min_level == "banned") and \
                   levels.index(n.trust_level) >= min_idx:
                    out.append({
                        "node_id": n.node_id,
                        "address": n.address,
                        "trust_score": round(n.trust_score, 1),
                        "trust_level": n.trust_level,
                        "connected": n.connected,
                        "is_validator": n.is_validator,
                        "uptime_hours": round(n.total_online_hours, 1),
                        "last_seen_ago": round((time.time() - n.last_seen) / 60, 1),
                    })
            return out

    def _get_percentile(self, node_id: str) -> float:
        n = self.nodes.get(node_id)
        if not n:
            return 0.0
        scores = sorted(s for s in (x.trust_score for x in self.nodes.values()
                                    if not x.banned))
        if not scores:
            return 0.0
        pos = sum(1 for s in scores if s <= n.trust_score)
        return round((pos / len(scores)) * 100.0, 1)

    def get_network_stats(self) -> Dict:
        with self._lock:
            ativos = [n for n in self.nodes.values() if not n.banned and n.connected]
            validos = [n for n in self.nodes.values() if not n.banned]
            avg = (sum(n.trust_score for n in validos) / max(1, len(validos)))
            return {
                "total_nodes": len(self.nodes),
                "connected_nodes": len(ativos),
                "banned_nodes": sum(1 for n in self.nodes.values() if n.banned),
                "validators_count": len(self.active_validators),
                "avg_trust": round(avg, 1),
                "top_validators": self.get_validator_list()[:5],
            }
