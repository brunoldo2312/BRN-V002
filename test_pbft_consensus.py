"""test_pbft_consensus.py — teste isolado do motor PBFT (sem rede).
"""
from __future__ import annotations
import os
os.environ["BRN_PBFT_ENABLED"] = "1"

import sys
import time
import hashlib
import threading
from typing import Optional

# ─── Mock chain mínimo ───────────────────────────────────────────────
class FakeDB:
    def __init__(self): self.h = 0
    def height(self): return self.h

class FakeP2P:
    def __init__(self, peers: list[str]):
        self.discovery = self
        self._peers = peers
    def listar_peers(self):
        return self._peers

class FakeChain:
    def __init__(self, validators, peers):
        self.db = FakeDB()
        self._validators = validators
        self.p2p = FakeP2P(peers)
        self._local_validator_sk = None
    def _get_active_validators(self):
        return list(self._validators)
    def validate_block(self, block, validators=None):
        return True, "ok"
    def accept_block(self, block):
        self.db.h += 1
        return True, block["hash"]

class FakeWallet:
    def __init__(self, tag: str):
        self.tag = tag
        self.pub_hex = hashlib.sha256(tag.encode()).hexdigest()
    def sign(self, data: bytes) -> str:
        return hashlib.sha256(self.pub_hex.encode() + data).hexdigest()

def fake_verify(data: bytes, sig: str, pub: str) -> bool:
    expected = hashlib.sha256(pub.encode() + data).hexdigest()
    return sig == expected

import wallet as _w
_w.Wallet.verify = staticmethod(fake_verify)

from pbft_consensus import (
    PBFTConsensus, _quorum, _faults, _msg_bytes,
    _PHASE_PRE, _PHASE_PP, _PHASE_COM,
)

# ─── Simulador de rede local (assíncrono via fila) ───────────────────
class InMemoryNet:
    def __init__(self):
        self.nodes: dict[str, PBFTConsensus] = {}
        self.queue = []
        self.lock = threading.Lock()
        
    def register(self, addr: str, consensus: PBFTConsensus):
        self.nodes[addr] = consensus
        
    def deliver(self, src: str, dst: str, msg: dict) -> Optional[dict]:
        with self.lock:
            self.queue.append((src, dst, msg))
        return {"ok": True}
        
    def process_queue(self):
        while True:
            with self.lock:
                if not self.queue:
                    break
                src, dst, msg = self.queue.pop(0)
            if dst not in self.nodes:
                continue
            tgt = self.nodes[dst]
            typ = msg.get("type")
            try:
                if typ == "pbft_preprepare":
                    tgt.handle_preprepare(msg)
                elif typ == "pbft_prepare":
                    tgt.handle_prepare(msg)
                elif typ == "pbft_commit":
                    tgt.handle_commit(msg)
            except Exception:
                pass

# ─── Test runner ─────────────────────────────────────────────────────
def test_scenario(n_honest: int, n_bizantine: int, label: str):
    total = n_honest + n_bizantine
    wallets = [FakeWallet(f"node-{i}") for i in range(total)]
    pubs = [w.pub_hex for w in wallets]

    net = InMemoryNet()
    chains = []
    consensuses = []
    
    all_addrs = [f"node-{i}" for i in range(total)]
    
    for i in range(total):
        my_peers = [a for a in all_addrs if a != f"node-{i}"]
        chain = FakeChain(pubs, my_peers)
        net_addr = f"node-{i}"
        def send_fn(peer_addr, msg, _net=net, _me=net_addr):
            return _net.deliver(_me, peer_addr, msg)
        c = PBFTConsensus(chain, wallets[i], send_fn)
        chains.append(chain)
        consensuses.append(c)
        net.register(net_addr, c)

    leader = consensuses[0]
    
    # Bizantinos: desativar votos
    for i in range(n_honest, total):
        consensuses[i].handle_prepare = lambda m: {"ok": False, "error": "byzantine"}
        consensuses[i].handle_commit  = lambda m: {"ok": False, "error": "byzantine"}
        consensuses[i].handle_preprepare = lambda m: {"ok": False, "error": "byzantine"}

    block = {"height": 1, "hash": "aa" * 32, "transactions": []}
    t0 = time.time()
    decided_result = [False]
    
    # Roda o propose em uma thread separada
    def run_propose():
        decided_result[0] = leader.propose(block, timeout=2.0)
        
    t = threading.Thread(target=run_propose)
    t.start()
    
    # Processa a fila enquanto o líder espera as respostas
    start_time = time.time()
    while t.is_alive() and time.time() - start_time < 2.5:
        net.process_queue()
        time.sleep(0.01)
        
    t.join()
    decided = decided_result[0]
    elapsed = time.time() - t0

    expected = len([i for i in range(total) if i < n_honest]) >= _quorum(total)
    n = total
    q = _quorum(n)
    f = _faults(n)

    print(f"  {label}")
    print(f"    n={n} f={f} quorum={q} | honestos={n_honest} bizantinos={n_bizantine}")
    print(f"    decidiu={decided} (esperado={expected}) em {elapsed*1000:.0f}ms")

    assert decided == expected, f"decisao inesperada: got {decided}, want {expected}"
    return decided

