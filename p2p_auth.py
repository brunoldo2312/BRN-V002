"""p2p_auth.py — Autenticação Ed25519 para o P2P BRN (v4)
================================================================
v4:
  [FIX] CRÍTICO: verify_auth() não estoura mais TypeError quando
        o JSON tem campos null. Qualquer peer malicioso podia
        derrubar o nó enviando {"nonce": null, ...}.
        Agora normalizamos TODOS os campos para string ANTES de
        qualquer operação, com isinstance() em vez de .get(default).
v3:
  [KEEP] Anti-replay via nonce cache (janela configurável)
  [KEEP] Anti-DoS: bloqueia pubkey após N falhas
  [KEEP] Nonce só consumido DEPOIS da assinatura válida
================================================================
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from typing import Optional

log = logging.getLogger("p2p_auth")


# ============================================================
# CONFIG
# ============================================================
_CONFIG: dict = {}


def reload_config() -> dict:
    mode = (os.environ.get("BRN_P2P_AUTH", "optional") or "").strip().lower()
    if mode not in ("off", "optional", "required"):
        log.warning("BRN_P2P_AUTH inválido (%r) — usando 'optional'", mode)
        mode = "optional"

    try:
        window = int(os.environ.get("BRN_P2P_AUTH_WINDOW", "30"))
        if window < 5 or window > 3600:
            raise ValueError
    except (ValueError, TypeError):
        window = 30

    try:
        cache_size = int(os.environ.get("BRN_P2P_NONCE_CACHE", "10000"))
        if cache_size < 100:
            raise ValueError
    except (ValueError, TypeError):
        cache_size = 10000

    try:
        fail_threshold = int(os.environ.get("BRN_P2P_FAIL_THRESHOLD", "10"))
    except (ValueError, TypeError):
        fail_threshold = 10

    try:
        fail_block_s = int(os.environ.get("BRN_P2P_FAIL_BLOCK_S", "300"))
    except (ValueError, TypeError):
        fail_block_s = 300

    _CONFIG.clear()
    _CONFIG.update({
        "mode":           mode,
        "window":         window,
        "cache_size":     cache_size,
        "fail_threshold": fail_threshold,
        "fail_block_s":   fail_block_s,
    })
    return dict(_CONFIG)


reload_config()


# ============================================================
# CONSTANTES
# ============================================================
_DOMAIN = b"BRN-P2P-AUTH-v3|"
MAX_PUB_LEN       = 128
MAX_SIG_LEN       = 256
MAX_NONCE_LEN     = 128
MAX_MSG_HASH_LEN  = 128
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024


# ============================================================
# Ed25519 (com fallback)
# ============================================================
_Ed25519PublicKey = None
_CRYPTO_SOURCE = ""

try:
    from crypto import Ed25519PublicKey as _Ed25519PublicKey
    _CRYPTO_SOURCE = "crypto"
except Exception:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey as _Ed25519PublicKey,
        )
        _CRYPTO_SOURCE = "cryptography"
    except Exception:
        _Ed25519PublicKey = None
        _CRYPTO_SOURCE = "(indisponível)"
        log.error("p2p_auth: Ed25519PublicKey indisponível")


# ============================================================
# ESTADO
# ============================================================
_node_id_priv = None
_node_id_pub_hex = ""
_node_lock = threading.RLock()

_nonce_cache: "OrderedDict[str, float]" = OrderedDict()
_nonce_lock = threading.RLock()

_fail_tracker: dict = {}
_fail_lock = threading.RLock()


# ============================================================
# API
# ============================================================
def auth_enabled() -> bool:
    return _CONFIG.get("mode") in ("optional", "required")


def auth_required() -> bool:
    return _CONFIG.get("mode") == "required"


def set_node_id_priv(priv) -> None:
    global _node_id_priv, _node_id_pub_hex
    with _node_lock:
        _node_id_priv = priv
        try:
            _node_id_pub_hex = priv.public_key().public_bytes_raw().hex()
        except Exception:
            _node_id_pub_hex = ""
    if _node_id_pub_hex:
        log.info("p2p_auth: chave registrada pub=%s...", _node_id_pub_hex[:16])
    else:
        log.warning("p2p_auth: chave privada registrada mas pubkey falhou")


def get_node_id_pub() -> str:
    return _node_id_pub_hex


# ============================================================
# HELPERS
# ============================================================
def _canonical_json(obj) -> bytes:
    return json.dumps(
        obj,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")


def _msg_hash(msg) -> str:
    if msg is None:
        return "0" * 64
    if not isinstance(msg, dict):
        raise ValueError("msg deve ser dict ou None")
    clean = {k: v for k, v in msg.items() if k != "_auth"}
    raw = _canonical_json(clean)
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload grande demais: {len(raw)}B")
    return hashlib.sha256(raw).hexdigest()


def _sign(data: bytes) -> str:
    priv = _node_id_priv
    if priv is None:
        raise RuntimeError("p2p_auth: chave privada não registrada")
    sig = priv.sign(data)
    if isinstance(sig, (bytes, bytearray)):
        return sig.hex()
    return str(sig)


def _verify_sig(pub_hex: str, sig_hex: str, data: bytes) -> bool:
    if _Ed25519PublicKey is None:
        return False
    if not pub_hex or not sig_hex:
        return False
    try:
        pub_bytes = bytes.fromhex(pub_hex)
        sig_bytes = bytes.fromhex(sig_hex)
    except (ValueError, TypeError):
        return False
    if len(pub_bytes) != 32 or len(sig_bytes) != 64:
        return False
    try:
        pk = _Ed25519PublicKey.from_public_bytes(pub_bytes)
        pk.verify(sig_bytes, data)
        return True
    except Exception:
        return False


# ============================================================
# ANTI-DOS
# ============================================================
def _is_blocked(pub_hex: str) -> bool:
    if not pub_hex:
        return False
    now = time.time()
    with _fail_lock:
        entry = _fail_tracker.get(pub_hex)
        if not entry:
            return False
        return entry.get("blocked_until", 0) > now


def _record_failure(pub_hex: str) -> None:
    if not pub_hex:
        return
    now = time.time()
    threshold = _CONFIG.get("fail_threshold", 10)
    block_s = _CONFIG.get("fail_block_s", 300)
    with _fail_lock:
        entry = _fail_tracker.get(pub_hex)
        if entry is None or (now - entry.get("first_ts", 0)) > block_s:
            _fail_tracker[pub_hex] = {
                "count": 1, "first_ts": now, "blocked_until": 0
            }
            return
        entry["count"] += 1
        if entry["count"] >= threshold:
            entry["blocked_until"] = now + block_s
            log.warning("p2p_auth: pubkey %s... bloqueada por %ds",
                        pub_hex[:16], block_s)


def _clear_failures(pub_hex: str) -> None:
    if not pub_hex:
        return
    with _fail_lock:
        _fail_tracker.pop(pub_hex, None)


# ============================================================
# NONCE CACHE
# ============================================================
def _nonce_seen(nonce: str, ts: int) -> bool:
    if not nonce:
        return True
    window = _CONFIG.get("window", 30)
    bucket = ts // max(1, window)
    key = f"{nonce}|{bucket}"
    now = time.time()
    with _nonce_lock:
        cutoff = now - (window * 2)
        while _nonce_cache:
            k, t = next(iter(_nonce_cache.items()))
            if t < cutoff:
                _nonce_cache.popitem(last=False)
            else:
                break
        if key in _nonce_cache:
            _nonce_cache.move_to_end(key)
            return True
        return False


def _nonce_mark(nonce: str, ts: int) -> None:
    if not nonce:
        return
    window = _CONFIG.get("window", 30)
    bucket = ts // max(1, window)
    key = f"{nonce}|{bucket}"
    now = time.time()
    cache_size = _CONFIG.get("cache_size", 10000)
    with _nonce_lock:
        _nonce_cache[key] = now
        _nonce_cache.move_to_end(key)
        while len(_nonce_cache) > cache_size:
            _nonce_cache.popitem(last=False)


# ============================================================
# BUILD AUTH
# ============================================================
def build_auth(msg=None) -> dict:
    priv = _node_id_priv
    if priv is None:
        return {}
    try:
        ts = int(time.time())
        nonce = os.urandom(16).hex()
        msg_hash = _msg_hash(msg)

        to_sign = b"".join([
            _DOMAIN,
            _node_id_pub_hex.encode("ascii"),
            b"|",
            str(ts).encode("ascii"),
            b"|",
            nonce.encode("ascii"),
            b"|",
            msg_hash.encode("ascii"),
        ])
        sig_hex = _sign(to_sign)
        return {
            "pub":       _node_id_pub_hex,
            "ts":        ts,
            "nonce":     nonce,
            "msg_hash":  msg_hash,
            "sig":       sig_hex,
        }
    except Exception as e:
        log.warning("build_auth falhou: %s", e)
        return {}


# ============================================================
# [FIX v4] VERIFY AUTH — normaliza TODOS os campos para string
# ============================================================
def _as_str(v) -> str:
    """Converte qualquer valor JSON para string segura.
    None, int, dict, list → string vazia.
    Só aceita str (e rejeita str com len absurdo)."""
    if isinstance(v, str):
        return v
    return ""


def verify_auth(auth: dict, msg=None) -> tuple:
    """
    Verifica bloco de autenticação.
    Retorna (True, "") se OK, senão (False, motivo).

    [FIX v4] Normaliza TODOS os campos para string ANTES de
    qualquer operação. Antes, se um peer enviasse {"nonce": null},
    o .get() retornava None e len(None) estourava TypeError,
    permitindo DoS trivial.
    """
    if not auth or not isinstance(auth, dict):
        return False, "auth vazio ou invalido"

    # -------- normalização segura --------
    pub       = _as_str(auth.get("pub"))
    sig       = _as_str(auth.get("sig"))
    nonce     = _as_str(auth.get("nonce"))
    msg_hash  = _as_str(auth.get("msg_hash"))
    ts_raw    = auth.get("ts", 0)

    # -------- limites de tamanho --------
    if len(pub) > MAX_PUB_LEN:
        return False, f"pub grande demais ({len(pub)})"
    if len(sig) > MAX_SIG_LEN:
        return False, f"sig grande demais ({len(sig)})"
    if len(nonce) > MAX_NONCE_LEN:
        return False, f"nonce grande demais ({len(nonce)})"
    if len(msg_hash) > MAX_MSG_HASH_LEN:
        return False, "msg_hash grande demais"
    if not pub or not sig:
        return False, "auth incompleto"

    # -------- ts --------
    try:
        ts = int(ts_raw)
    except (ValueError, TypeError):
        return False, "ts invalido"

    # -------- bloqueio por pubkey --------
    if _is_blocked(pub):
        return False, "pubkey temporariamente bloqueada"

    now = int(time.time())
    delta = abs(now - ts)
    window = _CONFIG.get("window", 30)
    if delta > window:
        return False, f"ts fora da janela (delta={delta}s > {window}s)"

    # -------- payload binding --------
    if msg is not None:
        try:
            expected_hash = _msg_hash(msg)
        except Exception as e:
            return False, f"msg invalida: {e}"
        if not msg_hash:
            _record_failure(pub)
            return False, "msg_hash ausente no auth"
        if not hmac.compare_digest(msg_hash, expected_hash):
            _record_failure(pub)
            return False, "msg_hash nao corresponde ao payload"

    # -------- assinatura --------
    to_verify = b"".join([
        _DOMAIN,
        pub.encode("ascii", "ignore"),
        b"|",
        str(ts).encode("ascii"),
        b"|",
        nonce.encode("ascii", "ignore"),
        b"|",
        msg_hash.encode("ascii", "ignore"),
    ])

    if not _verify_sig(pub, sig, to_verify):
        _record_failure(pub)
        return False, "assinatura invalida"

    _clear_failures(pub)

    # -------- anti-replay --------
    if not nonce:
        return False, "nonce ausente"
    if _nonce_seen(nonce, ts):
        return False, "replay detectado (nonce repetido)"
    _nonce_mark(nonce, ts)

    return True, ""


# ============================================================
# STATUS
# ============================================================
def status() -> dict:
    with _nonce_lock:
        n_nonces = len(_nonce_cache)
    with _fail_lock:
        n_blocked = sum(1 for e in _fail_tracker.values()
                        if e.get("blocked_until", 0) > time.time())
        n_tracked = len(_fail_tracker)
    with _node_lock:
        has_priv = _node_id_priv is not None
        pub_short = _node_id_pub_hex[:16] + "..." if _node_id_pub_hex else ""
    return {
        "mode":                   _CONFIG.get("mode"),
        "enabled":                auth_enabled(),
        "required":               auth_required(),
        "window_s":               _CONFIG.get("window"),
        "nonce_cache_size":       n_nonces,
        "nonce_cache_max":        _CONFIG.get("cache_size"),
        "fail_pubkeys_tracked":   n_tracked,
        "fail_pubkeys_blocked":   n_blocked,
        "has_priv":               has_priv,
        "node_pub":               pub_short,
        "crypto_source":          _CRYPTO_SOURCE,
    }


def info() -> dict:
    return {
        "domain":            _DOMAIN.decode("ascii"),
        "mode":              _CONFIG.get("mode"),
        "window_s":          _CONFIG.get("window"),
        "nonce_cache_max":   _CONFIG.get("cache_size"),
        "fail_threshold":    _CONFIG.get("fail_threshold"),
        "fail_block_s":      _CONFIG.get("fail_block_s"),
        "crypto_source":     _CRYPTO_SOURCE,
    }


def reset_nonce_cache() -> None:
    with _nonce_lock:
        _nonce_cache.clear()


def reset_fail_tracker() -> None:
    with _fail_lock:
        _fail_tracker.clear()


# ============================================================
# CLI
# ============================================================
def _cmd_status() -> int:
    print(json.dumps(status(), indent=2, ensure_ascii=False))
    return 0


def _cmd_info() -> int:
    print(json.dumps(info(), indent=2, ensure_ascii=False))
    return 0


def _cmd_test() -> int:
    if _Ed25519PublicKey is None:
        print(f"Ed25519 indisponível ({_CRYPTO_SOURCE})", file=sys.stderr)
        return 1
    try:
        from crypto import Ed25519PrivateKey
    except Exception:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
        except Exception as e:
            print(f"Ed25519PrivateKey indisponível: {e}", file=sys.stderr)
            return 1

    priv = Ed25519PrivateKey.generate()
    set_node_id_priv(priv)
    reset_nonce_cache()

    payload = {"type": "test", "value": 42}
    a = build_auth(msg=payload)
    if not a:
        print("build_auth falhou", file=sys.stderr)
        return 1
    print("build_auth OK")

    ok, err = verify_auth(a, msg=payload)
    print(f"verify_auth payload OK: ok={ok} err={err!r}")
    if not ok:
        return 1

    ok2, err2 = verify_auth(a, msg=payload)
    print(f"verify_auth replay:    ok={ok2} err={err2!r}")
    if ok2:
        print("ERRO: replay aceito", file=sys.stderr)
        return 1

    a3 = build_auth(msg=payload)
    payload_mod = dict(payload)
    payload_mod["value"] = 99
    ok3, err3 = verify_auth(a3, msg=payload_mod)
    print(f"verify_auth payload trocado: ok={ok3} err={err3!r}")
    if ok3:
        print("ERRO: payload trocado aceito", file=sys.stderr)
        return 1

    # ---- NOVO TESTE: campos null não derrubam ----
    evil = {"pub": None, "sig": None, "nonce": None,
            "msg_hash": None, "ts": None}
    ok4, err4 = verify_auth(evil)
    print(f"verify_auth null fields: ok={ok4} err={err4!r}")
    if ok4:
        print("ERRO: null aceito", file=sys.stderr)
        return 1
    if not isinstance(err4, str):
        print("ERRO: err não é string", file=sys.stderr)
        return 1

    evil2 = {"pub": 123, "sig": 456, "nonce": [], "msg_hash": {}, "ts": "abc"}
    ok5, err5 = verify_auth(evil2)
    print(f"verify_auth wrong types: ok={ok5} err={err5!r}")
    if ok5:
        print("ERRO: wrong types aceito", file=sys.stderr)
        return 1

    print("\nTudo OK.")
    return 0


def _cmd_self_sign() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        print(f"stdin invalido: {e}", file=sys.stderr)
        return 1
    if _node_id_priv is None:
        print("Chave privada não registrada.", file=sys.stderr)
        return 1
    a = build_auth(msg=payload)
    print(json.dumps(a, indent=2, ensure_ascii=False))
    return 0


def _cli() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(prog="p2p_auth.py")
    p.add_argument("--status", action="store_true")
    p.add_argument("--info", action="store_true")
    p.add_argument("--test", action="store_true")
    p.add_argument("--self-sign", action="store_true")
    args = p.parse_args()
    if args.status:    return _cmd_status()
    if args.info:      return _cmd_info()
    if args.test:      return _cmd_test()
    if args.self_sign: return _cmd_self_sign()
    p.print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_cli())
    except KeyboardInterrupt:
        sys.exit(130)
