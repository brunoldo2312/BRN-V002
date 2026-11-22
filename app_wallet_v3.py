"""app_wallet_v3.py — Ponte entre a GUI pywebview e o nó BRN (v3.23)
================================================================
v3.23 (2026-11-22) — VERIFICAÇÃO REAL DE SK (COMPATÍVEL v9.9.40):
  [FIX] _verify_sk_on_node() usava /api/consensus/validators como fonte
        primária — endpoint que NÃO EXISTE no server.py v9.9.40. Caía
        em "unavailable" sem nunca confirmar a SK.
  [NEW] _verify_sk_on_node() agora cascateia:
          /api/mine/gate  -> /api/status -> /health
        (todos existem no server v9.9.40; o primeiro é a fonte de verdade
        do gate de mineração).
  [NEW] Retorna sign_collector_ready, mining_allowed, gate_reason,
        fast_sync_active na resposta de unlock/load_wallet/create.
        Se sign_collector_ready=False, o usuário sabe que o nó não
        assinará blocos MESMO com sk_installed=True.
  [NEW] _verify_sk_on_node() é chamado SEMPRE após _sync_sk_to_chain,
        não só quando o sync falha. Confirma contra 3 fontes.
  [NEW] get_debug_info() inclui mining_gate completo (mining_allowed,
        reason, detail) — para colar em issue/chat quando travar.
  [KEEP] v3.22: guard API_BASE remoto, unlock_wallet honesto, retry_sk_sync,
        _verify_sk_on_node, --verify-sk, --self-test.
================================================================
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

__version__ = "3.23"

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    requests = None
    _HAS_REQUESTS = False


log = logging.getLogger("wallet-bridge")

BASE_DIR = Path(__file__).resolve().parent


# ============================================================
# [v3.22] RESOLUÇÃO SEGURA DE API_BASE
# ============================================================
def _is_running_on_render() -> bool:
    """Detecta se estamos rodando no Render (que injeta essas envs)."""
    return bool(
        os.environ.get("RENDER")
        or os.environ.get("RENDER_SERVICE_ID")
        or os.environ.get("RENDER_EXTERNAL_URL")
    )


def _is_remote_url(url: str) -> bool:
    """True se a URL NÃO aponta pra localhost/127.0.0.1/0.0.0.0."""
    u = (url or "").strip().lower()
    if not u:
        return False
    return not any(
        h in u for h in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]")
    )


def _resolve_api_base() -> tuple:
    """
    Resolve BRN_API_BASE com guard contra apontar pro Render sem querer.

    Retorna (url, source):
        url     — URL final (sempre termina com rstrip)
        source  — "env" (usado como está), "env-forced-local" (override),
                  "local-default" (sem BRN_API_BASE, assume local)
    """
    raw = (os.environ.get("BRN_API_BASE", "") or "").strip()
    allow_remote = os.environ.get("BRN_API_BASE_ALLOW_REMOTE", "0") == "1"
    on_render = _is_running_on_render()

    port = (
        os.environ.get("BRN_WEB_PORT")
        or os.environ.get("PORT")
        or "5000"
    )
    local_url = f"http://127.0.0.1:{port}"

    if not raw:
        return local_url, "local-default"

    raw = raw.rstrip("/")

    if _is_remote_url(raw):
        if on_render:
            return raw, "env"
        if allow_remote:
            log.warning(
                "[api_base] usando REMOTO por BRN_API_BASE_ALLOW_REMOTE=1: %s",
                raw,
            )
            return raw, "env"
        log.warning(
            "[api_base] BRN_API_BASE=%s aponta pra REMOTO, mas este processo "
            "NÃO está no Render. FORÇANDO %s. "
            "Se você REALMENTE quer conectar em servidor remoto da GUI local, "
            "defina BRN_API_BASE_ALLOW_REMOTE=1 (com cuidado!).",
            raw, local_url,
        )
        return local_url, "env-forced-local"

    return raw, "env"


API_BASE, _API_BASE_SOURCE = _resolve_api_base()
API_TIMEOUT = float(os.environ.get("BRN_API_TIMEOUT", "200"))
GET_TIMEOUT = float(os.environ.get("BRN_API_GET_TIMEOUT", "30"))
MAX_RETRIES = int(os.environ.get("BRN_API_RETRIES", "2"))
RETRY_DELAY = float(os.environ.get("BRN_API_RETRY_DELAY", "0.5"))

_WALLET_SESSION: dict = {}


# ============================================================
# Helpers HTTP
# ============================================================
def _classify_post_error(msg: str) -> str:
    """Classifica a msg de erro de _post em error_code."""
    m = (msg or "").lower()
    if "não instalado" in m or "nao instalado" in m:
        return "no_requests"
    if "offline" in m:
        return "server_offline"
    if "timeout" in m:
        return "timeout"
    if "connection" in m or "connect" in m:
        return "connection"
    if "400" in m or "401" in m or "403" in m or "422" in m:
        return "rejected"
    if "500" in m or "502" in m or "503" in m or "504" in m:
        return "server_error"
    return "exception"


def _sync_sk_to_chain(sk_hex: str, address: str = "",
                      pubkey: str = "") -> dict:
    """
    Instala SK no nó via HTTP /api/wallet/set_validator_sk.
    Retorna dict com error_code classificado + ok + sk_installed
    + todos os campos do server.
    """
    if not sk_hex:
        return {"ok": False, "msg": "sk vazio", "error_code": "empty_sk",
                "sk_installed": False}
    try:
        r = _post("/api/wallet/set_validator_sk", {
            "sk_hex": sk_hex,
            "address": address or "",
            "pubkey":  pubkey or "",
        }, timeout=15.0)
        if not isinstance(r, dict):
            return {"ok": False, "msg": "resposta inválida",
                    "error_code": "bad_response", "sk_installed": False}
        if not r.get("ok"):
            msg = r.get("msg", "") or str(r)
            r["error_code"] = _classify_post_error(msg)
            r.setdefault("sk_installed", False)
            log.warning("_sync_sk_to_chain falhou [%s]: %s",
                        r["error_code"], msg)
            return r
        log.info(
            "_sync_sk_to_chain ok — pk=%s… sk_installed=%s propagated=%s peers=%s",
            (r.get("pubkey") or pubkey or "")[:16],
            r.get("sk_installed"),
            r.get("propagated", False),
            r.get("peers_notified", 0),
        )
        return r
    except Exception as e:
        log.warning("_sync_sk_to_chain exception: %s", e)
        return {"ok": False, "msg": str(e), "error_code": "exception",
                "sk_installed": False}


# ============================================================
# [v3.23] VERIFICAÇÃO REAL DE SK NO NÓ — cascata de fallbacks
# ============================================================
def _verify_sk_on_node(pubkey_hex: str) -> dict:
    """
    Confirma via HTTP que o NÓ realmente tem a SK instalada com essa pubkey
    E que o sign_collector foi criado (para o nó efetivamente assinar).

    Cascata de fontes (todas existem no server v9.9.40):
      1. /api/mine/gate  — fonte de verdade (retorna detail.validators_ready,
                           detail.local_pk, detail.sk_installed, checks)
      2. /api/status     — fallback (retorna validators_ready,
                           validators_reason, mining_allowed, gate_reason)
      3. /health         — último recurso (wallet_sk_installed + validators)

    Nunca levanta exceção. Retorna dict com:
        installed, source, local_pk_full, n_trust, n_active, required_sigs,
        sign_collector_ready, mining_allowed, gate_reason, fast_sync_active,
        bootstrap_would_fire, raw
    """
    out = {
        "installed": False,
        "source": "unavailable",
        "local_pk_full": "",
        "n_trust": 0,
        "n_active": 0,
        "required_sigs": 0,
        "sign_collector_ready": None,   # None = não soubemos
        "mining_allowed": None,
        "gate_reason": "",
        "fast_sync_active": None,
        "bootstrap_would_fire": None,
        "raw": {},
    }
    if not pubkey_hex:
        out["source"] = "no_pubkey"
        return out

    pubkey_hex = pubkey_hex.strip().lower()

    # ---------- Fonte 1: /api/mine/gate (existe no v9.9.40) ----------
    try:
        g = _get("/api/mine/gate?force=1", timeout=5.0, retries=0)
        if isinstance(g, dict) and g.get("ok"):
            detail = g.get("detail") or {}
            out["raw"] = g
            out["mining_allowed"] = bool(g.get("mining_allowed", False))
            out["gate_reason"] = g.get("reason", "") or ""
            out["fast_sync_active"] = bool(detail.get("fast_sync_active", False))

            sk_inst = detail.get("sk_installed")
            local_pk = (detail.get("local_pk") or "").rstrip(".").strip().lower()
            # local_pk vem truncado no gate ("abcd…"); valida por prefixo
            if sk_inst is True and local_pk:
                if pubkey_hex.startswith(local_pk):
                    out["installed"] = True
                    out["source"] = "mine/gate:prefix_match"
                else:
                    out["source"] = f"mine/gate:prefix_mismatch({local_pk})"
                    return out
            elif sk_inst is False:
                out["source"] = "mine/gate:sk_installed=false"
                return out
            elif sk_inst is True and not local_pk:
                # instalado mas sem pk exposta — assume OK se source diz OK
                out["installed"] = True
                out["source"] = "mine/gate:flag_only"

            # sign_collector_ready — o gate não expõe diretamente,
            # mas se mining_allowed=False com reason=no_sk, é sinal claro
            reason = (g.get("reason") or "").lower()
            if reason in ("no_sk", "validators_not_synced"):
                # não conseguimos confirmar collector, mas sk existe e o gate
                # ainda reclama de OUTRA coisa — não é problema nosso
                pass

            # validação de pk OK — retorna
            if out["installed"]:
                # tenta obter sign_collector_ready via /api/status (campo opcional)
                try:
                    st = _get("/api/status", timeout=3.0, retries=0)
                    if isinstance(st, dict):
                        if "sign_collector_ready" in st:
                            out["sign_collector_ready"] = bool(
                                st["sign_collector_ready"])
                except Exception:
                    pass
                return out
    except Exception as e:
        log.debug("_verify_sk_on_node: /api/mine/gate falhou: %s", e)

    # ---------- Fonte 2: /api/status (existe no v9.9.40) ----------
    try:
        st = _get("/api/status", timeout=5.0, retries=0)
        if isinstance(st, dict) and "height" in st:
            out["raw"] = st
            out["validators_ready"] = bool(st.get("validators_ready", False))
            out["mining_allowed"] = bool(st.get("mining_allowed", False))
            out["gate_reason"] = st.get("mining_gate_reason", "") or ""
            out["fast_sync_active"] = bool(st.get("fast_sync_active", False))
            if "sign_collector_ready" in st:
                out["sign_collector_ready"] = bool(
                    st["sign_collector_ready"])

            # Não temos a pubkey local exposta aqui, mas
            # validators_ready=True + mining_allowed=True implica que SK
            # existe (server v9.9.40 marca validators_ready só com SK).
            if out["validators_ready"] and out["mining_allowed"]:
                out["installed"] = True
                out["source"] = "status:validators_ready+mining_allowed"
                return out
            if out["gate_reason"] == "no_sk":
                out["source"] = "status:mining_gate_reason=no_sk"
                return out
            if out["gate_reason"] == "validators_not_synced":
                # SK existe; rede é que não está pronta
                out["installed"] = True
                out["source"] = "status:validators_not_synced(but_sk_exists)"
                return out
    except Exception as e:
        log.debug("_verify_sk_on_node: /api/status falhou: %s", e)

    # ---------- Fonte 3: /health (último recurso) ----------
    try:
        h = _get("/health", timeout=5.0, retries=0)
        if isinstance(h, dict):
            out["raw"] = h
            installed_flag = bool(h.get("wallet_sk_installed"))
            val = h.get("validators") or {}
            local_trunc = (val.get("local_pk") or "").rstrip(".").strip().lower()
            if not installed_flag:
                out["source"] = "health:wallet_sk_installed=false"
                return out
            if local_trunc and pubkey_hex.startswith(local_trunc):
                out["installed"] = True
                out["source"] = "health:prefix_match"
                return out
            if local_trunc and not pubkey_hex.startswith(local_trunc):
                out["source"] = f"health:prefix_mismatch({local_trunc})"
                return out
            if installed_flag and not local_trunc:
                out["installed"] = True
                out["source"] = "health:flag_only"
                return out
    except Exception as e:
        log.debug("_verify_sk_on_node: /health falhou: %s", e)

    if out["source"] == "unavailable":
        out["source"] = "all_endpoints_failed"
    return out


def _get_default_password() -> str:
    return (
        os.environ.get("BRN_PASSWORD", "").strip()
        or os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
        or os.environ.get("BRN_NODE_PASSWORD", "").strip()
    )


def _post(path: str, body: dict, timeout: float = None,
          retries: int = None) -> dict:
    if not _HAS_REQUESTS:
        return {"ok": False, "msg": "requests não instalado (pip install requests)"}

    url = API_BASE + path
    t = timeout or API_TIMEOUT
    r_n = MAX_RETRIES if retries is None else retries

    last_err = ""
    for attempt in range(r_n + 1):
        try:
            r = requests.post(
                url, json=body, timeout=t,
                proxies={"http": None, "https": None},
            )
            try:
                return r.json()
            except Exception:
                return {"ok": False,
                        "msg": f"HTTP {r.status_code} (resposta não-JSON)"}
        except requests.exceptions.ReadTimeout:
            last_err = f"timeout ({t}s)"
        except requests.exceptions.ConnectionError as e:
            last_err = f"connection: {e}"
        except Exception as e:
            last_err = str(e)
            break

        if attempt < r_n:
            time.sleep(RETRY_DELAY * (attempt + 1))

    if "Connection" in last_err or "connection" in last_err:
        return {"ok": False, "msg": "Servidor offline (:5000)"}
    if "timeout" in last_err.lower():
        return {"ok": False, "msg": f"Timeout ({t}s)"}
    return {"ok": False, "msg": last_err or "erro desconhecido"}


def _get(path: str, timeout: float = None,
         retries: int = None) -> dict:
    if not _HAS_REQUESTS:
        return {"error": "requests não instalado (pip install requests)"}

    url = API_BASE + path
    t = timeout or GET_TIMEOUT
    r_n = MAX_RETRIES if retries is None else retries

    last_err = ""
    for attempt in range(r_n + 1):
        try:
            r = requests.get(
                url, timeout=t,
                proxies={"http": None, "https": None},
            )
            try:
                return r.json()
            except Exception:
                return {"error": f"HTTP {r.status_code} (resposta não-JSON)"}
        except requests.exceptions.ReadTimeout:
            return {"error": f"Timeout ({t}s)"}
        except requests.exceptions.ConnectionError as e:
            last_err = str(e)
        except Exception as e:
            return {"error": str(e)}

        if attempt < r_n:
            time.sleep(RETRY_DELAY * (attempt + 1))

    if "Connection" in last_err or "connection" in last_err:
        return {"error": "Servidor offline (:5000)"}
    return {"error": last_err or "erro desconhecido"}


# ============================================================
# Helpers WalletManager
# ============================================================
def _normalize_wallet_entry(w: dict) -> dict:
    name = (
        w.get("name")
        or w.get("nome")
        or w.get("filename")
        or w.get("file")
        or ""
    )
    if name.endswith(".wallet"):
        name = name[:-7]
    elif name.endswith(".enc"):
        name = name[:-4]

    address = w.get("address") or w.get("endereco") or ""
    pk = (
        w.get("public_key")
        or w.get("pubkey")
        or w.get("pub_hex")
        or ""
    )
    return {"name": name, "address": address, "public_key": pk}


def _list_wallet_names() -> list[str]:
    names = []
    try:
        from wallet import WalletManager
    except Exception:
        return names

    lst = None
    for meth in ("list_wallets", "listar_carteiras", "wallets"):
        if hasattr(WalletManager, meth):
            try:
                obj = getattr(WalletManager, meth)
                lst = obj() if callable(obj) else obj
                break
            except Exception as e:
                log.debug("WalletManager.%s falhou: %s", meth, e)

    if lst is None or not isinstance(lst, list):
        return names

    for w in lst:
        if isinstance(w, dict):
            n = _normalize_wallet_entry(w)["name"]
            if n:
                names.append(n)
        elif isinstance(w, str):
            n = w
            if n.endswith(".wallet"):
                n = n[:-7]
            elif n.endswith(".enc"):
                n = n[:-4]
            names.append(n)

    seen = set()
    out = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _try_load_via_wallet_manager(password: str) -> dict | None:
    try:
        from wallet import WalletManager
    except Exception:
        return None

    if not hasattr(WalletManager, "load_encrypted_wallet"):
        return None

    for name in _list_wallet_names():
        try:
            data = WalletManager.load_encrypted_wallet(name, password)
        except Exception as e:
            log.debug("load_encrypted_wallet(%s) falhou: %s", name, e)
            continue

        if not data:
            continue
        if isinstance(data, dict) and data.get("ok") is False:
            continue
        if not isinstance(data, dict):
            continue

        if not data.get("private_key"):
            data["private_key"] = data.get("priv_hex") or data.get("sk") or ""
        if not data.get("public_key"):
            data["public_key"] = data.get("pub_hex") or data.get("pubkey") or ""

        if data.get("private_key") and data.get("address"):
            log.info(
                "[wallet] decifrada via WalletManager: name=%s address=%s…",
                name, data["address"][:16],
            )
            return data

    return None


def _populate_session_from_data(data: dict) -> None:
    global _WALLET_SESSION
    _WALLET_SESSION["address"] = data.get("address", "")
    _WALLET_SESSION["private_key"] = data.get("private_key", "")
    _WALLET_SESSION["public_key"] = (
        data.get("public_key") or data.get("pubkey") or data.get("pub_hex") or ""
    )


def _build_unlock_response(source: str, sync_r: dict,
                            verification: dict | None = None) -> dict:
    """
    Monta a resposta de unlock_wallet/load_wallet/create consolidando
    sync_r + verificação cruzada do nó (v3.23: sempre com 3 fontes).
    """
    sk_installed = bool(sync_r.get("sk_installed", False))
    in_trust = bool(sync_r.get("in_trust", False))
    multisig_ready = bool(sync_r.get("multisig_ready", False))
    propagated = bool(sync_r.get("propagated", False))
    peers_notified = int(sync_r.get("peers_notified", 0) or 0)
    warnings = list(sync_r.get("warnings", []) or [])
    errors = list(sync_r.get("errors", []) or [])

    error_code = sync_r.get("error_code", "")
    msg = sync_r.get("msg", "")

    # [v3.23] SEMPRE verifica no nó (não só quando o sync falha).
    # sync_r pode dizer ok=True mas sk_installed=False (SK aceita mas
    # sem collector). A verificação em cascata pega esse caso.
    verify_pubkey = (_WALLET_SESSION.get("public_key") or "").strip()
    v = {"installed": False, "source": "no_pubkey"}
    if verify_pubkey:
        try:
            v = _verify_sk_on_node(verify_pubkey)
        except Exception as e:
            v = {"installed": False, "source": f"verify_exception:{e}"}

    if v.get("installed"):
        if not sk_installed:
            warnings.append(
                f"SK confirmada no nó via {v.get('source')} "
                f"(sync HTTP dizia sk_installed=False com {error_code or '?'})"
            )
        sk_installed = True
        verification_source = f"cross_check:{v.get('source', '?')}"
    else:
        verification_source = f"cross_check_failed:{v.get('source', '?')}"
        if sk_installed:
            # sync_r diz OK, mas verificação não confirma. Confia no mais
            # conservador: nó pode NÃO assinar.
            warnings.append(
                f"sync HTTP diz sk_installed=True, mas verificação "
                f"({v.get('source')}) NÃO confirmou. Assumindo não instalada."
            )
            sk_installed = False

    if not sk_installed:
        if error_code in ("connection", "timeout", "server_offline", "no_requests"):
            warnings.append(
                f"NÓ NÃO ASSINARÁ — servidor inacessível ({error_code}). "
                f"Verifique se main.py está rodando em :5000 e use "
                f"retry_sk_sync() quando voltar."
            )
        elif error_code:
            errors.append(
                f"SK REJEITADA pelo nó [{error_code}]: {msg or 'sem detalhes'}"
            )
        else:
            warnings.append(
                "SK NÃO foi instalada no nó — nó não vai assinar blocos. "
                "Chame retry_sk_sync() ou reinicie a carteira."
            )

    node_will_sign = bool(sk_installed)

    # [v3.23] Avisa se collector não foi criado (mesmo com SK OK)
    collector_ready = v.get("sign_collector_ready")
    if sk_installed and collector_ready is False:
        warnings.append(
            "SK instalada, mas sign_collector_ready=False no nó. "
            "O nó NÃO vai coletar assinaturas dos peers. "
            "Verifique se blockchain.py ≥ v9.6.0 e reinicie o main.py."
        )
        node_will_sign = False

    return {
        "ok": True,
        "address": _WALLET_SESSION.get("address", ""),
        "source": source,
        "sk_installed": sk_installed,
        "node_will_sign": node_will_sign,
        "verification_source": verification_source,
        "sign_collector_ready": collector_ready,
        "mining_allowed": v.get("mining_allowed"),
        "gate_reason": v.get("gate_reason", ""),
        "fast_sync_active": v.get("fast_sync_active"),
        "in_trust": in_trust,
        "multisig_ready": multisig_ready,
        "propagated": propagated,
        "peers_notified": peers_notified,
        "warnings": warnings,
        "errors": errors,
        "sync_error_code": error_code,
        "verification": v,
        "api_base": API_BASE,
        "api_base_source": _API_BASE_SOURCE,
    }


# ============================================================
# WalletApi
# ============================================================
class WalletApi:
    def __init__(self):
        log.info("WalletApi v%s iniciada (API_BASE=%s source=%s)",
                 __version__, API_BASE, _API_BASE_SOURCE)
        if not _HAS_REQUESTS:
            log.warning("requests NÃO instalado — pip install requests")

    def has_wallet(self) -> dict:
        try:
            from secure_store import CURRENT_WALLET_ENC
            p = Path(CURRENT_WALLET_ENC)
            if not p.is_absolute():
                p = BASE_DIR / p
            root_exists = p.exists()
        except Exception:
            p = BASE_DIR / "current_wallet.enc"
            root_exists = p.exists()

        has_named = len(_list_wallet_names()) > 0

        return {
            "ok": True,
            "exists": root_exists or has_named,
            "root_enc": root_exists,
            "named_wallets": _list_wallet_names(),
            "api_base": API_BASE,
        }

    def unlock_wallet(self, password: str) -> dict:
        global _WALLET_SESSION
        pwd = (password or "").strip() or _get_default_password()
        if not pwd:
            return {"ok": False, "msg": "senha vazia"}

        data = None
        source = None
        try:
            from secure_store import load_current_wallet
            r = load_current_wallet(pwd)
            if r and r.get("ok"):
                data = r
                source = "current_wallet.enc"
        except Exception as e:
            log.debug("load_current_wallet falhou: %s", e)

        if not data:
            data = _try_load_via_wallet_manager(pwd)
            if data:
                source = "WalletManager"
                try:
                    from secure_store import save_current_wallet
                    save_current_wallet(pwd, {
                        "address": data.get("address", ""),
                        "private_key": data.get("private_key", ""),
                        "public_key": data.get("public_key", ""),
                    })
                    log.info("[wallet] current_wallet.enc re-salvo via WalletManager")
                except Exception as e:
                    log.warning("[wallet] não consegui salvar current_wallet.enc: %s", e)

        if not data:
            return {
                "ok": False,
                "msg": (
                    "Senha incorreta em todos os arquivos "
                    "(current_wallet.enc + wallets/*.wallet)"
                ),
            }

        _populate_session_from_data(data)

        sync_r = {}
        if _WALLET_SESSION.get("private_key"):
            sync_r = _sync_sk_to_chain(
                _WALLET_SESSION["private_key"],
                _WALLET_SESSION.get("address", ""),
                _WALLET_SESSION.get("public_key", ""),
            )
        else:
            sync_r = {
                "ok": False, "error_code": "no_private_key",
                "msg": "carteira sem private_key no payload",
                "sk_installed": False,
            }

        log.info(
            "[wallet] unlock OK via %s — address=%s… sk_installed=%s node_will_sign=%s",
            source, _WALLET_SESSION["address"][:16],
            sync_r.get("sk_installed", False),
            sync_r.get("ok") and sync_r.get("sk_installed", False),
        )

        return _build_unlock_response(source or "unknown", sync_r)

    def retry_sk_sync(self) -> dict:
        if not _WALLET_SESSION.get("address"):
            return {"ok": False, "msg": "wallet não desbloqueada"}
        sk = _WALLET_SESSION.get("private_key", "")
        if not sk:
            return {"ok": False, "msg": "sessão sem private_key"}

        log.info("[retry_sk_sync] tentando reinstalar SK para %s…",
                 _WALLET_SESSION["address"][:16])

        sync_r = _sync_sk_to_chain(
            sk,
            _WALLET_SESSION.get("address", ""),
            _WALLET_SESSION.get("public_key", ""),
        )
        return _build_unlock_response("retry", sync_r)

    def create_and_unlock(self, password: str) -> dict:
        global _WALLET_SESSION
        pwd = (password or "").strip() or _get_default_password()
        if not pwd:
            return {"ok": False, "msg": "A senha é obrigatória."}

        try:
            from wallet import Wallet
            from secure_store import save_current_wallet
            w = Wallet()
            data = {
                "address":     w.address,
                "private_key": w.priv_hex,
                "public_key":  w.pub_hex,
            }

            r = save_current_wallet(pwd, data)
            if not r.get("ok"):
                return {"ok": False, "msg": "Erro ao salvar: " + str(r.get("msg", ""))}

            try:
                from wallet import WalletManager
                if hasattr(WalletManager, "save_encrypted_wallet"):
                    try:
                        WalletManager.save_encrypted_wallet(
                            name="default",
                            password=pwd,
                            address=data["address"],
                            sk=data["private_key"],
                            pk=data["public_key"],
                        )
                    except TypeError:
                        try:
                            WalletManager.save_encrypted_wallet(
                                "default", pwd, data["address"],
                                data["private_key"], data["public_key"],
                            )
                        except TypeError:
                            WalletManager.save_encrypted_wallet(
                                "default", pwd, data,
                            )
            except Exception as e:
                log.warning("[wallet] save_encrypted_wallet (best-effort): %s", e)

            try:
                uw_path = BASE_DIR / "user_wallets.json"
                uw = {}
                if uw_path.exists():
                    try:
                        uw = json.loads(uw_path.read_text(encoding="utf-8"))
                        if not isinstance(uw, dict):
                            uw = {}
                    except Exception:
                        uw = {}
                uw[data["address"]] = {"public_key": data["public_key"]}
                uw["default"] = {
                    "address": data["address"],
                    "public_key": data["public_key"],
                }
                uw_path.write_text(
                    json.dumps(uw, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as e:
                log.debug("[wallet] user_wallets.json: %s", e)

            _populate_session_from_data(data)
            sync_r = _sync_sk_to_chain(w.priv_hex, w.address, w.pub_hex)

            return _build_unlock_response("create", sync_r)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def lock_wallet(self) -> dict:
        global _WALLET_SESSION
        _WALLET_SESSION.clear()
        try:
            from wallet import WalletManager
            WalletManager.lock_session()
        except Exception:
            pass
        return {"ok": True}

    def get_active_wallet(self) -> dict:
        if _WALLET_SESSION.get("address"):
            return {"ok": True, **_WALLET_SESSION}
        return {"ok": False, "msg": "locked"}

    def list_wallets(self) -> list:
        entries = []

        try:
            from wallet import WalletManager
            lst = None
            for meth in ("list_wallets", "listar_carteiras", "wallets"):
                if hasattr(WalletManager, meth):
                    try:
                        obj = getattr(WalletManager, meth)
                        lst = obj() if callable(obj) else obj
                        break
                    except Exception:
                        continue

            if isinstance(lst, list):
                for w in lst:
                    if isinstance(w, dict):
                        entries.append(_normalize_wallet_entry(w))
                    elif isinstance(w, str):
                        n = w
                        if n.endswith(".wallet"):
                            n = n[:-7]
                        elif n.endswith(".enc"):
                            n = n[:-4]
                        entries.append({"name": n, "address": "", "public_key": ""})
        except Exception as e:
            log.debug("list_wallets WalletManager: %s", e)

        if not entries:
            wdir = BASE_DIR / "wallets"
            if wdir.exists():
                for p in sorted(wdir.glob("*.wallet")):
                    n = p.stem
                    entries.append({"name": n, "address": "", "public_key": ""})

        if not entries:
            try:
                from secure_store import CURRENT_WALLET_ENC
                p = Path(CURRENT_WALLET_ENC)
                if not p.is_absolute():
                    p = BASE_DIR / p
                if p.exists():
                    entries.append({
                        "name": "(current)",
                        "address": _WALLET_SESSION.get("address", ""),
                        "public_key": _WALLET_SESSION.get("public_key", ""),
                    })
            except Exception:
                pass

        seen = set()
        out = []
        for e in entries:
            if not e.get("name"):
                continue
            key = e["name"]
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
        return out

    def generate_wallet(self, password: str = "") -> dict:
        try:
            from wallet import Wallet
            from secure_store import save_current_wallet
            pwd = (password or "").strip() or _get_default_password()
            if not pwd:
                return {"erro": "Senha não definida"}
            w = Wallet()
            r = save_current_wallet(pwd, {
                "address": w.address,
                "private_key": w.priv_hex,
                "public_key": w.pub_hex,
            })
            if not r.get("ok"):
                return {"erro": f"Falha ao salvar: {r.get('msg')}"}
            _populate_session_from_data({
                "address": w.address,
                "private_key": w.priv_hex,
                "public_key": w.pub_hex,
            })
            sync_r = _sync_sk_to_chain(w.priv_hex, w.address, w.pub_hex)
            resp = _build_unlock_response("generate", sync_r)
            resp.update({
                "address": w.address,
                "private_key": w.priv_hex,
                "public_key": w.pub_hex,
            })
            return resp
        except Exception as e:
            return {"erro": str(e)}

    def save_wallet(self, name: str, password: str,
                    address: str, sk: str, pk: str) -> dict:
        if not name or not name.strip():
            return {"ok": False, "msg": "nome vazio"}
        if not password:
            return {"ok": False, "msg": "senha vazia"}
        if not address or not sk or not pk:
            return {"ok": False, "msg": "carteira vazia (gire/salve antes)"}

        name = name.strip()
        if name.endswith(".wallet"):
            name = name[:-7]

        try:
            from wallet import WalletManager
        except Exception as e:
            return {"ok": False, "msg": f"WalletManager indisponível: {e}"}

        if not hasattr(WalletManager, "save_encrypted_wallet"):
            return {"ok": False, "msg": "WalletManager.save_encrypted_wallet inexistente"}

        erros = []
        try:
            r = WalletManager.save_encrypted_wallet(
                name=name, password=password,
                address=address, sk=sk, pk=pk,
            )
            if r is True or (isinstance(r, dict) and r.get("ok")):
                log.info("[save_wallet] OK via kwargs → wallets/%s.wallet", name)
                return {"ok": True, "msg": f"salvo em wallets/{name}.wallet", "name": name}
            if isinstance(r, dict) and not r.get("ok"):
                erros.append(f"kwargs: {r.get('msg', '?')}")
        except TypeError as e:
            erros.append(f"kwargs: TypeError {e}")
        except Exception as e:
            erros.append(f"kwargs: {type(e).__name__} {e}")

        try:
            r = WalletManager.save_encrypted_wallet(name, password, address, sk, pk)
            if r is True or (isinstance(r, dict) and r.get("ok")):
                log.info("[save_wallet] OK via 5-args → wallets/%s.wallet", name)
                return {"ok": True, "msg": f"salvo em wallets/{name}.wallet", "name": name}
            if isinstance(r, dict) and not r.get("ok"):
                erros.append(f"5-args: {r.get('msg', '?')}")
        except TypeError as e:
            erros.append(f"5-args: TypeError {e}")
        except Exception as e:
            erros.append(f"5-args: {type(e).__name__} {e}")

        try:
            data = {"address": address, "private_key": sk, "public_key": pk}
            r = WalletManager.save_encrypted_wallet(name, password, data)
            if r is True or (isinstance(r, dict) and r.get("ok")):
                log.info("[save_wallet] OK via 3-args → wallets/%s.wallet", name)
                return {"ok": True, "msg": f"salvo em wallets/{name}.wallet", "name": name}
            if isinstance(r, dict) and not r.get("ok"):
                erros.append(f"3-args: {r.get('msg', '?')}")
        except TypeError as e:
            erros.append(f"3-args: TypeError {e}")
        except Exception as e:
            erros.append(f"3-args: {type(e).__name__} {e}")

        try:
            from secure_store import _save_blob
            path = BASE_DIR / "wallets" / f"{name}.wallet"
            path.parent.mkdir(parents=True, exist_ok=True)
            _save_blob(str(path), {
                "address": address, "private_key": sk, "public_key": pk,
            }, password, aad=b"wallet")
            log.info("[save_wallet] OK via _save_blob fallback → %s", path)
            return {"ok": True, "msg": f"salvo em wallets/{name}.wallet (fallback)",
                    "name": name}
        except Exception as e:
            erros.append(f"_save_blob fallback: {type(e).__name__} {e}")

        log.error("[save_wallet] TODAS as tentativas falharam: %s", erros)
        return {"ok": False, "msg": " | ".join(erros[-2:])}

    def load_wallet(self, name: str, password: str) -> dict:
        global _WALLET_SESSION
        if not name:
            return {"ok": False, "msg": "nome vazio"}
        if not password:
            return {"ok": False, "msg": "senha vazia"}

        name = name.strip()
        if name.endswith(".wallet"):
            name = name[:-7]
        elif name.endswith(".enc"):
            name = name[:-4]

        data = None
        source = None

        try:
            from wallet import WalletManager
            r = WalletManager.load_encrypted_wallet(name, password)
            if r and (not isinstance(r, dict) or r.get("ok") is not False):
                if isinstance(r, dict):
                    data = r
                    source = f"WalletManager:{name}"
                else:
                    return r if isinstance(r, dict) else {"ok": True, "address": ""}
            else:
                return r or {"ok": False, "msg": "resposta vazia"}
        except Exception as e_load:
            try:
                from secure_store import _load_blob
                path = BASE_DIR / "wallets" / f"{name}.wallet"
                if path.exists():
                    data = _load_blob(str(path), password, b"wallet")
                    if data and data.get("private_key"):
                        source = f"_load_blob:{name}"
                        log.info("[load_wallet] OK via _load_blob fallback: %s", path)
            except Exception:
                pass
            if not data:
                return {"ok": False, "msg": str(e_load)}

        if not data:
            return {"ok": False, "msg": "carteira carregada mas sem dados"}

        if not data.get("private_key"):
            data["private_key"] = data.get("priv_hex") or data.get("sk") or ""
        if not data.get("public_key"):
            data["public_key"] = data.get("pub_hex") or data.get("pubkey") or ""

        _populate_session_from_data(data)

        sync_r = {}
        if _WALLET_SESSION.get("private_key"):
            sync_r = _sync_sk_to_chain(
                _WALLET_SESSION["private_key"],
                _WALLET_SESSION.get("address", ""),
                _WALLET_SESSION.get("public_key", ""),
            )
        else:
            sync_r = {
                "ok": False, "error_code": "no_private_key",
                "msg": "carteira sem private_key",
                "sk_installed": False,
            }

        return _build_unlock_response(source or "load_wallet", sync_r)

    def get_experiment_variant(self) -> dict:
        try:
            from ab_test import get_variant, get_node_id_hash
            if not _WALLET_SESSION.get("address"):
                return {"variant": "A", "error": "locked"}
            from security import set_node_id_hash
            nh = get_node_id_hash(_WALLET_SESSION["address"])
            set_node_id_hash(nh)
            return {"variant": get_variant(nh), "node_hash": nh}
        except Exception as e:
            return {"variant": "A", "error": str(e)}

    def log_exposure(self, variant: str) -> dict:
        try:
            from security import audit_log
            audit_log("exposure_log", {
                "experiment_id": "activation_v1",
                "variant": variant,
                "trigger_event": "wallet_open",
                "rendered_flag": 1,
            })
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def node_status(self) -> dict:
        try:
            s = _get("/api/status", timeout=GET_TIMEOUT)
            if "error" in s and not s.get("height"):
                return {"ok": False, "msg": s.get("error", "erro")}
            return {
                "ok": True,
                "height": s.get("height", 0),
                "mempool": s.get("mempool", 0),
                "peers": s.get("peers", 0),
                "utxos": s.get("utxos", 0),
                "difficulty": s.get("difficulty", 0),
                "reward": s.get("reward", 0),
                "tip_hash": s.get("tip_hash", ""),
                "fila_tamanho": s.get("fila_tamanho", 0),
                "rede_descentralizada": s.get("rede_descentralizada", False),
                "peer_id": s.get("peer_id", ""),
                "mining_allowed": s.get("mining_allowed", None),
                "mining_gate_reason": s.get("mining_gate_reason", ""),
                "validators_ready": s.get("validators_ready", None),
                "validators_reason": s.get("validators_reason", ""),
                "fast_sync_active": s.get("fast_sync_active", None),
                "api_base": API_BASE,
            }
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def mine_gate(self) -> dict:
        """[v3.23] Endpoint que já existe no server v9.9.40.
        Retorna o gate completo de mineração."""
        try:
            r = _get("/api/mine/gate?force=1", timeout=10.0)
            return r
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def sync_info(self) -> dict:
        try:
            s = _get("/api/sync-info", timeout=GET_TIMEOUT)
            return s if "error" not in s else {"erro": s["error"]}
        except Exception as e:
            return {"erro": str(e)}

    def list_blocks(self, start: int = 0, limit: int = 15) -> dict:
        try:
            s = _get(f"/api/blocks?start={int(start)}&limit={int(limit)}")
            if "error" in s:
                return {"erro": s["error"]}
            return s
        except Exception as e:
            return {"erro": str(e)}

    def get_block(self, height: int) -> dict:
        try:
            s = _get(f"/api/block/{int(height)}")
            return {"erro": s["error"]} if "error" in s else {"block": s}
        except Exception as e:
            return {"erro": str(e)}

    def consensus_validators(self) -> dict:
        """[v3.23] Cai para /api/mine/gate se /api/consensus/validators
        não existir (server v9.9.40 não tem)."""
        try:
            r = _get("/api/consensus/validators", timeout=10.0, retries=0)
            if "error" not in r:
                return r
            log.debug("consensus/validators indisponível: %s", r.get("error"))
        except Exception as e:
            log.debug("consensus/validators exceção: %s", e)

        # Fallback: /api/mine/gate (existe no v9.9.40)
        try:
            g = _get("/api/mine/gate?force=1", timeout=10.0)
            if isinstance(g, dict) and "error" not in g:
                detail = g.get("detail") or {}
                return {
                    "ok": True,
                    "validators": detail.get("validators_detail", {}),
                    "required_sigs": 0,
                    "all_required": False,
                    "bootstrap_would_fire": bool(
                        g.get("mining_allowed", False) is False
                        and "quorum" in (g.get("reason", "") or "").lower()
                    ),
                    "mining_allowed": bool(g.get("mining_allowed", False)),
                    "gate_reason": g.get("reason", ""),
                    "source": "mine_gate_fallback",
                    "detail": detail,
                }
        except Exception as e:
            log.debug("mine_gate fallback falhou: %s", e)

        return {"ok": False, "msg": "consensus e mine/gate indisponíveis",
                "required_sigs": 0, "all_required": True, "validators": []}

    def block_signatures(self, height: int) -> dict:
        try:
            return _get(f"/api/block/{int(height)}/signatures", timeout=10.0,
                        retries=0)
        except Exception as e:
            return {"ok": False, "msg": str(e), "signatures": []}

    def get_multi_sig_status(self) -> dict:
        try:
            validators_resp = self.consensus_validators()
            required = int(validators_resp.get("required_sigs", 0) or 0)
            mining = self.mining_status()
            height = int(mining.get("height", 0) or 0)
            last_sigs = 0
            if height > 0:
                sigs_resp = self.block_signatures(height)
                last_sigs = int(sigs_resp.get("signatures_count", 0) or 0)
            return {
                "ok": True,
                "validators_active": len(validators_resp.get("validators", [])),
                "required_sigs": required,
                "all_required": bool(validators_resp.get("all_required", True)),
                "last_block_height": height,
                "last_block_sigs": last_sigs,
                "is_multisig_active": required >= 1,
                "last_block_ready": last_sigs >= required if required > 0 else True,
                "bootstrap_would_fire": bool(validators_resp.get("bootstrap_would_fire", False)),
                "mining_allowed": validators_resp.get("mining_allowed"),
                "gate_reason": validators_resp.get("gate_reason", ""),
                "source": validators_resp.get("source", "consensus/validators"),
            }
        except Exception as e:
            return {"ok": False, "msg": str(e), "validators_active": 0, "required_sigs": 0}

    def portfolio(self, address: str) -> dict:
        try:
            s = _get(f"/api/portfolio/{address}", timeout=15.0)
            if "error" in s:
                return {"erro": s["error"]}
            return s.get("portfolio", {"BRN": 0, "KYC": 0})
        except Exception as e:
            return {"erro": str(e)}

    def transfer(self, from_addr: str, to_addr: str, asset: str,
                 amount, sk: str, pk: str) -> dict:
        try:
            return _post("/api/transfer", {
                "from": from_addr, "to": to_addr, "asset_id": asset or "BRN",
                "amount": str(amount), "private_key": sk, "pubkey": pk,
            })
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def minhas_txs(self, address: str, limit: int = 50) -> dict:
        try:
            try:
                lim = max(1, min(int(limit), 500))
            except (ValueError, TypeError):
                lim = 50
            return _get(f"/api/minhas-txs/{address}?limit={lim}", timeout=60.0)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def tx_status(self, txid: str) -> dict:
        try:
            return _get(f"/api/tx-status/{txid}")
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def verificar_recebimento(self, address: str, txid: str) -> dict:
        try:
            return _get(f"/api/verificar-recebimento/{address}/{txid}")
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def check_sync_barrier(self) -> dict:
        try:
            st = _get("/api/status", timeout=5.0)
            if "error" in st:
                return {"ok": False, "msg": "Nó offline. Verifique o main.py."}

            local_h = st.get("height", 0)
            if local_h < 0:
                return {"ok": False, "msg": "Blockchain vazia. Sincronize primeiro."}

            sync = _get("/api/sync-info", timeout=5.0)
            if "error" in sync:
                return {"ok": True, "msg": "Sem dados de sync. Liberando mineração."}

            target_h = sync.get("sync", {}).get("target", local_h)
            peers = sync.get("sync", {}).get("peers", 0)

            if peers == 0:
                return {"ok": False, "msg": "Nenhum peer conectado. Aguardando a rede."}

            if local_h < (target_h - 1):
                falta = target_h - local_h
                return {"ok": False, "msg": f"Você está {falta} bloco(s) atrás do topo. Sincronizando..."}

            return {"ok": True, "msg": "Sincronizado. Iniciando mineração."}
        except Exception as e:
            return {"ok": False, "msg": f"Erro na verificação: {e}"}

    def start_mining(self, address: str, pubkey: str) -> dict:
        try:
            return _post("/api/miner/start", {
                "address": address, "miner_pubkey": pubkey,
            })
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def stop_mining(self) -> dict:
        try:
            return _post("/api/miner/stop", {})
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def mining_status(self) -> dict:
        try:
            r = _get("/api/miner/status", timeout=GET_TIMEOUT)
            if "error" not in r:
                r.setdefault("validators_active", 0)
                r.setdefault("signatures_required", 0)
                r.setdefault("signatures_collected", 0)
            return r
        except Exception as e:
            return {"running": False, "error": str(e)}

    def _resolve_pool_url(self) -> str:
        env_pool = (os.environ.get("BRN_POOL_URL", "") or "").strip()
        return env_pool.rstrip("/") if env_pool else API_BASE

    def pool_start_mining(self, address: str, pubkey: str) -> dict:
        if not address or not pubkey:
            return {"ok": False, "msg": "address e pubkey obrigatorios"}
        try:
            r = _post("/api/pool/start_local", {
                "address": address,
                "pubkey": pubkey,
            }, timeout=30.0)
            return r if r.get("ok") else {"ok": False, "msg": r.get("msg", "start_local falhou")}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def pool_stop_mining(self) -> dict:
        try:
            r = _post("/api/pool/stop_local", {}, timeout=30.0)
            return r if r.get("ok") else {"ok": False, "msg": r.get("msg", "stop_local falhou")}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def pool_status(self) -> dict:
        try:
            r = _get("/api/pool/local_status", timeout=10.0)
            return r if isinstance(r, dict) else {"ok": False, "running": False}
        except Exception as e:
            return {"ok": False, "running": False, "msg": str(e)}

    def pool_stats(self) -> dict:
        try:
            return _get("/api/pool/stats", timeout=10.0)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def pool_miners(self) -> dict:
        try:
            return _get("/api/pool/miners", timeout=10.0)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_trust_stats(self) -> dict:
        try:
            return _get("/api/trust/stats", timeout=10.0)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_trust_nodes(self, limit: int = 100) -> dict:
        try:
            return _get(f"/api/trust/nodes?limit={int(limit)}", timeout=10.0)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def call_faucet(self, address: str, sk: str, pk: str) -> dict:
        try:
            return _post("/api/faucet", {"address": address, "private_key": sk, "pubkey": pk})
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def bridge_claim(self, txid: str, address: str) -> dict:
        try:
            return _post("/api/l2/claim", {"btc_txid": txid, "buyer_brn": address})
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def ping(self) -> dict:
        try:
            return _get("/health", timeout=5.0)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_version(self) -> dict:
        return {
            "version": __version__,
            "api_base": API_BASE,
            "api_base_source": _API_BASE_SOURCE,
            "has_requests": _HAS_REQUESTS,
            "on_render": _is_running_on_render(),
            "allow_remote": os.environ.get("BRN_API_BASE_ALLOW_REMOTE", "0") == "1",
        }

    def get_debug_info(self) -> dict:
        consensus_info = {}
        try:
            consensus_info = self.get_multi_sig_status()
        except Exception:
            pass
        pool_running = False
        try:
            ps = self.pool_status()
            pool_running = bool(ps.get("running", False))
        except Exception:
            pass

        node_verification = {}
        if _WALLET_SESSION.get("public_key"):
            try:
                v = _verify_sk_on_node(_WALLET_SESSION["public_key"])
                node_verification = {
                    "installed": bool(v.get("installed")),
                    "source": v.get("source"),
                    "n_trust": v.get("n_trust"),
                    "n_active": v.get("n_active"),
                    "required_sigs": v.get("required_sigs"),
                    "bootstrap_would_fire": v.get("bootstrap_would_fire"),
                    "sign_collector_ready": v.get("sign_collector_ready"),
                    "mining_allowed": v.get("mining_allowed"),
                    "gate_reason": v.get("gate_reason"),
                    "fast_sync_active": v.get("fast_sync_active"),
                }
            except Exception as e:
                node_verification = {"error": str(e)}

        # [v3.23] Inclui o gate completo (para colar em issue/chat)
        mining_gate = {}
        try:
            mining_gate = self.mine_gate()
        except Exception as e:
            mining_gate = {"error": str(e)}

        return {
            "version": __version__,
            "api_base": API_BASE,
            "api_base_source": _API_BASE_SOURCE,
            "on_render": _is_running_on_render(),
            "allow_remote": os.environ.get("BRN_API_BASE_ALLOW_REMOTE", "0") == "1",
            "has_requests": _HAS_REQUESTS,
            "wallet_locked": not bool(_WALLET_SESSION.get("address")),
            "wallet_address": _WALLET_SESSION.get("address", ""),
            "wallet_public_key": _WALLET_SESSION.get("public_key", ""),
            "node_verification": node_verification,
            "mining_gate": mining_gate,
            "pool_client_running": pool_running,
            "pool_url_resolved": self._resolve_pool_url(),
            "consensus": consensus_info,
            "env": {
                "BRN_IS_ORIGIN":       os.environ.get("BRN_IS_ORIGIN", ""),
                "BRN_POOL_URL":        os.environ.get("BRN_POOL_URL", ""),
                "BRN_POOL_URL_LOCAL":  os.environ.get("BRN_POOL_URL_LOCAL", ""),
                "BRN_PASSWORD_set":    bool(os.environ.get("BRN_PASSWORD", "").strip()),
                "BRN_WALLET_SESSION_PASS_set": bool(
                    os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
                ),
            },
        }


# ============================================================
# Self-test
# ============================================================
def _self_test() -> int:
    print(f"app_wallet_v3.py v{__version__}")
    print("=" * 60)
    print(f"API_BASE:       {API_BASE}")
    print(f"API_BASE source:{_API_BASE_SOURCE}")
    print(f"on_render:      {_is_running_on_render()}")
    print(f"allow_remote:   {os.environ.get('BRN_API_BASE_ALLOW_REMOTE', '0')}")
    print(f"requests:       {'OK' if _HAS_REQUESTS else 'AUSENTE'}")
    print()

    if not _HAS_REQUESTS:
        print("[X] requests nao instalado. Rode: pip install requests")
        return 1

    api = WalletApi()

    r = api.ping()
    if r.get("ok"):
        print(f"[1] ping OK — height={r.get('height')} v={r.get('version', '?')}")
        val = r.get("validators") or {}
        if val:
            print(f"    validators: n_trust={val.get('n_trust')} "
                  f"n_active={val.get('n_active')} "
                  f"required_sigs={val.get('required_sigs')} "
                  f"bootstrap_would_fire={val.get('bootstrap_would_fire')}")
        pid = r.get("peer_id", "")
        if pid:
            print(f"    peer_id: {pid}")
    else:
        print(f"[1] [X] ping: {r} (main.py offline?)")

    print("\n[2] /api/mine/gate (fonte de verdade do gate):")
    g = api.mine_gate()
    if isinstance(g, dict) and g.get("ok"):
        print(f"    mining_allowed={g.get('mining_allowed')}")
        print(f"    reason={g.get('reason')}")
        detail = g.get("detail") or {}
        print(f"    sk_installed={detail.get('sk_installed')}")
        print(f"    local_pk={detail.get('local_pk')}")
        print(f"    validators_ready={detail.get('validators_ready')}")
        print(f"    validators_reason={detail.get('validators_reason')}")
        print(f"    fast_sync_active={detail.get('fast_sync_active')}")
    else:
        print(f"    [X] {g}")

    h = api.has_wallet()
    print(f"\n[3] has_wallet: exists={h.get('exists')} "
          f"root_enc={h.get('root_enc')} named={h.get('named_wallets')}")

    lw = api.list_wallets()
    print(f"[4] list_wallets: {len(lw)} carteira(s)")
    for w in lw:
        print(f"      - {w}")

    pw = _get_default_password()
    if pw and lw:
        first = lw[0]["name"]
        print(f"\n[5] Teste load_wallet('{first}.wallet'):")
        r = api.load_wallet(f"{first}.wallet", pw)
        print(f"    ok={r.get('ok')} address={(r.get('address') or '')[:16]}…")
        print(f"    sk_installed={r.get('sk_installed')} "
              f"node_will_sign={r.get('node_will_sign')} "
              f"sign_collector_ready={r.get('sign_collector_ready')} "
              f"verification_source={r.get('verification_source')}")
        print(f"    mining_allowed={r.get('mining_allowed')} "
              f"gate_reason={r.get('gate_reason')} "
              f"fast_sync_active={r.get('fast_sync_active')}")
        for w in (r.get("warnings") or []):
            print(f"    [WARN] {w}")
        for e in (r.get("errors") or []):
            print(f"    [ERR]  {e}")

    print("\n" + "=" * 60)
    print(" SELF-TEST CONCLUIDO")
    print("=" * 60)
    return 0


def _cmd_verify_sk() -> int:
    print(f"app_wallet_v3.py v{__version__} — --verify-sk")
    print(f"API_BASE: {API_BASE} (source={_API_BASE_SOURCE})")
    print(f"on_render: {_is_running_on_render()}")
    print("=" * 60)

    pw = _get_default_password()
    if not pw:
        print("[X] Senha não encontrada no env "
              "(BRN_PASSWORD / BRN_WALLET_SESSION_PASS / BRN_NODE_PASSWORD)")
        return 1

    if not _HAS_REQUESTS:
        print("[X] requests não instalado")
        return 1

    api = WalletApi()
    print("[1] unlock_wallet(pwd do env)…")
    r = api.unlock_wallet(pw)

    print(f"\n    ok                    = {r.get('ok')}")
    print(f"    address               = {r.get('address')}")
    print(f"    source                = {r.get('source')}")
    print(f"    api_base              = {r.get('api_base')}")
    print(f"    api_base_source       = {r.get('api_base_source')}")
    print(f"    sk_installed          = {r.get('sk_installed')}  <-- O QUE IMPORTA")
    print(f"    node_will_sign        = {r.get('node_will_sign')}  <-- DERIVADO")
    print(f"    sign_collector_ready  = {r.get('sign_collector_ready')}")
    print(f"    verification_source   = {r.get('verification_source')}")
    print(f"    mining_allowed        = {r.get('mining_allowed')}")
    print(f"    gate_reason           = {r.get('gate_reason')}")
    print(f"    fast_sync_active      = {r.get('fast_sync_active')}")
    print(f"    in_trust              = {r.get('in_trust')}")
    print(f"    multisig_ready        = {r.get('multisig_ready')}")
    print(f"    propagated            = {r.get('propagated')}")
    print(f"    peers_notified        = {r.get('peers_notified')}")
    print(f"    sync_error_code       = {r.get('sync_error_code') or '(nenhum)'}")

    for w in (r.get("warnings") or []):
        print(f"\n    [WARN] {w}")
    for e in (r.get("errors") or []):
        print(f"\n    [ERR]  {e}")

    if not r.get("ok"):
        print(f"\n[X] unlock falhou: {r.get('msg')}")
        return 1

    if not r.get("sk_installed"):
        print("\n" + "=" * 60)
        print(" [!] ATENÇÃO: SK NÃO INSTALADA — NÓ NÃO VAI ASSINAR")
        print("=" * 60)
        print(" Sem SK o nó fica com got=0 em validators_sig.")
        ec = r.get("sync_error_code") or ""
        if ec in ("connection", "timeout", "server_offline", "no_requests"):
            print(f"  - _sync_sk_to_chain falhou com erro TRANSITÓRIO ({ec})")
            print("  - main.py pode estar offline ou o servidor em :5000 caiu")
        elif ec:
            print(f"  - _sync_sk_to_chain foi REJEITADA pelo servidor ({ec})")
        else:
            print("  - sync_r veio vazio: carteira sem private_key?")

        print("\n  Verificação cruzada (3 fontes):")
        print(f"    source = {r.get('verification_source')}")
        v = r.get("verification") or {}
        print(f"    installed = {v.get('installed')}  source={v.get('source')}")
        print(f"    raw = {json.dumps({k: v.get(k) for k in ('n_trust', 'n_active', 'required_sigs') if v.get(k) is not None}, default=str)}")
        return 2

    if r.get("sign_collector_ready") is False:
        print("\n" + "=" * 60)
        print(" [!] SK OK MAS sign_collector_ready=False")
        print("=" * 60)
        print(" O nó tem a SK mas não vai COLETAR assinaturas dos peers.")
        print("  - Verifique se blockchain.py é ≥ v9.6.0 (auto-wire)")
        print("  - Reinicie o main.py e observe o log [wiring:post-p2p]")
        return 3

    print("\n" + "=" * 60)
    print(" [ok] SK INSTALADA — NÓ DEVE ASSINAR NORMALMENTE")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    p = argparse.ArgumentParser(prog="app_wallet_v3.py")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--verify-sk", action="store_true",
                   help="unlock do env + imprime estado da SK no nó")
    p.add_argument("--version", action="store_true")
    args = p.parse_args()

    if args.version:
        print(f"app_wallet_v3.py v{__version__}")
        print(f"api_base={API_BASE} source={_API_BASE_SOURCE}")
        sys.exit(0)

    if args.self_test:
        sys.exit(_self_test())

    if args.verify_sk:
        sys.exit(_cmd_verify_sk())

    print("Uso: python3 app_wallet_v3.py --self-test | --verify-sk | --version")
    sys.exit(0)
