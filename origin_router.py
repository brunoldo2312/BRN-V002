"""origin_router.py — Plano B+ (Origin Router) v1.0
================================================================
Roda em máquina separada OU na própria Origin-B.
Detecta queda do primário (Render) e promove standby automaticamente.

Anti-split-brain em duas camadas:
  1. Router só promove após N falhas consecutivas (default 4 = ~60s)
  2. Standby re-verifica por conta própria se o primário está inacessível
     ANTES de aceitar a promoção (parâmetro verify_primary)

Config via env (obrigatórias em maiúsculas):
  BRN_ROUTER_PRIMARY_URL       URL do origin primário (Render)
  BRN_ROUTER_STANDBY_URL       URL do origin standby (VPS)
  BRN_ROUTER_ADMIN_TOKEN       Token admin (igual em primário e standby)

Opcionais:
  BRN_ROUTER_POLL_INTERVAL      default 15s
  BRN_ROUTER_HEALTH_TIMEOUT     default 5s
  BRN_ROUTER_FAIL_THRESHOLD     default 4 (N ticks p/ promover)
  BRN_ROUTER_RECOVER_THRESHOLD  default 4 (N ticks p/ rebaixar)
  BRN_ROUTER_STATE_FILE         default origin_router_state.json
  BRN_ROUTER_ID                 default router-1

Uso:
  BRN_ROUTER_PRIMARY_URL=https://brn-v002.onrender.com \
  BRN_ROUTER_STANDBY_URL=http://origin-b:5000 \
  BRN_ROUTER_ADMIN_TOKEN=xxx \
  python origin_router.py
================================================================
"""
from __future__ import annotations
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("origin_router")

# ---- Config ----
PRIMARY_URL = os.environ.get("BRN_ROUTER_PRIMARY_URL", "").strip().rstrip("/")
STANDBY_URL = os.environ.get("BRN_ROUTER_STANDBY_URL", "").strip().rstrip("/")
ADMIN_TOKEN = os.environ.get("BRN_ROUTER_ADMIN_TOKEN", "").strip()
POLL_INTERVAL = int(os.environ.get("BRN_ROUTER_POLL_INTERVAL", "15"))
HEALTH_TIMEOUT = float(os.environ.get("BRN_ROUTER_HEALTH_TIMEOUT", "5"))
FAIL_THRESHOLD = max(2, int(os.environ.get("BRN_ROUTER_FAIL_THRESHOLD", "4")))
RECOVER_THRESHOLD = max(2, int(os.environ.get("BRN_ROUTER_RECOVER_THRESHOLD", "4")))
STATE_FILE = os.environ.get("BRN_ROUTER_STATE_FILE", "origin_router_state.json")
ROUTER_ID = os.environ.get("BRN_ROUTER_ID", "router-1")

if not PRIMARY_URL:
    raise SystemExit("BRN_ROUTER_PRIMARY_URL é obrigatório")
if not STANDBY_URL:
    raise SystemExit("BRN_ROUTER_STANDBY_URL é obrigatório")
if not ADMIN_TOKEN:
    raise SystemExit("BRN_ROUTER_ADMIN_TOKEN é obrigatório")

if PRIMARY_URL == STANDBY_URL:
    raise SystemExit("PRIMARY e STANDBY não podem ser a mesma URL")


# ---- Estado persistente ----
def _load_state() -> dict:
    p = Path(STATE_FILE)
    if not p.exists():
        return {"promoted": False, "since": 0, "primary_fails": 0,
                "primary_recovers": 0, "last_tick": 0}
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            raise ValueError("state não é dict")
        return data
    except Exception as e:
        log.warning("[ROUTER] state corrompido (%s) — resetando", e)
        return {"promoted": False, "since": 0, "primary_fails": 0,
                "primary_recovers": 0, "last_tick": 0}


def _save_state(s: dict) -> None:
    try:
        Path(STATE_FILE).write_text(json.dumps(s, indent=2))
    except Exception as e:
        log.warning("[ROUTER] não gravei %s: %s", STATE_FILE, e)


