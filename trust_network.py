"""trust_network.py — Rede de confiança e reputação do BRN (v3.10)
================================================================
v3.10 (2026-11-03):
  [NEW] sync_local_validator(pubkey, address, peer_addr) — registra
        a pubkey local como nó ativo no trust em runtime. Chamado
        quando a carteira muda via GUI (após /api/wallet/set_validator_sk).
        Para Origin: persiste no trust_network.json com re-assinatura.
        Para Cliente: persiste no trust_network.local.json.
        Sem esse método, a pubkey da carteira nova só entrava no trust
        no próximo boot (e o main.py não a registrava em runtime).
  [KEEP] Todas as correções de v3.9.
v3.9 (2026-11-03):
  [FIX CRÍTICO] get_active_validators() agora faz fallback para
        get_top_validators() em DOIS casos:
          (a) quando "active" está vazio (active=0, top=N)
          (b) quando len(active) < count E top tem mais candidatos
              (ex: active=1 local via origin_pubkey, top=4)
        Antes, apenas o caso (a) caía em [] e nada mais — o caso (b)
        retornava 1 validador só, forçando required=1/1 e desligando
        o multi-sig de fato. Mesmo bug corrigido em blockchain.py
        v9.4.12 (fallback lá é defesa em profundidade).
  [KEEP] Todas as correções de v3.8.
v3.8 (2026-11-03):
  [NEW] remove_ghosts(max_age_s) — Auto-limpeza de nós inativos.
        Remove da memória e dos arquivos (.json e .local.json) qualquer
        nó que não dá sinal de vida há mais de X segundos (default 300s).
        Chamado no boot pelo main.py para evitar que nós fantasmas
        (ex: travados na altura 299) entrem no cálculo de quórum.
v3.7 (2026-11-01):
  [NEW] Persistência cliente-local em trust_network.local.json.
        Clientes (is_origin=False) NÃO podem assinar o
        trust_network.json (só o Origin assina). Antes, no flush()
        salvavam sem assinatura, e no próximo boot _load() rejeitava
        o arquivo → cliente recomeçava vazio → perdia o self-register
        do main.py → não assinava sign_block.
        Agora clientes salvam em .local.json e carregam ambos.
  [NEW] heartbeat_self(pubkey) — convenience que só chama heartbeat().
        Chamado pelo run_status_loop do main.py v13.7.12 para
        renovar last_seen do próprio nó a cada 30s.
v3.6 (2026-10-30):
  [FIX] get_active_validators() SEMPRE inclui o Origin, mesmo que
        ele esteja fora de ACTIVE_TTL_SECONDS.
        Também removido o fallback para "top score" (que retornava
        fantasmas quando ninguém estava ativo).
v3.5 (2026-10-30):
  [NEW] _prune_inactive(), get_active_validators(), set_origin_pubkey()
v3.4 (2026-10-29):
  [FIX] Deduplicação por public_key
================================================================
"""
from __future__ import annotations

__version__ = "3.10"

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any

from crypto import Ed25519PrivateKey, Ed25519PublicKey

log = logging.getLogger("trust_network")

INITIAL_TRUST_SCORE = 100.0
MAX_TRUST_SCORE = 1000.0
MIN_TRUST_SCORE = 0.0
TRUST_THRESHOLD_VALIDATOR = 150.0
TRUST_THRESHOLD_ACTIVE = 120.0

GAIN_BLOCK_VALIDATED = 20.0
GAIN_ONLINE_HOUR = 0.5
GAIN_SHARE_SUBMITTED = 0.5
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
FLUSH_INTERVAL_S = 10.0

NODE_TTL_SECONDS   = int(os.environ.get("BRN_TRUST_NODE_TTL",   "3600"))
ACTIVE_TTL_SECONDS = int(os.environ.get("BRN_TRUST_ACTIVE_TTL", "300"))
SCORE_PROTECTED    = int(os.environ.get("BRN_TRUST_PROTECT_SCORE", "500"))
PRUNE_INTERVAL_S   = 60.0


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


