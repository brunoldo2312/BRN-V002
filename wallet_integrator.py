"""wallet_integrator.py — Integra carteira e propaga para a rede (v2.3)
================================================================
v2.3 (2026-11-13) — FIX "multisig_ready MENTIROSO":
  [FIX CRÍTICO] integrate() agora propaga o retorno BOOLEANO de
        ensure_multisig() em vez de setar result["multisig_ready"]
        = True cegamente. Antes: se ensure_multisig retornava
        False (sem exception), wallet_integrator reportava ok=True
        e multisig_ready=True, o AUTO-CONFIG logava "SK propagada"
        e o operador achava que o consenso estava OK — mas o bloco
        saía com 1/N sigs porque o collector não estava instalado.
        Era o "primo" do #4301: a mentira da flag escondia o
        problema do coletor.
  [FIX] PBFT: agora verifica chain._pbft depois de set_pbft().
        Antes, result["pbft_ready"] = True mesmo se set_pbft
        silenciosamente falhasse (chain sem atributo _pbft).
  [NEW] Logs INFO explícitos quando multisig_ready vira False
        (com motivo) e quando collector foi instalado com sucesso.
  [NEW] strict=1 passa a exigir multisig_ready=True de verdade
        (antes era mutuamente exclusivo com o bug acima).
  [KEEP] v2.2: _post_announce usa requests (SOCKS5 Tailscale).
  [KEEP] persist_sk_for_reboot, needs_integration, integrate_and_propagate.
================================================================
"""
from __future__ import annotations

import json
import logging
import os
import time
import requests
from typing import Optional

log = logging.getLogger("wallet_integrator")
__version__ = "2.3"


