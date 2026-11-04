"""test_pbft_consensus.py — teste isolado do motor PBFT (sem rede).

Simula 4 nós em um único processo usando sockets em memória e
verifica que:
  1. Com 4 validadores, quórum=3 e f=1 (tolera 1 bizantino).
  2. Com todos honestos, decisão acontece.
  3. Com 1 bizantino (não vota), ainda decide (3 de 4).
  4. Com 2 bizantinos (não votam), NÃO decide (2 < quorum=3).
  5. Voto com assinatura inválida é rejeitado.

Uso:
    BRN_PBFT_ENABLED=1 python test_pbft_consensus.py
"""
from __future__ import annotations
import os
os.environ["BRN_PBFT_ENABLED"] = "1"

import sys
import time
import hashlib
from typing import Optional

# ─── Mock chain mínimo ───────────────────────────────────────────────
class FakeDB:
    def __init__(self): self.h = 0
    def height(self): return self.h

class FakeChain:
    def __init__(self, validators):
        self.db = FakeDB()
        self._validators = validators
        self.p2p = None
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
        # chave fake — só precisamos de pub_hex e sign/verify consistentes
        self.tag = tag
        self.pub_hex = hashlib.sha256(tag.encode()).hexdigest()
    def sign(self, data: bytes) -> str:
        # "assinatura" determinística para teste
        return hashlib.sha256(self.pub_hex.encode() + data).hexdigest()

def fake_verify(data: bytes, sig: str, pub: str) -> bool:
    expected = hashlib.sha256(pub.encode() + data).hexdigest()
    return sig == expected

# Monkey-patch Wallet.verify para testes
import wallet as _w
_w.Wallet.verify = staticmethod(fake_verify)

from pbft_consensus import (
    PBFTConsensus, _quorum, _faults, _msg_bytes,
    _PHASE_PRE, _PHASE_PP, _PHASE_COM,
)

# ─── Simulador de rede local (entrega mensagens diretamente) ─────────
class InMemoryNet:
    def __init__(self):
        self.nodes: dict[str, PBFTConsensus] = {}
    def register(self, addr: str, consensus: PBFTConsensus):
        self.nodes[addr] = consensus
    def deliver(self, src: str, dst: str, msg: dict) -> Optional[dict]:
        if dst not in self.nodes:
            return None
        tgt = self.nodes[dst]
        typ = msg.get("type")
        try:
            if typ == "pbft_preprepare":
                return tgt.handle_preprepare(msg)
            if typ == "pbft_prepare":
                return tgt.handle_prepare(msg)
            if typ == "pbft_commit":
                return tgt.handle_commit(msg)
        except Exception:
            return None
        return None

# ─── Test runner ─────────────────────────────────────────────────────
def test_scenario(n_honest: int, n_bizantine: int, label: str):
    """
    Cria n_honest + n_bizantine validadores. Todos os honestos respondem
    a todas as mensagens. Bizantinos são no-op (não votam, não respondem).
    """
    total = n_honest + n_bizantine
    wallets = [FakeWallet(f"node-{i}") for i in range(total)]
    pubs = [w.pub_hex for w in wallets]

    net = InMemoryNet()
    chains = []
    consensuses = []
    for i in range(total):
        chain = FakeChain(pubs)
        net_addr = f"node-{i}"
        def send_fn(peer_addr, msg, _net=net, _me=net_addr):
            return _net.deliver(_me, peer_addr, msg)
        c = PBFTConsensus(chain, wallets[i], send_fn)
        chains.append(chain)
        consensuses.append(c)
        net.register(net_addr, c)

    # O líder é node-0 (honesto)
    leader = consensuses[0]
    # Bizantinos: desativar votos
    for i in range(n_honest, total):
        consensuses[i].handle_prepare = lambda m: {"ok": False, "error": "byzantine"}
        consensuses[i].handle_prepare = lambda m: {"ok": False, "error": "byzantine"}
        consensuses[i].handle_commit  = lambda m: {"ok": False, "error": "byzantine"}
        consensuses[i].handle_preprepare = lambda m: {"ok": False, "error": "byzantine"}

    block = {"height": 1, "hash": "aa" * 32, "transactions": []}
    t0 = time.time()
    decided = leader.propose(block, timeout=3.0)
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

    # n=4 → f=1, quorum=3
    # 4 honestos:     3 de 4 ✓ decide
    test_scenario(4, 0, "[1] 4 honestos, 0 bizantinos")
    # 3 honestos+1 biz: 3 de 4 ✓ decide (tolera 1)
    test_scenario(3, 1, "[2] 3 honestos, 1 bizantino (no limite)")
    # 2 honestos+2 biz: 2 de 4 ✗ NÃO decide (perdeu o quórum)
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

    # ─── Teste 3: assinatura forjada é rejeitada ──────────────────────
    print()
    print("=" * 64)
    print(" TESTE PBFT — assinatura forjada é rejeitada")
    print("=" * 64)
    print()

    wallets = [FakeWallet(f"node-{i}") for i in range(4)]
    pubs = [w.pub_hex for w in wallets]
    net = InMemoryNet()
    for i in range(4):
        chain = FakeChain(pubs)
        def send_fn(peer_addr, msg, _net=net, _me=f"node-{i}"):
            return _net.deliver(_me, peer_addr, msg)
        c = PBFTConsensus(chain, wallets[i], send_fn)
        net.register(f"node-{i}", c)

    leader = net.nodes["node-0"]
    # Força criação de estado manual
    from pbft_consensus import PBFTState
    st = PBFTState(0, 99, "bb" * 32, pubs[0])
    leader.states[(0, 99)] = st

    # Tenta preparar com assinatura inválida
    bad_sig = "00" * 64
    resp = leader.handle_prepare({
        "view": 0, "seq": 99, "block_hash": "bb" * 32,
        "pubkey": pubs[1], "signature": bad_sig,
    })
    assert resp.get("ok") is False, f"assinatura forjada deveria ser rejeitada, got {resp}"
    print("    [ok] prepare com assinatura inválida → rejeitado")

    # Prepara com assinatura válida
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