class TrustNetwork:
    def __init__(self, data_file: str = DEFAULT_DATA_FILE, is_origin: bool = False, node_sk=None):
        self.data_file = Path(data_file)
        # [v3.7] Cliente-local: trust_network.local.json (ao lado do principal)
        self.local_file = self.data_file.with_suffix(".local.json")
        self.is_origin = is_origin
        self.node_sk = node_sk

        self.nodes: Dict[str, NodeRecord] = {}
        self.active_validators: Set[str] = set()
        self.last_decay_time: float = time.time()
        self._last_rotation_height: int = 0

        self._origin_pubkey: str = ""
        self._last_prune: float = 0.0

        self._lock = threading.RLock()
        self._dirty = False
        self._dirty_local = False
        self._last_flush = 0.0

        self._load()
        log.info("[TrustNetwork] v%s Inicializada — %d nós, %d validadores "
                 "(origin=%s, local_file=%s)",
                 __version__, len(self.nodes), len(self.active_validators),
                 "sim" if is_origin else "não",
                 str(self.local_file) if not is_origin else "n/a")

    def set_origin_pubkey(self, pk: str) -> None:
        self._origin_pubkey = (pk or "").strip().lower()
        if self._origin_pubkey:
            log.info("[TrustNetwork] origin_pubkey=%s… (protegida)",
                     self._origin_pubkey[:16])

    def sync_local_validator(self, pubkey: str, address: str = "",
                             peer_addr: str = "") -> bool:
        """
        [v3.10] Registra a pubkey local como nó ativo no trust em runtime.

        Chamado quando a carteira muda via GUI (após
        /api/wallet/set_validator_sk, no server.py). Sem isso, a
        carteira nova só entrava no trust no próximo boot — e o
        main.py não registrava a pubkey em runtime, então o consenso
        local ficava com required=1/1 mesmo com a GUI "ativa".

        Comportamento:
          - Origin: persiste no trust_network.json (re-assina com node_sk).
          - Cliente: persiste no trust_network.local.json.
          - Em ambos: marca nó como connected=True e atualiza last_seen.

        Retorna True se registrou/atualizou, False se pubkey vazia.
        """
        if not pubkey:
            return False
        pk = pubkey.strip().lower()
        if not pk:
            return False

        with self._lock:
            self.register_node(pk, address or pk[:16], pk, peer_addr)
            n = self.nodes.get(pk)
            if n is not None:
                n.last_seen = time.time()
                n.connected = True
                n.last_connect_time = time.time()
                n.update_trust_level()
            self._mark_dirty()
            # Força flush imediato (não esperar FLUSH_INTERVAL_S)
            try:
                self._last_flush = 0.0
                self.flush()
            except Exception as e:
                log.warning("[TrustNetwork] sync_local_validator flush falhou: %s", e)

        log.info("[TrustNetwork] sync_local_validator: %s… (origin=%s)",
                 pk[:16], "sim" if self.is_origin else "não")
        return True

    def heartbeat_self(self, pubkey: str) -> None:
        """[v3.7] Convenience: renova last_seen do próprio nó."""
        self.heartbeat(pubkey)

    def remove_ghosts(self, max_age_s: int = 300) -> int:
        """
        [v3.8] Remove nós fantasmas (que não dão sinal de vida há mais de X segundos).
        Salva imediatamente os arquivos .json e .local.json limpos.
        """
        now = time.time()
        to_remove = []

        with self._lock:
            for nid, n in list(self.nodes.items()):
                age = now - n.last_seen

                # Nunca remove o próprio Origin (a não ser que esteja realmente morto)
                if self._origin_pubkey and n.public_key.lower() == self._origin_pubkey:
                    continue

                if age > max_age_s:
                    to_remove.append((nid, age))

            for nid, age in to_remove:
                log.info("[TrustNetwork] Auto-limpeza: removido fantasma %s… (inativo há %ds)",
                         nid[:12], int(age))
                del self.nodes[nid]

            if to_remove:
                self._mark_dirty()
                self.flush() # Persiste as mudanças imediatamente

        return len(to_remove)

    def _prune_inactive(self) -> int:
        now = time.time()
        if now - self._last_prune < PRUNE_INTERVAL_S:
            return 0
        self._last_prune = now

        to_remove = []
        with self._lock:
            for nid, n in list(self.nodes.items()):
                pk = (n.public_key or "").strip().lower()
                if pk and pk == self._origin_pubkey:
                    continue
                if n.trust_score >= SCORE_PROTECTED:
                    continue
                if (now - n.last_seen) > NODE_TTL_SECONDS:
                    to_remove.append(nid)

            for nid in to_remove:
                del self.nodes[nid]
                log.info("[TrustNetwork] pruned %s… (offline >%ds)",
                         nid[:12], NODE_TTL_SECONDS)

            if to_remove:
                self._mark_dirty()

        return len(to_remove)

    def _mark_dirty(self) -> None:
        self._dirty = True
        if not self.is_origin:
            self._dirty_local = True
        agora = time.time()
        if agora - self._last_flush > FLUSH_INTERVAL_S:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._dirty and not self._dirty_local:
                return

            self._prune_inactive()

            data = {
                "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
                "last_decay_time": self.last_decay_time,
                "active_validators": list(self.active_validators),
                "last_rotation_height": self._last_rotation_height,
            }

            # [v3.7] Client: salva em .local.json (sem assinatura)
            if not self.is_origin:
                try:
                    data["saved_at"] = time.time()
                    data["client_written"] = True
                    with open(self.local_file, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    self._dirty_local = False
                    self._last_flush = time.time()
                except Exception as e:
                    log.error("[TrustNetwork] flush local falhou: %s", e)
                return

            # Origin: salva main + assinatura (mantém local também)
            try:
                if self.node_sk is not None:
                    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    sig = self.node_sk.sign(payload)
                    data["signature"] = sig.hex()
                    data["origin_pubkey"] = self.node_sk.public_key().public_bytes_raw().hex()

                data["saved_at"] = time.time()

                with open(self.data_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)

                # Origin também atualiza seu próprio local (compat)
                try:
                    with open(self.local_file, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass

                self._dirty = False
                self._dirty_local = False
                self._last_flush = time.time()
            except Exception as e:
                log.error("[TrustNetwork] flush falhou: %s", e)

    def shutdown(self) -> None:
        self.flush()

    def _load(self) -> None:
        # ─── 1. Carrega arquivo principal (Origin-signed) ───
        if self.data_file.exists():
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                sig_hex = data.pop("signature", None)
                origin_pub_hex = data.pop("origin_pubkey", None)
                data.pop("saved_at", None)
                data.pop("client_written", None)

                if sig_hex and origin_pub_hex:
                    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    try:
                        pub_bytes = bytes.fromhex(origin_pub_hex)
                        pk = Ed25519PublicKey.from_public_bytes(pub_bytes)
                        pk.verify(bytes.fromhex(sig_hex), payload)
                        log.info("[TrustNetwork] Assinatura do Origem VALIDADA.")
                    except Exception as e:
                        log.error("[TrustNetwork] ASSINATURA INVÁLIDA! O arquivo foi alterado.")
                        if not self.is_origin:
                            # Cliente: ignora main inválido, mas ainda carrega local abaixo
                            data = {"nodes": {}}
                        else:
                            log.warning("[TrustNetwork] Origem ignorando falha de assinatura.")
                else:
                    if not self.is_origin:
                        log.info("[TrustNetwork] main sem assinatura — cliente carrega só local")
                        data = {"nodes": {}}
                    else:
                        log.info("[TrustNetwork] Origin iniciando sem assinatura (normal)")

                with self._lock:
                    for nid, nd in data.get("nodes", {}).items():
                        try:
                            self.nodes[nid] = NodeRecord.from_dict(nd)
                        except Exception as e:
                            log.warning("[TrustNetwork] node %s inválido: %s", nid[:12], e)
                    self.last_decay_time = data.get("last_decay_time", time.time())
                    self.active_validators = set(data.get("active_validators", []))
                    self._last_rotation_height = int(data.get("last_rotation_height", 0))
                log.info("[TrustNetwork] main carregado — %d nós", len(self.nodes))
            except Exception as e:
                log.error("[TrustNetwork] load main falhou: %s", e)

        # ─── 2. Cliente: carrega local (merge) ───
        if not self.is_origin and self.local_file.exists():
            try:
                with open(self.local_file, "r", encoding="utf-8") as f:
                    local = json.load(f)
                local.pop("saved_at", None)
                local.pop("client_written", None)
                with self._lock:
                    merged = 0
                    for nid, nd in local.get("nodes", {}).items():
                        if nid not in self.nodes:
                            try:
                                self.nodes[nid] = NodeRecord.from_dict(nd)
                                merged += 1
                            except Exception:
                                pass
                        else:
                            # Preserva o local (last_seen mais recente ganha)
                            try:
                                local_rec = NodeRecord.from_dict(nd)
                                if local_rec.last_seen > self.nodes[nid].last_seen:
                                    self.nodes[nid] = local_rec
                            except Exception:
                                pass
                log.info("[TrustNetwork] local merge — %d nós novos, total %d",
                         merged, len(self.nodes))
            except Exception as e:
                log.warning("[TrustNetwork] load local falhou: %s", e)

        log.info("[TrustNetwork] Carregada — %d nós, %d validadores",
                 len(self.nodes), len(self.active_validators))

    def register_node(self, node_id: str, address: str, public_key: str,
                      peer_addr: str = "") -> NodeRecord:
        with self._lock:
            if public_key:
                for existing_id, existing_node in list(self.nodes.items()):
                    if existing_node.public_key == public_key:
                        existing_node.last_seen = time.time()
                        if peer_addr:
                            existing_node.peer_addr = peer_addr
                        if existing_id != node_id:
                            self.nodes.pop(existing_id)
                            existing_node.node_id = node_id
                            self.nodes[node_id] = existing_node
                        self._mark_dirty()
                        return existing_node

            if node_id in self.nodes:
                n = self.nodes[node_id]
                n.last_seen = time.time()
                if peer_addr:
                    n.peer_addr = peer_addr
                if not n.public_key and public_key:
                    n.public_key = public_key
                    self._mark_dirty()
                return n

            n = NodeRecord(
                node_id=node_id,
                address=address,
                public_key=public_key,
                peer_addr=peer_addr,
            )
            self.nodes[node_id] = n
            self._mark_dirty()
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
            log.warning("[TrustNetwork] %s banido: %s",
                        node_id[:12] + "…", reason)
            self._mark_dirty()
            return True

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

    def select_validators(self, current_block_height: int) -> List[str]:
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
            candidates.sort(key=lambda n: (-n.trust_score, n.node_id))
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

    def get_top_validators(self, count: int = 5) -> List[str]:
        try:
            n_target = max(1, int(count))
        except (TypeError, ValueError):
            n_target = 5

        with self._lock:
            cands = [
                n for n in self.nodes.values()
                if not n.banned and n.public_key
            ]
            cands.sort(key=lambda n: (-n.trust_score, n.public_key))

            out: List[str] = []
            seen_pk: Set[str] = set()
            for n in cands:
                pk = (n.public_key or "").strip().lower()
                if not pk or pk in seen_pk:
                    continue
                seen_pk.add(pk)
                out.append(pk)
                if len(out) >= n_target:
                    break
            return out

    def get_active_validators(self, count: int = 5) -> List[str]:
        try:
            n_target = max(1, int(count))
        except (TypeError, ValueError):
            n_target = 5

        now = time.time()
        with self._lock:
            cands = [
                n for n in self.nodes.values()
                if not n.banned and n.public_key
            ]

            active = [n for n in cands
                      if (now - n.last_seen) < ACTIVE_TTL_SECONDS]

            if self._origin_pubkey:
                origin = next(
                    (n for n in cands
                     if (n.public_key or "").strip().lower() == self._origin_pubkey),
                    None,
                )
                if origin is not None and origin not in active:
                    active.append(origin)

            # [v3.9] Fallback (a): active totalmente vazio → usa top.
            if not active:
                top = self.get_top_validators(count=n_target)
                if top:
                    log.warning(
                        "[TrustNetwork] get_active_validators: nenhum nó "
                        "ativo e Origin ausente — usando get_top_validators "
                        "(%d, modo bootstrap)",
                        len(top),
                    )
                    return top
                log.warning(
                    "[TrustNetwork] get_active_validators: nenhum nó ativo "
                    "e Origin ausente — retornando [] (modo bootstrap)"
                )
                return []

            active.sort(key=lambda n: (-n.trust_score, n.public_key))

            out: List[str] = []
            seen_pk: Set[str] = set()
            for n in active:
                pk = (n.public_key or "").strip().lower()
                if not pk or pk in seen_pk:
                    continue
                seen_pk.add(pk)
                out.append(pk)
                if len(out) >= n_target:
                    break

            # [v3.9] Fallback (b): active incompleto (< n_target) e top
            # tem mais candidatos → usa top para não reduzir o quórum.
            # Ex.: active=1 (só local), top=4 → retorna os 4.
            if len(out) < n_target:
                top = self.get_top_validators(count=n_target)
                if len(top) > len(out):
                    log.warning(
                        "[TrustNetwork] get_active_validators incompleto "
                        "(%d/%d ativos, %d no trust) — usando top (%d)",
                        len(out), n_target, len(cands), len(top),
                    )
                    return top

            log.info("[TrustNetwork] get_active_validators: %d/%d (total=%d, origin=%s)",
                     len(out), len(active), len(cands),
                     "sim" if self._origin_pubkey else "não")
            return out

    def get_validator_list(self) -> List[Dict]:
        with self._lock:
            ids = [nid for nid in self.active_validators if nid in self.nodes]
            return [
                {
                    "node_id": nid,
                    "address": self.nodes[nid].address,
                    "public_key": self.nodes[nid].public_key,
                    "trust_score": round(self.nodes[nid].trust_score, 1),
                    "trust_level": self.nodes[nid].trust_level,
                    "uptime_score": round(self.nodes[nid].uptime_score, 1),
                    "blocks_validated": self.nodes[nid].blocks_validated,
                }
                for nid in sorted(ids,
                                  key=lambda x: -self.nodes[x].trust_score)
            ]

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
                if (not n.banned or min_level == "banned") and \
                   levels.index(n.trust_level) >= min_idx:
                    out.append({
                        "node_id": n.node_id,
                        "address": n.address,
                        "public_key": n.public_key,
                        "peer_addr": n.peer_addr,
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
            n_with_pk = sum(1 for n in validos if n.public_key)
            return {
                "total_nodes": len(self.nodes),
                "connected_nodes": len(ativos),
                "banned_nodes": sum(1 for n in self.nodes.values() if n.banned),
                "validators_count": len(self.active_validators),
                "nodes_with_pubkey": n_with_pk,
                "avg_trust": round(avg, 1),
                "top_validators": self.get_validator_list()[:5],
            }