# ============================================================
# INTEGRAÇÃO LOCAL
# ============================================================
def integrate(
    chain,
    sk_hex: str,
    address: str = "",
    pubkey: str = "",
    p2p=None,
    trust=None,
    logger=None,
) -> dict:
    """
    Instala a SK local, registra no trust, instala o coletor de
    assinaturas (multi-sig) e, se habilitado, o motor PBFT.

    Retorna dict com:
      ok              — False se houve erro fatal (ou strict=1 e multisig
                        falhou). Sem strict, ok=True mesmo com
                        multisig_ready=False (mas com warning claro).
      sk_installed    — SK foi instalada em chain._local_validator_sk?
      in_trust        — pubkey foi registrada no trust?
      in_top5         — pubkey aparece em get_top_validators(5)?
      multisig_ready  — collector foi REALMENTE instalado?
                        (v2.3: propaga bool de ensure_multisig — antes
                        mentia True quando ensure_multisig retornava
                        False sem exception.)
      pbft_ready      — chain._pbft não é None após set_pbft?
      warnings, errors
    """
    L = logger or log
    result = {
        "ok": False, "pubkey": "", "address": "",
        "sk_installed": False, "in_trust": False, "in_top5": False,
        "multisig_ready": False, "pbft_ready": False,
        "warnings": [], "errors": [],
    }

    if chain is None:
        result["errors"].append("chain é None")
        return result
    if not sk_hex or len(sk_hex) != 64:
        result["errors"].append(f"sk_hex inválido (len={len(sk_hex or '')})")
        return result

    strict = os.environ.get("BRN_STRICT_MULTISIG", "0") == "1"

    # ─── 1. SK local ───
    from wallet import Wallet
    try:
        sk_obj = Wallet(private_key_hex=sk_hex)
        chain.set_local_validator_sk(sk_obj)
    except Exception as e:
        result["errors"].append(f"set_local_validator_sk: {e}")
        return result

    installed_pk = sk_obj.pub_hex.lower()
    result["pubkey"] = installed_pk
    result["sk_installed"] = True
    result["address"] = address or sk_obj.address

    # ─── 2. Trust local ───
    trust = trust or getattr(chain, "trust_net", None)
    if trust is None:
        result["warnings"].append("trust_net é None")
    else:
        try:
            if hasattr(trust, "sync_local_validator"):
                trust.sync_local_validator(installed_pk, result["address"], "")
                result["in_trust"] = True
            elif hasattr(trust, "register_node"):
                trust.register_node(
                    node_id=installed_pk,
                    address=result["address"],
                    public_key=installed_pk,
                )
                result["in_trust"] = True

            try:
                if hasattr(trust, "get_top_validators"):
                    top = trust.get_top_validators(count=5) or []
                    result["in_top5"] = installed_pk in [p.lower() for p in top]
            except Exception:
                pass
        except Exception as e:
            result["errors"].append(f"trust sync: {e}")

    # ─── 3. Multi-sig (coletor de assinaturas) ───
    # v2.3: propaga o retorno BOOLEANO de ensure_multisig.
    if p2p is None:
        result["warnings"].append(
            "p2p é None — não coletará sigs de peers (multi-sig "
            "ficará limitado à assinatura local)"
        )
        result["multisig_ready"] = False
    else:
        try:
            from brn_multisig import ensure_multisig
            ok_ms = ensure_multisig(
                chain, sk_obj, p2p, trust=trust, logger=L
            )
            result["multisig_ready"] = bool(ok_ms)
            if ok_ms:
                L.info(
                    "[integrator] ✅ multi-sig ATIVO — coletor instalado "
                    "em chain._sign_collector (pk=%s…)",
                    installed_pk[:16],
                )
            else:
                result["warnings"].append(
                    "ensure_multisig retornou False — coletor NÃO "
                    "instalado. Nó só assinará localmente; quórum "
                    "multi-validador vai falhar em blocos futuros."
                )
                L.warning(
                    "[integrator] ⚠️ multi-sig NÃO instalado "
                    "(ensure_multisig=False). O nó vai assinar apenas "
                    "localmente. Verifique chain.set_sign_collector e "
                    "p2p.discovery em brn_multisig.py."
                )
        except ImportError as e:
            result["multisig_ready"] = False
            result["warnings"].append(f"brn_multisig indisponível: {e}")
            L.error(
                "[integrator] brn_multisig.py NÃO encontrado — "
                "co-assinatura desativada. Coloque o arquivo na raiz "
                "do projeto. Detalhe: %s", e,
            )
        except Exception as e:
            result["multisig_ready"] = False
            result["warnings"].append(f"ensure_multisig exception: {e}")
            L.exception(
                "[integrator] ensure_multisig lançou exception: %s", e,
            )

    # strict=1 + multisig falhou → abortar ANTES de propagar
    if strict and not result["multisig_ready"]:
        result["errors"].append(
            "STRICT_MULTISIG=1 e multisig_ready=False — recusando "
            "prosseguir (propagar SK sem coletor deixa o bloco "
            "com 1/N sigs)"
        )
        return result

    # ─── 4. PBFT (opcional) ───
    if (getattr(chain, "_pbft", None) is None
            and os.environ.get("BRN_PBFT_ENABLED") == "1"):
        try:
            from pbft_consensus import PBFTConsensus
            from p2p_unified import P2PClient

            def _pbft_send(peer_addr, msg):
                try:
                    ip, port_s = peer_addr.rsplit(":", 1)
                    resp, _ = P2PClient.send_message(
                        ip, int(port_s), msg, timeout=15.0
                    )
                    return resp
                except Exception:
                    return None

            pbft = PBFTConsensus(chain, sk_obj, _pbft_send)
            chain.set_pbft(pbft)
            # v2.3: verifica de verdade
            if getattr(chain, "_pbft", None) is pbft:
                result["pbft_ready"] = True
                L.info("[integrator] ✅ PBFT instalado")
            else:
                result["warnings"].append(
                    "set_pbft() não armazenou o motor (chain._pbft "
                    "continua None) — PBFT desativado"
                )
            if p2p is not None and getattr(p2p, "server", None):
                try:
                    p2p.server.set_pbft(pbft)
                except Exception as e:
                    L.debug("[integrator] p2p.server.set_pbft: %s", e)
        except ImportError as e:
            result["warnings"].append(f"PBFT indisponível: {e}")
        except Exception as e:
            result["warnings"].append(f"PBFT: {e}")
            L.warning("[integrator] PBFT init falhou: %s", e)

    # ─── 5. Veredito ───
    result["ok"] = (
        result["sk_installed"]
        and not result["errors"]
        and (not strict or result["multisig_ready"])
    )
    return result


