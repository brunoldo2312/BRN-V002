"""backup_manager.py — Backup/restore do estado do nó BRN (v1.0)
================================================================
Soluções:
  A) Manual  — create_backup() / restore_backup() (ZIP local)
  B) Automática — save_to_mongo() / restore_from_mongo() (GridFS)
================================================================
"""
from __future__ import annotations
import io
import os
import json
import time
import zipfile
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger("backup_manager")

MAX_BACKUP_SIZE = 500 * 1024 * 1024   # 500 MB
MONGO_BACKUP_KEEP = 3                  # mantém os 3 mais recentes
MONGO_COLLECTION = "brn_backups"

# (kind, caminho principal, caminho alternativo)
# ordem importa: node_password e node_identity antes do resto
CRITICAL_FILES = [
    ("node_password",   ".node_password",           None),
    ("node_identity",   "node_identity.enc",        None),
    ("wallet",          "current_wallet.enc",       "wallets/current_wallet.enc"),
    ("validator_sk",    ".validator_sk",            None),
    ("admin_token",     ".brn_admin_token",         None),
    ("chain",           "brn_v2_chain.db",          None),
    ("chain_wal",       "brn_v2_chain.db-wal",      None),
    ("chain_shm",       "brn_v2_chain.db-shm",      None),
    ("trust",           "trust_network.json",       None),
    ("user_wallets",    "user_wallets.json",        None),
    ("network_env",     "brn_network.env",          None),
    ("bootstrap",       "bootstrap_peers.json",     None),
    ("checkpoints",     "checkpoints.json",         None),
]


def _base() -> Path:
    return Path(__file__).resolve().parent


# ============================================================
# LISTAGEM
# ============================================================
def list_backup_files() -> list:
    """Arquivos críticos que existem agora e entrariam no backup."""
    base = _base()
    out = []
    for kind, main, alt in CRITICAL_FILES:
        for rel in (main, alt):
            if not rel:
                continue
            p = base / rel
            if p.exists() and p.is_file():
                st = p.stat()
                out.append({
                    "kind": kind,
                    "path": rel,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                })
                break
    return out


def _get_chain_height() -> int:
    try:
        db = _base() / "brn_v2_chain.db"
        if not db.exists():
            return -1
        c = sqlite3.connect(str(db), timeout=2)
        try:
            r = c.execute("SELECT MAX(height) FROM blocks").fetchone()
            return int(r[0]) if r and r[0] is not None else -1
        finally:
            c.close()
    except Exception:
        return -1


# ============================================================
# SOLUÇÃO A — ZIP LOCAL
# ============================================================
def create_backup(password: Optional[str] = None) -> bytes:
    """
    Cria ZIP com todos os arquivos críticos.

    Formato:
      - password=None  → b"BRNR" + ZIP puro (legível, só para debug local)
      - password!=None → b"BRNZ" + envelope cifrado com Argon2id+ChaCha20
    """
    files = list_backup_files()
    if not files:
        raise ValueError("nenhum arquivo crítico encontrado")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        manifest = {
            "version": 1,
            "created_at": int(time.time()),
            "chain_height": _get_chain_height(),
            "files": files,
            "note": "Contém chaves privadas. NÃO compartilhe.",
        }
        zf.writestr("MANIFEST.json", json.dumps(manifest, indent=2))
        for f in files:
            zf.write(_base() / f["path"], arcname=f"files/{f['path']}")
    raw = buf.getvalue()

    if password:
        from secure_store import encrypt_blob
        return b"BRNZ" + encrypt_blob(raw, password)
    return b"BRNR" + raw


def restore_backup(blob: bytes, password: Optional[str] = None,
                   backup_current: bool = True) -> dict:
    """Restaura ZIP local. backup_current=True faz snapshot antes."""
    if len(blob) < 5:
        return {"ok": False, "msg": "arquivo vazio ou truncado"}

    header = blob[:4]
    if header == b"BRNZ":
        if not password:
            return {"ok": False, "msg": "backup cifrado — senha obrigatória"}
        try:
            from secure_store import decrypt_blob
            raw = decrypt_blob(blob[4:], password)
        except Exception as e:
            return {"ok": False, "msg": f"senha incorreta / backup corrompido: {e}"}
    elif header == b"BRNR":
        raw = blob[4:]
    else:
        return {"ok": False, "msg": "formato desconhecido (esperado BRNR/BRNZ)"}

    if len(raw) > MAX_BACKUP_SIZE:
        return {"ok": False, "msg": "backup excede 500MB"}

    # Snapshot do estado atual
    current_backup = None
    if backup_current:
        try:
            snap = _base() / f"pre_restore_{int(time.time())}.zip"
            snap.write_bytes(create_backup())
            current_backup = str(snap)
        except Exception as e:
            log.warning("snapshot pré-restore falhou: %s", e)

    # Extrai
    restored, errors = [], []
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        if "MANIFEST.json" not in zf.namelist():
            return {"ok": False, "msg": "backup sem MANIFEST.json"}
        manifest = json.loads(zf.read("MANIFEST.json"))
        if manifest.get("version") != 1:
            return {"ok": False, "msg": f"versão {manifest.get('version')} não suportada"}

        base = _base().resolve()
        for entry in zf.infolist():
            if not entry.filename.startswith("files/"):
                continue
            rel = entry.filename[len("files/"):]
            target = (base / rel).resolve()

            # path traversal guard
            if not str(target).startswith(str(base) + os.sep):
                errors.append(f"path traversal bloqueado: {rel}")
                continue

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                data = zf.read(entry.filename)
                tmp = target.with_name(f".{target.name}.restore.{os.getpid()}")
                tmp.write_bytes(data)
                os.replace(tmp, target)
                try:
                    os.chmod(target, 0o600)
                except Exception:
                    pass
                restored.append(rel)
            except Exception as e:
                errors.append(f"{rel}: {e}")

        return {
            "ok": len(errors) == 0,
            "restored": restored,
            "errors": errors,
            "snapshot_of_current": current_backup,
            "restart_required": True,
            "msg": f"{len(restored)} arquivo(s) restaurado(s)",
        }
    except Exception as e:
        return {"ok": False, "msg": f"falha ao extrair: {e}"}


