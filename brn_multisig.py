"""brn_multisig.py — Coletor de assinaturas (Multi-Sig) do BRN (v2.4)
================================================================
v2.4 (2026-11-13) — FIX #4301 (0 assinaturas / quórum não atinge):
  [FIX CRÍTICO] Timeout alinhado com blockchain.SIG_COLLECT_TIMEOUT.
        Antes: build_collector usava timeout=15s internamente e
        as_completed(timeout=timeout+2)=17s, mas _collect_signatures
        em blockchain.py usa _fut.result(timeout=SIG_COLLECT_TIMEOUT)
        = 10s. Resultado: o coletor era CANCELADO antes de retornar,
        o extra virava [] e o bloco ia com 1/N assinaturas. O #4301
        "tem 0. Faltam: 02eeec..., 03244d..., 03b992d..." era isso.
        Agora o budget interno é min(user_timeout, SIG_COLLECT_TIMEOUT-2).
  [FIX] sign_block msg agora inclui _chain e _port, igual ao
        test_sign_local.py e sync_manual.py. Sem isso o peer pode
        recusar por chain_id mismatch.
  [FIX] Peers com pubkey desconhecida (discovery sem public_key)
        NÃO são mais pulados. Pedimos de qualquer forma, e a
        verificação contra active_validators acontece no retorno.
  [FIX] ensure_multisig agora REINSTALA o collector se já existe
        (antes early-return True deixava collector antigo morto no
        chain mesmo se p2p tivesse sido trocado).
  [FIX] build_collector lê p2p de chain.p2p DINAMICAMENTE a cada
        chamada. Antes capturava p2p em closure, então um restart
        do P2PManager deixava o collector apontando para o p2p morto.
  [NEW] _resolve_collector_budget(): prioriza env BRN_SIG_COLLECT_TIMEOUT
        e respeita o valor passado, cap no (SIG_COLLECT_TIMEOUT - 2).
  [NEW] log INFO explícito com N peers perguntados, N responderam,
        N sigs válidas, tempo total. Fica claro no log o motivo de
        um bloco sair com menos sigs que o esperado.
  [KEEP] API pública idêntica: build_collector(chain, wallet, p2p,
        timeout), ensure_multisig(chain, wallet, p2p, trust, logger),
        status(chain).
  [KEEP] test_pool_signing.py e server.py continuam funcionando sem
        mudança.
================================================================
"""
from __future__ import annotations

__version__ = "2.4"

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout, as_completed
from typing import Callable, List, Optional

log = logging.getLogger("multisig")

# Timeout do blockchain.py (o "outro lado"). Tem que ser LIDO do
# mesmo env que blockchain.py lê, senão divergimos de novo.
def _blockchain_sig_timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("BRN_SIG_COLLECT_TIMEOUT", "10")))
    except (TypeError, ValueError):
        return 10.0


def _resolve_collector_budget(user_timeout: Optional[float]) -> float:
    """
    Budget interno do coletor. Precisa ser ESTRITAMENTE MENOR que
    SIG_COLLECT_TIMEOUT do blockchain.py, senão a thread do coletor
    é cancelada antes de retornar e as sigs dos peers são perdidas.
    Reservamos 2s de folga para o overhead de dedup e retorno.
    """
    outer = _blockchain_sig_timeout()
    ceiling = max(0.5, outer - 2.0)
    if user_timeout is None:
        return ceiling
    try:
        ut = float(user_timeout)
    except (TypeError, ValueError):
        return ceiling
    return max(0.5, min(ut, ceiling))


def _send_sign_request(ip: str, port: int, block: dict, timeout: float) -> dict:
    """Envia sign_block com _chain e _port (obrigatório para p2p_unified)."""
    try:
        from p2p_unified import P2PClient, CHAIN_ID
    except Exception as e:
        log.debug("[multisig] p2p_unified indisponível: %s", e)
        return {}

    try:
        p2p_port = int(os.environ.get("BRN_P2P_PORT", "6001"))
    except (TypeError, ValueError):
        p2p_port = 6001

    msg = {
        "type": "sign_block",
        "block": block,
        "_chain": CHAIN_ID,
        "_port": p2p_port,
    }
    try:
        resp, _lat = P2PClient.send_message(ip, port, msg, timeout=timeout)
        return resp or {}
    except Exception as e:
        log.debug("[multisig] send %s:%d falhou: %s", ip, port, e)
        return {}


