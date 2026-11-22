"""auto_wire.py — Auto-configura trust_net, local_validator_sk e sign_collector.
================================================================
Detecta automaticamente, na ordem:
  SK:
    1. BRN_VALIDATOR_SK_HEX  (env, hex 64)
    2. BRN_VALIDATOR_SEED    (env, string → sha256)
    3. .validator_sk         (arquivo, hex na 1ª linha)
    4. current_wallet.enc    (via BRN_NODE_PASSWORD / BRN_WALLET_SESSION_PASS)
    5. current_wallet.json   (legado plaintext)

  TRUST:
    1. BRN_TRUST_NETWORK     (env, path)
    2. trust_network.json    (BASE_DIR)
    3. Stub (lê peers do DB) se BRN_TRUST_FALLBACK=1

  SIGN_COLLECTOR:
    - Criado quando chain.p2p é injetado (pede sigs aos peers via P2P)

Idempotente. Pode chamar várias vezes. Controlado por BRN_AUTO_WIRE=0 para desligar.
================================================================
"""
from __future__ import annotations

import os
import json
import hashlib
import logging
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("auto_wire")
BASE_DIR = Path(__file__).resolve().parent

__all__ = ["wire_all", "make_default_collector"]


# ============================================================
# DETECÇÃO DE SK
# ============================================================
def _detect_sk():
    """Retorna (Wallet, source_str) ou (None, None)."""
    try:
        from wallet import Wallet
    except ImportError as e:
        log.warning(f"[auto_wire] wallet.py inacessível: {e}")
        return None, None

    # 1. BRN_VALIDATOR_SK_HEX
    sk_hex = os.environ.get("BRN_VALIDATOR_SK_HEX", "").strip()
    if sk_hex:
        if len(sk_hex) == 64 and all(c in "0123456789abcdefABCDEF" for c in sk_hex):
            try:
                return Wallet(private_key_hex=sk_hex), "BRN_VALIDATOR_SK_HEX"
            except Exception as e:
                log.warning(f"[auto_wire] SK_HEX inválido: {e}")
        else:
            log.warning("[auto_wire] BRN_VALIDATOR_SK_HEX setado mas não tem 64 hex")

    # 2. BRN_VALIDATOR_SEED
    seed = os.environ.get("BRN_VALIDATOR_SEED", "").strip()
    if seed:
        try:
            h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
            return Wallet(private_key_hex=h), "BRN_VALIDATOR_SEED"
        except Exception as e:
            log.warning(f"[auto_wire] SEED falhou: {e}")

    # 3. .validator_sk
    sk_file = BASE_DIR / ".validator_sk"
    if sk_file.exists():
        try:
            content = sk_file.read_text(encoding="utf-8").strip().split("\n")[0].strip()
            if len(content) == 64:
                return Wallet(private_key_hex=content), ".validator_sk"
        except Exception as e:
            log.debug(f"[auto_wire] .validator_sk ilegível: {e}")

    # 4. current_wallet.enc (senha no env)
    password = (
        os.environ.get("BRN_NODE_PASSWORD", "").strip()
        or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
    )
    if password:
        try:
            from secure_store import load_current_wallet
            r = load_current_wallet(password)
            if r and r.get("ok"):
                priv = (r.get("private_key") or r.get("priv_hex") or "").strip()
                if len(priv) == 64:
                    return Wallet(private_key_hex=priv), "current_wallet.enc"
        except Exception as e:
            log.debug(f"[auto_wire] current_wallet.enc não decifrou: {e}")

    # 5. current_wallet.json (legado)
    json_file = BASE_DIR / "current_wallet.json"
    if json_file.exists():
        try:
            with json_file.open("r", encoding="utf-8") as f:
                d = json.load(f)
            priv = (d.get("private_key") or d.get("priv_hex") or "").strip()
            if len(priv) == 64:
                return Wallet(private_key_hex=priv), "current_wallet.json"
        except Exception as e:
            log.debug(f"[auto_wire] current_wallet.json ilegível: {e}")

    return None, None