# ============================================================
# SOLUÇÃO B — MongoDB GridFS (automático)
# ============================================================
def save_to_mongo(password: Optional[str] = None) -> dict:
    """Salva backup no GridFS. Mantém os N mais recentes."""
    try:
        from mongo_client import mongo
        if not mongo.is_configured():
            return {"ok": False, "msg": "mongo não configurado"}
        import gridfs
        fs = gridfs.GridFS(mongo.db(), collection=MONGO_COLLECTION)

        blob = create_backup(password=password)
        # apaga antigos
        try:
            old = list(fs.find().sort("uploadDate", -1).skip(MONGO_BACKUP_KEEP))
            for f in old:
                fs.delete(f._id)
        except Exception:
            pass

        file_id = fs.put(
            blob,
            filename=f"brn_backup_{int(time.time())}.zip",
            metadata={
                "created_at": time.time(),
                "size": len(blob),
                "encrypted": bool(password),
                "chain_height": _get_chain_height(),
            },
        )
        return {"ok": True, "file_id": str(file_id), "size": len(blob)}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def restore_from_mongo(password: Optional[str] = None) -> dict:
    """Restaura o backup mais recente do GridFS."""
    try:
        from mongo_client import mongo
        if not mongo.is_configured():
            return {"ok": False, "msg": "mongo não configurado"}
        import gridfs
        fs = gridfs.GridFS(mongo.db(), collection=MONGO_COLLECTION)

        latest = fs.find_one(sort=[("uploadDate", -1)])
        if latest is None:
            return {"ok": False, "msg": "nenhum backup no mongo"}

        blob = latest.read()
        pw = password or os.environ.get("BRN_BACKUP_PASSWORD", "").strip() or None
        result = restore_backup(blob, password=pw, backup_current=False)
        result["source"] = f"mongo:{latest._id}"
        return result
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def list_mongo_backups() -> list:
    try:
        from mongo_client import mongo
        if not mongo.is_configured():
            return []
        import gridfs
        fs = gridfs.GridFS(mongo.db(), collection=MONGO_COLLECTION)
        out = []
        for f in fs.find().sort("uploadDate", -1):
            md = f.metadata or {}
            out.append({
                "file_id": str(f._id),
                "filename": f.filename,
                "size": md.get("size", 0),
                "chain_height": md.get("chain_height", -1),
                "encrypted": md.get("encrypted", False),
                "created_at": int(md.get("created_at", 0)),
            })
        return out
    except Exception:
        return []


# ============================================================
# AUTO-RESTORE NO BOOT
# ============================================================
def _db_is_empty() -> bool:
    db = _base() / "brn_v2_chain.db"
    if not db.exists():
        return True
    try:
        c = sqlite3.connect(str(db), timeout=2)
        try:
            r = c.execute("SELECT COUNT(*) FROM blocks").fetchone()
            return (r[0] if r else 0) == 0
        finally:
            c.close()
    except Exception:
        return True


def auto_restore_on_boot() -> dict:
    """
    Chamado no main.py ANTES de abrir o DB.
    Ordem: mongo → diretório externo. Só roda se DB estiver vazio.
    """
    if not _db_is_empty():
        return {"ok": False, "msg": "DB já tem dados — pulando auto-restore"}

    # 1. MongoDB
    try:
        r = restore_from_mongo()
        if r.get("ok"):
            log.warning("[AUTO-RESTORE] mongo OK: %d arquivo(s)", len(r.get("restored", [])))
            return r
    except Exception as e:
        log.debug("[AUTO-RESTORE] mongo falhou: %s", e)

    # 2. Diretório externo (BRN_RESTORE_DIR)
    rdir = os.environ.get("BRN_RESTORE_DIR", "").strip()
    if rdir:
        try:
            p = Path(rdir)
            if p.exists():
                backups = sorted(
                    p.glob("brn_backup_*.zip*"),
                    key=lambda x: x.stat().st_mtime,
                    reverse=True,
                )
                if backups:
                    blob = backups[0].read_bytes()
                    pw = os.environ.get("BRN_BACKUP_PASSWORD", "").strip() or None
                    r = restore_backup(blob, password=pw, backup_current=False)
                    r["source"] = str(backups[0])
                    if r.get("ok"):
                        log.warning("[AUTO-RESTORE] dir OK: %s", backups[0])
                        return r
        except Exception as e:
            log.debug("[AUTO-RESTORE] dir falhou: %s", e)

    return {"ok": False, "msg": "nenhuma fonte de restore disponível"}


def start_periodic_backup(interval_s: int = 300) -> threading.Thread:
    """Thread que salva no MongoDB a cada N segundos."""
    def _loop():
        time.sleep(30)  # boot grace
        while True:
            try:
                pw = os.environ.get("BRN_BACKUP_PASSWORD", "").strip() or None
                r = save_to_mongo(password=pw)
                if r.get("ok"):
                    log.info("[AUTO-BACKUP] mongo: %s (%d bytes)",
                             r.get("file_id"), r.get("size"))
                else:
                    log.debug("[AUTO-BACKUP] falhou: %s", r.get("msg"))
            except Exception as e:
                log.debug("[AUTO-BACKUP] erro: %s", e)
            time.sleep(interval_s)

    t = threading.Thread(target=_loop, daemon=True, name="AutoBackup")
    t.start()
    return t
