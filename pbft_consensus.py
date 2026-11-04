"""pbft_consensus.py — PBFT simplificado para o BRN (v1.0)
================================================================
Implementa consenso de 2 fases (prepare + commit) com quórum 2f+1,
usando as assinaturas Ed25519 já existentes em wallet.Wallet.

Modelo matemático:
    n = |validadores ativos|
    f = (n - 1) // 3          # máximo de nós bizantinos tolerados
    quorum = 2f + 1           # mínimo de votos por fase

    n=4  → f=1 → quorum=3     (tolera 1 nó malicioso)
    n=7  → f=2 → quorum=5     (tolera 2 nós maliciosos)
    n=10 → f=3 → quorum=7     (tolera 3 nós maliciosos)

Fluxo:
    Líder (minerador)     Seguidores
    ─────────────────     ──────────
    propose(block)        ──preprepare──▶  handle_preprepare()
      ↓ assina prepare                       ↓ assina prepare
      ↓                      ◀─prepare──────┤
    handle_prepare()  ────────────────────  (broadcast)
      ↓ quórum prepares
      ↓ assina commit       ──commit──────▶ handle_commit()
    handle_commit()                         ↓
      ↓ quórum commits                      ↓
    decided=True                            decided=True (via msg)

Segurança (garantias):
  - Safety: dois nós honestos nunca decidem blocos diferentes para o
    mesmo (view, seq). Provado por quórum 2f+1 sobre 2 fases.
  - Liveness: enquanto ≤ f nós falharem, decide eventualmente
    (assumindo que os nós honestos enviam prepares/commits).

Prova anexada ao bloco:
  block["pbft_view"]     : int
  block["pbft_seq"]      : int
  block["pbft_commits"]  : [{"pubkey": hex, "signature": hex}, ...]

Domain separation impede replay entre fases/views/seqs.

Env:
  BRN_PBFT_ENABLED       "0"|"1" (default 0 — desligado por padrão)
  BRN_PBFT_ENFORCE       "0"|"1" (default 0 — se 1, exige pbft_commits)
  BRN_PBFT_TIMEOUT       segundos (default 15)
  BRN_PBFT_MAX_STATES    tamanho máximo de estados em memória (default 100)
================================================================
"""
from __future__ import annotations

import os
import time
import logging
import threading
from typing import Optional, Callable

log = logging.getLogger("pbft")

__version__ = "1.0"

# ─── Config ──────────────────────────────────────────────────────────
PBFT_ENABLED   = os.environ.get("BRN_PBFT_ENABLED", "0") == "1"
PBFT_ENFORCE   = os.environ.get("BRN_PBFT_ENFORCE", "0") == "1"
PBFT_TIMEOUT_S = float(os.environ.get("BRN_PBFT_TIMEOUT", "15"))
PBFT_MAX_STATES = int(os.environ.get("BRN_PBFT_MAX_STATES", "100"))

# ─── Domínio (domain separation) ─────────────────────────────────────
_DOMAIN = b"BRN-PBFT-v1|"
_PHASE_PP  = b"PRE-PREPARE"
_PHASE_PRE = b"PREPARE"
_PHASE_COM = b"COMMIT"


def _msg_bytes(phase: bytes, view: int, seq: int, block_hash: str) -> bytes:
    return (b"".join([
        _DOMAIN, phase, b"|",
        str(int(view)).encode(), b"|",
        str(int(seq)).encode(), b"|",
        block_hash.encode("ascii"),
    ]))


def _faults(n: int) -> int:
    """Máximo de nós bizantinos tolerados com n validadores."""
    return (n - 1) // 3 if n > 0 else 0


def _quorum(n: int) -> int:
    """Mínimo de votos para decidir (2f+1)."""
    if n == 0:
        return 0
    return 2 * _faults(n) + 1


# ─── Estado por rodada ───────────────────────────────────────────────
class PBFTState:
    __slots__ = (
        "view", "seq", "block_hash", "leader_pk",
        "preprepare_sig", "prepares", "commits",
        "decided", "ready_emitted", "started_at",
    )

    def __init__(self, view: int, seq: int, block_hash: str, leader_pk: str):
        self.view = view
        self.seq = seq
        self.block_hash = block_hash
        self.leader_pk = leader_pk
        self.preprepare_sig: Optional[str] = None
        self.prepares: dict[str, str] = {}   # pubkey -> hex sig
        self.commits: dict[str, str] = {}
        self.decided = False
        self.ready_emitted = False
        self.started_at = time.time()