def _split_addr(addr: str) -> tuple:
    """Aceita ip:port e [::1]:port."""
    if not addr or ":" not in addr:
        return ("", 0)
    if addr.startswith("["):
        end = addr.find("]")
        if end < 0:
            return ("", 0)
        rest = addr[end + 1:]
        if not rest.startswith(":"):
            return ("", 0)
        try:
            return (addr[1:end], int(rest[1:]))
        except ValueError:
            return ("", 0)
    ip, _, p = addr.rpartition(":")
    try:
        return (ip, int(p))
    except ValueError:
        return ("", 0)


def _list_peers_for_collector(chain, active_validators: set) -> List[tuple]:
    """
    Retorna [(addr, pubkey_or_empty), ...] dos peers vivos.
    Não exige que o peer esteja no trust (o caller decide).
    """
    out = []
    seen = set()

    my_pub = ""
    sk = getattr(chain, "_local_validator_sk", None)
    if sk is not None:
        try:
            my_pub = (getattr(sk, "pub_hex", "") or "").lower()
        except Exception:
            pass

    p2p = getattr(chain, "p2p", None)  # dinâmico!
    if p2p is None:
        return out

    disc = getattr(p2p, "discovery", None)
    if disc is None:
        return out

    rows = None
    if hasattr(disc, "listar_peers_com_altura"):
        try:
            rows = disc.listar_peers_com_altura() or []
        except Exception as e:
            log.debug("[multisig] listar_peers_com_altura falhou: %s", e)
            rows = None
    if rows is None and hasattr(disc, "listar_peers"):
        try:
            # discovery antigo: lista de strings
            addrs = disc.listar_peers() or []
            rows = [{"address": a, "public_key": ""} for a in addrs]
        except Exception as e:
            log.debug("[multisig] listar_peers falhou: %s", e)
            return out

    for p in rows:
        if not isinstance(p, dict):
            continue
        addr = (p.get("address") or "").strip()
        if not addr or ":" not in addr:
            continue
        ip, port = _split_addr(addr)
        if not ip or not port:
            continue
        if ip in ("127.0.0.1", "0.0.0.0", "::1") or ip == "localhost":
            continue
        my_addr = os.environ.get("BRN_SEU_ENDERECO", "").strip()
        if my_addr:
            my_ip, _ = _split_addr(my_addr)
            if my_ip and ip == my_ip:
                continue
        pubkey = (p.get("public_key") or "").strip().lower()
        if pubkey and pubkey == my_pub:
            continue
        if addr in seen:
            continue
        seen.add(addr)
        out.append((addr, pubkey))
    return out


