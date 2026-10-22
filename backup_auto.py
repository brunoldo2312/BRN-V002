"""backup_auto.py - Backup rotativo do DB do BRN (v3)
================================================================
v3 (2026-10-22):
  [NEW] Configuravel via env vars:
        BRN_DB_FILE (default brn_v2_chain.db)
        BRN_BACKUP_DIR (default backups)
        BRN_MAX_BACKUPS (default 10)
        BRN_BACKUP_RETRIES (default 3)
  [NEW] Limpa arquivos .tmp orfaos de runs anteriores (crash recovery)
  [NEW] Verifica integridade do backup com sqlite PRAGMA integrity_check
  [NEW] Retry automatico em Windows (ERR_LOCK_VIOLATION) — 3 tentativas
  [NEW] CLI: --once (default), --list, --info, --verify <file>
  [NEW] Estatisticas: tamanho, tempo, espaco em disco livre
  [FIX] fsync antes do rename (garante dados no disco)
  [FIX] Preserva permissoes 0600 em POSIX
  [FIX] Erros nao abortam o loop (retorna False, nao levanta)
================================================================
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path


# ============================================================
# CONFIG
# ============================================================
DB_FILE        = os.environ.get("BRN_DB_FILE", "brn_v2_chain.db")
BACKUP_DIR     = os.environ.get("BRN_BACKUP_DIR", "backups")
MAX_BACKUPS    = int(os.environ.get("BRN_MAX_BACKUPS", "10"))
BACKUP_RETRIES = int(os.environ.get("BRN_BACKUP_RETRIES", "3"))
RETRY_DELAY_S  = float(os.environ.get("BRN_BACKUP_RETRY_DELAY", "1.0"))
VERIFY_INTEGRITY = os.environ.get("BRN_BACKUP_VERIFY", "1") == "1"

if MAX_BACKUPS < 1:
    MAX_BACKUPS = 1
if BACKUP_RETRIES < 1:
    BACKUP_RETRIES = 1


# ============================================================
# HELPERS
# ============================================================
def _log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [BACKUP/{level}] {msg}", flush=True)


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def _fsync_file(path: str) -> None:
    try:
        with open(path, "rb+") as f:
            os.fsync(f.fileno())
    except Exception:
        pass


def _copy_with_retry(src: str, dst: str, retries: int = BACKUP_RETRIES) -> bool:
    """
    Copia arquivo com retry em Windows file lock.
    Retorna True em sucesso, False se esgotar tentativas.
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            shutil.copy2(src, dst)
            return True
        except (PermissionError, OSError) as e:
            last_err = e
            # WinError 32 = ERROR_SHARING_VIOLATION
            winerr = getattr(e, "winerror", None)
            if winerr == 32 or isinstance(e, PermissionError):
                if attempt < retries:
                    _log(f"file lock (tentativa {attempt}/{retries}): {e}", "WARN")
                    time.sleep(RETRY_DELAY_S * attempt)
                    continue
            raise
    if last_err:
        raise last_err
    return False


def _verify_sqlite(path: str) -> tuple[bool, str]:
    """Verifica integridade do backup com PRAGMA integrity_check."""
    try:
        conn = sqlite3.connect(path, timeout=10.0)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            result = row[0] if row else "?"
            if result == "ok":
                # Conta blocos para sanity check
                try:
                    n = conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
                    h = conn.execute("SELECT MAX(height) FROM blocks").fetchone()[0]
                    return True, f"OK (blocks={n}, height={h})"
                except Exception:
                    return True, "OK"
            return False, f"integrity_check: {result}"
        finally:
            conn.close()
    except Exception as e:
        return False, f"erro: {e}"


# ============================================================
# BACKUP ATÔMICO
# ============================================================
def _copy_db_atomic(src: str, dst: str) -> None:
    """
    Copia o DB + WAL + SHM de forma atomica.
    fsync antes do rename. Preserva permissoes.
    """
    dst_wal = dst + "-wal"
    dst_shm = dst + "-shm"
    tmp = dst + ".tmp"
    tmp_wal = dst_wal + ".tmp"
    tmp_shm = dst_shm + ".tmp"

    # 1) Copia para .tmp (com retry no source)
    _copy_with_retry(src, tmp)
    _fsync_file(tmp)

    src_wal = src + "-wal"
    src_shm = src + "-shm"

    if os.path.exists(src_wal):
        _copy_with_retry(src_wal, tmp_wal)
        _fsync_file(tmp_wal)

    if os.path.exists(src_shm):
        _copy_with_retry(src_shm, tmp_shm)
        _fsync_file(tmp_shm)

    # 2) Renomeia (atômico em ambos OS)
    os.replace(tmp, dst)
    if os.path.exists(tmp_wal):
        os.replace(tmp_wal, dst_wal)
    if os.path.exists(tmp_shm):
        os.replace(tmp_shm, dst_shm)

    # 3) Permissoes restritas (POSIX)
    if os.name != "nt":
        try:
            os.chmod(dst, 0o600)
            for p in (dst_wal, dst_shm):
                if os.path.exists(p):
                    os.chmod(p, 0o600)
        except Exception:
            pass


# ============================================================
# CLEANUP DE .TMP ORFAOS
# ============================================================
def _cleanup_stale_tmp() -> int:
    """Remove .tmp orfaos de runs anteriores (crash recovery)."""
    if not os.path.isdir(BACKUP_DIR):
        return 0
    removed = 0
    for pattern in ("*.db.tmp", "*.db-wal.tmp", "*.db-shm.tmp"):
        for f in glob.glob(os.path.join(BACKUP_DIR, pattern)):
            try:
                os.remove(f)
                removed += 1
            except Exception:
                pass
    if removed:
        _log(f"removidos {removed} arquivo(s) .tmp orfao(s)")
    return removed