# ---- HTTP helpers ----
def _http_json(url: str, method: str = "GET", data=None,
               timeout: float = 5, admin: bool = False) -> tuple:
    headers = {"Content-Type": "application/json"}
    if admin:
        headers["Authorization"] = f"Bearer {ADMIN_TOKEN}"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                return resp.status, json.loads(resp.read().decode() or "{}")
            except Exception:
                return resp.status, {}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {"error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return 0, {"error": str(e.reason)}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def _probe(url: str) -> tuple:
    """Retorna (ok, reason, body)."""
    status, body = _http_json(f"{url}/health", timeout=HEALTH_TIMEOUT)
    if status == 0:
        return False, body.get("error", "unreachable"), body
    if status != 200:
        return False, f"HTTP {status}", body
    if not isinstance(body, dict) or body.get("ok") is not True:
        return False, "bad_body", body
    return True, "ok", body


# ---- Ações ----
def _promote_standby(primary_was_down: int) -> bool:
    log.warning("[ROUTER] PROMOVENDO %s (primary caiu %d vezes)",
                STANDBY_URL, primary_was_down)
    status, body = _http_json(
        f"{STANDBY_URL}/admin/origin/promote",
        method="POST",
        data={
            "by": ROUTER_ID,
            "verify_primary": PRIMARY_URL,  # ← anti-split-brain
            "primary_fails": primary_was_down,
        },
        timeout=10,
        admin=True,
    )
    if 200 <= status < 300:
        log.warning("[ROUTER] promote OK: %s", body.get("msg", body))
        return True
    if status == 409:
        log.error("[ROUTER] promote RECUSADO pelo standby: %s", body.get("msg"))
    else:
        log.error("[ROUTER] promote falhou: HTTP %d %s", status, body)
    return False


def _demote_standby() -> bool:
    log.warning("[ROUTER] REBAIXANDO %s (primary voltou)", STANDBY_URL)
    status, body = _http_json(
        f"{STANDBY_URL}/admin/origin/demote",
        method="POST",
        data={"by": ROUTER_ID},
        timeout=10,
        admin=True,
    )
    if 200 <= status < 300:
        log.warning("[ROUTER] demote OK: %s", body.get("msg", body))
        return True
    log.error("[ROUTER] demote falhou: HTTP %d %s", status, body)
    return False


# ---- Loop ----
_state = _load_state()
if _state.get("promoted"):
    log.warning("[ROUTER] estado anterior: standby promovido em %s",
                time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(_state.get("since", 0))))


def _tick() -> None:
    global _state
    now = time.time()

    # 1) Probe primário
    p_ok, p_reason, p_body = _probe(PRIMARY_URL)

    if p_ok:
        prev_recovers = _state["primary_recovers"]
        _state["primary_fails"] = 0
        _state["primary_recovers"] = int(prev_recovers) + 1
        if prev_recovers == 0:
            log.info("[ROUTER] primary OK (recuperando...)")
    else:
        prev_fails = _state["primary_fails"]
        _state["primary_recovers"] = 0
        _state["primary_fails"] = int(prev_fails) + 1
        if _state["primary_fails"] in (1, FAIL_THRESHOLD):
            log.warning("[ROUTER] primary DOWN (%d/%d): %s",
                        _state["primary_fails"], FAIL_THRESHOLD, p_reason)
        else:
            log.debug("[ROUTER] primary ainda down (%d/%d)",
                      _state["primary_fails"], FAIL_THRESHOLD)

    _state["last_tick"] = int(now)

    # 2) Máquina de estados
    if not _state["promoted"]:
        # Normal: promove se primário caiu o suficiente
        if _state["primary_fails"] >= FAIL_THRESHOLD:
            if _promote_standby(_state["primary_fails"]):
                _state["promoted"] = True
                _state["since"] = int(now)
            else:
                log.warning("[ROUTER] promote falhou — retry no próximo tick "
                            "(faltam ~%ds)", POLL_INTERVAL)
    else:
        # Promovida: rebaixa se primário voltou o suficiente
        if _state["primary_recovers"] >= RECOVER_THRESHOLD:
            if _demote_standby():
                _state["promoted"] = False
                _state["since"] = 0
            else:
                log.warning("[ROUTER] demote falhou — retry no próximo tick")

    _save_state(_state)


_stop_evt = threading.Event()


def _loop() -> None:
    log.info("[ROUTER] iniciado (id=%s)", ROUTER_ID)
    log.info("[ROUTER] primary = %s", PRIMARY_URL)
    log.info("[ROUTER] standby = %s", STANDBY_URL)
    log.info("[ROUTER] poll=%ds health_timeout=%.1fs fail=%d (~%ds) recover=%d (~%ds)",
             POLL_INTERVAL, HEALTH_TIMEOUT,
             FAIL_THRESHOLD, FAIL_THRESHOLD * POLL_INTERVAL,
             RECOVER_THRESHOLD, RECOVER_THRESHOLD * POLL_INTERVAL)
    log.info("[ROUTER] state_file=%s", STATE_FILE)

    while not _stop_evt.is_set():
        try:
            _tick()
        except Exception as e:
            log.exception("[ROUTER] tick falhou: %s", e)
        _stop_evt.wait(POLL_INTERVAL)

    log.info("[ROUTER] encerrado")


def _sigterm(_signum, _frame) -> None:
    log.info("[ROUTER] recebi SIGTERM — encerrando")
    _stop_evt.set()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("BRN_ROUTER_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        signal.signal(signal.SIGTERM, _sigterm)
        signal.signal(signal.SIGINT, _sigterm)
    except Exception:
        pass

    t = threading.Thread(target=_loop, name="RouterLoop", daemon=False)
    t.start()
    t.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