def build_collector(chain, wallet, p2p=None, timeout: Optional[float] = None
                    ) -> Callable[[dict], List[dict]]:
    """
    Construtor do coletor. Compatível com o chamador existente:

        collector = build_collector(chain, wallet, p2p, timeout=5.0)
        chain.set_sign_collector(collector)

    A função retornada é chamada por blockchain._collect_signatures
    dentro de ThreadPoolExecutor com SIG_COLLECT_TIMEOUT. O budget
    INTERNO aqui é sempre menor para não ser cortado no meio.
    """
    budget = _resolve_collector_budget(timeout)
    outer = _blockchain_sig_timeout()
    if budget + 1.0 > outer:
        log.warning(
            "[multisig] collector budget %.1fs muito próximo do "
            "SIG_COLLECT_TIMEOUT=%.1fs — risco de perder sigs no "
            "corte do blockchain.py", budget, outer,
        )

    log.info(
        "[multisig] build_collector: budget interno=%.1fs (outer=%.1fs)",
        budget, outer,
    )

    def collector(block: dict) -> List[dict]:
        t0 = time.time()
        sigs: List[dict] = []
        seen_pks: set = set()

        # 1. Assinatura local primeiro (garantida)
        try:
            from blockchain import sign_block_as_validator
            local_sig = sign_block_as_validator(block, wallet)
        except Exception as e:
            log.warning("[multisig] assinatura local falhou: %s", e)
            local_sig = {}
        if local_sig and local_sig.get("pubkey"):
            sigs.append(local_sig)
            seen_pks.add(local_sig["pubkey"].lower())

        # 2. Lista de validadores ativos (só para filtrar respostas)
        active_validators: set = set()
        try:
            if hasattr(chain, "_get_active_validators"):
                active_validators = {
                    (p or "").lower()
                    for p in chain._get_active_validators()
                    if p
                }
        except Exception as e:
            log.debug("[multisig] _get_active_validators falhou: %s", e)

        # 3. Peers a perguntar (usa chain.p2p dinamicamente)
        peers = _list_peers_for_collector(chain, active_validators)
        if not peers:
            log.debug("[multisig] nenhum peer conhecido — só assinatura local")
            return sigs

        # 4. Pergunta em paralelo (budget interno respeitado)
        try:
            from blockchain import verify_block_signature
        except Exception as e:
            log.warning("[multisig] verify_block_signature indisponível: %s", e)
            return sigs

        def _ask(addr: str) -> Optional[dict]:
            ip, port = _split_addr(addr)
            if not ip or not port:
                return None
            return _send_sign_request(ip, port, block, budget)

        answered = 0
        rejected_foreign = 0
        max_workers = min(len(peers), 5)
        deadline = t0 + budget

        with ThreadPoolExecutor(max_workers=max_workers,
                                 thread_name_prefix="sigprobe") as ex:
            futs = {ex.submit(_ask, addr): addr for addr, _pk in peers}
            # as_completed com o MESMO deadline global — nada de
            # timeout + 2.0 (era esse o bug do v2.3).
            remaining = max(0.1, deadline - time.time())
            try:
                for fut in as_completed(futs, timeout=remaining):
                    addr = futs[fut]
                    try:
                        resp = fut.result()
                    except Exception as e:
                        log.debug("[multisig] %s exception: %s", addr, e)
                        continue
                    if not resp:
                        continue
                    answered += 1
                    pk = (resp.get("pubkey") or "").strip().lower()
                    sg = (resp.get("signature") or "").strip()
                    if not pk or not sg:
                        continue
                    if pk in seen_pks:
                        continue
                    if not verify_block_signature(pk, block, sg):
                        log.warning(
                            "[multisig] assinatura inválida de %s (pk=%s…)",
                            addr, pk[:16],
                        )
                        continue
                    if active_validators and pk not in active_validators:
                        # peer assinou mas não é validador ativo do nosso
                        # set — blockchain.py rejeitaria de qualquer
                        # forma; descartamos aqui e logamos.
                        rejected_foreign += 1
                        log.debug(
                            "[multisig] %s assinou mas não é validador "
                            "ativo (pk=%s…)", addr, pk[:16],
                        )
                        continue
                    sigs.append({"pubkey": pk, "signature": sg})
                    seen_pks.add(pk)
            except FutTimeout:
                log.warning(
                    "[multisig] timeout na coleta (budget=%.1fs, "
                    "%d/%d peers responderam)",
                    budget, answered, len(peers),
                )

        dt_ms = (time.time() - t0) * 1000
        log.info(
            "[multisig] #%s: %d sig(s) — local + %d peer(s) de %d | "
            "%d responderam | %d estrangeiras | %.0fms",
            block.get("height", "?"),
            len(sigs),
            max(0, len(sigs) - (1 if local_sig else 0)),
            len(peers),
            answered, rejected_foreign, dt_ms,
        )
        return sigs

    return collector


