"""mongo_client.py — Cliente MongoDB opcional do BRN
================================================================
Wrapper fino sobre pymongo com detecção de configuração,
reconexão preguiçosa e fallback silencioso.

API pública (usada por main.py, server.py, rendezvous_mongo.py):
    mongo.is_configured() -> bool
    mongo.db()            -> Database | None
    mongo.ping()          -> (ok: bool, err: str | None)
    mongo.close()
    mongo.uri             -> str
    mongo.db_name         -> str

Campos:
    user, password, host, db_name, uri

Env vars:
    MONGO_USER     (default: "")
    MONGO_PASS     (default: "")
    MONGO_HOST     (default: "")
    MONGO_DB       (default: "brn_analytics")
    MONGO_URI      (override direto — se definido, ignora os outros)
    MONGO_TIMEOUT  (default: 3s)

Comportamento:
    - Se MONGO_PASS vazio → is_configured() retorna False
    - Se pymongo não instalado → db() retorna None (silencioso)
    - Reconecta automaticamente em caso de falha transiente
    - close() idempotente

Requer (opcional):
    pip install "pymongo[srv]"
================================================================
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional, Tuple


log = logging.getLogger("mongo")


# ============================================================
# CONFIG (lido do ambiente)
# ============================================================
DEFAULT_TIMEOUT_MS = int(os.environ.get("MONGO_TIMEOUT", "3")) * 1000


class _Mongo:
    """
    Cliente MongoDB singleton com reconexão preguiçosa.

    Não abre conexão até o primeiro db()/ping().
    Se pymongo ausente ou config vazia, opera em modo no-op.
    """

    def __init__(self):
        self.user = os.environ.get("MONGO_USER", "").strip()
        self.password = os.environ.get("MONGO_PASS", "").strip()
        self.host = os.environ.get("MONGO_HOST", "").strip()
        self.db_name = os.environ.get("MONGO_DB", "brn_analytics").strip()
        self.uri_override = os.environ.get("MONGO_URI", "").strip()

        self._client = None
        self._db = None
        self._last_ping = 0.0
        self._last_error: Optional[str] = None
        self._lock = threading.RLock()

        # cacheia o uri final
        self.uri = self._build_uri()

    # ============================================================
    # URI
    # ============================================================
    def _build_uri(self) -> str:
        """Monta o URI mongodb+srv:// a partir das credenciais."""
        if self.uri_override:
            return self.uri_override

        if not (self.user and self.password and self.host):
            return ""

        # URL-encode user e password (podem ter @, :, etc.)
        try:
            from urllib.parse import quote_plus
            u = quote_plus(self.user)
            p = quote_plus(self.password)
        except Exception:
            u = self.user
            p = self.password

        return (
            f"mongodb+srv://{u}:{p}@{self.host}/"
            f"?retryWrites=true&w=majority"
        )

    # ============================================================
    # CONFIG CHECK
    # ============================================================
    def is_configured(self) -> bool:
        """
        True se todos os campos necessários estão preenchidos.
        Não verifica conectividade.
        """
        return bool(self.uri)

    # ============================================================
    # CLIENTE (lazy)
    # ============================================================
    def _get_client(self):
        """
        Retorna o MongoClient, criando-o na primeira chamada.
        Retorna None se pymongo indisponível ou config vazia.
        """
        if not self.uri:
            return None

        with self._lock:
            if self._client is not None:
                return self._client

            try:
                from pymongo import MongoClient
            except ImportError:
                log.debug("pymongo nao instalado — Mongo desabilitado")
                self._last_error = "pymongo nao instalado"
                return None

            try:
                client = MongoClient(
                    self.uri,
                    serverSelectionTimeoutMS=DEFAULT_TIMEOUT_MS,
                    connectTimeoutMS=DEFAULT_TIMEOUT_MS,
                    socketTimeoutMS=DEFAULT_TIMEOUT_MS * 3,
                    appname="brn-node",
                )
                self._client = client
                return client
            except Exception as e:
                self._last_error = str(e)
                log.warning(f"falha criando MongoClient: {e}")
                return None

    # ============================================================
    # DB
    # ============================================================
    def db(self):
        """
        Retorna a database configurada, ou None se indisponível.
        Não valida conectividade — só retorna a referência.
        """
        if not self.uri:
            return None

        with self._lock:
            if self._db is not None:
                return self._db

            client = self._get_client()
            if client is None:
                return None

            try:
                self._db = client[self.db_name]
                return self._db
            except Exception as e:
                self._last_error = str(e)
                log.warning(f"falha obtendo database: {e}")
                return None

    # ============================================================
    # PING
    # ============================================================
    def ping(self) -> Tuple[bool, Optional[str]]:
        """
        Testa a conexão.

        Retorna:
            (True, None)       — conexão OK
            (False, "motivo")  — falhou
        """
        if not self.uri:
            return False, "nao configurado"

        client = self._get_client()
        if client is None:
            return False, self._last_error or "pymongo indisponivel"

        try:
            client.admin.command("ping")
            self._last_ping = time.time()
            self._last_error = None
            return True, None
        except Exception as e:
            self._last_error = str(e)
            return False, str(e)

    # ============================================================
    # CLOSE
    # ============================================================
    def close(self) -> None:
        """Fecha a conexão. Idempotente."""
        with self._lock:
            client = self._client
            self._client = None
            self._db = None

        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    # ============================================================
    # RECONNECT (útil em falha transiente)
    # ============================================================
    def reconnect(self) -> None:
        """Fecha e recria o cliente na próxima chamada."""
        self.close()
        log.info("mongo: cliente resetado (reconnect na proxima chamada)")

    # ============================================================
    # STATUS
    # ============================================================
    def status(self) -> dict:
        """Estado atual (para /api/diagnostico)."""
        now = time.time()
        return {
            "configured":     self.is_configured(),
            "db_name":        self.db_name,
            "host":           self.host,
            "user":           self.user[:8] + "..." if self.user else "",
            "uri_prefix":     self.uri.split("@")[0][:20] + "..." if self.uri else "",
            "has_client":     self._client is not None,
            "has_db":         self._db is not None,
            "last_ping_ago":  (int(now - self._last_ping)
                               if self._last_ping else None),
            "last_error":     self._last_error,
        }

    # ============================================================
    # HELPERS DE ALTO NÍVEL
    # ============================================================
    def collection(self, name: str):
        """Retorna uma collection ou None."""
        db = self.db()
        if db is None:
            return None
        try:
            return db[name]
        except Exception:
            return None

    def is_available(self) -> bool:
        """Ping rápido — True se responde em < timeout."""
        ok, _ = self.ping()
        return ok


