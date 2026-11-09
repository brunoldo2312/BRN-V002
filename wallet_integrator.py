"""wallet_integrator.py — Integra carteira desbloqueada ao nó BRN (v1.0)
================================================================
Ponto único de integração GUI → nó. Chamado por:
  - server.py::api_wallet_set_validator_sk
  - main.py::_auto_heal_wallet_loop (a cada 30s)

Faz em ordem:
  1. chain.set_local_validator_sk(sk)  → SK no nó
  2. trust.sync_local_validator(pubkey) → trust reconhece
  3. ensure_multisig(chain, sk, p2p, trust) → _sign_collector configurado
  4. PBFT dinâmico se BRN_PBFT_ENABLED=1
  5. Verifica /health — wallet_sk_installed + multisig_configured

Retorna dict detalhado para a GUI mostrar:
  {
    "ok": bool,
    "pubkey": str,
    "address": str,
    "sk_installed": bool,
    "in_trust": bool,
    "in_top5": bool,
    "multisig_ready": bool,
    "pbft_ready": bool,
    "warnings": [str],
    "errors": [str],
  }
================================================================
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

log = logging.getLogger("wallet_integrator")

__version__ = "1.0"


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
    Faz a integração completa da carteira no nó.

    Pré-requisitos:
      - chain: Blockchain já aberta
      - sk_hex: 64 hex chars

    Opcionais:
      - p2p: P2PManager (se None, _sign_collector só assina localmente)
      - trust: TrustNetwork (se None, tenta chain.trust_net)

    Retorna dict detalhado. Não levanta exceção.
    """
    L = logger or log

    result = {
        "ok": False,
        "pubkey": "",
        "address": "",
        "sk_installed": False,
        "in_trust": False,
        "in_top5": False,
        "multisig_ready": False,
        "pbft_ready": False,
        "warnings": [],
        "errors": [],
    }

    # ─── 0. Sanity ───
    if chain is None:
        result["errors"].append("chain é None")
        return result

    if not sk_hex or len(sk_hex) != 64:
        result["errors"].append(f"sk_hex inválido (len={len(sk_hex or '')})")
        return result

    try:
        int(sk_hex, 16)
    except ValueError:
        result["errors"].append("sk_hex não é hex")
        return result

    # ─── 1. Instala SK no chain ───
    try:
        chain.set_local_validator_sk(sk_hex)
    except Exception as e:
        result["errors"].append(f"set_local_validator_sk: {e}")
        return result

    installed_pk = ""
    try:
        if chain._local_validator_sk is not None:
            installed_pk = chain._local_validator_sk.pub_hex.lower()
    except Exception:
        pass

    if not installed_pk:
        result["errors"].append("chain._local_validator_sk não instalou")
        return result

    result["pubkey"] = installed_pk
    result["sk_installed"] = True
    if not pubkey:
        pubkey = installed_pk
    result["address"] = address or installed_pk[:16]

    # ─── 2. Sincroniza trust ───
    trust = trust or getattr(chain, "trust_net", None)
    if trust is None:
        result["warnings"].append("trust_net é None — não sincronizo trust")
    else:
        try:
            if hasattr(trust, "sync_local_validator"):
                trust.sync_local_validator(installed_pk, address or "", "")
                result["in_trust"] = True
            elif hasattr(trust, "register_node"):
                trust.register_node(
                    node_id=installed_pk,
                    address=address or installed_pk[:16],
                    public_key=installed_pk,
                )
                if hasattr(trust, "mark_connected"):
                    trust.mark_connected(installed_pk)
                result["in_trust"] = True
            else:
                result["warnings"].append("trust sem sync_local_validator/register_node")

            # Verifica se está no top5
            try:
                top = trust.get_top_validators(count=5) if hasattr(trust, "get_top_validators") else []
                result["in_top5"] = installed_pk in [p.lower() for p in top]
                if not result["in_top5"]:
                    result["warnings"].append(
                        f"pubkey {installed_pk[:16]}… NÃO está em top5. "
                        f"Rode validator_manager.py --promote ou reinicie o main.py."
                    )
            except Exception as e:
                result["warnings"].append(f"get_top_validators: {e}")
        except Exception as e:
            result["errors"].append(f"trust sync: {e}")

    # ─── 3. Configura multisig ───
    strict = os.environ.get("BRN_STRICT_MULTISIG", "0") == "1"

    if p2p is None:
        result["warnings"].append(
            "p2p é None — _sign_collector só vai assinar localmente. "
            "Rede multi-nó NÃO terá quórum."
        )
    else:
        try:
            from brn_multisig import ensure_multisig
            ensure_multisig(
                chain,
                chain._local_validator_sk,
                p2p,
                trust=trust,
                logger=L,
            )
            result["multisig_ready"] = True
        except ImportError as e:
            msg = f"brn_multisig ausente: {e}"
            if strict:
                result["errors"].append(msg)
                return result
            result["warnings"].append(msg)
        except Exception as e:
            msg = f"ensure_multisig: {e}"
            if strict:
                result["errors"].append(msg)
                return result
            result["warnings"].append(msg)

    if strict and not result["multisig_ready"]:
        result["errors"].append("STRICT_MULTISIG=1 e multisig_ready=False")
        return result

    # ─── 4. PBFT dinâmico ───
    if chain._pbft is None and os.environ.get("BRN_PBFT_ENABLED") == "1":
        try:
            from pbft_consensus import PBFTConsensus
            from p2p_unified import P2PClient

            def _pbft_send(peer_addr, msg):
                try:
                    ip, port_s = peer_addr.rsplit(":", 1)
                    resp, _ = P2PClient.send_message(ip, int(port_s), msg, timeout=15.0)
                    return resp
                except Exception:
                    return None

            pbft = PBFTConsensus(chain, chain._local_validator_sk, _pbft_send)
            chain.set_pbft(pbft)
            if p2p and getattr(p2p, "server", None):
                try:
                    p2p.server.set_pbft(pbft)
                except Exception:
                    pass
            result["pbft_ready"] = True
        except Exception as e:
            result["warnings"].append(f"PBFT dinâmico: {e}")

    # ─── 5. Verificação final ───
    try:
        if chain._local_validator_sk is None:
            result["errors"].append("chain._local_validator_sk ficou None após integração")
            return result
        if chain._local_validator_sk.pub_hex.lower() != installed_pk:
            result["errors"].append("pubkey da SK mudou durante integração")
            return result
    except Exception as e:
        result["warnings"].append(f"verificação final: {e}")

    result["ok"] = (
        result["sk_installed"]
        and not result["errors"]
        and (not strict or result["multisig_ready"])
    )

    L.info(
        "[integrator] %s — pk=%s… sk=%s trust=%s top5=%s multisig=%s pbft=%s",
        "OK" if result["ok"] else "FALHA",
        installed_pk[:16],
        result["sk_installed"],
        result["in_trust"],
        result["in_top5"],
        result["multisig_ready"],
        result["pbft_ready"],
    )

    if result["warnings"]:
        for w in result["warnings"]:
            L.warning("[integrator] %s", w)
    if result["errors"]:
        for e in result["errors"]:
            L.error("[integrator] %s", e)

    return result


def persist_sk_for_reboot(sk_hex: str, password: str) -> bool:
    """
    Salva a SK em current_wallet.enc para o próximo boot do main.py
    já carregar automaticamente. Sem isso, o main.py só carrega
    no próximo start se a senha do .env estiver certa.
    """
    try:
        from secure_store import save_current_wallet, load_current_wallet
        # Só re-salva se a senha atual funciona
        r = load_current_wallet(password)
        if not r.get("ok"):
            log.warning("[integrator] persist: senha atual não decifra, pulando")
            return False
        data = {
            "address": r.get("address", ""),
            "private_key": sk_hex,
            "public_key": r.get("public_key") or r.get("pub_hex", ""),
        }
        r2 = save_current_wallet(password, data)
        return bool(r2.get("ok"))
    except Exception as e:
        log.warning("[integrator] persist falhou: %s", e)
        return False
