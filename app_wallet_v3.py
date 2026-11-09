"""app_wallet_v3.py — Ponte entre a GUI pywebview e o nó BRN (v3.20)
================================================================
v3.20 (2026-11-09) — FIX CARREGAR .wallet:
  [FIX CRÍTICO] load_wallet(): strip de ".wallet" do nome ANTES de
        chamar WalletManager.load_encrypted_wallet. Antes, o HTML
        passava "masmasmasmas.wallet" e o WalletManager procurava
        "wallets/masmasmasmas.wallet.wallet" → "arquivo vazio".
  [FIX] save_wallet(): detecta assinatura de save_encrypted_wallet
        por introspecção (kwargs → positional 5-args → positional
        3-args). Antes, chamada positional fixa falhava em
        versões antigas com assinatura diferente.
  [FIX] list_wallets(): retorna [{name, address, public_key}]
        sempre como dict — antes alguns retornos viravam string.
  [KEEP] v3.19: fallback WalletManager, create_and_unlock em 3
        lugares, unlock propaga SK ao Origin, has_wallet considera
        wallets/*.wallet.

v3.19 (2026-11-09):
  [NEW] unlock_wallet(): fallback WalletManager se .enc falhar.
  [NEW] unlock_wallet(): chama integrate_and_propagate via HTTP.
  [NEW] list_wallets(): normaliza 3 formatos de retorno.
  [NEW] create_and_unlock(): salva em current_wallet.enc E wallets/.

v3.18 (2026-11-06):
  [NEW] check_sync_barrier().
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

__version__ = "3.20"

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    requests = None
    _HAS_REQUESTS = False


log = logging.getLogger("wallet-bridge")

BASE_DIR     = Path(__file__).resolve().parent
API_BASE     = os.environ.get("BRN_API_BASE", "http://127.0.0.1:5000").rstrip("/")
API_TIMEOUT  = float(os.environ.get("BRN_API_TIMEOUT", "200"))
GET_TIMEOUT  = float(os.environ.get("BRN_API_GET_TIMEOUT", "30"))
MAX_RETRIES  = int(os.environ.get("BRN_API_RETRIES", "2"))
RETRY_DELAY  = float(os.environ.get("BRN_API_RETRY_DELAY", "0.5"))

_WALLET_SESSION: dict = {}


# ═══════════════════════════════════════════════════════════════
# Helpers HTTP
# ═══════════════════════════════════════════════════════════════
def _sync_sk_to_chain(sk_hex: str, address: str = "", pubkey: str = "") -> dict:
    """
    Instala SK no nó via HTTP /api/wallet/set_validator_sk.

    O server.py v9.9.28 chama wallet_integrator.integrate_and_propagate:
      1. instala SK local
      2. configura multisig
      3. avisa o Origin (se cliente)
    """
    if not sk_hex:
        return {"ok": False, "msg": "sk vazio"}
    try:
        r = _post("/api/wallet/set_validator_sk", {
            "sk_hex": sk_hex,
            "address": address or "",
            "pubkey":  pubkey or "",
        }, timeout=15.0)
        if not r.get("ok"):
            log.warning("_sync_sk_to_chain falhou: %s", r.get("msg"))
        else:
            log.info(
                "_sync_sk_to_chain ok — pk=%s… propagated=%s peers=%s",
                (r.get("pubkey") or "")[:16],
                r.get("propagated", False),
                r.get("peers_notified", 0),
            )
        return r
    except Exception as e:
        log.warning("_sync_sk_to_chain exception: %s", e)
        return {"ok": False, "msg": str(e)}


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


# ═══════════════════════════════════════════════════════════════
# Helpers WalletManager
# ═══════════════════════════════════════════════════════════════
def _normalize_wallet_entry(w: dict) -> dict:
    """Normaliza entrada de WalletManager.list_wallets() em {name, address, public_key}."""
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
    """Lista nomes de carteiras via WalletManager (robusto a variações)."""
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

    if lst is None:
        return names

    if not isinstance(lst, list):
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
    """
    Fallback: se load_current_wallet falhou, tenta decifrar cada
    wallets/*.wallet com WalletManager.load_encrypted_wallet.

    Retorna dict normalizado {address, private_key, public_key} ou None.
    """
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
    """Preenche _WALLET_SESSION com dados normalizados."""
    global _WALLET_SESSION
    _WALLET_SESSION["address"]     = data.get("address", "")
    _WALLET_SESSION["private_key"] = data.get("private_key", "")
    _WALLET_SESSION["public_key"]  = (
        data.get("public_key") or data.get("pubkey") or data.get("pub_hex") or ""
    )


# ═══════════════════════════════════════════════════════════════
# WalletApi
# ═══════════════════════════════════════════════════════════════
class WalletApi:
    def __init__(self):
        log.info("WalletApi v%s iniciada (API_BASE=%s)", __version__, API_BASE)
        if not _HAS_REQUESTS:
            log.warning("requests NÃO instalado — pip install requests")

    def has_wallet(self) -> dict:
        """Também considera wallets/*.wallet."""
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
        }

    def unlock_wallet(self, password: str) -> dict:
        """
        Desbloqueia a carteira ativa.

        Ordem:
          1. load_current_wallet(pwd) em current_wallet.enc
          2. Se falhar: WalletManager.load_encrypted_wallet em wallets/*.wallet
          3. Se decifrou via WalletManager, salva em current_wallet.enc
          4. POST /api/wallet/set_validator_sk (integra + propaga)
        """
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

        log.info(
            "[wallet] unlock OK via %s — address=%s…",
            source, _WALLET_SESSION["address"][:16],
        )

        return {
            "ok": True,
            "address": _WALLET_SESSION["address"],
            "source": source,
            "sk_installed": sync_r.get("sk_installed", False),
            "in_trust": sync_r.get("in_trust", False),
            "in_top5": sync_r.get("in_top5", False),
            "multisig_ready": sync_r.get("multisig_ready", False),
            "propagated": sync_r.get("propagated", False),
            "peers_notified": sync_r.get("peers_notified", 0),
            "warnings": sync_r.get("warnings", []),
            "errors": sync_r.get("errors", []),
        }

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

            return {
                "ok": True,
                "address": w.address,
                "source": "create",
                "sk_installed": sync_r.get("sk_installed", False),
                "in_trust": sync_r.get("in_trust", False),
                "in_top5": sync_r.get("in_top5", False),
                "multisig_ready": sync_r.get("multisig_ready", False),
                "propagated": sync_r.get("propagated", False),
                "peers_notified": sync_r.get("peers_notified", 0),
            }
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
        """
        Retorna [{name, address, public_key}] para a aba "arquivos".

        [FIX v3.20] Sempre devolve lista de dicts (nunca strings soltas).
        """
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
            _sync_sk_to_chain(w.priv_hex, w.address, w.pub_hex)
            return {
                "address": w.address,
                "private_key": w.priv_hex,
                "public_key": w.pub_hex,
                "ok": True,
            }
        except Exception as e:
            return {"erro": str(e)}

    # ═══════════════════════════════════════════════════════════
    # [FIX v3.20] save_wallet com detecção de assinatura
    # ═══════════════════════════════════════════════════════════
    def save_wallet(self, name: str, password: str,
                    address: str, sk: str, pk: str) -> dict:
        """
        Salva carteira em wallets/{name}.wallet via WalletManager.

        [FIX v3.20] Detecta 3 assinaturas possíveis de
        save_encrypted_wallet (kwargs, positional 5-args, positional
        3-args). Antes, chamada positional fixa falhava em versões
        antigas com assinatura diferente.
        """
        # Validação básica
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

        # Tenta 3 assinaturas — na ordem mais provável
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

        # Fallback: write direto via _save_blob (aad=b"wallet")
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

    # ═══════════════════════════════════════════════════════════
    # [FIX v3.20] load_wallet com strip de .wallet
    # ═══════════════════════════════════════════════════════════
    def load_wallet(self, name: str, password: str) -> dict:
        """
        Carrega carteira nomeada.

        [FIX v3.20] Strip de ".wallet" ANTES de chamar o WalletManager.
        Antes, se o HTML passasse "masmasmasmas.wallet", o
        WalletManager procurava "wallets/masmasmasmas.wallet.wallet"
        e retornava "arquivo vazio".
        """
        global _WALLET_SESSION
        if not name:
            return {"ok": False, "msg": "nome vazio"}
        if not password:
            return {"ok": False, "msg": "senha vazia"}

        # [FIX] Strip .wallet do nome
        name = name.strip()
        if name.endswith(".wallet"):
            name = name[:-7]
        elif name.endswith(".enc"):
            name = name[:-4]

        try:
            from wallet import WalletManager
            r = WalletManager.load_encrypted_wallet(name, password)
            if r and (not isinstance(r, dict) or r.get("ok") is not False):
                if isinstance(r, dict):
                    _populate_session_from_data(r)
                    if _WALLET_SESSION.get("private_key"):
                        _sync_sk_to_chain(
                            _WALLET_SESSION["private_key"],
                            _WALLET_SESSION["address"],
                            _WALLET_SESSION["public_key"],
                        )
                return r if isinstance(r, dict) else {"ok": True, "address": ""}
            return r or {"ok": False, "msg": "resposta vazia"}

        except Exception as e_load:
            # Fallback: tenta _load_blob direto
            try:
                from secure_store import _load_blob
                path = BASE_DIR / "wallets" / f"{name}.wallet"
                if path.exists():
                    data = _load_blob(str(path), password, b"wallet")
                    if data and data.get("private_key"):
                        _populate_session_from_data(data)
                        if _WALLET_SESSION.get("private_key"):
                            _sync_sk_to_chain(
                                _WALLET_SESSION["private_key"],
                                _WALLET_SESSION["address"],
                                _WALLET_SESSION["public_key"],
                            )
                        log.info("[load_wallet] OK via _load_blob fallback: %s", path)
                        return {"ok": True, **data}
            except Exception:
                pass

            return {"ok": False, "msg": str(e_load)}

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
            }
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
        try:
            r = _get("/api/consensus/validators", timeout=10.0)
            return r if "error" not in r else {
                "ok": False, "msg": r["error"],
                "required_sigs": 0, "all_required": True, "validators": [],
            }
        except Exception as e:
            return {"ok": False, "msg": str(e), "required_sigs": 0,
                    "all_required": True, "validators": []}

    def block_signatures(self, height: int) -> dict:
        try:
            return _get(f"/api/block/{int(height)}/signatures", timeout=10.0)
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
        return {"version": __version__, "api_base": API_BASE, "has_requests": _HAS_REQUESTS}

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
        return {
            "version": __version__, "api_base": API_BASE,
            "has_requests": _HAS_REQUESTS,
            "wallet_locked": not bool(_WALLET_SESSION.get("address")),
            "wallet_address": _WALLET_SESSION.get("address", ""),
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


# ═══════════════════════════════════════════════════════════════
# Self-test
# ═══════════════════════════════════════════════════════════════
def _self_test() -> int:
    print(f"app_wallet_v3.py v{__version__}")
    print("=" * 60)
    print(f"API_BASE:      {API_BASE}")
    print(f"requests:      {'OK' if _HAS_REQUESTS else 'AUSENTE'}")
    print()

    if not _HAS_REQUESTS:
        print("[X] requests nao instalado. Rode: pip install requests")
        return 1

    api = WalletApi()

    r = api.ping()
    if r.get("ok"):
        print(f"[1] ping OK — height={r.get('height')} v={r.get('version', '?')}")
    else:
        print(f"[1] [X] ping: {r} (main.py offline?)")

    h = api.has_wallet()
    print(f"[2] has_wallet: exists={h.get('exists')} "
          f"root_enc={h.get('root_enc')} named={h.get('named_wallets')}")

    lw = api.list_wallets()
    print(f"[3] list_wallets: {len(lw)} carteira(s)")
    for w in lw:
        print(f"      - {w}")

    # [NOVO v3.20] Testa strip de .wallet
    pw = _get_default_password()
    if pw and lw:
        first = lw[0]["name"]
        print(f"\n[4] Teste load_wallet('{first}.wallet') — deve strip:")
        r = api.load_wallet(f"{first}.wallet", pw)
        print(f"      resultado: ok={r.get('ok')} address={(r.get('address') or '')[:16]}…")

    print("\n" + "=" * 60)
    print(" SELF-TEST CONCLUIDO")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    p = argparse.ArgumentParser(prog="app_wallet_v3.py")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--version", action="store_true")
    args = p.parse_args()

    if args.version:
        print(f"app_wallet_v3.py v{__version__}")
        sys.exit(0)

    if args.self_test:
        sys.exit(_self_test())

    print("Uso: python3 app_wallet_v3.py --self-test")
    sys.exit(0)