# ============================================================
# SINGLETON
# ============================================================
mongo = _Mongo()


# ============================================================
# API DE MÓDULO (compat com rendezvous_mongo)
# ============================================================
def is_configured() -> bool:
    return mongo.is_configured()


def db():
    return mongo.db()


def ping() -> Tuple[bool, Optional[str]]:
    return mongo.ping()


def close() -> None:
    mongo.close()


def status() -> dict:
    return mongo.status()


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("=" * 60)
    print(" mongo_client.py — Auto-teste")
    print("=" * 60)

    # ------------------------------------------------------------
    # 1. Sem config
    # ------------------------------------------------------------
    print("\n[1] Sem config (env vars vazias):")

    # Salva e limpa
    saved_env = {k: os.environ.pop(k, None)
                 for k in ("MONGO_USER", "MONGO_PASS",
                           "MONGO_HOST", "MONGO_DB", "MONGO_URI")}

    try:
        # Cria nova instância limpa
        import importlib
        importlib.reload(__import__("mongo_client"))

        from mongo_client import _Mongo
        m = _Mongo()

        print(f"    is_configured: {m.is_configured()}")
        print(f"    db():          {m.db()}")
        ok, err = m.ping()
        print(f"    ping():        ok={ok} err={err!r}")
        assert m.is_configured() is False
        assert m.db() is None
        assert ok is False
        print("    [ok] no-op silencioso")
    finally:
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v

    # ------------------------------------------------------------
    # 2. Config completa (host fake)
    # ------------------------------------------------------------
    print("\n[2] Config completa (host fake, sem conectar):")

    os.environ["MONGO_USER"] = "user_test"
    os.environ["MONGO_PASS"] = "pass_test"
    os.environ["MONGO_HOST"] = "cluster0.fake.mongodb.net"
    os.environ["MONGO_DB"] = "test_db"

    try:
        import importlib
        import mongo_client as mc
        importlib.reload(mc)

        m2 = mc._Mongo()
        print(f"    is_configured: {m2.is_configured()}")
        print(f"    uri:           {m2.uri[:60]}...")
        print(f"    db_name:       {m2.db_name}")
        print(f"    user:          {m2.user}")
        print(f"    host:          {m2.host}")

        assert m2.is_configured() is True
        assert m2.db_name == "test_db"
        assert "user_test" in m2.uri
        assert "test_db" not in m2.uri  # db nome fica separado
        print("    [ok] config lida corretamente")

        # status()
        st = m2.status()
        print(f"\n    status:")
        for k, v in st.items():
            print(f"      {k}: {v}")
    finally:
        for k in ("MONGO_USER", "MONGO_PASS", "MONGO_HOST", "MONGO_DB"):
            os.environ.pop(k, None)

    # ------------------------------------------------------------
    # 3. URI override
    # ------------------------------------------------------------
    print("\n[3] MONGO_URI override:")

    os.environ["MONGO_URI"] = (
        "mongodb+srv://alice:secret@cluster.example.mongodb.net/"
        "?retryWrites=true&w=majority"
    )

    try:
        import importlib
        import mongo_client as mc
        importlib.reload(mc)

        m3 = mc._Mongo()
        print(f"    is_configured: {m3.is_configured()}")
        print(f"    uri:           {m3.uri[:60]}...")
        assert m3.is_configured() is True
        assert "cluster.example" in m3.uri
        print("    [ok] override funcionou")
    finally:
        os.environ.pop("MONGO_URI", None)

    # ------------------------------------------------------------
    # 4. Recarregar singleton
    # ------------------------------------------------------------
    print("\n[4] Singleton module-level:")

    import importlib
    import mongo_client as mc
    importlib.reload(mc)

    print(f"    mongo.is_configured(): {mc.mongo.is_configured()}")
    print(f"    is_configured():       {mc.is_configured()}")
    print(f"    db():                  {mc.db()}")
    ok, err = mc.ping()
    print(f"    ping():                ok={ok} err={err!r}")
    print(f"    status():              {json.dumps(mc.status(), default=str)}")

    # ------------------------------------------------------------
    # 5. close idempotente
    # ------------------------------------------------------------
    print("\n[5] close() idempotente:")
    mc.close()
    mc.close()
    mc.close()
    print("    [ok] sem erro")

    # ------------------------------------------------------------
    # 6. Reconnect
    # ------------------------------------------------------------
    print("\n[6] reconnect():")
    mc.mongo.reconnect()
    print(f"    has_client após reconnect: {mc.mongo._client is not None}")
    print("    [ok]")

    # ------------------------------------------------------------
    # 7. Se pymongo disponível, tenta conexão real?
    # ------------------------------------------------------------
    print("\n[7] pymongo instalado?")
    try:
        import pymongo
        print(f"    pymongo versao: {pymongo.version}")
    except ImportError:
        print("    pymongo NAO instalado (Mongo desabilitado)")
        print("    Para ativar: pip install \"pymongo[srv]\"")

    print("\n" + "=" * 60)
    print(" mongo_client.py OK.")
    print("=" * 60)