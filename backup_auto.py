"""backup_auto.py - Backup rotativo do DB do BRN (v4)
================================================================
v4 (2026-10-27):
  [FIX CRÍTICO] Backup agora usa sqlite3.Connection.backup() em vez
                de shutil.copy2. Isso garante um snapshot CONSISTENTE
                do DB mesmo com o nó escrevendo em paralelo (WAL,
                transações em andamento, checkpoint automático).
                O copy2 antigo podia gerar backup corrompido se a
                escrita caísse entre páginas.
  [FIX] WAL e SHM do destino NÃO são copiados manualmente: a API
        de backup os incorpora no arquivo principal. Arquivos
        residuais (se existirem após close) são removidos.
  [FIX] Se o SQLite não conseguir abrir o DB (arquivo corrompido),
        ABORTA em vez de copiar cego. Backup de arquivo corrompido
        com nome de backup é pior que falha explícita.
  [KEEP] Retry automático em Windows (ERR_LOCK_VIOLATION).
  [KEEP] Verificação de integridade com PRAGMA integrity_check.
  [KEEP] fsync antes do rename.
  [KEEP] Preserva permissões 0600 em POSIX.
  [KEEP] Limpa .tmp órfãos de runs anteriores.
  [KEEP] CLI: --once (default), --list, --info, --verify <file>.
  [NEW] Mostra duração + tamanho + espaço livre + resultado verify.
v3 (2026-10-22):
  [NEW] Configurável via env vars:
        BRN_DB_FILE, BRN_BACKUP_DIR, BRN_MAX_BACKUPS,
        BRN_BACKUP_RETRIES, BRN_BACKUP_RETRY_DELAY, BRN_BACKUP_VERIFY
================================================================
"""
from __future__ import annotations

import argparse
import glob
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


def _verify_sqlite(path: str) -> tuple[bool, str]:
    """Verifica integridade do backup com PRAGMA integrity_check."""
    try:
        conn = sqlite3.connect(path, timeout=10.0)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            result = row[0] if row else "?"
            if result == "ok":
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


def _verify_is_readonly(path: str) -> tuple[bool, str]:
    """
    Abre o arquivo em modo read-only para confirmar que o SQLite
    consegue lê-lo sem recuperação pendente. Não faz integrity_check
    (que já é feito em _verify_sqlite).
    """
    try:
        p = Path(path).resolve()
        uri = p.as_uri()
        sep = "&" if "?" in uri else "?"
        conn = sqlite3.connect(f"{uri}{sep}mode=ro", uri=True, timeout=10.0)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return True, "leitura OK"
    except Exception as e:
        return False, f"nao abre read-only: {e}"


# ============================================================
# BACKUP ATÔMICO VIA SQLite API
# ============================================================
def _backup_sqlite_atomic(
    src: str, dst: str, retries: int = BACKUP_RETRIES
) -> None:
    """
    Backup consistente do DB via sqlite3.Connection.backup().

    - Escreve em dst.tmp (SQLite cria o arquivo).
    - fsync no arquivo final antes do rename.
    - Permissões 0600 em POSIX.
    - os.replace() para renomear atomicamente.

    Retry automático para locks (Windows ERR_LOCK_VIOLATION, "busy",
    "locked"). Em erro definitivo, limpa .tmp parcial e propaga.
    """
    dst_tmp = dst + ".tmp"
    residues = (dst_tmp, dst_tmp + "-wal", dst_tmp + "-shm")

    # Cleanup de qualquer resíduo antes da primeira tentativa
    for p in residues:
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            # Source: RW mode permite WAL recovery se necessário.
            # Nunca escrevemos no source, só lemos via backup().
            src_conn = sqlite3.connect(src, timeout=30.0)
            try:
                dst_conn = sqlite3.connect(dst_tmp, timeout=30.0)
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()

            # Força dados no disco antes do rename
            _fsync_file(dst_tmp)

            # Permissões restritas (POSIX)
            if os.name != "nt":
                try:
                    os.chmod(dst_tmp, 0o600)
                except OSError:
                    pass

            # Rename atômico
            os.replace(dst_tmp, dst)

            # Remove WAL/SHM residuais do destino (se houver)
            for ext in ("-wal", "-shm"):
                p = dst + ext
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            return

        except (sqlite3.Error, OSError) as e:
            last_err = e
            winerr = getattr(e, "winerror", None)
            msg = str(e).lower()
            is_retryable = (
                winerr == 32
                or isinstance(e, PermissionError)
                or "locked" in msg
                or "busy" in msg
            )
            if is_retryable and attempt < retries:
                _log(
                    f"lock no SQLite backup (tentativa {attempt}/{retries}): {e}",
                    "WARN",
                )
                time.sleep(RETRY_DELAY_S * attempt)
                for p in residues:
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except OSError:
                        pass
                continue

            # Não retentável OU esgotou tentativas: limpar e propagar
            for p in residues:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            raise

    if last_err:
        raise last_err