def ensure_multisig(chain, wallet, p2p=None, trust=None, logger=None) -> bool:
    """
    Instala (ou REinstala) o coletor em chain._sign_collector.
    Retorna True somente se ficou instalado E callable.
    """
    L = logger or log

    if chain is None:
        L.error("[multisig] ensure_multisig: chain é None")
        return False
    if wallet is None:
        L.error("[multisig] ensure_multisig: wallet é None")
        return False

    # SK local: reforça se não estiver instalada
    if getattr(chain, "_local_validator_sk", None) is None:
        try:
            chain.set_local_validator_sk(wallet)
            L.info(
                "[multisig] SK local instalada: %s…",
                getattr(wallet, "pub_hex", "?")[:16],
            )
        except Exception as e:
            L.error("[multisig] set_local_validator_sk falhou: %s", e)
            return False

    # Constrói SEMPRE (permite reinstall quando p2p foi trocado)
    try:
        collector = build_collector(chain, wallet, p2p)
    except Exception as e:
        L.error("[multisig] build_collector falhou: %s", e)
        return False

    try:
        if hasattr(chain, "set_sign_collector"):
            chain.set_sign_collector(collector)
        else:
            chain._sign_collector = collector
    except Exception as e:
        L.error("[multisig] set_sign_collector falhou: %s", e)
        return False

    installed = getattr(chain, "_sign_collector", None) is not None
    if not installed:
        L.error("[multisig] collector NÃO ficou instalado em chain")
        return False

    n_peers = len(_list_peers_for_collector(chain, set()))
    L.info(
        "[multisig] collector INSTALADO (%d peer(s) conhecido(s), "
        "budget=%.1fs)",
        n_peers, _resolve_collector_budget(None),
    )
    return True


def status(chain) -> dict:
    """Snapshot para /health ou CLI."""
    has_collector = getattr(chain, "_sign_collector", None) is not None
    has_sk = getattr(chain, "_local_validator_sk", None) is not None
    p2p = getattr(chain, "p2p", None)
    n_peers = len(_list_peers_for_collector(chain, set())) if p2p else 0
    return {
        "version": __version__,
        "sign_collector_installed": has_collector,
        "local_sk_installed": has_sk,
        "p2p_attached": p2p is not None,
        "peers_known": n_peers,
        "budget_s": _resolve_collector_budget(None),
        "outer_timeout_s": _blockchain_sig_timeout(),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("=" * 60)
    print(f" brn_multisig.py v{__version__} — auto-teste")
    print("=" * 60)

    # 1. Budget respeita ceiling
    os.environ.pop("BRN_SIG_COLLECT_TIMEOUT", None)
    b = _resolve_collector_budget(15.0)
    assert b <= 8.0, f"budget {b} deveria ser <= 8 (outer=10 - 2)"
    print(f"  [ok] budget(15s) = {b}s (capped por SIG_COLLECT_TIMEOUT=10s)")

    b = _resolve_collector_budget(5.0)
    assert b == 5.0, f"budget(5s) deveria ser 5, got {b}"
    print(f"  [ok] budget(5s) = {b}s (respeita o passado)")

    os.environ["BRN_SIG_COLLECT_TIMEOUT"] = "20"
    b = _resolve_collector_budget(15.0)
    assert b == 15.0, f"budget(15s, outer=20) = {b}, esperado 15"
    print(f"  [ok] budget(15s, outer=20s) = {b}s")
    os.environ.pop("BRN_SIG_COLLECT_TIMEOUT", None)

    # 2. _split_addr
    assert _split_addr("1.2.3.4:6001") == ("1.2.3.4", 6001)
    assert _split_addr("[::1]:6001") == ("::1", 6001)
    assert _split_addr("nope") == ("", 0)
    print("  [ok] _split_addr")

    # 3. ensure_multisig instala (e reinstala)
    class FakeChain:
        def __init__(self):
            self._sign_collector = None
            self._local_validator_sk = None
            self.p2p = None
            self.trust_net = None
            self.is_origin = False
        def set_local_validator_sk(self, sk): self._local_validator_sk = sk
        def set_sign_collector(self, c): self._sign_collector = c

    class FakeWallet:
        pub_hex = "02" + "a" * 64

    ch = FakeChain()
    w = FakeWallet()
    ok = ensure_multisig(ch, w, None, logger=logging.getLogger())
    assert ok and ch._sign_collector is not None
    first = ch._sign_collector
    ok = ensure_multisig(ch, w, None, logger=logging.getLogger())
    assert ok and ch._sign_collector is not first, \
        "reinstall deveria substituir o collector"
    print("  [ok] ensure_multisig instala + reinstala")

    # 4. status
    st = status(ch)
    assert st["sign_collector_installed"] is True
    assert st["local_sk_installed"] is True
    print(f"  [ok] status: {st}")

    print()
    print(" TODOS OS TESTES PASSARAM")
    print("=" * 60)
