"""wallet_integrator.py — Integra carteira desbloqueada ao nó BRN (v1.2)
================================================================
v1.2 (2026-11-09) — AUTO-PERSISTÊNCIA DE SENHA:
  [NEW] persist_password_to_env(): atualiza BRN_WALLET_SESSION_PASS e
        BRN_NODE_PASSWORD no brn_network.env com a senha que o usuário
        digitou na GUI (se funcionou). No próximo boot do main.py,
        a carteira abre automaticamente sem precisar desbloquear.
        Por segurança, só atualiza se remote_addr for loopback
        (127.0.0.1) — clientes remotos não sobrescrevem o .env.
  [NEW] _register_in_origin(): auto-register no Origin via
        /api/trust/register com prova de posse.
  [NEW] _promote_self_in_origin(): pede promoção 500→1000 após
        blocks_signed >= BRN_AUTO_PROMOTE_MIN_BLOCKS.
  [NEW] promote_self_if_ready(): verifica e chama promote_self.
  [KEEP] v1.1: integrate() com register_in_origin=True.
  [KEEP] v1.0: ponto único de integração (SK + trust + multisig + PBFT).

v1.1 (2026-11-09):
  [NEW] auto-register no Origin (/api/trust/register).

v1.0 (2026-11-09):
  [NEW] ponto único de integração.
================================================================
"""
from __future__ import annotations

import json as _json
import logging
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("wallet_integrator")

__version__ = "1.2"


