"""contracts/vm.py — VM declarativa e limitada de contratos BRN.

Não é uma EVM. Os métodos são listas de operações declarativas validadas.
A execução não é consenso de blockchain por si só; callers devem garantir
autorização e atomicidade. Transferências só podem gastar fundos do caller.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from copy import deepcopy

MAX_OPS_PER_CALL = 64
MAX_STATE_KEYS = 1000
MAX_STRING_LEN = 256
MAX_METHODS = 32
MAX_CONTRACT_NAME = 64
CONTRACT_ID_PREFIX = "ctr1"
_PATH_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _valid_path(path):
    return isinstance(path, str) and bool(_PATH_RE.fullmatch(path))


def _now():
    return int(time.time())


def _new_contract_id(owner, code):
    seed = json.dumps(code, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    raw = (str(owner) + ":" + seed + ":" + os.urandom(16).hex() + ":" + str(time.time_ns())).encode()
    return CONTRACT_ID_PREFIX + hashlib.sha256(raw).hexdigest()[:32]


def _set_state(db, cid, state):
    if not isinstance(state, dict) or len(state) > MAX_STATE_KEYS:
        raise ValueError("state invalido ou com chaves demais")
    db.contract_set_state(cid, state)


def _safe_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return not (isinstance(value, str) and len(value) > MAX_STRING_LEN)
    if isinstance(value, float):
        return math.isfinite(value)
    return False


class ContractVM:
    """Validador estático de bytecode declarativo."""

    @staticmethod
    def validate(code):
        if not isinstance(code, dict):
            return False, "code deve ser dict"
        if code.get("version", 1) != 1:
            return False, "versao nao suportada"
        name = code.get("name")
        if not isinstance(name, str) or not name or len(name) > MAX_CONTRACT_NAME:
            return False, "campo name invalido"
        state = code.get("initial_state", {})
        if not isinstance(state, dict) or len(state) > MAX_STATE_KEYS:
            return False, "initial_state invalido ou grande demais"
        for key, value in state.items():
            if not _valid_path(key) or not _safe_value(value):
                return False, f"initial_state invalido em {key!r}"
        methods = code.get("methods", {})
        if not isinstance(methods, dict) or len(methods) > MAX_METHODS:
            return False, "methods invalido ou com metodos demais"
        for method, ops in methods.items():
            if not _valid_path(method) or not isinstance(ops, list) or len(ops) > MAX_OPS_PER_CALL:
                return False, f"metodo invalido: {method!r}"
            for idx, op in enumerate(ops):
                ok, msg = ContractVM._validate_op(op, method, idx)
                if not ok:
                    return False, msg
        return True, "ok"

    @staticmethod
    def _validate_op(op, method, idx):
        prefix = f"op #{idx} em {method}"
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            return False, f"{prefix}: operacao invalida"
        kind = op["op"]
        if kind in ("set", "add", "sub", "require_eq", "require_ge"):
            if not _valid_path(op.get("path")):
                return False, f"{prefix}: path invalido"
            value = op.get("value")
            if not _safe_value(value) or value is None:
                return False, f"{prefix}: value invalido"
            if kind in ("add", "sub", "require_ge") and (isinstance(value, bool) or not isinstance(value, (int, float))):
                return False, f"{prefix}: value deve ser numerico"
        elif kind == "emit":
            event = op.get("event")
            if not isinstance(event, str) or not event or len(event) > MAX_STRING_LEN:
                return False, f"{prefix}: event invalido"
        elif kind == "transfer":
            if not isinstance(op.get("to"), str) or not op["to"]:
                return False, f"{prefix}: destinatario invalido"
            amount = op.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                return False, f"{prefix}: amount deve ser int > 0"
            if op.get("from") not in (None, ""):
                return False, f"{prefix}: 'from' nao pode ser definido pelo contrato; usa o caller"
        elif kind != "inc_nonce":
            return False, f"{prefix}: operacao desconhecida {kind!r}"
        return True, "ok"


def _run_op(op, state, ctx):
    kind = op["op"]
    path = op.get("path")
    if kind == "set":
        state[path] = deepcopy(op["value"])
    elif kind in ("add", "sub"):
        current = state.get(path, 0)
        value = op["value"]
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise ValueError(f"{kind}: estado nao numerico em {path}")
        if isinstance(current, float) and not math.isfinite(current):
            raise ValueError(f"{kind}: estado nao finito em {path}")
        result = current + value if kind == "add" else current - value
        if isinstance(result, float) and not math.isfinite(result):
            raise ValueError(f"{kind}: resultado nao finito")
        state[path] = result
    elif kind == "require_eq":
        if state.get(path) != op["value"]:
            raise ValueError(f"require_eq falhou em {path}")
    elif kind == "require_ge":
        current = state.get(path, 0)
        try:
            if isinstance(current, bool) or current < op["value"]:
                raise ValueError(f"require_ge falhou em {path}")
        except TypeError as exc:
            raise ValueError(f"require_ge: tipos incompatíveis em {path}") from exc
    elif kind == "emit":
        ctx["db"].contract_log_event(ctx["contract_id"], op["event"], {
            "caller": ctx.get("caller", ""), "args": ctx.get("args", {}), "ts": _now()
        })
    elif kind == "transfer":
        caller, destination = ctx.get("caller", ""), op["to"]
        if not caller:
            raise ValueError("transfer exige caller autenticado")
        # Impede que código de contrato escolha arbitrariamente a carteira debitada.
        ctx["db"].contract_spend(caller, destination, op["amount"])
        ctx["db"].contract_log_event(ctx["contract_id"], "transfer", {
            "from": caller, "to": destination, "amount": op["amount"], "ts": _now()
        })
    elif kind == "inc_nonce":
        current = state.get("__nonce__", 0)
        if isinstance(current, bool) or not isinstance(current, int) or current < 0:
            raise ValueError("nonce do contrato invalido")
        state["__nonce__"] = current + 1


def deploy(chain, owner: str, code: dict, metadata=None) -> dict:
    if not isinstance(owner, str) or not owner:
        return {"ok": False, "msg": "owner obrigatorio"}
    ok, msg = ContractVM.validate(code)
    if not ok:
        return {"ok": False, "msg": f"contrato invalido: {msg}"}
    db = getattr(chain, "db", None)
    if db is None:
        return {"ok": False, "msg": "chain.db indisponivel"}
    cid = _new_contract_id(owner, code)
    try:
        db.contract_insert(cid, owner, code, metadata or {})
        _set_state(db, cid, deepcopy(code.get("initial_state", {})))
        db.contract_log_event(cid, "deployed", {"owner": owner, "name": code["name"], "ts": _now()})
    except Exception as exc:
        try:
            db.contract_delete(cid)
        except Exception:
            pass
        return {"ok": False, "msg": f"falha ao deployar contrato: {exc}"}
    return {"ok": True, "contract_id": cid, "owner": owner, "name": code["name"], "msg": f"contrato {cid} deployado"}


def call(chain, contract_id: str, caller: str, args=None) -> dict:
    if not isinstance(args, dict):
        return {"ok": False, "msg": "args deve ser dict"}
    method = args.get("method")
    if not isinstance(method, str) or not method:
        return {"ok": False, "msg": "args.method obrigatorio"}
    db = getattr(chain, "db", None)
    if db is None:
        return {"ok": False, "msg": "chain.db indisponivel"}
    try:
        contract = db.contract_get(contract_id)
    except Exception as exc:
        return {"ok": False, "msg": f"falha ao consultar contrato: {exc}"}
    if not contract:
        return {"ok": False, "msg": f"contrato {contract_id} nao existe"}
    code = contract.get("code") or {}
    ok, msg = ContractVM.validate(code)
    if not ok:
        return {"ok": False, "msg": f"codigo armazenado invalido: {msg}"}
    ops = code.get("methods", {}).get(method)
    if not isinstance(ops, list):
        return {"ok": False, "msg": f"metodo {method!r} nao existe"}
    # Evita efeitos financeiros parciais: uma transferência precisa ser a
    # única operação do método. DB.contract_spend faz sua própria transação.
    if any(op.get("op") == "transfer" for op in ops) and len(ops) != 1:
        return {"ok": False, "msg": "transfer deve ser a unica operacao do metodo"}
    try:
        state = db.contract_get_state(contract_id)
        if not isinstance(state, dict):
            raise ValueError("state persistido invalido")
        state = deepcopy(state)
        context = {"db": db, "contract_id": contract_id, "caller": caller, "args": args, "chain": chain}
        for idx, op in enumerate(ops):
            _run_op(op, state, context)
        _set_state(db, contract_id, state)
        db.contract_log_event(contract_id, "called", {
            "caller": caller, "method": method,
            "args": {k: v for k, v in args.items() if k != "method"}, "ts": _now()
        })
    except Exception as exc:
        return {"ok": False, "msg": f"execucao falhou: {exc}"}
    return {"ok": True, "contract_id": contract_id, "method": method, "state": state, "msg": f"metodo {method} executado"}


if __name__ == "__main__":
    good = {
        "version": 1, "name": "Counter", "initial_state": {"count": 0},
        "methods": {
            "increment": [{"op": "add", "path": "count", "value": 1}, {"op": "emit", "event": "Incremented"}],
            "reset": [{"op": "set", "path": "count", "value": 0}],
        },
    }
    assert ContractVM.validate(good)[0]
    bad = {**good, "version": 99}
    assert not ContractVM.validate(bad)[0]
    bad_transfer = {"version": 1, "name": "Bad", "initial_state": {}, "methods": {"steal": [{"op": "transfer", "from": "victim", "to": "attacker", "amount": 1}]}}
    assert not ContractVM.validate(bad_transfer)[0]
    assert not ContractVM.validate({**good, "methods": {"x": [{"op": "explode"}]}})[0]
    print("ContractVM OK.")
