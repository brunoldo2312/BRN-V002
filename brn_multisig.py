"""brn_multisig.py — Coletor de assinaturas (Multi-Sig) do BRN (v2.3)
================================================================
v2.3 (2026-11-10) — PATCH DE ESTABILIDADE E CONCORRÊNCIA:
  [NEW] build_collector agora usa ThreadPoolExecutor para pedir as
        assinaturas em paralelo (muito mais rápido que sequencial).
  [FIX] Ignora IPs de peers que não estão no Trust Network ativo,
        evitando gastar tempo pedindo assinatura para nós mortos.
  [FIX] Garante que a assinatura local esteja sempre na lista final,
        mesmo se todos os peers derem timeout.
  [KEEP] ensure_multisig: injeta o coletor no blockchain.
  [KEEP] status(chain): mostra se o coletor está instalado.
================================================================
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Optional

from blockchain import sign_block_as_validator, verify_block_signature

log = logging.getLogger("multisig")

__version__ = "2.3"


def build_collector(
    chain,
    wallet,
    p2p,
    timeout: float = 15.0,
) -> Callable[[dict], List[dict]]:
    """
    Retorna uma função `collector(block) -> list[dict]` que:
      1. Assina o bloco localmente.
      2. Pede assinatura para os peers em paralelo (ThreadPool).
      3. Verifica as assinaturas.
      4. Retorna a lista de assinaturas válidas.
    """
    def collector(block: dict) -> List[dict]:
        sigs = []
        seen_pks = set()

        # 1. Assinatura local
        local_sig = sign_block_as_validator(block, wallet)
        if local_sig:
            sigs.append(local_sig)
            seen_pks.add(local_sig["pubkey"].lower())

        # 2. Descobre os peers para pedir assinatura
        peers_to_ask = []
        if p2p is not None and hasattr(p2p, "discovery"):
            try:
                # Pega peers com altura e IP
                all_peers = p2p.discovery.listar_peers_com_altura()
                
                # Pega a lista de validadores ativos do trust
                active_validators = []
                trust = getattr(chain, "trust_net", None)
                if trust and hasattr(trust, "get_active_validators"):
                    active_validators = [p.lower() for p in trust.get_active_validators(5)]
                elif hasattr(chain, "_get_active_validators"):
                    active_validators = [p.lower() for p in chain._get_active_validators()]

                for p in all_peers:
                    addr = p.get("address")
                    pubkey = (p.get("public_key") or "").lower()
                    
                    # Só pede para quem está no trust (ou se o trust estiver vazio, pede para todos)
                    if active_validators and pubkey not in active_validators:
                        continue
                    if pubkey == wallet.pub_hex.lower():
                        continue # Não pede pra si mesmo
                        
                    if addr and ":" in addr:
                        peers_to_ask.append(addr)
            except Exception as e:
                log.warning("[multisig] Falha ao listar peers para coleta: %s", e)

        # 3. Pede assinaturas em paralelo
        if peers_to_ask:
            from p2p_unified import P2PClient

            def _ask_peer(peer_addr):
                try:
                    ip, port_s = peer_addr.rsplit(":", 1)
                    msg = {
                        "type": "sign_block",
                        "block": block,
                    }
                    resp, _ = P2PClient.send_message(
                        ip, int(port_s), msg, timeout=timeout
                    )
                    return resp
                except Exception:
                    return None

            # Limita a 5 threads para não estourar recursos
            max_workers = min(len(peers_to_ask), 5)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_ask_peer, addr): addr for addr in peers_to_ask}
                for future in as_completed(futures, timeout=timeout + 2.0):
                    addr = futures[future]
                    try:
                        resp = future.result()
                        if not resp:
                            continue
                        
                        pk = (resp.get("pubkey") or "").strip().lower()
                        sg = (resp.get("signature") or "").strip()
                        
                        if not pk or not sg:
                            continue
                        if pk in seen_pks:
                            continue
                            
                        # Verifica criptograficamente se a assinatura é válida
                        if verify_block_signature(pk, block, sg):
                            sigs.append({"pubkey": pk, "signature": sg})
                            seen_pks.add(pk)
                        else:
                            log.warning("[multisig] Assinatura inválida de %s", addr)
                    except Exception as e:
                        log.debug("[multisig] Erro na thread do peer %s: %s", addr, e)

        return sigs

    return collector


def ensure_multisig(chain, wallet, p2p, trust=None, logger=None) -> bool:
    """
    Instala o coletor no blockchain e configura o autosign.
    """
    L = logger or log

    if not wallet:
        L.error("[multisig] Wallet inválida, não posso instalar multisig")
        return False

    if getattr(chain, "_sign_collector", None) is not None:
        L.info("[multisig] Coletor já está instalado.")
        return True

    try:
        # Constrói o coletor
        collector = build_collector(chain, wallet, p2p, timeout=15.0)
        
        # Injeta no blockchain
        if hasattr(chain, "set_sign_collector"):
            chain.set_sign_collector(collector)
        else:
            chain._sign_collector = collector
            chain._collect_signatures = collector
            
        L.info("[multisig] Coletor instalado com sucesso.")
        return True
    except Exception as e:
        L.error("[multisig] Falha ao instalar coletor: %s", e)
        return False


def status(chain) -> dict:
    """Retorna o status do multisig no nó."""
    collector_installed = getattr(chain, "_sign_collector", None) is not None
    accept_block_autosign = getattr(chain, "_auto_sign_accept_block", False)
    
    trust = getattr(chain, "trust_net", None)
    trust_installed = trust is not None
    
    sk = getattr(chain, "_local_validator_sk", None)
    sk_installed = sk is not None
    
    is_origin = bool(getattr(chain, "is_origin", False))
    
    origin_pubkey = ""
    if trust and hasattr(trust, "_origin_pubkey"):
        origin_pubkey = trust._origin_pubkey
        
    top_validators = []
    if trust and hasattr(trust, "get_top_validators"):
        try:
            top_validators = trust.get_top_validators(count=5)
        except Exception:
            pass

    return {
        "version": __version__,
        "sign_collector_installed": collector_installed,
        "accept_block_autosign": accept_block_autosign,
        "trust_net_installed": trust_installed,
        "local_sk_installed": sk_installed,
        "is_origin": is_origin,
        "origin_pubkey": origin_pubkey,
        "top_validators": len(top_validators),
    }


# ══════════════════════════════════════════════════════════════
# AUTO-TESTE
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("=" * 60)
    print(f" brn_multisig.py v{__version__} — auto-teste")
    print("=" * 60)

    class FakeChain:
        def __init__(self):
            self._sign_collector = None
            self._local_validator_sk = None
            self.trust_net = None
            self.is_origin = False

    class FakeWallet:
        pub_hex = "02" + "a" * 64

    class FakeP2P:
        pass

    chain = FakeChain()
    w = FakeWallet()
    p2p = FakeP2P()

    print("\n[1] Testando build_collector...")
    col = build_collector(chain, w, p2p, timeout=2.0)
    assert callable(col), "build_collector deve retornar uma função"
    print("    [ok] Coletor criado.")

    print("\n[2] Testando ensure_multisig...")
    ok = ensure_multisig(chain, w, p2p, trust=None, logger=logging.getLogger())
    assert ok is True, "ensure_multisig deveria retornar True"
    assert chain._sign_collector is not None, "O coletor não foi injetado"
    print("    [ok] Coletor injetado no chain.")

    print("\n[3] Testando status...")
    st = status(chain)
    print(f"    {st}")
    assert st["sign_collector_installed"] is True, "Status incorreto"
    print("    [ok] Status retornado corretamente.")

    print("\n" + "=" * 60)
    print(" TODOS OS TESTES PASSARAM")
    print("=" * 60)