"""
update_peers.py — Heartbeat do nó local em peers.json (v3)
================================================================
v3 (2026-10-20):
  [SECURITY] fcntl.flock inter-processo (POSIX + Windows).
  [SECURITY] Lock file dedicado (.json.lock).
  [SECURITY] Removido default hardcoded.
  [FIX] Validação estrita de endereço.
  [FIX] Temp file com PID.
  [NEW] CLI: --dump, --remove-address, --cleanup.
================================================================
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import threading
from pathlib import Path

PEERS_FILE = Path(__file__).parent / "peers.json"
LOCK_FILE  = PEERS_FILE.parent / (PEERS_FILE.name + ".lock")

try:
    LOCK_TIMEOUT_S = float(os.environ.get("BRN_PEERS_LOCK_TIMEOUT", "5.0"))
    if LOCK_TIMEOUT_S <= 0:
        LOCK_TIMEOUT_S = 5.0
except (ValueError, TypeError):
    LOCK_TIMEOUT_S = 5.0


def _validate_address(addr: str) -> str:
    if not addr:
        raise ValueError("SEU_ENDERECO vazio")
    if ":" not in addr:
        raise ValueError(
            f"SEU_ENDERECO deve ser 'IP:PORTA' (ex: 177.82.132.98:6001); "
            f"recebido: {addr!r}"
        )
    host, _, port_s = addr.rpartition(":")
    host = host.strip("[]").strip()
    if not host:
        raise ValueError(f"SEU_ENDERECO sem host: {addr!r}")
    try:
        port = int(port_s)
    except ValueError:
        raise ValueError(f"SEU_ENDERECO porta inválida: {port_s!r}")
    if not (1 <= port <= 65535):
        raise ValueError(f"SEU_ENDERECO porta fora do range: {port}")
    return f"{host}:{port}"


SEU_ENDERECO = os.environ.get("BRN_SEU_ENDERECO", "").strip()

if SEU_ENDERECO:
    try:
        SEU_ENDERECO = _validate_address(SEU_ENDERECO)
    except ValueError as e:
        raise ValueError(str(e))


if os.name == "nt":
    import msvcrt

    def _lock_exclusive(fd: int, timeout: float) -> bool:
        deadline = time.time() + timeout
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if time.time() >= deadline:
                    return False
                time.sleep(0.05)

    def _unlock(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock_exclusive(fd: int, timeout: float) -> bool:
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except (OSError, IOError):
                if time.time() >= deadline:
                    return False
                time.sleep(0.05)

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except (OSError, IOError):
            pass


class _FileLock:
    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_S):
        self.path = path
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        if not _lock_exclusive(self.fd, self.timeout):
            try:
                os.close(self.fd)
            finally:
                self.fd = None
            raise TimeoutError(
                f"não consegui lock {self.path} em {self.timeout:.1f}s"
            )
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fd is not None:
            try:
                _unlock(self.fd)
            finally:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
        return False


_lock_thread = threading.Lock()


def _load_peers() -> dict:
    if not PEERS_FILE.exists():
        return {}
    try:
        texto = PEERS_FILE.read_text(encoding="utf-8").strip()
        if not texto:
            return {}
        peers = json.loads(texto)
        if not isinstance(peers, dict):
            print(f"[update_peers] formato inválido ({type(peers).__name__}) — recriando")
            return {}
        return peers
    except json.JSONDecodeError as e:
        print(f"[update_peers] JSON corrompido — recriando: {e}")
        return {}
    except Exception as e:
        print(f"[update_peers] erro lendo: {e}")
        return {}


def _atomic_save_peers(peers: dict) -> None:
    tmp = PEERS_FILE.parent / (
        PEERS_FILE.name + f".tmp.{os.getpid()}"
    )
    try:
        tmp.write_text(
            json.dumps(peers, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, PEERS_FILE)
    except Exception as e:
        print(f"[update_peers] erro salvando: {e}")
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


def update_local_timestamp(height: int = 0, node_id: str = "") -> int:
    if not SEU_ENDERECO:
        raise RuntimeError(
            "BRN_SEU_ENDERECO não configurado — não há onde registrar. "
            "Defina em brn_network.env ou como variável de ambiente."
        )
    agora = int(time.time())
    with _lock_thread:
        with _FileLock(LOCK_FILE, timeout=LOCK_TIMEOUT_S):
            peers = _load_peers()
            dados_no = peers.get(SEU_ENDERECO, {})
            if not isinstance(dados_no, dict):
                dados_no = {}
            dados_no["ts"] = agora
            if height and height > 0:
                dados_no["h"] = height
            if node_id:
                dados_no["id"] = str(node_id)[:32]
            peers[SEU_ENDERECO] = dados_no
            _atomic_save_peers(peers)
            _id = dados_no.get("id", "")
            _id_short = _id[:8] + "..." if _id else "(sem id)"
            _h = dados_no.get("h", "?")
            print(
                f"[update_peers] {SEU_ENDERECO} | ts={agora} | "
                f"h={_h} | id={_id_short}"
            )
            return agora


def _cli_dump() -> int:
    if not PEERS_FILE.exists():
        print(f"[peers] {PEERS_FILE} não existe")
        return 1
    try:
        peers = json.loads(PEERS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[peers] erro lendo: {e}", file=sys.stderr)
        return 1
    if not isinstance(peers, dict):
        print(f"[peers] formato inválido: {type(peers).__name__}")
        return 1
    if not peers:
        print("[peers] vazio")
        return 0
    agora = int(time.time())
    print(f"{'endereço':<30} {'ts':<12} {'idade':<10} {'h':<8} id")
    print("-" * 90)
    for addr, dados in sorted(peers.items()):
        if not isinstance(dados, dict):
            print(f"{addr:<30} <malformado>")
            continue
        ts = int(dados.get("ts", 0) or 0)
        if ts:
            idade_s = max(0, agora - ts)
            idade = f"{idade_s}s"
        else:
            idade = "?"
        h = dados.get("h", "?")
        id_short = (dados.get("id", "") or "")[:16] or "-"
        print(f"{addr:<30} {ts:<12} {idade:<10} {str(h):<8} {id_short}")
    return 0


def _cli_remove_address(addr: str) -> int:
    if not PEERS_FILE.exists():
        print(f"[peers] {PEERS_FILE} não existe")
        return 1
    with _FileLock(LOCK_FILE, timeout=LOCK_TIMEOUT_S):
        peers = _load_peers()
        if addr not in peers:
            print(f"[peers] {addr} não encontrado")
            return 1
        del peers[addr]
        _atomic_save_peers(peers)
    print(f"[peers] removido: {addr}")
    return 0


def _cli_cleanup(max_age_s: int) -> int:
    if not PEERS_FILE.exists():
        print(f"[peers] {PEERS_FILE} não existe")
        return 1
    agora = int(time.time())
    removidos = 0
    with _FileLock(LOCK_FILE, timeout=LOCK_TIMEOUT_S):
        peers = _load_peers()
        for addr in list(peers.keys()):
            dados = peers[addr]
            if not isinstance(dados, dict):
                del peers[addr]
                removidos += 1
                continue
            ts = int(dados.get("ts", 0) or 0)
            if ts and (agora - ts) > max_age_s:
                del peers[addr]
                removidos += 1
        if removidos > 0:
            _atomic_save_peers(peers)
    print(f"[peers] {removidos} entrada(s) removida(s) (max_age={max_age_s}s)")
    return 0


def _main() -> int:
    p = argparse.ArgumentParser(
        prog="update_peers.py",
        description="Heartbeat e utilidades do peers.json (BRN)",
    )
    p.add_argument("--dump", action="store_true",
                   help="mostra o conteúdo de peers.json")
    p.add_argument("--remove-address", metavar="ADDR", default=None,
                   help="remove uma entrada específica (ex: 1.2.3.4:6001)")
    p.add_argument("--cleanup", metavar="MAX_AGE_S", type=int, default=None,
                   help="remove entradas com ts mais antigo que N segundos")
    args = p.parse_args()
    if args.dump:
        return _cli_dump()
    if args.remove_address:
        return _cli_remove_address(args.remove_address)
    if args.cleanup is not None:
        if args.cleanup <= 0:
            print("[peers] --cleanup requer valor > 0", file=sys.stderr)
            return 2
        return _cli_cleanup(args.cleanup)
    update_local_timestamp()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_main())
    except KeyboardInterrupt:
        sys.exit(130)
    except TimeoutError as e:
        print(f"[update_peers] timeout no lock: {e}", file=sys.stderr)
        sys.exit(3)
    except Exception as e:
        print(f"[update_peers] erro fatal: {e}", file=sys.stderr)
        sys.exit(1)