# ============================================================
# DETECÇÃO DE ORIGIN
# ============================================================
def _chain_is_origin(chain) -> bool:
    if bool(getattr(chain, "is_origin", False)):
        return True
    return os.environ.get("BRN_IS_ORIGIN", "0") == "1"


# ============================================================
# PROPAGAÇÃO PARA O ORIGIN
# ============================================================
def _post_announce(origin_url: str, payload: dict,
                    timeout: float = 10.0) -> tuple:
    """
    POST /api/validator/announce usando requests.
    requests respeita ALL_PROXY=socks5h:// do Tailscale;
    urllib NÃO respeita (era o "incompatible SOCKS version" do v2.1).
    """
    url = origin_url.rstrip("/") + "/api/validator/announce"
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        try:
            resp = r.json()
        except Exception:
            return False, f"HTTP {r.status_code} (resposta não-JSON)"
        if resp.get("ok"):
            return True, f"origin registrou (top5={resp.get('in_top5')})"
        return False, resp.get("msg", f"origin recusou ({r.status_code})")
    except requests.exceptions.ConnectTimeout:
        return False, f"connect timeout ({timeout}s)"
    except requests.exceptions.ReadTimeout:
        return False, f"read timeout ({timeout}s)"
    except requests.exceptions.ConnectionError as e:
        return False, f"connection: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def integrate_and_propagate(
    chain, sk_hex: str, address: str = "", pubkey: str = "",
    p2p=None, trust=None, logger=None, origin_url: Optional[str] = None,
) -> dict:
    """
    Faz integrate() local e, se este nó for CLIENTE (não Origin),
    propaga a pubkey para o Origin via HTTP.

    Retorna dict adicional:
      propagated       — Origin confirmou registro?
      propagate_msg    — mensagem do Origin
      peers_notified   — quantos peers foram avisados via broadcast P2P
      trust_refreshed  — sync_trust_network() foi forçado e funcionou?
    """
    L = logger or log
    result = integrate(chain, sk_hex, address=address, pubkey=pubkey,
                        p2p=p2p, trust=trust, logger=L)
    result["propagated"] = False
    result["propagate_msg"] = ""
    result["peers_notified"] = 0
    result["trust_refreshed"] = False

    if not result.get("ok"):
        result["propagate_msg"] = "integração local falhou"
        return result

    installed_pk = result["pubkey"]

    # Se é Origin, a pubkey já está no trust local — só broadcast P2P
    if _chain_is_origin(chain):
        L.info("[integrator] é Origin — pubkey já está no trust local")
        result["propagated"] = True
        result["propagate_msg"] = "origin local"
        if p2p is not None and hasattr(p2p, "broadcast_validator_registration"):
            try:
                p2p.broadcast_validator_registration(
                    installed_pk, result["address"]
                )
                result["peers_notified"] = len(p2p.discovery.listar_peers())
            except Exception:
                pass
        return result

    # Cliente: descobre Origin
    target_origin = (
        origin_url
        or os.environ.get("BRN_POOL_URL", "").strip().rstrip("/")
    )
    if not target_origin:
        bp = os.environ.get("BRN_BOOTSTRAP_PEERS", "").split(",")
        for p in bp:
            p = p.strip()
            if p:
                ip = p.split(":")[0].split("/")[0]
                target_origin = f"http://{ip}:5000"
                break

    if not target_origin:
        result["propagate_msg"] = "sem Origin configurado (BRN_POOL_URL vazio)"
        L.warning("[integrator] %s", result["propagate_msg"])
        return result

    L.info("[integrator] propagando pubkey para Origin %s", target_origin)
    payload = {
        "pubkey": installed_pk,
        "address": result["address"],
        "peer_addr": os.environ.get("BRN_SEU_ENDERECO", ""),
    }
    ok, msg = _post_announce(target_origin, payload)
    result["propagated"] = ok
    result["propagate_msg"] = msg

    if ok:
        L.info("[integrator] Origin registrou pubkey: %s", msg)

        # Força refresh do trust (se sync_manager estiver ligado)
        try:
            sm = (
                getattr(chain, "_sync_manager", None)
                or getattr(chain, "sync_manager", None)
            )
            if sm is not None and hasattr(sm, "sync_trust_network"):
                sm._last_trust_sync = 0
                sm.sync_trust_network()
                result["trust_refreshed"] = True
        except Exception as e:
            L.debug("[integrator] trust refresh falhou: %s", e)

        if p2p is not None and hasattr(p2p, "broadcast_validator_registration"):
            try:
                p2p.broadcast_validator_registration(
                    installed_pk, result["address"]
                )
                result["peers_notified"] = len(p2p.discovery.listar_peers())
            except Exception:
                pass
    else:
        L.warning("[integrator] Origin recusou: %s", msg)

    L.info(
        "[integrator] propagate — ok=%s msg=%s refreshed=%s peers=%d "
        "multisig_ready=%s",
        result["propagated"], msg,
        result["trust_refreshed"], result["peers_notified"],
        result["multisig_ready"],
    )
    return result


