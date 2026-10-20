"""brn_config.py — Configuração externa do BRN
================================================================
Carrega config em 3 camadas (prioridade crescente):

    1. DEFAULTS (hardcoded neste arquivo)
    2. config.json (se existir)
    3. Variáveis de ambiente (BRN_*)

API usada por main.py v12.5:

    cfg = Config("config.json")     # ou Config() — usa default

    cfg["web_port"]                  # acesso por item
    cfg.get("p2p_port", 6001)        # acesso com default
    cfg.source                       # "defaults" | "config.json" | "config.json + env"
    cfg.data                         # dict cru
    cfg.apply_to_env()               # propaga para os.environ
    cfg.to_dict()                    # copia do dict

Campos suportados (defaults):
    web_port          — porta HTTP (5000)
    explorer_port     — porta explorer (8080)
    p2p_port          — porta P2P (6001)
    db_path           — arquivo SQLite (brn_v2_chain.db)
    read_only         — (bool) não assina blocos
    headless          — (bool) sem GUI
    miner_enabled     — (bool) inicia miner no boot
    miner_interval    — (int) segundos entre tentativas
    miner_whitelist   — (list) endereços autorizados (vazio = todos)
    upnp              — (bool) mapear porta via UPnP
    log_level         — (str) DEBUG/INFO/WARNING/ERROR
    log_file          — (str) arquivo de log (vazio = sem arquivo)
    tracker_url       — (str) tracker HTTP (opcional)
    bootstrap_peers   — (list) IPs iniciais
    network_secret    — (str) HMAC da rede
    web_user          — (str) usuário HTTP (default admin)
    web_pass          — (str) senha HTTP

Bloco github:
    user              — usuário GitHub
    repo              — repositório
    file              — nome do arquivo (peers.json)
    branch            — branch (main)
    token             — token de acesso
    interval          — (int) segundos entre consultas
================================================================
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path


log = logging.getLogger("brn.config")


# ============================================================
# DEFAULTS
# ============================================================
DEFAULTS = {
    # rede
    "web_port":        5000,
    "explorer_port":   8080,
    "p2p_port":        6001,
    "web_host":        "0.0.0.0",
    "explorer_host":   "0.0.0.0",

    # banco
    "db_path":         "brn_v2_chain.db",
    "read_only":       False,

    # runtime
    "headless":        False,
    "miner_enabled":   False,
    "miner_interval":  5,
    "miner_whitelist": [],
    "upnp":            True,

    # logs
    "log_level":       "INFO",
    "log_file":        "",

    # rede P2P
    "tracker_url":     "",
    "bootstrap_peers": [],
    "network_secret":  "",

    # HTTP auth
    "web_user":        "admin",
    "web_pass":        "",

    # GitHub peers (opcional)
    "github": {
        "user":     "",
        "repo":     "",
        "file":     "peers.json",
        "branch":   "main",
        "token":    "",
        "interval": 180,
    },
}


# ============================================================
# MAPA: campo → env var
# ============================================================
ENV_MAP = {
    "web_port":        "BRN_WEB_PORT",
    "explorer_port":   "BRN_EXPLORER_PORT",
    "p2p_port":        "BRN_P2P_PORT",
    "web_host":        "BRN_WEB_HOST",
    "explorer_host":   "BRN_EXPLORER_HOST",
    "db_path":         "BRN_DB",
    "read_only":       "BRN_READ_ONLY",
    "headless":        "BRN_HEADLESS",
    "miner_enabled":   "BRN_MINER_ENABLED",
    "miner_interval":  "BRN_MINER_INTERVAL",
    "upnp":            "BRN_UPNP",
    "log_level":       "BRN_LOG_LEVEL",
    "log_file":        "BRN_LOG_FILE",
    "tracker_url":     "BRN_TRACKER",
    "network_secret":  "BRN_NETWORK_SECRET",
    "web_user":        "BRN_WEB_USER",
    "web_pass":        "BRN_WEB_PASS",
}

# Env vars do bloco github
GH_ENV = {
    "user":     "BRN_GH_USER",
    "repo":     "BRN_GH_REPO",
    "file":     "BRN_GH_FILE",
    "branch":   "BRN_GH_BRANCH",
    "token":    "BRN_GH_TOKEN",
    "interval": "BRN_GH_INTERVAL",
}


# ============================================================
# HELPERS DE PARSE
# ============================================================
def _bool(v) -> bool:
    """Converte vários formatos para bool."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "sim", "y", "s")
    return False


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (ValueError, TypeError):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return default


def _list(v) -> list:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return []


