"""brn_multisig.py — Multi-sig drop-in para o BRN (v2)

Instala, numa única chamada, todos os componentes de co-assinatura:

  1. chain.set_sign_collector(...)          — para mine_block() (minerador local)
  2. chain.accept_block = auto-sign wrapper — para o pool (accept_block direto)
  3. trust.set_origin_pubkey(...)           — corrige "Origin ausente"
  4. chain.set_trust_net(trust)             — se ainda não estiver injetado

Uso no main.py (2 linhas, após p2p.start() e set_local_validator_sk):

    from brn_multisig import ensure_multisig
    ensure_multisig(chain, sk, p2p, trust=trust, logger=log)

Idempotente: rodar 2× não duplica wrapper nem collector.
"""
from __future__ import annotations

import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("multisig")

__version__ = "2.0"


# ═══════════════════════════════════════════════════════════════
# COLETOR DE ASSINATURAS (usado por mine_block e accept_block)
# ═══════════════════════════════════════════════════════════════
def build_collector(chain, sk, p2p, timeout: float = 5.0,
                    max_workers: int = 8, logger=None):
    """Retorna uma função collector(block) → list[{pubkey, signature}].

    A função:
      - assina localmente (1 sig imediata)
      - pede assinatura aos peers EM PARALELO (~1s para 6 peers)
      - deduplica por pubkey
    """
    L = logger or log

    def _collect(block):
        sigs = []

        # 1) assinatura local
        try:
            from blockchain import sign_block_as_validator
            local = sign_block_as_validator(block, sk)
            if local and local.get("pubkey") and local.get("signature"):
                sigs.append(local)
        except Exception as e:
            L.warning("[SIG] assinatura local falhou: %s", e)

        # 2) peers em paralelo
        try:
            peers = list(p2p.discovery.listar_peers())
        except Exception:
            peers = []

        if peers:
            from p2p_unified import P2PClient

            def _fetch_one(addr):
                try:
                    ip, port_s = addr.rsplit(":", 1)
                    return P2PClient.request_block_signature(
                        ip, int(port_s), block, timeout=timeout,
                    )
                except Exception:
                    return None

            workers = min(max_workers, len(peers))
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="sig-collect") as pool:
                futures = [pool.submit(_fetch_one, a) for a in peers]
                for fut in as_completed(futures):
                    try:
                        r = fut.result()
                        if r and r.get("pubkey") and r.get("signature"):
                            sigs.append(r)
                    except Exception:
                        pass

        # 3) dedup por pubkey (mantém a primeira ocorrência)
        seen = set()
        out = []
        for s in sigs:
            pk = (s or {}).get("pubkey", "").lower()
            if pk and pk not in seen:
                seen.add(pk)
                out.append(s)
        return out

    return _collect


# ═══════════════════════════════════════════════════════════════
# PATCH DO accept_block (para o POOL)
# ═══════════════════════════════════════════════════════════════
def install_accept_block_autosign(chain, logger=None):
    """Wrapper de chain.accept_block: se bloco do pool chega sem sigs,
    auto-assina com o _sign_collector e re-tenta.

    Só age no origin (BRN_IS_ORIGIN=1). Idempotente.
    Retorna True se instalou agora, False se já estava.
    """
    L = logger or log

    if getattr(chain, "_accept_block_autosign_installed", False):
        L.debug("[multisig] accept_block auto-sign já instalado")
        return False

    original = chain.accept_block

    def _accept_autosign(block, *args, **kwargs):
        ok, msg = original(block, *args, **kwargs)
        if ok:
            return ok, msg

        # Só origin
        if os.environ.get("BRN_IS_ORIGIN", "0") != "1":
            return ok, msg

        # Só falha por falta de sigs
        ml = str(msg).lower()
        sig_fail = (
            "sem assinatura" in ml
            or "validators_sig" in ml
            or "assinaturas válidas" in ml
            or "assinaturas validas" in ml
        )
        if not sig_fail:
            return ok, msg

        # Só próximo bloco
        try:
            local_h = chain.db.height()
        except Exception:
            return ok, msg
        if block.get("height") != local_h + 1:
            return ok, msg

        # Só se realmente está vazio
        if block.get("validators_sig"):
            return ok, msg

        collector = getattr(chain, "_sign_collector", None)
        if collector is None:
            return ok, msg

        try:
            L.info("[SIG] auto-assinando bloco #%s (motivo: %s)",
                   block.get("height"), str(msg)[:60])
            t0 = time.time()
            sigs = collector(block)
            dt_ms = (time.time() - t0) * 1000
            if sigs:
                block["validators_sig"] = sigs
                L.info("[SIG] %d sig(s) coletada(s) em %.0fms — re-tentando",
                       len(sigs), dt_ms)
                return original(block, *args, **kwargs)
            L.warning("[SIG] collector retornou 0 sig(s) para #%s em %.0fms",
                      block.get("height"), dt_ms)
        except Exception as e:
            L.exception("[SIG] auto-sign #%s falhou: %s",
                        block.get("height"), e)

        return ok, msg

    chain.accept_block = _accept_autosign
    chain._accept_block_autosign_installed = True
    chain._accept_block_original = original
    L.info("[multisig] accept_block auto-sign instalado (origin only)")
    return True


def uninstall_accept_block_autosign(chain):
    """Reverte o wrapper (não usado em produção, mas útil pra debug)."""
    if not getattr(chain, "_accept_block_autosign_installed", False):
        return False
    original = getattr(chain, "_accept_block_original", None)
    if original is None:
        return False
    chain.accept_block = original
    chain._accept_block_autosign_installed = False
    chain._accept_block_original = None
    return True


