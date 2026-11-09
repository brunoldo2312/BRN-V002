"""wallet_integrator.py — Integra carteira e propaga para a rede (v2.0)
================================================================
v2.0 (2026-11-09) — PROPAGAÇÃO PARA PEERS:
  [NEW] integrate_and_propagate(): após instalar SK local + multisig,
        avisa o Origin via POST /api/validator/announce para que o
        Origin adicione a pubkey no trust_network.json e re-assine.
        Sem isso, a pubkey do cliente nunca entra no top5 do Origin
        e o cliente nunca é contado no consenso.
  [NEW] Se o nó É o Origin, registra localmente direto (sem HTTP).
  [NEW] Após propagate, força sync_trust_network() para o cliente
        pegar o trust novo (sem esperar 60s).
  [NEW] Retorna "propagated": true/false e "peers_notified": N.
  [KEEP] integrate() (v1.0) — usado internamente.
================================================================
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import urllib.error
from typing import Optional

log = logging.getLogger("wallet_integrator")

__version__ = "2.0"


# ═══════════════════════════════════════════════════════════════
# INTEGRAÇÃO LOCAL (v1.0 — não mudou)
# ═══════════════════════════════════════════════════════════════
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
    Instala SK local + trust local + multisig + PBFT.
    NÃO propaga para peers (use integrate_and_propagate para isso).
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

    strict = os.environ.get("BRN_STRICT_MULTISIG", "0") == "1"

    # 1. SK local
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
    result["address"] = address or installed_pk[:16]

    if pubkey and pubkey.lower() != installed_pk:
        result["warnings"].append(
            f"pubkey passada ≠ derivada — usando a derivada"
        )

    # 2. Trust local
    trust = trust or getattr(chain, "trust_net", None)
    if trust is None:
        result["warnings"].append("trust_net é None")
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

            try:
                if hasattr(trust, "get_top_validators"):
                    top = trust.get_top_validators(count=5) or []
                    result["in_top5"] = installed_pk in [p.lower() for p in top]
                    if not result["in_top5"]:
                        result["warnings"].append(
                            f"pubkey não está em top5 LOCAL — propague ao Origin"
                        )
            except Exception as e:
                result["warnings"].append(f"get_top_validators: {e}")
        except Exception as e:
            result["errors"].append(f"trust sync: {e}")

    # 3. Multisig
    if p2p is None:
        result["warnings"].append("p2p é None — não coletará sigs de peers")
    else:
        try:
            from brn_multisig import ensure_multisig
            ensure_multisig(
                chain, chain._local_validator_sk, p2p,
                trust=trust, logger=L,
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
        result["errors"].append(
            "STRICT_MULTISIG=1 e multisig_ready=False"
        )
        return result

    # 4. PBFT
    if chain._pbft is None and os.environ.get("BRN_PBFT_ENABLED") == "1":
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

            pbft = PBFTConsensus(chain, chain._local_validator_sk, _pbft_send)
            chain.set_pbft(pbft)
            if p2p is not None and getattr(p2p, "server", None):
                try:
                    p2p.server.set_pbft(pbft)
                except Exception:
                    pass
            result["pbft_ready"] = True
        except Exception as e:
            result["warnings"].append(f"PBFT: {e}")

    # 5. Verificação final
    try:
        if chain._local_validator_sk is None:
            result["errors"].append("SK ficou None após integração")
            return result
        if chain._local_validator_sk.pub_hex.lower() != installed_pk:
            result["errors"].append("pubkey mudou durante integração")
            return result
    except Exception as e:
        result["warnings"].append(f"verificação final: {e}")

    result["ok"] = (
        result["sk_installed"]
        and not result["errors"]
        and (not strict or result["multisig_ready"])
    )

    L.info(
        "[integrator] local — ok=%s pk=%s… sk=%s trust=%s top5=%s multisig=%s",
        result["ok"], installed_pk[:16],
        result["sk_installed"], result["in_trust"],
        result["in_top5"], result["multisig_ready"],
    )
    for w in result["warnings"]:
        L.warning("[integrator] %s", w)
    for e in result["errors"]:
        L.error("[integrator] %s", e)

    return result


# ═══════════════════════════════════════════════════════════════
# NOVO v2.0 — PROPAGAÇÃO PARA PEERS
# ═══════════════════════════════════════════════════════════════
def _chain_is_origin(chain) -> bool:
    """Detecta se este nó é o Origin (env-aware)."""
    if bool(getattr(chain, "is_origin", False)):
        return True
    return os.environ.get("BRN_IS_ORIGIN", "0") == "1"


def _post_announce(origin_url: str, payload: dict,
                    timeout: float = 8.0) -> tuple[bool, str]:
    """
    POST para /api/validator/announce no Origin.
    Retorna (ok, msg).
    """
    url = origin_url.rstrip("/") + "/api/validator/announce"
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "wallet-integrator/2.0",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )
        with opener.open(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode())
            if resp.get("ok"):
                return True, f"origin registrou (top5={resp.get('in_top5')})"
            return False, resp.get("msg", "origin recusou")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
            return False, f"HTTP {e.code}: {body.get('msg', '?')}"
        except Exception:
            return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def integrate_and_propagate(
    chain,
    sk_hex: str,
    address: str = "",
    pubkey: str = "",
    p2p=None,
    trust=None,
    logger=None,
    origin_url: Optional[str] = None,
) -> dict:
    """
    Integração COMPLETA: local + propagação para o Origin.

    Fluxo:
      1. integrate() — instala SK + trust local + multisig + PBFT
      2. Detecta se é Origin:
         - SIM: registra direto no trust local (já feito em integrate)
         - NÃO: POST /api/validator/announce no Origin para que ele
                adicione a pubkey e re-assine o trust
      3. Se cliente e propagate OK, força sync_trust_network() para
         pegar o trust atualizado sem esperar 60s

    Retorna dict igual ao integrate() + campos:
      - propagated: bool     # conseguiu avisar o Origin
      - propagate_msg: str   # detalhe
      - peers_notified: int  # quantos peers foram avisados via P2P
      - trust_refreshed: bool # cliente baixou trust novo?
    """
    L = logger or log

    # ─── 1. Integra local ───
    result = integrate(
        chain, sk_hex,
        address=address, pubkey=pubkey,
        p2p=p2p, trust=trust, logger=L,
    )

    result["propagated"] = False
    result["propagate_msg"] = ""
    result["peers_notified"] = 0
    result["trust_refreshed"] = False

    if not result.get("ok"):
        result["propagate_msg"] = "integração local falhou, não propagando"
        return result

    installed_pk = result["pubkey"]

    # ─── 2a. Se é Origin: já está no trust local, propaga via P2P ───
    if _chain_is_origin(chain):
        L.info("[integrator] é Origin — pubkey já está no trust local")
        result["propagated"] = True
        result["propagate_msg"] = "origin local"

        # Propaga para peers via P2P (best-effort, não bloqueia)
        if p2p is not None and hasattr(p2p, "broadcast_validator_registration"):
            try:
                p2p.broadcast_validator_registration(
                    installed_pk, address or installed_pk[:16]
                )
                result["peers_notified"] = len(p2p.discovery.listar_peers())
                L.info(
                    "[integrator] announce_validator enviado para %d peer(s)",
                    result["peers_notified"],
                )
            except Exception as e:
                L.warning("[integrator] broadcast_validator falhou: %s", e)

        return result

    # ─── 2b. Se é cliente: POST no Origin ───
    target_origin = origin_url
    if not target_origin:
        target_origin = os.environ.get("BRN_POOL_URL", "").strip().rstrip("/")
    if not target_origin:
        # Tenta descobrir via BRN_BOOTSTRAP_PEERS
        bp = os.environ.get("BRN_BOOTSTRAP_PEERS", "").split(",")
        for p in bp:
            p = p.strip()
            if not p:
                continue
            ip = p.split(":")[0].split("/")[0]
            target_origin = f"http://{ip}:5000"
            break

    if not target_origin:
        result["propagate_msg"] = (
            "sem Origin configurado (BRN_POOL_URL vazio). "
            "Configure para que sua pubkey entre no trust da rede."
        )
        L.warning("[integrator] %s", result["propagate_msg"])
        return result

    L.info("[integrator] propagando pubkey para Origin %s", target_origin)
    payload = {
        "pubkey": installed_pk,
        "address": address or installed_pk[:16],
        "peer_addr": os.environ.get("BRN_SEU_ENDERECO", ""),
    }
    ok, msg = _post_announce(target_origin, payload)
    result["propagated"] = ok
    result["propagate_msg"] = msg

    if ok:
        L.info("[integrator] Origin registrou pubkey: %s", msg)

        # ─── 3. Força sync do trust agora (não espera 60s) ───
        try:
            if hasattr(chain, "_sync_manager") and chain._sync_manager is not None:
                sm = chain._sync_manager
                if hasattr(sm, "sync_trust_network"):
                    sm._last_trust_sync = 0  # força sync
                    sm.sync_trust_network()
                    result["trust_refreshed"] = True
                    L.info("[integrator] trust re-sincronizado do Origin")
            elif hasattr(chain, "sync_manager") and chain.sync_manager is not None:
                sm = chain.sync_manager
                if hasattr(sm, "sync_trust_network"):
                    sm._last_trust_sync = 0
                    sm.sync_trust_network()
                    result["trust_refreshed"] = True
        except Exception as e:
            L.debug("[integrator] trust refresh falhou: %s", e)

        # ─── 4. Propaga também via P2P (peer-to-peer broadcast) ───
        if p2p is not None and hasattr(p2p, "broadcast_validator_registration"):
            try:
                p2p.broadcast_validator_registration(
                    installed_pk, address or installed_pk[:16]
                )
                result["peers_notified"] = len(p2p.discovery.listar_peers())
            except Exception:
                pass
    else:
        L.warning("[integrator] Origin recusou: %s", msg)

    L.info(
        "[integrator] propagate — ok=%s msg=%s refreshed=%s peers=%d",
        result["propagated"], msg,
        result["trust_refreshed"], result["peers_notified"],
    )

    return result


# ═══════════════════════════════════════════════════════════════
# UTILITÁRIOS (mantidos)
# ═══════════════════════════════════════════════════════════════
def persist_sk_for_reboot(sk_hex: str, password: str) -> bool:
    try:
        from secure_store import save_current_wallet, load_current_wallet
        r = load_current_wallet(password)
        if not r.get("ok"):
            log.warning("[integrator] persist: senha não decifra")
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


def needs_integration(chain, expected_pubkey: str = "") -> bool:
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


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════
def _self_test() -> int:
    import sys
    import inspect

    print(f"wallet_integrator.py v{__version__}")
    print("=" * 60)
    falhas = 0

    # 1. Ambas as funções existem
    for fn_name in ("integrate", "integrate_and_propagate"):
        try:
            fn = globals()[fn_name]
            sig = inspect.signature(fn)
            print(f"  [ok] {fn_name}{tuple(sig.parameters)}")
        except Exception as e:
            print(f"  [X]  {fn_name}: {e}")
            falhas += 1

    # 2. Sem chain → não propaga
    try:
        r = integrate_and_propagate(None, "a" * 64)
        if r.get("ok") is False and r.get("propagated") is False:
            print(f"  [ok] integrate_and_propagate(None) → ok=False propagated=False")
        else:
            print(f"  [X]  resultado: {r}")
            falhas += 1
    except Exception as e:
        print(f"  [X]  exceção: {e}")
        falhas += 1

    # 3. _chain_is_origin respeita env
    try:
        class FakeChain:
            pass
        old = os.environ.pop("BRN_IS_ORIGIN", None)
        assert _chain_is_origin(FakeChain()) is False
        os.environ["BRN_IS_ORIGIN"] = "1"
        assert _chain_is_origin(FakeChain()) is True
        if old is not None:
            os.environ["BRN_IS_ORIGIN"] = old
        else:
            os.environ.pop("BRN_IS_ORIGIN", None)
        print(f"  [ok] _chain_is_origin respeita env")
    except Exception as e:
        print(f"  [X]  _chain_is_origin: {e}")
        falhas += 1

    # 4. Integrate real local
    try:
        from blockchain import Blockchain
        from wallet import Wallet
        import tempfile
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd); os.unlink(db_path)
        try:
            chain = Blockchain(db_path, auto_genesis=True)
            w = Wallet()

            class FakeTrust:
                def __init__(self):
                    self.nodes_called = []
                def sync_local_validator(self, pk, addr, peer):
                    self.nodes_called.append(pk[:16])
                def get_top_validators(self, count=5):
                    return [w.pub_hex]

            trust = FakeTrust()
            r = integrate(
                chain, w.priv_hex, address=w.address,
                pubkey=w.pub_hex, p2p=None, trust=trust,
            )
            if r.get("sk_installed") and r.get("in_trust"):
                print(f"  [ok] integrate local → sk=True trust=True")
            else:
                print(f"  [X]  integrate: {r}")
                falhas += 1
        finally:
            try: chain.db.close()
            except: pass
            for ext in ("", "-wal", "-shm"):
                try: os.unlink(db_path + ext)
                except: pass
    except Exception as e:
        print(f"  [skip] integrate real: {e}")

    print("=" * 60)
    if falhas == 0:
        print(" TODOS OS TESTES PASSARAM")
        return 0
    print(f" {falhas} falha(s)")
    return 1


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    sys.exit(_self_test())