# ============================================================
# DETECÇÃO DE TRUST NETWORK
# ============================================================
def _detect_trust():
    """Retorna (trust_obj, source_str) ou (None, None)."""
    path = os.environ.get("BRN_TRUST_NETWORK", "").strip()
    if not path:
        default = BASE_DIR / "trust_network.json"
        if default.exists():
            path = str(default)

    if path:
        try:
            from trust_network import TrustNetwork
            t = TrustNetwork(path)
            return t, f"TrustNetwork({Path(path).name})"
        except ImportError:
            log.warning("[auto_wire] trust_network.py não encontrado")
        except Exception as e:
            log.warning(f"[auto_wire] TrustNetwork falhou: {e}")

    if os.environ.get("BRN_TRUST_FALLBACK", "0") == "1":
        try:
            from trust_network import TrustNetwork
            t = TrustNetwork()
            return t, "TrustNetwork() (vazio)"
        except Exception:
            return _StubTrustNetwork(), "_StubTrustNetwork"
    return None, None


class _StubTrustNetwork:
    """Trust mínimo que lê validadores dos peers do DB.
    Não faz score, ban, nem nada — só evita que `_get_active_validators` retorne [].
    """
    def __init__(self):
        self.nodes = {}

    def _db_pubkeys(self):
        try:
            from blockchain import Blockchain
            # Hack: usa o DB direto
            from db import ChainDB
            db = ChainDB(str(BASE_DIR / "brn_v2_chain.db"))
            peers = db.listar_peers(apenas_ativos=False) or []
            pks = []
            for p in peers:
                if not isinstance(p, dict):
                    continue
                pk = (p.get("public_key") or p.get("node_id") or "").strip().lower()
                if len(pk) >= 32:
                    pks.append(pk)
            return pks
        except Exception:
            return []

    def get_top_validators(self, count=5):
        return self._db_pubkeys()[:count]

    def get_active_validators(self, count=5):
        return self._db_pubkeys()[:count]

    def record_block_signed(self, *a, **kw):
        pass


# ============================================================
# SIGN COLLECTOR — pede sigs aos peers via P2P
# ============================================================
def make_default_collector(chain):
    """Cria collector que:
      1. Assina localmente com a SK do chain
      2. Pergunta a cada peer P2P: 'assina esse bloco?'
      3. Retorna as sigs válidas
    """
    def collector(block: dict) -> list:
        p2p = getattr(chain, "p2p", None)
        if p2p is None:
            log.debug("[collector] p2p não injetado — só assina local")
            sigs = []
            local_sk = getattr(chain, "_local_validator_sk", None)
            if local_sk is not None:
                try:
                    from blockchain import sign_block_as_validator
                    entry = sign_block_as_validator(block, local_sk)
                    if entry:
                        sigs.append(entry)
                except Exception as e:
                    log.warning(f"[collector] assinatura local falhou: {e}")
            return sigs

        try:
            validators = chain._get_active_validators()
        except Exception:
            validators = []
        if not validators:
            return []

        # Pega peers do P2P
        peers: list = []
        try:
            peers = p2p.discovery.listar_peers()
        except Exception:
            try:
                stats = p2p.get_peer_stats()
                peers = [f"{p['ip']}:{p.get('port', 6001)}" for p in stats]
            except Exception:
                peers = []

        collected = []
        seen_pks = set()

        # 1. Assinatura local primeiro (mais rápida e garante pelo menos 1)
        local_sk = getattr(chain, "_local_validator_sk", None)
        if local_sk is not None:
            try:
                from blockchain import sign_block_as_validator
                entry = sign_block_as_validator(block, local_sk)
                if entry and entry.get("pubkey"):
                    collected.append(entry)
                    seen_pks.add(entry["pubkey"].strip().lower())
            except Exception as e:
                log.warning(f"[collector] local assinatura falhou: {e}")

        # 2. Pede para os peers em paralelo
        if peers:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def ask(peer_str):
                try:
                    ip, port_s = peer_str.rsplit(":", 1)
                    port = int(port_s)
                    return p2p.request_block_signature(
                        ip, port, block, timeout=5.0
                    )
                except Exception:
                    return None

            try:
                with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
                    futures = {pool.submit(ask, p): p for p in peers[:16]}
                    for fut in as_completed(futures, timeout=10.0):
                        try:
                            entry = fut.result(timeout=0.5)
                        except Exception:
                            continue
                        if not entry:
                            continue
                        pk = (entry.get("pubkey") or "").strip().lower()
                        if not pk or pk in seen_pks:
                            continue
                        # Só aceita se estiver na lista de validadores (opcional)
                        seen_pks.add(pk)
                        collected.append(entry)
            except Exception as e:
                log.debug(f"[collector] parallel ask falhou: {e}")

        log.info("[collector] %d assinatura(s) coletada(s) de %d peer(s)",
                 len(collected), len(peers))
        return collected

    return collector