# ============================================================
# CONFIG
# ============================================================
class Config:
    """
    Config em 3 camadas: DEFAULTS → config.json → env vars.

    Atributos:
        data    — dict cru com todos os valores
        source  — string indicando a fonte dominante
        path    — Path do arquivo de config (pode não existir)
    """

    def __init__(self, path: str = "config.json"):
        self.path = Path(path)
        self.source = "defaults"
        self.data = self._copy_defaults()

        self._load_file()
        self._load_env()

    # ---------- cópia profunda ----------
    @staticmethod
    def _copy_defaults() -> dict:
        d = dict(DEFAULTS)
        d["github"] = dict(DEFAULTS["github"])
        d["bootstrap_peers"] = list(DEFAULTS["bootstrap_peers"])
        d["miner_whitelist"] = list(DEFAULTS["miner_whitelist"])
        return d

    # ============================================================
    # CARREGAR ARQUIVO
    # ============================================================
    def _load_file(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                log.warning(f"{self.path}: nao contem um objeto JSON, ignorando")
                return

            for k, v in loaded.items():
                if k == "github" and isinstance(v, dict):
                    self.data["github"].update(v)
                else:
                    self.data[k] = v
            self.source = str(self.path)
            log.debug(f"config carregado de {self.path}")
        except json.JSONDecodeError as e:
            log.warning(f"{self.path} inválido (JSON malformado): {e}")
        except Exception as e:
            log.warning(f"falha lendo {self.path}: {e}")

    # ============================================================
    # CARREGAR ENV VARS
    # ============================================================
    def _load_env(self) -> None:
        any_env = False

        for key, env_name in ENV_MAP.items():
            val = os.environ.get(env_name)
            if val is None:
                continue

            # tipo do default determina o parse
            if isinstance(DEFAULTS.get(key), bool):
                self.data[key] = _bool(val)
            elif isinstance(DEFAULTS.get(key), int):
                self.data[key] = _int(val, self.data.get(key, 0))
            else:
                self.data[key] = val
            any_env = True

        # bloco github
        for key, env_name in GH_ENV.items():
            val = os.environ.get(env_name)
            if val is None:
                continue
            if key == "interval":
                self.data["github"][key] = _int(val, 180)
            else:
                self.data["github"][key] = val
            any_env = True

        # bootstrap peers (CSV)
        bs = os.environ.get("BRN_BOOTSTRAP_PEERS")
        if bs:
            self.data["bootstrap_peers"] = _list(bs)
            any_env = True

        # miner whitelist (CSV)
        wl = os.environ.get("BRN_MINER_WHITELIST")
        if wl is not None:
            self.data["miner_whitelist"] = _list(wl)
            any_env = True

        if any_env:
            if self.source == "defaults":
                self.source = "env"
            else:
                self.source = f"{self.source} + env"

    # ============================================================
    # ACESSO
    # ============================================================
    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def __contains__(self, key):
        return key in self.data

    def keys(self):
        return self.data.keys()

    def items(self):
        return self.data.items()

    def to_dict(self) -> dict:
        """Cópia rasa do dict."""
        return dict(self.data)

    # ============================================================
    # APLICAR AO AMBIENTE
    # ============================================================
    def apply_to_env(self) -> None:
        """
        Propaga config → os.environ para outros módulos lerem.
        Não sobrescreve env vars já definidas com valor não-vazio.
        """
        mapping = {
            "web_port":       "BRN_WEB_PORT",
            "explorer_port":  "BRN_EXPLORER_PORT",
            "p2p_port":       "BRN_P2P_PORT",
            "web_host":       "BRN_WEB_HOST",
            "explorer_host":  "BRN_EXPLORER_HOST",
            "db_path":        "BRN_DB",
            "tracker_url":    "BRN_TRACKER",
            "network_secret": "BRN_NETWORK_SECRET",
            "web_user":       "BRN_WEB_USER",
            "web_pass":       "BRN_WEB_PASS",
            "log_level":      "BRN_LOG_LEVEL",
            "log_file":       "BRN_LOG_FILE",
        }

        for key, env_name in mapping.items():
            val = self.data.get(key)
            if val is None or str(val) == "":
                continue
            existing = os.environ.get(env_name)
            if existing:
                continue   # não sobrescreve env do shell
            os.environ[env_name] = str(val)

        # flags booleanas
        if self.data.get("read_only"):
            os.environ["BRN_READ_ONLY"] = "1"
        if self.data.get("headless"):
            os.environ["BRN_HEADLESS"] = "1"
        if not self.data.get("upnp"):
            os.environ["BRN_UPNP"] = "0"

        # listas → CSV
        if self.data.get("bootstrap_peers"):
            os.environ.setdefault(
                "BRN_BOOTSTRAP_PEERS",
                ",".join(self.data["bootstrap_peers"]),
            )
        if self.data.get("miner_whitelist"):
            os.environ.setdefault(
                "BRN_MINER_WHITELIST",
                ",".join(self.data["miner_whitelist"]),
            )

        # github
        gh = self.data.get("github") or {}
        for key, env_name in GH_ENV.items():
            val = gh.get(key)
            if val is None or str(val) == "":
                continue
            os.environ.setdefault(env_name, str(val))

    # ============================================================
    # SALVAR (opcional)
    # ============================================================
    def save(self, path: str = None) -> bool:
        """
        Grava o estado atual no arquivo JSON.
        Útil para persistir mudanças feitas em runtime.
        """
        target = Path(path) if path else self.path
        try:
            directory = target.parent
            if directory and str(directory) != ".":
                directory.mkdir(parents=True, exist_ok=True)

            tmp = target.with_suffix(target.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(target))
            log.info(f"config salvo em {target}")
            return True
        except Exception as e:
            log.error(f"falha salvando config em {target}: {e}")
            return False

    # ============================================================
    # RELOAD
    # ============================================================
    def reload(self) -> None:
        """Recarrega arquivo + env."""
        self.data = self._copy_defaults()
        self.source = "defaults"
        self._load_file()
        self._load_env()

    # ============================================================
    # REPR
    # ============================================================
    def __repr__(self) -> str:
        return f"<Config source={self.source!r} keys={len(self.data)}>"


# ============================================================
# API DE MÓDULO
# ============================================================
_config_singleton: "Config | None" = None
_config_lock = __import__("threading").Lock()


def get_config(path: str = None) -> Config:
    """Retorna Config singleton (cria se necessário)."""
    global _config_singleton
    with _config_lock:
        if _config_singleton is None or path:
            _config_singleton = Config(path or "config.json")
        return _config_singleton


def reset_config() -> None:
    """Limpa singleton (útil em testes)."""
    global _config_singleton
    with _config_lock:
        _config_singleton = None


def info() -> dict:
    """Status da config singleton (para /api/diagnostico)."""
    c = _config_singleton
    if c is None:
        return {"configured": False}
    return {
        "configured": True,
        "source":     c.source,
        "path":       str(c.path),
        "exists":     c.path.exists(),
        "keys":       len(c.data),
    }


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import tempfile

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("=" * 60)
    print(" brn_config.py — Auto-teste")
    print("=" * 60)

    # ---------- 1. Defaults ----------
    print("\n[1] Config sem arquivo (defaults puros):")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(os.path.join(td, "inexistente.json"))
        print(f"    source: {cfg.source}")
        assert cfg.source == "defaults"
        assert cfg["web_port"] == 5000
        assert cfg["p2p_port"] == 6001
        assert cfg["db_path"] == "brn_v2_chain.db"
        assert cfg["log_level"] == "INFO"
        assert cfg["upnp"] is True
        assert cfg["read_only"] is False
        print(f"    web_port={cfg['web_port']} p2p={cfg['p2p_port']}")
        print("    [ok]")

    # ---------- 2. Config com arquivo ----------
    print("\n[2] Config com config.json:")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "web_port": 6000,
                "p2p_port": 7000,
                "db_path": "/tmp/custom.db",
                "miner_enabled": True,
                "github": {"user": "bruno", "repo": "brn-peers"},
            }, f)

        cfg = Config(path)
        print(f"    source: {cfg.source}")
        assert "config.json" in cfg.source
        assert cfg["web_port"] == 6000
        assert cfg["p2p_port"] == 7000
        assert cfg["db_path"] == "/tmp/custom.db"
        assert cfg["miner_enabled"] is True
        assert cfg["github"]["user"] == "bruno"
        assert cfg["github"]["repo"] == "brn-peers"
        assert cfg["github"]["file"] == "peers.json"   # default mantido
        print(f"    web_port={cfg['web_port']} p2p={cfg['p2p_port']}")
        print(f"    github={cfg['github']['user']}/{cfg['github']['repo']}")
        print("    [ok]")

    # ---------- 3. Config com env vars ----------
    print("\n[3] Env vars sobrescrevem arquivo:")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"web_port": 6000, "p2p_port": 7000}, f)

        # variáveis de ambiente
        os.environ["BRN_WEB_PORT"] = "8000"
        os.environ["BRN_P2P_PORT"] = "9000"
        os.environ["BRN_READ_ONLY"] = "1"
        os.environ["BRN_BOOTSTRAP_PEERS"] = "10.0.0.1:6001,10.0.0.2:6001"

        try:
            cfg = Config(path)
            print(f"    source: {cfg.source}")
            assert cfg["web_port"] == 8000     # env vence
            assert cfg["p2p_port"] == 9000     # env vence
            assert cfg["read_only"] is True    # parse bool
            assert cfg["bootstrap_peers"] == ["10.0.0.1:6001", "10.0.0.2:6001"]
            print(f"    web_port={cfg['web_port']} (env venceu 6000)")
            print(f"    p2p_port={cfg['p2p_port']} (env venceu 7000)")
            print(f"    read_only={cfg['read_only']}")
            print(f"    bootstrap_peers={cfg['bootstrap_peers']}")
            print("    [ok]")
        finally:
            for k in ("BRN_WEB_PORT", "BRN_P2P_PORT", "BRN_READ_ONLY",
                      "BRN_BOOTSTRAP_PEERS"):
                os.environ.pop(k, None)

    # ---------- 4. apply_to_env ----------
    print("\n[4] apply_to_env() propaga para os.environ:")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(os.path.join(td, "vazio.json"))
        cfg.data["web_port"] = 4321
        cfg.data["network_secret"] = "abc123"
        cfg.data["bootstrap_peers"] = ["1.2.3.4:6001"]

        # limpa env antes
        for k in ("BRN_WEB_PORT", "BRN_NETWORK_SECRET", "BRN_BOOTSTRAP_PEERS"):
            os.environ.pop(k, None)

        cfg.apply_to_env()

        assert os.environ.get("BRN_WEB_PORT") == "4321"
        assert os.environ.get("BRN_NETWORK_SECRET") == "abc123"
        assert os.environ.get("BRN_BOOTSTRAP_PEERS") == "1.2.3.4:6001"
        print(f"    BRN_WEB_PORT={os.environ['BRN_WEB_PORT']}")
        print(f"    BRN_NETWORK_SECRET={os.environ['BRN_NETWORK_SECRET']}")
        print(f"    BRN_BOOTSTRAP_PEERS={os.environ['BRN_BOOTSTRAP_PEERS']}")
        print("    [ok]")

        # limpa
        for k in ("BRN_WEB_PORT", "BRN_NETWORK_SECRET", "BRN_BOOTSTRAP_PEERS"):
            os.environ.pop(k, None)

    # ---------- 5. save ----------
    print("\n[5] save() persiste mudanças:")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "out.json")
        cfg = Config(path)
        cfg.data["web_port"] = 9999
        cfg.data["bootstrap_peers"] = ["5.6.7.8:6001"]
        ok = cfg.save()
        assert ok
        assert os.path.exists(path)

        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded["web_port"] == 9999
        assert loaded["bootstrap_peers"] == ["5.6.7.8:6001"]
        print(f"    salvo em {path}")
        print(f"    web_port salvo: {loaded['web_port']}")
        print("    [ok]")

    # ---------- 6. reload ----------
    print("\n[6] reload() recarrega:")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "cfg.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"web_port": 1111}, f)

        cfg = Config(path)
        assert cfg["web_port"] == 1111

        with open(path, "w", encoding="utf-8") as f:
            json.dump({"web_port": 2222}, f)

        cfg.reload()
        assert cfg["web_port"] == 2222
        print(f"    antes: 1111, depois do reload: {cfg['web_port']}")
        print("    [ok]")

    # ---------- 7. get() e __getitem__ ----------
    print("\n[7] get() e __getitem__:")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(os.path.join(td, "nada.json"))
        assert cfg["web_port"] == 5000
        assert cfg.get("web_port") == 5000
        assert cfg.get("nao_existe", "default") == "default"
        try:
            _ = cfg["nao_existe"]
            print("    [X] deveria ter levantado KeyError")
        except KeyError:
            print("    [ok] KeyError esperado")

    # ---------- 8. to_dict ----------
    print("\n[8] to_dict():")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(os.path.join(td, "nada.json"))
        d = cfg.to_dict()
        assert isinstance(d, dict)
        assert d["web_port"] == 5000
        # mutação não afeta o original
        d["web_port"] = 1
        assert cfg["web_port"] == 5000
        print(f"    keys: {len(d)}")
        print("    [ok]")

    # ---------- 9. Singleton ----------
    print("\n[9] get_config() singleton:")
    reset_config()
    c1 = get_config()
    c2 = get_config()
    assert c1 is c2
    print(f"    singleton: {c1 is c2}")
    print(f"    info: {info()}")
    print("    [ok]")

    # ---------- 10. Repr ----------
    print("\n[10] repr():")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(os.path.join(td, "nada.json"))
        print(f"    {repr(cfg)}")

    print("\n" + "=" * 60)
    print(" brn_config.py OK.")
    print("=" * 60)