# ============================================================
# CLEANUP DE .TMP ÓRFÃOS
# ============================================================
def _cleanup_stale_tmp() -> int:
    """Remove .tmp órfãos de runs anteriores (crash recovery)."""
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

    # 3) Backup consistente via API SQLite
    try:
        _backup_sqlite_atomic(DB_FILE, dest)
    except Exception as e:
        _log(f"backup SQLite falhou: {e}", "ERROR")
        # Cleanup parcial
        for p in (dest + ".tmp", dest + "-wal", dest + "-shm"):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        return False

    dt = time.time() - t0
    try:
        size = os.path.getsize(dest)
    except OSError:
        size = 0

    # 4) Verificação: read-only abrir + integrity_check
    verify_msg = ""
    if VERIFY_INTEGRITY:
        # 4a) Confirma que abre read-only (pega arquivo meio-escrito)
        ok_ro, msg_ro = _verify_is_readonly(dest)
        if not ok_ro:
            _log(f"verificacao read-only falhou: {msg_ro}", "ERROR")
            try:
                os.remove(dest)
                for ext in ("-wal", "-shm"):
                    p = dest + ext
                    if os.path.exists(p):
                        os.remove(p)
            except OSError:
                pass
            return False

        # 4b) PRAGMA integrity_check
        ok, verify_msg = _verify_sqlite(dest)
        if not ok:
            _log(f"verificacao falhou: {verify_msg}", "ERROR")
            try:
                os.remove(dest)
                for ext in ("-wal", "-shm"):
                    p = dest + ext
                    if os.path.exists(p):
                        os.remove(p)
            except OSError:
                pass
            return False

    # 5) Disk space
    try:
        df = shutil.disk_usage(BACKUP_DIR)
        free_str = _human_size(df.free)
    except Exception:
        free_str = "?"

    _log(
        f"OK: {dest} ({_human_size(size)}, {dt:.2f}s, "
        f"verify={verify_msg}, free={free_str})"
    )

    # 6) Remove antigos (rotação)
    backups = sorted(
        [b for b in glob.glob(os.path.join(BACKUP_DIR, "chain_*.db"))
         if ".tmp" not in b]
    )
    while len(backups) > MAX_BACKUPS:
        old = backups.pop(0)
        try:
            os.remove(old)
            for ext in ("-wal", "-shm"):
                p = old + ext
                if os.path.exists(p):
                    os.remove(p)
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
    print(f"DB_FILE          = {os.path.abspath(DB_FILE)}")
    print(f"BACKUP_DIR       = {os.path.abspath(BACKUP_DIR)}")
    print(f"MAX_BACKUPS      = {MAX_BACKUPS}")
    print(f"BACKUP_RETRIES   = {BACKUP_RETRIES}")
    print(f"RETRY_DELAY_S    = {RETRY_DELAY_S}")
    print(f"VERIFY_INTEGRITY = {VERIFY_INTEGRITY}")
    print()

    if os.path.exists(DB_FILE):
        st = os.stat(DB_FILE)
        mod = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"DB existe        = sim ({_human_size(st.st_size)}, mod {mod})")
        for ext in ("-wal", "-shm"):
            if os.path.exists(DB_FILE + ext):
                s = os.stat(DB_FILE + ext)
                print(f"  {ext:<5}           = {_human_size(s.st_size)}")
    else:
        print(f"DB existe        = NAO")

    n = len(glob.glob(os.path.join(BACKUP_DIR, "chain_*.db"))) if os.path.isdir(BACKUP_DIR) else 0
    print(f"Backups          = {n}")

    try:
        df = shutil.disk_usage(BACKUP_DIR if os.path.isdir(BACKUP_DIR) else ".")
        print(f"Espaco livre     = {_human_size(df.free)} de {_human_size(df.total)}")
    except Exception:
        pass
    return 0


def _cmd_verify(path: str) -> int:
    if not os.path.exists(path):
        print(f"ARQUIVO NAO ENCONTRADO: {path}")
        return 1
    print(f"Verificando {path} ({_human_size(os.path.getsize(path))})...")

    ok_ro, msg_ro = _verify_is_readonly(path)
    if not ok_ro:
        print(f"  [X] read-only: {msg_ro}")
        return 1
    print(f"  [ok] read-only: {msg_ro}")

    ok, msg = _verify_sqlite(path)
    if ok:
        print(f"  [ok] integrity: {msg}")
        return 0
    else:
        print(f"  [X] integrity: {msg}")
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