# ============================================================
# WIRING PRINCIPAL
# ============================================================
def wire_all(chain, verbose: bool = True) -> dict:
    """Injeta tudo que puder. Idempotente."""
    if os.environ.get("BRN_AUTO_WIRE", "1") != "1":
        return {"skipped": "BRN_AUTO_WIRE=0"}

    result = {
        "trust_net": None,
        "sk_source": None,
        "sign_collector": False,
        "warnings": [],
    }

    # 1. TrustNetwork
    if getattr(chain, "trust_net", None) is None:
        trust, src = _detect_trust()
        if trust is not None:
            try:
                chain.set_trust_net(trust)
                result["trust_net"] = src
            except Exception as e:
                result["warnings"].append(f"set_trust_net: {e}")
        else:
            result["warnings"].append(
                "trust_network.py ausente e BRN_TRUST_FALLBACK != 1"
            )

    # 2. SK local
    if getattr(chain, "_local_validator_sk", None) is None:
        sk, src = _detect_sk()
        if sk is not None:
            try:
                chain.set_local_validator_sk(sk)
                result["sk_source"] = src
            except Exception as e:
                result["warnings"].append(f"set_local_validator_sk: {e}")
        else:
            result["warnings"].append(
                "nenhuma SK encontrada (BRN_VALIDATOR_SK_HEX, .validator_sk, "
                "current_wallet.enc, current_wallet.json)"
            )

    # 3. Sign collector (precisa do p2p — pode chegar depois)
    if (getattr(chain, "_sign_collector", None) is None
            and getattr(chain, "p2p", None) is not None):
        try:
            chain.set_sign_collector(make_default_collector(chain))
            result["sign_collector"] = True
        except Exception as e:
            result["warnings"].append(f"set_sign_collector: {e}")

    # 4. P2P global (se server.py já criou)
    if getattr(chain, "p2p", None) is None:
        try:
            from server import _get_p2p_manager
            p2p = _get_p2p_manager()
            if p2p is not None:
                chain.set_p2p(p2p)
                # tenta collector de novo
                if getattr(chain, "_sign_collector", None) is None:
                    chain.set_sign_collector(make_default_collector(chain))
                    result["sign_collector"] = True
        except Exception:
            pass

    if verbose:
        _log_result(chain, result)

    return result


def _log_result(chain, r: dict) -> None:
    trust = getattr(chain, "trust_net", None)
    sk = getattr(chain, "_local_validator_sk", None)
    collector = getattr(chain, "_sign_collector", None)
    p2p = getattr(chain, "p2p", None)

    n_validators = 0
    if trust is not None:
        try:
            n_validators = len(chain._get_active_validators())
        except Exception:
            n_validators = -1

    log.warning(
        "[auto_wire] trust=%s sk=%s collector=%s p2p=%s validators=%d",
        type(trust).__name__ if trust else "None",
        r.get("sk_source") or ("OK" if sk else "None"),
        "OK" if collector else "None",
        type(p2p).__name__ if p2p else "None",
        n_validators,
    )
    for w in r.get("warnings", []):
        log.warning("[auto_wire] ⚠ %s", w)