# ============================================================
# PERSISTÊNCIA E DETECÇÃO
# ============================================================
def persist_sk_for_reboot(sk_hex: str, password: str) -> bool:
    """Salva a SK em current_wallet.enc para o próximo boot."""
    try:
        from secure_store import save_current_wallet, load_current_wallet
        r = load_current_wallet(password)
        if not r.get("ok"):
            return False
        data = {
            "address": r.get("address", ""),
            "private_key": sk_hex,
            "public_key": r.get("public_key") or r.get("pub_hex", ""),
        }
        r2 = save_current_wallet(password, data)
        return bool(r2.get("ok"))
    except Exception:
        return False


def needs_integration(chain, expected_pubkey: str = "") -> bool:
    """
    True se a chain NÃO está pronta para assinar/co-assinar:
      - _local_validator_sk é None, OU
      - pubkey diverge da esperada, OU
      - pubkey não está em get_top_validators(5).
    """
    if chain is None:
        return True
    try:
        sk = getattr(chain, "_local_validator_sk", None)
        if sk is None:
            return True
        pk = sk.pub_hex.lower()
        if expected_pubkey and pk != expected_pubkey.lower():
            return True
        trust = getattr(chain, "trust_net", None)
        if trust is not None and hasattr(trust, "get_top_validators"):
            try:
                top = trust.get_top_validators(count=5) or []
                if pk not in [p.lower() for p in top]:
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return True


# ============================================================
# AUTOTESTE
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(f"wallet_integrator.py v{__version__}")
    print("=" * 60)

    # 1. _chain_is_origin
    class C:
        def __init__(self, v=False): self.is_origin = v

    os.environ.pop("BRN_IS_ORIGIN", None)
    assert _chain_is_origin(C(False)) is False
    assert _chain_is_origin(C(True)) is True
    os.environ["BRN_IS_ORIGIN"] = "1"
    assert _chain_is_origin(C(False)) is True  # env vence
    os.environ.pop("BRN_IS_ORIGIN", None)
    print("  [ok] _chain_is_origin")

    # 2. _post_announce (URL inválida → falha rápida)
    ok, msg = _post_announce("http://127.0.0.1:1", {"pubkey": "x"}, timeout=0.5)
    assert ok is False, f"deveria falhar, got {ok}, {msg}"
    print(f"  [ok] _post_announce timeout rápido: {msg[:50]}")

    # 3. needs_integration com chain vazia
    class FakeChain:
        _local_validator_sk = None
        trust_net = None

    assert needs_integration(FakeChain()) is True
    print("  [ok] needs_integration(sem SK) = True")

    print()
    print(" TODOS OS TESTES PASSARAM")
    print("=" * 60)
