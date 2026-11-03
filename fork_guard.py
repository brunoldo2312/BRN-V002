"""
fork_guard.py — Monitor de forks e resolução automática (v2.0)
================================================================
v2.0 (2026-11-03):
  [NEW] Integração total com o sync_manager v9.1.
  [NEW] Detecta forks mesmo quando os peers estão na mesma altura,
        comparando o tip_hash via P2P.
  [NEW] Se o peer tem a mesma altura mas hash diferente, chama
        sync._resolve_fork() automaticamente.
  [NEW] Previne execuções simultâneas com lock e cooldown.
  [FIX] Fecha corretamente no shutdown (stop_fork_guard).
================================================================
"""
from __future__ import annotations

import logging
import os
import threading
import time

from p2p_unified import P2PClient

log = logging.getLogger("fork_guard")

FORK_CHECK_INTERVAL_S = float(os.environ.get("BRN_FORK_CHECK_S", "3.0"))
COOLDOWN_AFTER_FORK_S = float(os.environ.get("BRN_FORK_COOLDOWN", "60.0"))


class ForkGuard(threading.Thread):
    def __init__(self, chain, sync_manager, p2p_manager):
        super().__init__(daemon=True, name="ForkGuard")
        self.chain = chain
        self.sync = sync_manager
        self.p2p = p2p_manager

        self.running = False
        self._stop_evt = threading.Event()
        self._lock = threading.RLock()
        self._last_fork_resolution = 0.0

    def run(self):
        self.running = True
        log.info(f"[ForkGuard] Iniciado (check a cada {FORK_CHECK_INTERVAL_S}s)")

        while self.running and not self._stop_evt.wait(FORK_CHECK_INTERVAL_S):
            try:
                self._check_chain()
            except Exception as e:
                log.warning(f"[ForkGuard] Erro no ciclo: {e}")

        self.running = False
        log.info("[ForkGuard] Parado")

    def stop(self):
        self._stop_evt.set()
        if self.is_alive():
            self.join(timeout=5)

    def _check_chain(self):
        if self.sync is None:
            return
            
        # Se o sync_manager já está sincronizando ou resolvendo fork, não faz nada
        if self.sync.status.syncing or self.sync.status.forking:
            return

        # Se um fork foi resolvido recentemente, espera o cooldown
        agora = time.time()
        if agora - self._last_fork_resolution < COOLDOWN_AFTER_FORK_S:
            return

        local_h = self.chain.db.height()
        if local_h < 1:
            return

        # Pega o peer com mais trabalho que o local
        best_peer = self.sync._best_peer_by_work()
        if best_peer is None:
            return

        # Caso 1: O peer está à frente (nós estamos atrasados)
        if best_peer.height > local_h + 1:
            log.info(f"[ForkGuard] Peer {best_peer.node_id[:12]} está à frente "
                     f"(h={best_peer.height} vs local={local_h}). Iniciando sync.")
            self.sync.sync_with_peer(best_peer)
            return

        # Caso 2: O peer está na mesma altura, mas em um fork diferente
        if best_peer.height == local_h:
            try:
                resp, _ = P2PClient.get_chain_height(best_peer.ip, best_peer.port, timeout=3)
                if not resp:
                    return
                
                peer_tip = resp.get("hash", "")
                local_tip = self.chain.db.tip_hash()

                if peer_tip and local_tip and peer_tip != local_tip:
                    log.warning(f"[ForkGuard] FORK na mesma altura #{local_h}! "
                                f"local={local_tip[:16]}... peer={peer_tip[:16]}...")
                    
                    with self._lock:
                        # Resolve o fork
                        ok = self.sync._resolve_fork(best_peer)
                        if ok:
                            self._last_fork_resolution = time.time()
                            log.info("[ForkGuard] Fork resolvido com sucesso.")
                        else:
                            log.error("[ForkGuard] Falha ao resolver fork.")
            except Exception as e:
                log.debug(f"[ForkGuard] Erro ao comparar tip_hash: {e}")

    def on_block_accepted(self, block):
        """Chamado quando um bloco é aceito localmente (opcional para lógica extra)."""
        # Se for um bloco novo, podemos resetar o cooldown de fork
        self._last_fork_resolution = 0.0


# ============================================================
# SINGLETON
# ============================================================
_fork_guard_instance = None
_fork_guard_lock = threading.Lock()


def get_fork_guard(chain=None, sync=None, p2p=None) -> ForkGuard:
    """Retorna a instância global do ForkGuard (cria se não existir)."""
    global _fork_guard_instance
    with _fork_guard_lock:
        if _fork_guard_instance is None or not _fork_guard_instance.is_alive():
            if chain is None:
                raise ValueError("ForkGuard não iniciado e sem parâmetros para criar.")
            _fork_guard_instance = ForkGuard(chain, sync, p2p)
        return _fork_guard_instance


def stop_fork_guard() -> bool:
    """Para o ForkGuard se estiver rodando."""
    global _fork_guard_instance
    with _fork_guard_lock:
        if _fork_guard_instance is not None:
            _fork_guard_instance.stop()
            _fork_guard_instance = None
            return True
        return False