# ============================================================
# BACKUP PRINCIPAL
# ============================================================
def backup() -> bool:
    t0 = time.time()

    os.makedirs(BACKUP_DIR, exist_ok=True)

    if not os.path.exists(DB_FILE):
        _log(f"{DB_FILE} nao encontrado", "ERROR")
        return False

    # 1) Limpa .tmp de runs anteriores
    _cleanup_stale_tmp()

    # 2) Nome do backup
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"chain_{ts}.db")

    # 3) Copia atomica
    try:
        _copy_db_atomic(DB_FILE, dest)
    except Exception as e:
        _log(f"copia falhou: {e}", "ERROR")
        # Cleanup parcial
        for f in (dest + ".tmp", dest + "-wal.tmp", dest + "-shm.tmp"):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass
        return False

    dt = time.time() - t0
    size = os.path.getsize(dest)

    # 4) Verifica integridade
    verify_msg = ""
    if VERIFY_INTEGRITY:
        ok, verify_msg = _verify_sqlite(dest)
        if not ok:
            _log(f"verificacao falhou: {verify_msg}", "ERROR")
            # Remove o backup corrompido
            try:
                os.remove(dest)
                for ext in ("-wal", "-shm"):
                    if os.path.exists(dest + ext):
                        os.remove(dest + ext)
            except Exception:
                pass
            return False

    # 5) Disk space
    try:
        df = shutil.disk_usage(BACKUP_DIR)
        free_str = _human_size(df.free)
    except Exception:
        free_str = "?"

    _log(f"OK: {dest} ({_human_size(size)}, {dt:.2f}s, verify={verify_msg}, free={free_str})")

    # 6) Remove antigos
    backups = sorted(
        [b for b in glob.glob(os.path.join(BACKUP_DIR, "chain_*.db"))
         if ".tmp" not in b]
    )
    while len(backups) > MAX_BACKUPS:
        old = backups.pop(0)
        try:
            os.remove(old)
            for ext in ("-wal", "-shm"):
                if os.path.exists(old + ext):
                    os.remove(old + ext)
            _log(f"removido antigo: {os.path.basename(old)}")
        except Exception:
            pass

    return True


# ============================================================
# CLI UTILITARIOS
# ============================================================
def _cmd_list() -> int:
    if not os.path.isdir(BACKUP_DIR):
        print(f"(diretorio {BACKUP_DIR} nao existe)")
        return 0

    backups = sorted(
        [b for b in glob.glob(os.path.join(BACKUP_DIR, "chain_*.db"))
         if ".tmp" not in b]
    )
    if not backups:
        print("(nenhum backup)")
        return 0

    print(f"{'arquivo':<35} {'tamanho':>12} {'modificado':<20}")
    print("-" * 70)
    for b in backups:
        try:
            st = os.stat(b)
            mod = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            print(f"{os.path.basename(b):<35} {_human_size(st.st_size):>12} {mod:<20}")
        except Exception:
            print(f"{os.path.basename(b):<35} (erro)")

    print(f"\nTotal: {len(backups)} backup(s), max={MAX_BACKUPS}")
    return 0


def _cmd_info() -> int:
    print(f"DB_FILE         = {os.path.abspath(DB_FILE)}")
    print(f"BACKUP_DIR      = {os.path.abspath(BACKUP_DIR)}")
    print(f"MAX_BACKUPS     = {MAX_BACKUPS}")
    print(f"BACKUP_RETRIES  = {BACKUP_RETRIES}")
    print(f"VERIFY_INTEGRITY = {VERIFY_INTEGRITY}")
    print()

    if os.path.exists(DB_FILE):
        st = os.stat(DB_FILE)
        mod = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"DB existe       = sim ({_human_size(st.st_size)}, mod {mod})")
        # WAL/SHM
        for ext in ("-wal", "-shm"):
            if os.path.exists(DB_FILE + ext):
                s = os.stat(DB_FILE + ext)
                print(f"  {ext:<5}          = {_human_size(s.st_size)}")
    else:
        print(f"DB existe       = NAO")

    n = len(glob.glob(os.path.join(BACKUP_DIR, "chain_*.db"))) if os.path.isdir(BACKUP_DIR) else 0
    print(f"Backups         = {n}")

    try:
        df = shutil.disk_usage(BACKUP_DIR if os.path.isdir(BACKUP_DIR) else ".")
        print(f"Espaco livre    = {_human_size(df.free)} de {_human_size(df.total)}")
    except Exception:
        pass
    return 0


def _cmd_verify(path: str) -> int:
    if not os.path.exists(path):
        print(f"ARQUIVO NAO ENCONTRADO: {path}")
        return 1
    print(f"Verificando {path} ({_human_size(os.path.getsize(path))})...")
    ok, msg = _verify_sqlite(path)
    if ok:
        print(f"OK: {msg}")
        return 0
    else:
        print(f"FALHOU: {msg}")
        return 1


# ============================================================
# CLI
# ============================================================
def main() -> int:
    p = argparse.ArgumentParser(
        prog="backup_auto.py",
        description="Backup rotativo do DB do BRN",
    )
    p.add_argument("--once", action="store_true", default=True,
                   help="faz 1 backup (default)")
    p.add_argument("--list", action="store_true",
                   help="lista backups existentes")
    p.add_argument("--info", action="store_true",
                   help="mostra configuracao e estado")
    p.add_argument("--verify", metavar="FILE",
                   help="verifica integridade de um arquivo de backup")
    args = p.parse_args()

    if args.list:
        return _cmd_list()
    if args.info:
        return _cmd_info()
    if args.verify:
        return _cmd_verify(args.verify)

    # Default: backup
    ok = backup()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