# ─── Consenso ────────────────────────────────────────────────────────
class PBFTConsensus:
    """
    Motor PBFT injetado na Blockchain via `chain.set_pbft()`.
    """

    def __init__(self, chain, wallet_sk,
                 send_fn: Callable[[str, dict], Optional[dict]]):
        self.chain = chain
        self.sk = wallet_sk
        self.pub = wallet_sk.pub_hex
        self.send = send_fn              # (peer_addr, msg) -> resp|None
        self.states: dict[tuple[int, int], PBFTState] = {}
        self.lock = threading.RLock()
        self.view = 0
        self.seq = 0
        self.on_decided: Optional[Callable[[PBFTState, dict], None]] = None

    # ────────────────────────────────────────────────────────────
    # Setup / callbacks
    # ────────────────────────────────────────────────────────────
    def set_on_decided(self, cb: Callable[[PBFTState, dict], None]) -> None:
        self.on_decided = cb

    # ────────────────────────────────────────────────────────────
    # Helpers
    # ────────────────────────────────────────────────────────────
    def _validators(self) -> list[str]:
        try:
            return self.chain._get_active_validators() or []
        except Exception:
            return []

    def _peers(self) -> list[str]:
        p2p = getattr(self.chain, "p2p", None)
        if p2p is None:
            return []
        try:
            return p2p.discovery.listar_peers() or []
        except Exception:
            return []

    def _broadcast(self, msg: dict) -> int:
        sent = 0
        for peer in self._peers():
            try:
                if self.send(peer, msg):
                    sent += 1
            except Exception as e:
                log.debug("pbft broadcast %s: %s", peer, e)
        return sent

    def _verify_sig(self, data: bytes, sig_hex: str, pub_hex: str) -> bool:
        try:
            from wallet import Wallet
            return Wallet.verify(data, sig_hex, pub_hex)
        except Exception:
            return False

    def _sign(self, data: bytes) -> str:
        return self.sk.sign(data)

    def _emit_commit(self, st: PBFTState) -> None:
        """Uma vez que quórum de prepares é visto, assina e transmite commit."""
        if st.ready_emitted:
            return
        st.ready_emitted = True
        sig = self._sign(_msg_bytes(_PHASE_COM, st.view, st.seq, st.block_hash))
        st.commits[self.pub] = sig
        msg = {
            "type": "pbft_commit",
            "view": st.view, "seq": st.seq,
            "block_hash": st.block_hash,
            "pubkey": self.pub, "signature": sig,
        }
        self._broadcast(msg)
        log.info("[PBFT] commit emitido view=%d seq=%d (%d/%d prepares)",
                 st.view, st.seq, len(st.prepares), len(self._validators()))

    def _maybe_decide(self, st: PBFTState, block: Optional[dict] = None) -> bool:
        """Verifica quórum de commits. Se atingido, marca decidido."""
        if st.decided:
            return True
        n = len(self._validators())
        q = _quorum(n)
        if q == 0:
            # Sem validadores — modo bootstrap (aceita)
            st.decided = True
            log.warning("[PBFT] sem validadores — decidindo em bootstrap")
            return True
        if len(st.commits) >= q:
            st.decided = True
            log.warning("[PBFT] DECIDIDO view=%d seq=%d hash=%s… (%d/%d commits)",
                        st.view, st.seq, st.block_hash[:16],
                        len(st.commits), q)
            if self.on_decided and block is not None:
                try:
                    self.on_decided(st, block)
                except Exception as e:
                    log.warning("[PBFT] on_decided: %s", e)
            return True
        return False

    def _gc(self) -> None:
        """Limita memória de estados."""
        with self.lock:
            if len(self.states) > PBFT_MAX_STATES:
                ordered = sorted(self.states.keys())
                for k in ordered[:len(self.states) - PBFT_MAX_STATES]:
                    del self.states[k]

    # ────────────────────────────────────────────────────────────
    # Proposta (líder / minerador)
    # ────────────────────────────────────────────────────────────
    def propose(self, block: dict, timeout: float = None) -> bool:
        """
        Líder propõe bloco. Bloqueia até decisão ou timeout.
        Retorna True se decidido (bloco pode ser aceito), False caso contrário.
        """
        if not PBFT_ENABLED:
            return True   # desligado → comportamento antigo

        validators = self._validators()
        n = len(validators)
        if n < 1:
            log.warning("[PBFT] sem validadores — aceitando bloco em bootstrap")
            return True

        block_hash = block.get("hash", "")
        if not block_hash:
            return False

        with self.lock:
            self.seq += 1
            st = PBFTState(self.view, self.seq, block_hash, self.pub)
            self.states[(self.view, self.seq)] = st

        # 1) assina e transmite pre-prepare
        pp_sig = self._sign(_msg_bytes(_PHASE_PP, st.view, st.seq, block_hash))
        st.preprepare_sig = pp_sig
        self._broadcast({
            "type": "pbft_preprepare",
            "view": st.view, "seq": st.seq,
            "block": block, "leader": self.pub,
            "preprepare_sig": pp_sig,
        })

        # 2) assina e transmite prepare (auto-voto)
        pre_sig = self._sign(_msg_bytes(_PHASE_PRE, st.view, st.seq, block_hash))
        st.prepares[self.pub] = pre_sig
        self._broadcast({
            "type": "pbft_prepare",
            "view": st.view, "seq": st.seq,
            "block_hash": block_hash,
            "pubkey": self.pub, "signature": pre_sig,
        })

        # 3) espera quórum de prepares → commit → decisão
        tmo = timeout if timeout is not None else PBFT_TIMEOUT_S
        deadline = time.time() + tmo
        q = _quorum(n)

        while time.time() < deadline:
            with self.lock:
                if len(st.prepares) >= q and not st.ready_emitted:
                    self._emit_commit(st)
                if self._maybe_decide(st, block):
                    self._gc()
                    return True
            time.sleep(0.05)

        log.warning("[PBFT] TIMEOUT propose view=%d seq=%d — prepares=%d/%d commits=%d/%d",
                    st.view, st.seq, len(st.prepares), q, len(st.commits), q)
        self._gc()
        return st.decided

    # ────────────────────────────────────────────────────────────
    # Handlers de mensagens (chamados pelo P2PServer)
    # ────────────────────────────────────────────────────────────
    def handle_preprepare(self, msg: dict) -> dict:
        """Recebe pre-prepare do líder; verifica, valida bloco, assina prepare."""
        view = int(msg.get("view", 0))
        seq = int(msg.get("seq", 0))
        leader = msg.get("leader", "")
        block = msg.get("block") or {}
        pp_sig = msg.get("preprepare_sig", "")
        block_hash = block.get("hash", "")

        if not block_hash or not leader or not pp_sig:
            return {"ok": False, "error": "preprepare incompleto"}

        if leader not in self._validators():
            return {"ok": False, "error": "leader nao e validador"}

        if not self._verify_sig(_msg_bytes(_PHASE_PP, view, seq, block_hash),
                                pp_sig, leader):
            return {"ok": False, "error": "preprepare sig invalida"}

        # Valida o bloco (PoW/merkle/auth_tag/txs) — SEM validators_sig
        try:
            ok, why = self.chain.validate_block(block, validators=[])
            if not ok:
                return {"ok": False, "error": f"bloco invalido: {why}"}
        except Exception as e:
            return {"ok": False, "error": f"validate_block erro: {e}"}

        with self.lock:
            key = (view, seq)
            if key not in self.states:
                st = PBFTState(view, seq, block_hash, leader)
                st.preprepare_sig = pp_sig
                self.states[key] = st
            else:
                st = self.states[key]
                if st.block_hash != block_hash:
                    return {"ok": False, "error": "estado conflitante"}

            # assina prepare
            sig = self._sign(_msg_bytes(_PHASE_PRE, view, seq, block_hash))
            st.prepares[self.pub] = sig

        # Transmite prepare para os demais
        self._broadcast({
            "type": "pbft_prepare",
            "view": view, "seq": seq,
            "block_hash": block_hash,
            "pubkey": self.pub, "signature": sig,
        })

        return {"ok": True, "pubkey": self.pub, "signature": sig}

    def handle_prepare(self, msg: dict) -> dict:
        view = int(msg.get("view", 0))
        seq = int(msg.get("seq", 0))
        bh = msg.get("block_hash", "")
        pk = msg.get("pubkey", "")
        sig = msg.get("signature", "")

        if pk not in self._validators():
            return {"ok": False, "error": "prepare de nao-validador"}

        if not self._verify_sig(_msg_bytes(_PHASE_PRE, view, seq, bh), sig, pk):
            return {"ok": False, "error": "prepare sig invalida"}

        with self.lock:
            st = self.states.get((view, seq))
            if st is None:
                return {"ok": False, "error": "sem estado"}
            if st.block_hash != bh:
                return {"ok": False, "error": "hash divergente"}
            st.prepares[pk] = sig

            q = _quorum(len(self._validators()))
            if q and len(st.prepares) >= q and not st.ready_emitted:
                self._emit_commit(st)

        return {"ok": True}

    def handle_commit(self, msg: dict) -> dict:
        view = int(msg.get("view", 0))
        seq = int(msg.get("seq", 0))
        bh = msg.get("block_hash", "")
        pk = msg.get("pubkey", "")
        sig = msg.get("signature", "")

        if pk not in self._validators():
            return {"ok": False, "error": "commit de nao-validador"}

        if not self._verify_sig(_msg_bytes(_PHASE_COM, view, seq, bh), sig, pk):
            return {"ok": False, "error": "commit sig invalida"}

        with self.lock:
            st = self.states.get((view, seq))
            if st is None:
                return {"ok": False, "error": "sem estado"}
            if st.block_hash != bh:
                return {"ok": False, "error": "hash divergente"}
            st.commits[pk] = sig

            # Tenta decidir (mas sem o block em mãos aqui — só marca)
            n = len(self._validators())
            q = _quorum(n)
            decided_now = (q == 0) or (len(st.commits) >= q)
            if decided_now and not st.decided:
                st.decided = True
                log.warning("[PBFT] DECIDIDO (via commit) view=%d seq=%d hash=%s…",
                            view, seq, bh[:16])

        return {"ok": True, "decided": st.decided}

    # ────────────────────────────────────────────────────────────
    # Verificação de prova anexada ao bloco (usada por accept_block)
    # ────────────────────────────────────────────────────────────
    def validate_proof(self, block: dict) -> tuple[bool, str]:
        """
        Verifica que block["pbft_commits"] atinge quórum.
        Retorna (True, "") se OK, senão (False, motivo).
        """
        if not PBFT_ENFORCE:
            return True, ""   # enforce desligado → não bloqueia

        commits = block.get("pbft_commits")
        if not isinstance(commits, list) or not commits:
            return False, "sem pbft_commits"

        view = int(block.get("pbft_view", 0))
        seq  = int(block.get("pbft_seq", 0))
        bh   = block.get("hash", "")
        if not bh:
            return False, "sem hash"

        validators = set(self._validators())
        if not validators:
            return True, ""   # bootstrap

        data = _msg_bytes(_PHASE_COM, view, seq, bh)
        verified = set()
        for entry in commits:
            if not isinstance(entry, dict):
                continue
            pk = (entry.get("pubkey") or "").strip().lower()
            sg = (entry.get("signature") or "").strip()
            if not pk or not sg or pk in verified:
                continue
            if pk not in validators:
                continue
            if not self._verify_sig(data, sg, pk):
                continue
            verified.add(pk)

        q = _quorum(len(validators))
        if len(verified) >= q:
            return True, ""
        return False, f"pbft_commits {len(verified)}/{q}"

    # ────────────────────────────────────────────────────────────
    # Anexa commits ao bloco antes do accept_block
    # ────────────────────────────────────────────────────────────
    def attach_proof(self, block: dict) -> bool:
        """Após propose() decidir, anexa view/seq/commits ao bloco."""
        key = (self.view, self.seq)
        with self.lock:
            st = self.states.get(key)
            if st is None or not st.decided or st.block_hash != block.get("hash"):
                return False
            block["pbft_view"] = st.view
            block["pbft_seq"] = st.seq
            block["pbft_commits"] = [
                {"pubkey": pk, "signature": sg}
                for pk, sg in st.commits.items()
            ]
        return True

    # ────────────────────────────────────────────────────────────
    # Status
    # ────────────────────────────────────────────────────────────
    def status(self) -> dict:
        with self.lock:
            n = len(self._validators())
            return {
                "enabled": PBFT_ENABLED,
                "enforce": PBFT_ENFORCE,
                "view": self.view,
                "seq": self.seq,
                "states_in_memory": len(self.states),
                "decided": sum(1 for s in self.states.values() if s.decided),
                "validators": n,
                "faults_tolerated_f": _faults(n),
                "quorum_2f_plus_1": _quorum(n),
            }
