"""brn_logger.py — Logs estruturados do BRN
================================================================
Fornece saída colorida no terminal + JSON opcional em arquivo.

API usada por main.py v12.5:
    log = setup_logger(level="INFO", log_file=None, quiet=False)
    log = get_logger("nome_do_modulo")

Uso típico:
    from brn_logger import setup_logger, get_logger

    setup_logger(level="INFO", log_file="run.log")
    log = get_logger("main")
    log.info("nó iniciado")

Níveis: DEBUG, INFO, WARNING, ERROR, CRITICAL
Env: BRN_LOG_LEVEL (default "INFO")

Formato terminal:
    21:34:12 [INFO ] main: nó iniciado

Formato arquivo (JSON por linha):
    {"ts": "2026-10-14T21:34:12.123", "level": "INFO",
     "logger": "brn.main", "msg": "nó iniciado", ...}
================================================================
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime


# ============================================================
# CORES ANSI
# ============================================================
class Colors:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    GRAY    = "\033[90m"
    CYAN    = "\033[96m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    MAGENTA = "\033[95m"
    WHITE   = "\033[97m"


LEVEL_COLORS = {
    "DEBUG":    Colors.GRAY,
    "INFO":     Colors.CYAN,
    "WARNING":  Colors.YELLOW,
    "ERROR":    Colors.RED,
    "CRITICAL": Colors.MAGENTA,
}

LEVEL_ALIASES = {
    "TRACE":    "DEBUG",
    "WARN":     "WARNING",
    "FATAL":    "CRITICAL",
    "VERBOSE":  "DEBUG",
}


# ============================================================
# DETECÇÃO DE TTY (para colorir somente se for terminal)
# ============================================================
def _is_tty(stream=None) -> bool:
    if stream is None:
        stream = sys.stdout
    try:
        return bool(hasattr(stream, "isatty") and stream.isatty())
    except Exception:
        return False


# ============================================================
# FORMATTER: terminal com cores
# ============================================================
class ColorFormatter(logging.Formatter):
    def __init__(self, use_color: bool = True):
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        level = record.levelname
        name = record.name

        # extrai brn.* do nome
        if name.startswith("brn."):
            name = name[4:]
        elif name == "brn":
            name = "brn"

        if self.use_color:
            color = LEVEL_COLORS.get(level, Colors.RESET)
            prefix = (f"{Colors.GRAY}{ts}{Colors.RESET} "
                      f"{color}[{level:5s}]{Colors.RESET} "
                      f"{Colors.BOLD}{name}{Colors.RESET}:")
        else:
            prefix = f"{ts} [{level:5s}] {name}:"

        msg = record.getMessage()

        # exceção (traceback)
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            msg = msg + "\n" + record.exc_text

        # linhas adicionais (multiline) — indentadas
        lines = msg.split("\n")
        if len(lines) > 1:
            out = [prefix + " " + lines[0]]
            for ln in lines[1:]:
                out.append(" " * 8 + ln)
            return "\n".join(out)

        return prefix + " " + msg


# ============================================================
# FORMATTER: JSON por linha (para arquivo)
# ============================================================
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts":      datetime.fromtimestamp(record.created).isoformat(),
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
            "module":  record.module,
            "func":    record.funcName,
            "line":    record.lineno,
            "process": record.process,
            "thread":  record.threadName,
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=False)


# ============================================================
# FORMATTER: simples para arquivo texto
# ============================================================
class FileFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime(
            "%Y-%m-%d %H:%M:%S")
        level = record.levelname
        name = record.name
        if name.startswith("brn."):
            name = name[4:]
        msg = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            msg = msg + "\n" + record.exc_text
        return f"{ts} [{level:5s}] {name}: {msg}"


# ============================================================
# ESTADO GLOBAL
# ============================================================
_configured = False
_root_logger: "logging.Logger | None" = None
_current_level = "INFO"


def _normalize_level(level: str) -> str:
    """Normaliza apelidos de nível e valida."""
    if not isinstance(level, str):
        return "INFO"
    upper = level.strip().upper()
    upper = LEVEL_ALIASES.get(upper, upper)
    valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if upper not in valid:
        return "INFO"
    return upper


# ============================================================
# SETUP
# ============================================================
def setup_logger(level: str = "INFO",
                 log_file: str = None,
                 quiet: bool = False,
                 color: bool = None) -> logging.Logger:
    """
    Configura o logger raiz do BRN.

    Parâmetros:
        level    — DEBUG | INFO | WARNING | ERROR | CRITICAL
                   (aliases: TRACE→DEBUG, WARN→WARNING, FATAL→CRITICAL)
        log_file — se fornecido, grava também em arquivo (JSON por linha)
                   use extensão .log → texto formatado
                   use extensão .jsonl → JSON estruturado
        quiet    — se True, não emite no stdout (útil em testes)
        color    — força cores (True/False). Se None, detecta TTY.

    Idempotente: chamadas subsequentes apenas ajustam nível.

    Retorna: logger raiz "brn"
    """
    global _configured, _root_logger, _current_level

    level = _normalize_level(level)
    _current_level = level

    root = logging.getLogger("brn")
    root.setLevel(getattr(logging, level))

    if _configured and _root_logger is root:
        return root

    # limpa handlers antigos (evita duplicação em reload)
    root.handlers.clear()
    root.propagate = False

    # --- console handler ---
    if not quiet:
        use_color = _is_tty() if color is None else bool(color)
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(getattr(logging, level))
        ch.setFormatter(ColorFormatter(use_color=use_color))
        root.addHandler(ch)

    # --- file handler ---
    if log_file:
        try:
            directory = os.path.dirname(log_file)
            if directory:
                os.makedirs(directory, exist_ok=True)

            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(getattr(logging, level))

            lower = log_file.lower()
            if lower.endswith(".jsonl") or lower.endswith(".json"):
                fh.setFormatter(JsonFormatter())
            else:
                fh.setFormatter(FileFormatter())
            root.addHandler(fh)
        except Exception as e:
            # não deixa o logging quebrar o boot
            print(f"[brn_logger] aviso: nao consegui abrir {log_file}: {e}",
                  file=sys.stderr)

    _root_logger = root
    _configured = True
    return root


# ============================================================
# GET LOGGER
# ============================================================
def get_logger(name: str = "brn") -> logging.Logger:
    """
    Retorna um logger filho de "brn".

    - get_logger()           → logger raiz "brn"
    - get_logger("main")     → logger "brn.main"
    - get_logger("brn.main") → logger "brn.main"
    - get_logger("brn")      → logger raiz
    """
    if name == "brn" or not name:
        return logging.getLogger("brn")
    if not name.startswith("brn."):
        name = "brn." + name
    return logging.getLogger(name)


# ============================================================
# HELPERS DE CONVENIÊNCIA
# ============================================================
def set_level(level: str) -> None:
    """Muda o nível em runtime."""
    global _current_level
    level = _normalize_level(level)
    _current_level = level
    root = logging.getLogger("brn")
    root.setLevel(getattr(logging, level))
    for h in root.handlers:
        h.setLevel(getattr(logging, level))


def get_level() -> str:
    return _current_level


def is_configured() -> bool:
    return _configured


def add_file_handler(path: str, level: str = None) -> None:
    """Adiciona um FileHandler em runtime."""
    root = logging.getLogger("brn")
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(getattr(logging, _normalize_level(level or _current_level)))

    lower = path.lower()
    if lower.endswith(".jsonl") or lower.endswith(".json"):
        fh.setFormatter(JsonFormatter())
    else:
        fh.setFormatter(FileFormatter())
    root.addHandler(fh)


def banner(title: str, char: str = "=", width: int = 60) -> str:
    """Retorna uma string de banner para log."""
    line = char * width
    return f"\n{line}\n  {title}\n{line}"


# ============================================================
# INFO / STATUS
# ============================================================
def info() -> dict:
    return {
        "configured":     _configured,
        "level":          _current_level,
        "handlers":       len(logging.getLogger("brn").handlers),
        "propagate":      logging.getLogger("brn").propagate,
        "stdout_is_tty":  _is_tty(),
    }


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print(" brn_logger.py — Auto-teste")
    print("=" * 60)

    # ---------- 1. Setup básico ----------
    print("\n[1] setup_logger(level='INFO')")
    log = setup_logger(level="INFO")
    print(f"    {info()}")
    assert is_configured()
    assert get_level() == "INFO"
    print("    [ok]")

    # ---------- 2. Emitir nos 5 níveis ----------
    print("\n[2] Emitir 5 níveis de log (cores se TTY):")
    lg = get_logger("teste")
    lg.debug("mensagem de DEBUG (não aparece com level=INFO)")
    lg.info("mensagem de INFO")
    lg.warning("mensagem de WARNING")
    lg.error("mensagem de ERROR")
    lg.critical("mensagem de CRITICAL")
    print("    [ok]")

    # ---------- 3. Mudar nível para DEBUG ----------
    print("\n[3] set_level('DEBUG'):")
    set_level("DEBUG")
    print(f"    nível atual: {get_level()}")
    lg.debug("agora DEBUG aparece")
    print("    [ok]")

    # ---------- 4. Normalização de aliases ----------
    print("\n[4] Aliases (WARN, TRACE, FATAL):")
    for alias in ("WARN", "TRACE", "FATAL", "invalido"):
        lg2 = setup_logger(level=alias)
        print(f"    {alias!r} → {get_level()}")
    assert get_level() == "INFO"  # último foi inválido, caiu pra INFO
    print("    [ok]")

    # ---------- 5. Logger com nome ----------
    print("\n[5] get_logger('modulo'):")
    lmod = get_logger("modulo")
    print(f"    nome completo: {lmod.name}")
    assert lmod.name == "brn.modulo"
    lmod.info("log de módulo")
    print("    [ok]")

    # ---------- 6. Arquivo JSON ----------
    print("\n[6] Gravar em arquivo JSONL:")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        jsonl = os.path.join(td, "test.jsonl")
        # reconfigura com arquivo
        setup_logger(level="INFO", log_file=jsonl, quiet=True)
        lj = get_logger("json_test")
        lj.info("linha 1")
        lj.warning("linha 2")

        with open(jsonl, "r", encoding="utf-8") as f:
            linhas = [l.strip() for l in f if l.strip()]
        print(f"    arquivo tem {len(linhas)} linha(s)")
        for linha in linhas:
            d = json.loads(linha)
            print(f"    {d['level']} :: {d['msg']}")
        assert len(linhas) == 2
        print("    [ok]")

    # ---------- 7. Arquivo .log (texto) ----------
    print("\n[7] Gravar em arquivo .log (texto):")
    with tempfile.TemporaryDirectory() as td:
        logf = os.path.join(td, "test.log")
        setup_logger(level="INFO", log_file=logf, quiet=True)
        ll = get_logger("log_test")
        ll.info("linha A")
        ll.error("linha B")

        with open(logf, "r", encoding="utf-8") as f:
            conteudo = f.read()
        print(f"    conteúdo ({len(conteudo)} bytes):")
        for ln in conteudo.splitlines():
            print(f"    {ln}")
        assert "linha A" in conteudo
        assert "linha B" in conteudo
        print("    [ok]")

    # ---------- 8. Exception logging ----------
    print("\n[8] log.exception():")
    setup_logger(level="INFO", quiet=False)
    le = get_logger("exc_test")
    try:
        _ = 1 / 0
    except ZeroDivisionError:
        le.exception("erro capturado")
    print("    [ok]")

    # ---------- 9. add_file_handler em runtime ----------
    print("\n[9] add_file_handler() em runtime:")
    with tempfile.TemporaryDirectory() as td:
        extra = os.path.join(td, "extra.log")
        setup_logger(level="INFO", quiet=True)
        add_file_handler(extra)
        get_logger("extra").info("mensagem extra")
        assert os.path.exists(extra)
        with open(extra, "r", encoding="utf-8") as f:
            print(f"    conteúdo: {f.read().strip()}")
        print("    [ok]")

    # ---------- 10. Banner ----------
    print("\n[10] banner():")
    print(banner("TESTE DE BANNER"))

    print("\n" + "=" * 60)
    print(" brn_logger.py OK.")
    print("=" * 60)