# ═══════════════════════════════════════════════════════════════
# INSTALL PRINCIPAL
# ═══════════════════════════════════════════════════════════════
def install_multisig(chain, sk, p2p, trust=None,
                     timeout: float = 5.0,
                     install_accept_patch: bool = True,
                     logger=None):
    """Instala o sign_collector + (opcional) accept_block autosign +
    origin_pubkey. Retorna o collector.

    Pré-requisitos:
      - chain: Blockchain já aberta
      - sk:    Wallet com a SK local já carregada
      - p2p:   P2PManager já startado (discovery ativo)
      - trust: TrustNetwork (opcional — se dado, set_origin_pubkey)
    """
    L = logger or log

    if chain is None or sk is None:
        L.error("[multisig] install: chain ou sk é None — abortado")
        return None

    if not hasattr(chain, "set_sign_collector"):
        L.error("[multisig] Blockchain não tem set_sign_collector()")
        L.error("[multisig] atualize blockchain.py — sem isso multi-sig nunca funciona")
        return None

    # 1) collector
    collector = build_collector(chain, sk, p2p, timeout=timeout, logger=L)
    chain.set_sign_collector(collector)

    n_peers = 0
    try:
        n_peers = len(p2p.discovery.listar_peers())
    except Exception:
        pass
    L.info("[multisig] sign_collector instalado (%d peer(s) alvo)", n_peers)

    # 2) origin_pubkey
    if trust is not None and hasattr(trust, "set_origin_pubkey"):
        try:
            trust.set_origin_pubkey(sk.pub_hex)
            L.info("[multisig] trust._origin_pubkey = %s...", sk.pub_hex[:16])
        except Exception as e:
            L.warning("[multisig] set_origin_pubkey: %s", e)

    # 3) patch do accept_block (para o pool)
    if install_accept_patch:
        try:
            install_accept_block_autosign(chain, logger=L)
        except Exception as e:
            L.warning("[multisig] accept_block auto-sign: %s", e)

    # 4) teste rápido com o tip local (não bloqueia se falhar)
    try:
        h = chain.db.height()
        if h >= 0:
            tip = chain.db.get_block(h)
            if tip:
                t0 = time.time()
                sigs = collector(tip)
                dt_ms = (time.time() - t0) * 1000
                L.info(
                    "[multisig] teste com bloco #%d: %d assinatura(s) em %.0fms",
                    h, len(sigs), dt_ms,
                )
                if len(sigs) < 2:
                    L.warning(
                        "[multisig] ATENÇÃO: só %d assinatura(s) no teste. "
                        "Se for 1, o tip local não está no top-5 do trust. "
                        "Rode: python3 populate_trust.py (ou fix_all.py)",
                        len(sigs),
                    )
    except Exception as e:
        L.debug("[multisig] teste pós-install falhou: %s", e)

    return collector


def ensure_multisig(chain, sk, p2p, trust=None, logger=None):
    """Wrapper: garante chain.trust_net + install_multisig completo."""
    L = logger or log

    if trust is not None and hasattr(chain, "set_trust_net"):
        try:
            if getattr(chain, "trust_net", None) is None:
                chain.set_trust_net(trust)
                L.info("[multisig] chain.trust_net injetado")
        except Exception as e:
            L.warning("[multisig] set_trust_net: %s", e)

    return install_multisig(chain, sk, p2p, trust=trust, logger=L)


# ═══════════════════════════════════════════════════════════════
# STATUS (diagnóstico rápido sem rodar nada)
# ═══════════════════════════════════════════════════════════════
def status(chain) -> dict:
    """Retorna um dict com o estado atual dos componentes de multi-sig."""
    out = {
        "version": __version__,
        "sign_collector_installed": False,
        "accept_block_autosign": False,
        "trust_net_installed": False,
        "local_sk_installed": False,
        "origin_pubkey": "",
        "top_validators": 0,
    }

    if chain is None:
        return out

    out["sign_collector_installed"] = (
        getattr(chain, "_sign_collector", None) is not None
    )
    out["accept_block_autosign"] = bool(
        getattr(chain, "_accept_block_autosign_installed", False)
    )

    trust = getattr(chain, "trust_net", None)
    if trust is not None:
        out["trust_net_installed"] = True
        try:
            out["origin_pubkey"] = getattr(trust, "_origin_pubkey", "") or ""
            out["top_validators"] = len(trust.get_top_validators(count=5))
        except Exception:
            pass

    sk = getattr(chain, "_local_validator_sk", None)
    out["local_sk_installed"] = sk is not None
    if sk is not None and hasattr(sk, "pub_hex"):
        out["local_pubkey"] = sk.pub_hex[:32] + "..."

    return out


# ═══════════════════════════════════════════════════════════════
# AUTO-TESTE (rodar este arquivo sozinho)
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    print("=" * 60)
    print(f" brn_multisig.py v{__version__} — auto-teste")
    print("=" * 60)

    try:
        from blockchain import Blockchain
        chain = Blockchain("brn_v2_chain.db", auto_genesis=False)
    except Exception as e:
        print(f"[X] Blockchain: {e}")
        sys.exit(2)

    try:
        s = status(chain)
        print(f"\nEstado atual:")
        for k, v in s.items():
            print(f"  {k:<28} = {v}")

        print(f"\nPara ativar no main.py, adicione após p2p.start():")
        print(f"  from brn_multisig import ensure_multisig")
        print(f"  ensure_multisig(chain, sk, p2p, trust=trust, logger=log)")
    finally:
        try:
            chain.db.close()
        except Exception:
            pass