def main():
    print("=" * 64)
    print(" TESTE PBFT — 4 validadores, variando nº de bizantinos")
    print("=" * 64)
    print()

    test_scenario(4, 0, "[1] 4 honestos, 0 bizantinos")
    test_scenario(3, 1, "[2] 3 honestos, 1 bizantino (no limite)")
    test_scenario(2, 2, "[3] 2 honestos, 2 bizantinos (acima do limite)")

    print()
    print("=" * 64)
    print(" TESTE PBFT — 7 validadores (f=2, quorum=5)")
    print("=" * 64)
    print()
    test_scenario(7, 0, "[4] 7 honestos, 0 bizantinos")
    test_scenario(6, 1, "[5] 6 honestos, 1 bizantino")
    test_scenario(5, 2, "[6] 5 honestos, 2 bizantinos (no limite)")
    test_scenario(4, 3, "[7] 4 honestos, 3 bizantinos (acima do limite)")

    print()
    print("=" * 64)
    print(" TESTE PBFT — assinatura forjada é rejeitada")
    print("=" * 64)
    print()

    wallets = [FakeWallet(f"node-{i}") for i in range(4)]
    pubs = [w.pub_hex for w in wallets]
    net = InMemoryNet()
    
    all_addrs = [f"node-{i}" for i in range(4)]
    for i in range(4):
        my_peers = [a for a in all_addrs if a != f"node-{i}"]
        chain = FakeChain(pubs, my_peers)
        def send_fn(peer_addr, msg, _net=net, _me=f"node-{i}"):
            return _net.deliver(_me, peer_addr, msg)
        c = PBFTConsensus(chain, wallets[i], send_fn)
        net.register(f"node-{i}", c)

    leader = net.nodes["node-0"]
    from pbft_consensus import PBFTState
    st = PBFTState(0, 99, "bb" * 32, pubs[0])
    leader.states[(0, 99)] = st

    bad_sig = "00" * 64
    resp = leader.handle_prepare({
        "view": 0, "seq": 99, "block_hash": "bb" * 32,
        "pubkey": pubs[1], "signature": bad_sig,
    })
    assert resp.get("ok") is False, f"assinatura forjada deveria ser rejeitada, got {resp}"
    print("    [ok] prepare com assinatura inválida → rejeitado")

    valid_sig = wallets[1].sign(_msg_bytes(_PHASE_PRE, 0, 99, "bb" * 32))
    resp = leader.handle_prepare({
        "view": 0, "seq": 99, "block_hash": "bb" * 32,
        "pubkey": pubs[1], "signature": valid_sig,
    })
    assert resp.get("ok") is True, f"prepare válido rejeitado: {resp}"
    print("    [ok] prepare com assinatura válida → aceito")

    print()
    print("=" * 64)
    print(" TODOS OS TESTES PASSARAM")
    print("=" * 64)
    return 0

if __name__ == "__main__":
    sys.exit(main())