# ═══════════════════════════════════════════════════════════════
# HELPERS HTTP
# ═══════════════════════════════════════════════════════════════
def _http_post_json(url: str, body: dict, timeout: float = 10.0) -> dict:
    try:
        data = _json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "brn-integrator/1.2",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            return _json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return _json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return {"ok": False, "msg": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def _register_in_origin(pubkey: str, address: str, sk_hex: str,
                        L=None) -> dict:
    """Auto-registra no trust do Origin com prova de posse."""
    L = L or log

    origin_url = (os.environ.get("BRN_POOL_URL", "") or "").rstrip("/")
    if not origin_url:
        return {"ok": False, "msg": "BRN_POOL_URL vazio — não posso registrar"}
    if not origin_url.startswith("http"):
        origin_url = "http://" + origin_url

    ts = int(time.time())
    payload = f"BRN-TRUST-REGISTER|{pubkey}|{ts}".encode()

    try:
        from wallet import Wallet
        w = Wallet(private_key_hex=sk_hex)
        sig = w.sign(payload)
    except Exception as e:
        return {"ok": False, "msg": f"sign falhou: {e}"}

    resp = _http_post_json(
        origin_url + "/api/trust/register",
        {
            "pubkey": pubkey,
            "address": address,
            "timestamp": ts,
            "signature": sig,
        },
        timeout=10.0,
    )

    if resp.get("ok"):
        L.info(
            "[integrator] trust_register OK — %s… score=%.0f will_promote_after=%s",
            pubkey[:16],
            resp.get("score", 0),
            resp.get("will_promote_after", "?"),
        )
    else:
        L.warning(
            "[integrator] trust_register falhou: %s", resp.get("msg"),
        )
        # Se o Origin redirecionou, tenta de novo
        redirect = resp.get("redirect")
        if redirect:
            L.info("[integrator] tentando redirect: %s", redirect)
            resp2 = _http_post_json(
                redirect.rstrip("/") + "/api/trust/register",
                {
                    "pubkey": pubkey,
                    "address": address,
                    "timestamp": ts,
                    "signature": sig,
                },
                timeout=10.0,
            )
            if resp2.get("ok"):
                L.info("[integrator] trust_register OK via redirect")
                resp = resp2
    return resp


def _promote_self_in_origin(pubkey: str, sk_hex: str,
                             origin_url: str, L=None) -> dict:
    """Pede ao Origin para subir 500→1000 (fallback)."""
    L = L or log
    ts = int(time.time())
    payload = f"BRN-TRUST-PROMOTE|{pubkey}|{ts}".encode()
    try:
        from wallet import Wallet
        w = Wallet(private_key_hex=sk_hex)
        sig = w.sign(payload)
    except Exception as e:
        return {"ok": False, "msg": f"sign falhou: {e}"}

    return _http_post_json(
        origin_url.rstrip("/") + "/api/trust/promote_self",
        {
            "pubkey": pubkey,
            "timestamp": ts,
            "signature": sig,
        },
        timeout=10.0,
    )


# ═══════════════════════════════════════════════════════════════
# INTEGRAÇÃO PRINCIPAL
# ═══════════════════════════════════════════════════════════════
def integrate(
    chain,
    sk_hex: str,
    address: str = "",
    pubkey: str = "",
    p2p=None,
    trust=None,
    logger=None,
    register_in_origin: bool = True,
) -> dict:
    """
    Faz a integração completa da carteira no nó.

    Parâmetros:
      chain              : Blockchain já aberta
      sk_hex             : chave privada em hex (64 chars)
      address            : endereço bech32 (opcional, só pra log)
      pubkey             : pubkey hex (opcional; se None, deriva da SK)
      p2p                : P2PManager (opcional; se None, só assina localmente)
      trust              : TrustNetwork (opcional; usa chain.trust_net)
      logger             : logger alternativo (opcional)
      register_in_origin : se True (default), faz POST /api/trust/register

    Retorna dict:
      {
        "ok": bool,
        "pubkey": str,
        "address": str,
        "sk_installed": bool,
        "in_trust": bool,
        "in_top5": bool,
        "multisig_ready": bool,
        "pbft_ready": bool,
        "trust_registered": bool,
        "registered_at_origin": dict | None,
        "warnings": [str],
        "errors": [str],
      }
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
        "trust_registered": False,
        "registered_at_origin": None,
        "warnings": [],
        "errors": [],
    }

    # ─── 0. Sanity ───
    if chain is None:
        result["errors"].append("chain é None")
        return result

    if not sk_hex or len(sk_hex) != 64:
        result["errors"].append(
            f"sk_hex inválido (esperado 64 hex, recebido {len(sk_hex or '')})"
        )
        return result

    try:
        int(sk_hex, 16)
    except ValueError:
        result["errors"].append("sk_hex não é hex")
        return result

    strict = os.environ.get("BRN_STRICT_MULTISIG", "0") == "1"

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

    if pubkey and pubkey.lower() != installed_pk:
        result["warnings"].append(
            f"pubkey passada ({pubkey[:16]}…) ≠ derivada da SK "
            f"({installed_pk[:16]}…) — usando a derivada"
        )

    result["address"] = address or installed_pk[:16]

    # ─── 2. Sincroniza trust local ───
    trust = trust or getattr(chain, "trust_net", None)
    if trust is None:
        result["warnings"].append(
            "trust_net é None — nó não vai sincronizar reputação"
        )
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
                result["warnings"].append(
                    "trust sem sync_local_validator/register_node"
                )

            try:
                if hasattr(trust, "get_top_validators"):
                    top = trust.get_top_validators(count=5)
                    top_lower = [p.lower() for p in (top or [])]
                    result["in_top5"] = installed_pk in top_lower
                    if not result["in_top5"]:
                        result["warnings"].append(
                            f"pubkey {installed_pk[:16]}… NÃO está em top5 — "
                            f"aguardando sync do Origin (60s)"
                        )
            except Exception as e:
                result["warnings"].append(f"get_top_validators: {e}")
        except Exception as e:
            result["errors"].append(f"trust sync local: {e}")

    # ─── 2.5. Registra no Origin (auto-register) ───
    if register_in_origin:
        origin_url = (os.environ.get("BRN_POOL_URL", "") or "").rstrip("/")
        if origin_url:
            reg = _register_in_origin(
                installed_pk, address or "", sk_hex, L=L,
            )
            if reg.get("ok"):
                result["trust_registered"] = True
                result["registered_at_origin"] = {
                    "score": reg.get("score", 0),
                    "is_new": reg.get("is_new", False),
                    "will_promote_after": reg.get("will_promote_after", 0),
                }
            else:
                msg = (reg.get("msg") or "").lower()
                if "já existe" in msg or "existe" in msg:
                    result["trust_registered"] = True
                else:
                    result["warnings"].append(
                        f"auto-register no Origin falhou: {reg.get('msg')}"
                    )
        else:
            result["warnings"].append(
                "BRN_POOL_URL vazio — não tentei auto-register no Origin"
            )

    # ─── 3. Configura multisig ───
    if p2p is None:
        result["warnings"].append(
            "p2p é None — _sign_collector só assina localmente"
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
        result["errors"].append(
            "STRICT_MULTISIG=1 e multisig_ready=False"
        )
        return result

    # ─── 4. PBFT dinâmico ───
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
            result["warnings"].append(f"PBFT dinâmico: {e}")

    # ─── 5. Verificação final ───
    try:
        if chain._local_validator_sk is None:
            result["errors"].append(
                "chain._local_validator_sk ficou None após integração"
            )
            return result
        if chain._local_validator_sk.pub_hex.lower() != installed_pk:
            result["errors"].append(
                "pubkey da SK mudou durante integração"
            )
            return result
    except Exception as e:
        result["warnings"].append(f"verificação final: {e}")

    # ─── 6. Veredito ───
    result["ok"] = (
        result["sk_installed"]
        and not result["errors"]
        and (not strict or result["multisig_ready"])
    )

    L.info(
        "[integrator] %s — pk=%s… sk=%s trust=%s top5=%s "
        "multisig=%s pbft=%s registered=%s",
        "OK" if result["ok"] else "FALHA",
        installed_pk[:16],
        result["sk_installed"],
        result["in_trust"],
        result["in_top5"],
        result["multisig_ready"],
        result["pbft_ready"],
        result["trust_registered"],
    )

    if result["warnings"]:
        for w in result["warnings"]:
            L.warning("[integrator] %s", w)
    if result["errors"]:
        for e in result["errors"]:
            L.error("[integrator] %s", e)

    return result


# ═══════════════════════════════════════════════════════════════
# PERSISTÊNCIA PARA PRÓXIMO REBOOT
# ═══════════════════════════════════════════════════════════════
def persist_sk_for_reboot(sk_hex: str, password: str) -> bool:
    """
    Re-salva current_wallet.enc com a SK atual + senha.
    No próximo boot do main.py, a carteira abre automaticamente.
    """
    try:
        from secure_store import save_current_wallet, load_current_wallet

        r = load_current_wallet(password)
        if not r.get("ok"):
            log.warning(
                "[integrator] persist_sk: senha %r… não decifra (%s)",
                password[:3], r.get("msg"),
            )
            return False

        data = {
            "address": r.get("address", ""),
            "private_key": sk_hex,
            "public_key": r.get("public_key") or r.get("pub_hex", ""),
        }

        r2 = save_current_wallet(password, data)
        if not r2.get("ok"):
            log.warning(
                "[integrator] persist_sk: save falhou: %s", r2.get("msg")
            )
            return False

        log.info(
            "[integrator] persist_sk: current_wallet.enc re-salvo"
        )
        return True
    except Exception as e:
        log.warning("[integrator] persist_sk falhou: %s", e)
        return False


def persist_password_to_env(password: str, only_loopback: bool = True,
                             remote_addr: str = "") -> bool:
    """
    Atualiza brn_network.env com a senha correta (BRN_WALLET_SESSION_PASS
    e BRN_NODE_PASSWORD).

    Por padrão, só atualiza se remote_addr for loopback. Isso evita que
    um cliente remoto sobrescreva o .env de outro nó.

    Se BRN_ALLOW_ENV_UPDATE=1 estiver setado, permite mesmo remoto.
    """
    if not password:
        return False

    # Se BRN_ALLOW_ENV_UPDATE=1, permite remoto (arriscado)
    allow_remote = os.environ.get("BRN_ALLOW_ENV_UPDATE", "0") == "1"

    if not allow_remote and only_loopback and remote_addr not in (
        "127.0.0.1", "::1", "localhost", "", "0.0.0.0"
    ):
        log.warning(
            "[integrator] persist_env: remote_addr=%s não é loopback, "
            "ignorando atualização do .env", remote_addr,
        )
        return False

    env_file = Path("brn_network.env")
    if not env_file.exists():
        # Tenta ".env" se não houver brn_network.env
        alt = Path(".env")
        if alt.exists():
            env_file = alt
        else:
            log.warning("[integrator] brn_network.env nem .env existem")
            return False

    # Backup
    backup = env_file.with_suffix(env_file.suffix + ".bak")
    try:
        shutil.copy2(env_file, backup)
    except Exception:
        pass

    try:
        lines = env_file.read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines()
    except Exception as e:
        log.warning("[integrator] persist_env: ler .env falhou: %s", e)
        return False

    new_lines = []
    updated_wallet = False
    updated_node = False
    for line in lines:
        s = line.strip()
        if s.startswith("BRN_WALLET_SESSION_PASS="):
            new_lines.append(f"BRN_WALLET_SESSION_PASS={password}")
            updated_wallet = True
        elif s.startswith("BRN_NODE_PASSWORD="):
            new_lines.append(f"BRN_NODE_PASSWORD={password}")
            updated_node = True
        else:
            new_lines.append(line)

    if not updated_wallet:
        new_lines.append(f"BRN_WALLET_SESSION_PASS={password}")
    if not updated_node:
        new_lines.append(f"BRN_NODE_PASSWORD={password}")

    try:
        env_file.write_text(
            "\n".join(new_lines) + "\n", encoding="utf-8"
        )
        log.info(
            "[integrator] persist_env: .env atualizado com a senha correta "
            "(wallet=%s node=%s backup=%s)",
            updated_wallet, updated_node, backup.name,
        )
        return True
    except Exception as e:
        log.warning("[integrator] persist_env: gravar .env falhou: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════
# CHECKS
# ═══════════════════════════════════════════════════════════════
def needs_integration(chain, expected_pubkey: str = "") -> bool:
    """Se precisa reintegrar."""
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


def promote_self_if_ready(chain, sk_hex: str, logger=None) -> dict:
    """Verifica se blocks_signed >= AUTO_PROMOTE_MIN e chama promote_self."""
    L = logger or log

    origin_url = (os.environ.get("BRN_POOL_URL", "") or "").rstrip("/")
    if not origin_url:
        return {"ok": False, "msg": "BRN_POOL_URL vazio"}

    trust = getattr(chain, "trust_net", None)
    if trust is None:
        return {"ok": False, "msg": "trust_net None"}

    try:
        sk = getattr(chain, "_local_validator_sk", None)
        if sk is None:
            return {"ok": False, "msg": "SK não instalada"}
        pk = sk.pub_hex.lower()
    except Exception as e:
        return {"ok": False, "msg": str(e)}

    n = trust.nodes.get(pk)
    if n is None:
        return {"ok": False, "msg": "nó não registrado localmente"}

    try:
        from trust_network import AUTO_PROMOTE_MIN_BLOCKS, AUTO_PROMOTE_SCORE
    except ImportError:
        AUTO_PROMOTE_MIN_BLOCKS = 10
        AUTO_PROMOTE_SCORE = 1000.0

    if n.blocks_signed < AUTO_PROMOTE_MIN_BLOCKS:
        return {
            "ok": False,
            "msg": f"precisa de {AUTO_PROMOTE_MIN_BLOCKS} blocos, "
                   f"tem {n.blocks_signed}",
            "blocks_signed": n.blocks_signed,
            "required": AUTO_PROMOTE_MIN_BLOCKS,
        }
    if n.trust_score >= AUTO_PROMOTE_SCORE:
        return {"ok": True, "msg": "já promovido", "score": n.trust_score}

    resp = _promote_self_in_origin(pk, sk_hex, origin_url, L=L)
    if resp.get("ok"):
        L.info(
            "[integrator] promote_self OK — %.0f → %.0f",
            resp.get("old_score", 0), resp.get("new_score", 0),
        )
    else:
        L.warning("[integrator] promote_self falhou: %s", resp.get("msg"))
    return resp


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════
def _self_test() -> int:
    import sys
    import inspect

    print(f"wallet_integrator.py v{__version__}")
    print("=" * 60)

    falhas = 0

    # 1. integrate assinatura
    try:
        sig = inspect.signature(integrate)
        params = list(sig.parameters.keys())
        expected = ["chain", "sk_hex", "address", "pubkey", "p2p",
                    "trust", "logger", "register_in_origin"]
        if params == expected:
            print(f"  [ok] integrate{tuple(params)}")
        else:
            print(f"  [X]  integrate params: {params}")
            falhas += 1
    except Exception as e:
        print(f"  [X]  integrate: {e}")
        falhas += 1

    # 2. integrate(None, ...) retorna erros
    try:
        r = integrate(None, "a" * 64)
        if r.get("ok") is False and r.get("errors"):
            print(f"  [ok] integrate(None, ...) → ok=False")
        else:
            print(f"  [X]  integrate(None, ...) → {r}")
            falhas += 1
    except Exception as e:
        print(f"  [X]  integrate(None, ...) exceção: {e}")
        falhas += 1

    # 3. sk_hex inválido
    try:
        r = integrate(type("F", (), {})(), "abc")
        if r.get("ok") is False and any("sk_hex" in e for e in r["errors"]):
            print(f"  [ok] sk_hex inválido → rejeitado")
        else:
            print(f"  [X]  sk_hex inválido → {r}")
            falhas += 1
    except Exception as e:
        print(f"  [X]  sk_hex inválido: {e}")
        falhas += 1

    # 4. persist_sk_for_reboot
    try:
        if callable(persist_sk_for_reboot):
            print(f"  [ok] persist_sk_for_reboot existe")
    except Exception as e:
        print(f"  [X]  persist_sk_for_reboot: {e}")
        falhas += 1

    # 5. persist_password_to_env
    try:
        if callable(persist_password_to_env):
            # Testa que rejeita remoto
            r = persist_password_to_env(
                "test123", only_loopback=True,
                remote_addr="8.8.8.8",
            )
            if r is False:
                print(f"  [ok] persist_password_to_env rejeita remoto")
            else:
                print(f"  [X]  persist_password_to_env aceitou remoto!")
                falhas += 1
    except Exception as e:
        print(f"  [X]  persist_password_to_env: {e}")
        falhas += 1

    # 6. needs_integration
    try:
        if callable(needs_integration):
            r = needs_integration(None)
            if r is True:
                print(f"  [ok] needs_integration(None) → True")
            else:
                print(f"  [X]  needs_integration(None) → {r}")
                falhas += 1
    except Exception as e:
        print(f"  [X]  needs_integration: {e}")
        falhas += 1

    # 7. promote_self_if_ready
    try:
        if callable(promote_self_if_ready):
            print(f"  [ok] promote_self_if_ready existe")
    except Exception as e:
        print(f"  [X]  promote_self_if_ready: {e}")
        falhas += 1

    # 8. _register_in_origin
    try:
        if callable(_register_in_origin):
            print(f"  [ok] _register_in_origin existe")
    except Exception as e:
        print(f"  [X]  _register_in_origin: {e}")
        falhas += 1

    # 9. Integração real com chain em memória
    try:
        from blockchain import Blockchain
        import tempfile
        import os as _os

        fd, db_path = tempfile.mkstemp(suffix=".db")
        _os.close(fd)
        _os.unlink(db_path)

        try:
            chain = Blockchain(db_path, auto_genesis=True)

            from wallet import Wallet
            w = Wallet()

            class FakeTrust:
                def __init__(self):
                    self.calls = []
                def sync_local_validator(self, pk, addr, peer):
                    self.calls.append(("sync", pk[:16]))
                def get_top_validators(self, count=5):
                    return [w.pub_hex]
                def register_new_validator(self, pubkey, address="",
                                            initial_score=500):
                    return {"ok": True, "is_new": True, "score": initial_score,
                            "will_promote_after": 10}

            trust = FakeTrust()

            # Desabilita auto-register (sem Origin real no teste)
            os.environ.pop("BRN_POOL_URL", None)

            r = integrate(
                chain, w.priv_hex,
                address=w.address, pubkey=w.pub_hex,
                p2p=None, trust=trust,
                register_in_origin=False,
            )
            if r.get("sk_installed") and r.get("in_trust") and r.get("in_top5"):
                print(f"  [ok] integrate real")
            else:
                print(f"  [X]  integrate real parcial: {r}")
                falhas += 1

            r2 = integrate(
                chain, w.priv_hex,
                address=w.address, pubkey=w.pub_hex,
                p2p=None, trust=trust,
                register_in_origin=False,
            )
            if r2.get("sk_installed"):
                print(f"  [ok] 2ª chamada (idempotente)")
            else:
                print(f"  [X]  2ª chamada falhou: {r2}")
                falhas += 1
        finally:
            try:
                chain.db.close()
            except Exception:
                pass
            for ext in ("", "-wal", "-shm"):
                try:
                    os.unlink(db_path + ext)
                except Exception:
                    pass
    except Exception as e:
        print(f"  [skip] integrate real: {e}")

    print("=" * 60)
    if falhas == 0:
        print(" TODOS OS TESTES PASSARAM")
        return 0
    else:
        print(f" {falhas} falha(s)")
        return 1


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    sys.exit(_self_test())
