"""wallet_integrator.py — Integra carteira e propaga para a rede (v2.2)
================================================================
v2.2 (2026-11-11) — SUPORTE A PROXY SOCKS5:
  [FIX] _post_announce trocou urllib por requests para funcionar
        com ALL_PROXY (Tailscale/Render).
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

__version__ = "2.2"


def integrate(
    chain,
    sk_hex: str,
    address: str = "",
    pubkey: str = "",
    p2p=None,
    trust=None,
    logger=None,
) -> dict:
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

    # 1. SK local
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

    # 2. Trust local
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

    # 3. Multisig
    if p2p is None:
        result["warnings"].append("p2p é None — não coletará sigs de peers")
    else:
        try:
            from brn_multisig import ensure_multisig
            ensure_multisig(chain, sk_obj, p2p, trust=trust, logger=L)
            result["multisig_ready"] = True
        except Exception as e:
            result["warnings"].append(f"ensure_multisig: {e}")

    if strict and not result["multisig_ready"]:
        result["errors"].append("STRICT_MULTISIG=1 e multisig_ready=False")
        return result

    # 4. PBFT
    if getattr(chain, "_pbft", None) is None and os.environ.get("BRN_PBFT_ENABLED") == "1":
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
            pbft = PBFTConsensus(chain, sk_obj, _pbft_send)
            chain.set_pbft(pbft)
            if p2p is not None and getattr(p2p, "server", None):
                p2p.server.set_pbft(pbft)
            result["pbft_ready"] = True
        except Exception as e:
            result["warnings"].append(f"PBFT: {e}")

    result["ok"] = (
        result["sk_installed"]
        and not result["errors"]
        and (not strict or result["multisig_ready"])
    )
    return result


def _chain_is_origin(chain) -> bool:
    if bool(getattr(chain, "is_origin", False)):
        return True
    return os.environ.get("BRN_IS_ORIGIN", "0") == "1"


def _post_announce(origin_url: str, payload: dict, timeout: float = 8.0) -> tuple[bool, str]:
    """POST /api/validator/announce — usa requests para suportar SOCKS5."""
    url = origin_url.rstrip("/") + "/api/validator/announce"
    try:
        # requests respeita ALL_PROXY (socks5h://) automaticamente
        r = requests.post(url, json=payload, timeout=timeout)
        resp = r.json()
        if resp.get("ok"):
            return True, f"origin registrou (top5={resp.get('in_top5')})"
        return False, resp.get("msg", "origin recusou")
    except requests.exceptions.RequestException as e:
        return False, f"{type(e).__name__}: {e}"
    except Exception as e:
        return False, str(e)


def integrate_and_propagate(
    chain, sk_hex: str, address: str = "", pubkey: str = "",
    p2p=None, trust=None, logger=None, origin_url: Optional[str] = None,
) -> dict:
    L = logger or log
    result = integrate(chain, sk_hex, address=address, pubkey=pubkey, p2p=p2p, trust=trust, logger=L)
    result["propagated"] = False
    result["propagate_msg"] = ""
    result["peers_notified"] = 0
    result["trust_refreshed"] = False

    if not result.get("ok"):
        result["propagate_msg"] = "integração local falhou"
        return result

    installed_pk = result["pubkey"]

    if _chain_is_origin(chain):
        result["propagated"] = True
        result["propagate_msg"] = "origin local"
        if p2p is not None and hasattr(p2p, "broadcast_validator_registration"):
            try:
                p2p.broadcast_validator_registration(installed_pk, result["address"])
                result["peers_notified"] = len(p2p.discovery.listar_peers())
            except Exception:
                pass
        return result

    target_origin = origin_url or os.environ.get("BRN_POOL_URL", "").strip().rstrip("/")
    if not target_origin:
        bp = os.environ.get("BRN_BOOTSTRAP_PEERS", "").split(",")
        for p in bp:
            p = p.strip()
            if p:
                ip = p.split(":")[0].split("/")[0]
                target_origin = f"http://{ip}:5000"
                break

    if not target_origin:
        result["propagate_msg"] = "sem Origin configurado"
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
        try:
            if hasattr(chain, "_sync_manager") and chain._sync_manager is not None:
                sm = chain._sync_manager
                if hasattr(sm, "sync_trust_network"):
                    sm._last_trust_sync = 0
                    sm.sync_trust_network()
                    result["trust_refreshed"] = True
        except Exception:
            pass

        if p2p is not None and hasattr(p2p, "broadcast_validator_registration"):
            try:
                p2p.broadcast_validator_registration(installed_pk, result["address"])
                result["peers_notified"] = len(p2p.discovery.listar_peers())
            except Exception:
                pass

    return result
