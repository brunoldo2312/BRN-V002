"""
sync_manual.py — Sincroniza o nó local com um peer específico (v2)
================================================================
Uso: python sync_manual.py 192.168.0.10 6001 [-y]
================================================================
"""
from __future__ import annotations

import argparse
import sys
import time

from blockchain import Blockchain

try:
    from blockchain import block_hash, meets_difficulty
    _HAS_HELPERS = True
except ImportError:
    block_hash = None
    meets_difficulty = None
    _HAS_HELPERS = False

try:
    from p2p_unified import P2PClient, CHAIN_ID
except Exception as e:
    print(f"[sync] erro importando p2p_unified: {e}", file=sys.stderr)
    sys.exit(3)


BATCH             = 5
MAX_RPC_RETRIES   = 3
RETRY_BASE_S      = 0.5
RPC_TIMEOUT_S     = 15.0


def _rpc(ip: str, port: int, msg: dict, timeout: float = RPC_TIMEOUT_S):
    last_err = ""
    for attempt in range(MAX_RPC_RETRIES):
        try:
            result = P2PClient.send_message(ip, port, msg, timeout=timeout)
            if isinstance(result, tuple) and len(result) == 2:
                resp, _latency = result
            else:
                resp = result
            if resp is not None:
                return resp
            last_err = "sem resposta"
        except Exception as e:
            last_err = str(e)
        if attempt < MAX_RPC_RETRIES - 1:
            time.sleep(RETRY_BASE_S * (attempt + 1))
    print(f"[sync] peer não respondeu após {MAX_RPC_RETRIES} tentativas: {last_err}",
          file=sys.stderr)
    return None


def _fmt_eta(seconds: float) -> str:
    if seconds < 0:
        return "?"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s}s"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h {m}m"


def _validate_block(blk: dict, expected_h: int, expected_prev: str):
    if not isinstance(blk, dict):
        return False, "bloco não é dict"
    if blk.get("height") != expected_h:
        return False, f"height {blk.get('height')} != esperado {expected_h}"
    if blk.get("prev_hash") != expected_prev:
        return False, (
            f"prev_hash diverge em #{expected_h}: "
            f"{str(blk.get('prev_hash'))[:16]}... != {expected_prev[:16]}..."
        )
    if not _HAS_HELPERS:
        return True, ""
    try:
        h_calc = block_hash(
            blk["prev_hash"], blk["merkle"], blk["timestamp"],
            blk["nonce"], blk["difficulty"],
        )
    except KeyError as e:
        return False, f"campo ausente: {e}"
    except Exception as e:
        return False, f"block_hash falhou: {e}"
    if h_calc != blk["hash"]:
        return False, f"hash inválido em #{expected_h}"
    try:
        if not meets_difficulty(blk["hash"], blk["difficulty"]):
            return False, f"PoW insuficiente em #{expected_h}"
    except Exception as e:
        return False, f"meets_difficulty falhou: {e}"
    return True, ""


def _parse_args():
    p = argparse.ArgumentParser(
        prog="sync_manual.py",
        description="Sincroniza nó local com um peer específico (BRN)",
    )
    p.add_argument("ip", help="IP/host do peer")
    p.add_argument("port", type=int, help="porta TCP do peer (ex: 6001)")
    p.add_argument("-y", "--yes", action="store_true",
                   help="não pede confirmação (para automação)")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    ip, port = args.ip, args.port
    if not (1 <= port <= 65535):
        print(f"[sync] porta inválida: {port}", file=sys.stderr)
        return 3
    print(f"[sync] chain_id : {CHAIN_ID}")
    print(f"[sync] peer     : {ip}:{port}")
    r = _rpc(ip, port, {"type": "get_chain_height"})
    if not r:
        print(f"[sync] peer inacessível: {ip}:{port}", file=sys.stderr)
        return 3
    remote_h = int(r.get("height", -1))
    if remote_h < 0:
        print("[sync] peer sem genesis", file=sys.stderr)
        return 3
    print(f"[sync] altura peer: {remote_h}")

    chain = Blockchain("brn_v2_chain.db", auto_genesis=False)
    try:
        local_h   = chain.db.height()
        local_tip = chain.db.tip_hash() or ("0" * 64)
        print(f"[sync] altura local: {local_h}")
        print(f"[sync] tip local   : {local_tip[:24]}...")

        if local_h >= 0:
            r = _rpc(ip, port, {"type": "get_block", "height": local_h})
            if not r or not r.get("block"):
                print(f"[sync] peer não tem bloco #{local_h}", file=sys.stderr)
                return 3
            peer_blk = r["block"]
            if peer_blk["hash"] != local_tip:
                print(f"[sync] FORK em #{local_h}", file=sys.stderr)
                print(f"[sync]   local: {local_tip[:24]}...", file=sys.stderr)
                print(f"[sync]   peer : {peer_blk['hash'][:24]}...", file=sys.stderr)
                return 2
            print(f"[sync] bloco #{local_h} bate. Mesma cadeia até aqui.")

        if remote_h <= local_h:
            print("[sync] peer não tem blocos novos. Nada a fazer.")
            return 0

        n_blocos = remote_h - local_h
        if not args.yes:
            print(f"[sync] vai aplicar {n_blocos} bloco(s) "
                  f"#{local_h + 1}...#{remote_h}")
            try:
                resp = input("[sync] continuar? [s/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return 1
            if resp not in ("s", "sim", "y", "yes"):
                print("[sync] abortado")
                return 1

        applied = 0
        cursor  = local_h + 1
        t0      = time.time()

        while cursor <= remote_h:
            end = min(cursor + BATCH, remote_h + 1)
            r = _rpc(ip, port, {
                "type": "get_blocks_range",
                "start": cursor, "end": end,
            })
            if not r or "blocks" not in r:
                print(f"[sync] peer não devolveu lote {cursor}-{end}",
                      file=sys.stderr)
                return 3
            lote = r["blocks"] or []
            if not lote:
                print(f"[sync] peer devolveu lote vazio {cursor}-{end}",
                      file=sys.stderr)
                return 3
            expected_h    = chain.db.height() + 1
            expected_prev = chain.db.tip_hash() or ("0" * 64)
            for blk in lote:
                ok, motivo = _validate_block(blk, expected_h, expected_prev)
                if not ok:
                    print(f"[sync] BLOCO INVÁLIDO: {motivo}", file=sys.stderr)
                    return 3
                expected_h    += 1
                expected_prev = blk["hash"]
            for blk in lote:
                ok, msg = chain.accept_block(blk)
                if not ok:
                    print(f"[sync] accept_block #{blk['height']} falhou: {msg}",
                          file=sys.stderr)
                    return 4
                applied += 1
            cursor  = end
            pct     = applied / n_blocos * 100 if n_blocos else 100
            elapsed = time.time() - t0
            rate    = applied / elapsed if elapsed > 0 else 0
            eta     = (n_blocos - applied) / rate if rate > 0 else 0
            print(f"[sync] {applied}/{n_blocos} ({pct:.0f}%) | "
                  f"{rate:.1f} bl/s | ETA {_fmt_eta(eta)}")

        print(f"[sync] OK! {applied} bloco(s) aplicados")
        print(f"[sync] nova altura: {chain.db.height()}")
        return 0
    finally:
        try:
            chain.db.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[sync] cancelado", file=sys.stderr)
        sys.exit(130)