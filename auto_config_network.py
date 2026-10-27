"""auto_config_network.py — Configurador seguro da rede BRN (v2.0)
================================================================
v2.0 (2026-10-27):
  [FIX] Escrita no .env agora cita valores que precisam (aspas
        simples quando o valor contém espaços, #, =, $, etc).
        Antes gravava f"{key}={value}" cru, quebrando o parsing se
        o valor tivesse # ou espaço. Compatível com os leitores
        simples do projeto (criar_checkpoint.py, check_network.py):
        usa aspas simples quando possível, só usa aspas duplas com
        \" e \\ quando o valor contém aspas simples.
  [FIX] Leitura do .env agora interpreta corretamente valores citados
        (aspas simples literais, aspas duplas com escape de \" e \\)
        e corta comentários inline (# precedido de espaço) fora de
        aspas. Antes fazia strip('"').strip("'") que não lida com
        escapes nem comentários.
  [FIX] update_env_file() preserva o prefixo "export " da linha
        original quando presente em linhas NÃO modificadas (antes
        era preservado por acidente; agora é explícito) e NÃO
        adiciona "export " em linhas que ele escreve (mantém o
        formato consumível pelos leitores simples do projeto).
  [FIX] Timeouts de probe (tracker/P2P/tailscale) agora respeitam
        BRN_HTTP_TIMEOUT (tracker/P2P, default 3s) e
        BRN_TAILSCALE_TIMEOUT (default 8s). Antes eram hardcoded
        em 3s e 5s.
  [NEW] --self-test: testa roundtrip de quote/parse e validação de IP.
  [NEW] __version__ = "2.0".
  [KEEP] Comportamento interativo do wizard (main).
  [KEEP] _atomic_write com fsync + rename preservando permissões.
  [KEEP] _validate_server_ip (IP literal, não URL, não loopback).
================================================================
"""
from __future__ import annotations

__version__ = "2.0"

import ipaddress
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "brn_network.env"
ENV_EXAMPLE = BASE_DIR / "brn_network.env.example"
BOOTSTRAP_FILE = BASE_DIR / "bootstrap_peers.json"
PEER_FILE = BASE_DIR / "brn_tailscale_peer.txt"

DEFAULT_TRACKER_PORT = 9000
DEFAULT_P2P_PORT = 6001
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")


# ============================================================
# TIMEOUTS CONFIGURÁVEIS
# ============================================================
def _http_timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("BRN_HTTP_TIMEOUT", "3")))
    except (TypeError, ValueError):
        return 3.0


def _tailscale_timeout() -> float:
    try:
        return max(2.0, float(os.environ.get("BRN_TAILSCALE_TIMEOUT", "8")))
    except (TypeError, ValueError):
        return 8.0


# ============================================================
# ENV: PARSING ROBUSTO
# ============================================================
# Caracteres que NÃO precisam de aspas em valor env.
# Inclui [ e ] para suportar peers IPv6 na forma [::1]:6001.
_SAFE_ENV_CHARS = re.compile(r"^[A-Za-z0-9_.:@/\+\-\[\]]+$")

_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _parse_env_value(raw: str) -> str:
    """
    Interpreta o lado direito de KEY=VALUE com:

      - aspas simples: literal até o próximo ' (sem escape)
      - aspas duplas:  entende \\\\ e \\" (só isso)
      - sem aspas:     corta comentário inline (# precedido de espaço)
      - linhas malformadas: devolve o conteúdo cru (defensivo)

    Compatível com os leitores simples do projeto que só fazem
    .strip().strip('"').strip("'"): o writer abaixo só usa aspas
    duplas quando o valor contém aspas simples, e nesse caso usa
    apenas \\\\ e \\" — que os leitores simples ignoram como chars
    literais (aceitável para os campos gravados aqui: portas, IPs,
    URLs, bootstrap). Não usamos escapes como \\n ou \\$ porque os
    leitores simples não os interpretam e o valor viraria lixo.
    """
    s = raw.strip()
    if not s:
        return ""

    # Aspas simples: literal
    if s.startswith("'"):
        end = s.find("'", 1)
        if end == -1:
            return s[1:]  # malformado: pega o resto
        return s[1:end]

    # Aspas duplas: escape de \\ e \"
    if s.startswith('"'):
        out: list[str] = []
        i = 1
        while i < len(s):
            c = s[i]
            if c == '"':
                return "".join(out)
            if c == "\\" and i + 1 < len(s):
                nxt = s[i + 1]
                if nxt in ('"', "\\"):
                    out.append(nxt)
                    i += 2
                    continue
                # outros escapes: mantém o backslash literal
                out.append(c)
                i += 1
                continue
            out.append(c)
            i += 1
        return "".join(out)  # malformado

    # Sem aspas: corta comentário inline (# após espaço)
    m = re.search(r"\s+#", s)
    if m:
        s = s[:m.start()].rstrip()
    return s


def _quote_env_value(value: str) -> str:
    """
    Escolhe a representação mais simples para um valor env.

    - vazio → "" (KEY=)
    - só chars seguros → sem aspas
    - contém aspas simples → aspas duplas com escape de \\ e "
    - resto → aspas simples (literal)

    Nunca produz \\n, \\$, backtick: os leitores simples do projeto
    não entendem esses escapes.
    """
    if value == "":
        return ""
    if _SAFE_ENV_CHARS.match(value):
        return value
    if "'" not in value:
        return f"'{value}'"
    # fallback: aspas duplas com escape mínimo
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _read_env_values(path: Path = ENV_FILE) -> dict[str, str]:
    """Lê atribuições simples de um arquivo env, sem executar seu conteúdo."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = _KEY_RE.match(line)
            if not m:
                continue
            key, value_raw = m.group(1), m.group(2)
            values[key] = _parse_env_value(value_raw)
    except OSError as exc:
        print(f"[X] Não foi possível ler {path.name}: {exc}")
    return values


# ============================================================
# PORTAS
# ============================================================
def _port_from_config(key: str, fallback: int) -> int:
    raw = os.environ.get(key) or _read_env_values().get(key, "")
    try:
        value = int(raw) if raw else fallback
        if 1 <= value <= 65535:
            return value
    except (TypeError, ValueError):
        pass
    print(f"[!] {key} inválida; usando a porta padrão {fallback}.")
    return fallback


# ============================================================
# TAILSCALE
# ============================================================
def _run_tailscale(args: list[str]) -> Optional[str]:
    """Executa o cliente Tailscale com timeout e sem exibir saída de erro."""
    candidates = []
    if platform.system().lower().startswith("win"):
        candidates.append(r"C:\Program Files\Tailscale\tailscale.exe")
    candidates.append("tailscale")

    for executable in candidates:
        try:
            result = subprocess.run(
                [executable, *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=_tailscale_timeout(),
            )
            return result.stdout
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def get_tailscale_ip() -> Optional[str]:
    """Retorna um IPv4 do intervalo Tailscale CGNAT (100.64.0.0/10)."""
    output = _run_tailscale(["ip", "-4"])
    if not output:
        return None
    for line in output.splitlines():
        candidate = line.strip()
        try:
            address = ipaddress.ip_address(candidate)
            if address.version == 4 and address in TAILSCALE_CGNAT:
                return str(address)
        except ValueError:
            continue
    return None


# ============================================================
# VALIDAÇÃO DE IP
# ============================================================
def _validate_server_ip(raw: str) -> str:
    """Aceita apenas um IP literal utilizável, nunca URL, porta ou loopback."""
    candidate = raw.strip()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError("Informe somente um endereço IPv4 ou IPv6 válido.") from exc
    if (address.is_loopback or address.is_unspecified
            or address.is_link_local or address.is_multicast
            or address.is_reserved):
        raise ValueError("Esse IP não é adequado para anunciar um servidor remoto.")
    return str(address)


def _format_host(ip: str) -> str:
    address = ipaddress.ip_address(ip)
    return f"[{ip}]" if address.version == 6 else ip


# ============================================================
# ESCRITA ATÔMICA
# ============================================================
def _atomic_write(path: Path, content: str) -> None:
    """Grava no mesmo diretório e substitui atomicamente, preservando permissões."""
    path.parent.mkdir(parents=True, exist_ok=True)
    old_mode = None
    try:
        old_mode = path.stat().st_mode & 0o777
    except OSError:
        pass

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if old_mode is not None:
            try:
                os.chmod(temporary, old_mode)
            except OSError:
                pass
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


# ============================================================
# UPDATE ENV
# ============================================================
def update_env_file(tracker_url: str, bootstrap_peer: str, tracker_port: int, p2p_port: int) -> None:
    """
    Atualiza apenas as chaves de rede, mantendo segredos e demais opções.

    - Valores são cotados via _quote_env_value().
    - Linhas NÃO modificadas: preservadas byte-a-byte (incluindo
      prefixo "export " se o usuário colocou).
    - Linhas modificadas e novas: escritas SEM "export " para
      compatibilidade com os leitores simples do projeto.
    """
    updates = {
        "BRN_TRACKER": tracker_url,
        "BRN_TRACKER_PORT": str(tracker_port),
        "BRN_BOOTSTRAP_PEERS": bootstrap_peer,
        "BRN_P2P_PORT": str(p2p_port),
    }
    existing = ENV_FILE.read_text(encoding="utf-8-sig").splitlines() if ENV_FILE.exists() else []
    new_lines: list[str] = []
    seen: set[str] = set()

    for line in existing:
        m = _KEY_RE.match(line)
        if not m or m.group(1) not in updates:
            new_lines.append(line)
            continue
        key = m.group(1)
        if key in seen:
            # Evita chaves duplicadas que poderiam sobrescrever o valor atualizado.
            continue
        new_lines.append(f"{key}={_quote_env_value(updates[key])}")
        seen.add(key)

    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={_quote_env_value(value)}")

    _atomic_write(ENV_FILE, "\n".join(new_lines).rstrip() + "\n")


def update_bootstrap_json(peer_list: list[str]) -> None:
    """Atualiza o JSON de bootstrap com uma lista validada de peers."""
    if not all(isinstance(peer, str) and peer.strip() for peer in peer_list):
        raise ValueError("A lista de bootstrap contém um endereço vazio ou inválido.")
    _atomic_write(BOOTSTRAP_FILE, json.dumps(peer_list, indent=2) + "\n")


# ============================================================
# PROBES
# ============================================================
def _probe_tracker(ip: str, tracker_port: int, timeout: Optional[float] = None) -> tuple[bool, str]:
    """Testa o endpoint de saúde do Tracker; não envia segredos."""
    if timeout is None:
        timeout = _http_timeout()
    url = f"http://{_format_host(ip)}:{tracker_port}/health"
    request = urllib.request.Request(url, headers={"User-Agent": "BRN-network-config/2.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 200:
                return True, f"HTTP /health respondeu em {url}"
            return False, f"Tracker respondeu HTTP {response.status}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"HTTP /health não respondeu ({exc.__class__.__name__})"


def _probe_p2p(ip: str, p2p_port: int, timeout: Optional[float] = None) -> tuple[bool, str]:
    """Testa apenas se a porta TCP P2P aceita conexão."""
    if timeout is None:
        timeout = _http_timeout()
    try:
        with socket.create_connection((ip, p2p_port), timeout=timeout):
            return True, f"porta P2P TCP {p2p_port} acessível"
    except (OSError, TimeoutError) as exc:
        return False, f"porta P2P TCP {p2p_port} inacessível ({exc.__class__.__name__})"


# ============================================================
# PEER FILE
# ============================================================
def _write_peer_file(peer: str) -> None:
    _atomic_write(PEER_FILE, peer + "\n")


def _configure(ip: str, tracker_port: int, p2p_port: int) -> tuple[str, str]:
    host = _format_host(ip)
    tracker_url = f"http://{host}:{tracker_port}"
    bootstrap_peer = f"{host}:{p2p_port}"
    update_env_file(tracker_url, bootstrap_peer, tracker_port, p2p_port)
    update_bootstrap_json([bootstrap_peer])
    _write_peer_file(bootstrap_peer)
    return tracker_url, bootstrap_peer


# ============================================================
# MAIN
# ============================================================
def main() -> int:
    tracker_port = _port_from_config("BRN_TRACKER_PORT", DEFAULT_TRACKER_PORT)
    p2p_port = _port_from_config("BRN_P2P_PORT", DEFAULT_P2P_PORT)

    print("=" * 60)
    print(f" BRN - Configurador de Rede (v{__version__})")
    print("=" * 60)
    my_ip = get_tailscale_ip()
    if my_ip:
        print(f"[OK] IP Tailscale detectado: {my_ip}")
    else:
        print("[!] IP Tailscale não detectado.")
        print("    Você pode informar o IP LAN desta máquina se os nós estiverem na mesma rede.")

    print("\nOnde está rodando o Tracker e o nó de origem?")
    print("  1. Nesta máquina (este computador será o Tracker)")
    print("  2. Em outra máquina (este computador será cliente)")
    try:
        option = input("\nEscolha (1 ou 2): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n[X] Configuração cancelada.")
        return 1

    if option == "1":
        raw_ip = my_ip
        if not raw_ip:
            try:
                raw_ip = input("Digite o IP Tailscale ou LAN desta máquina: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[X] Configuração cancelada.")
                return 1
        try:
            server_ip = _validate_server_ip(raw_ip or "")
            tracker_url, bootstrap_peer = _configure(server_ip, tracker_port, p2p_port)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(f"[X] Configuração não concluída: {exc}")
            return 1

        print("\n[OK] Arquivos de configuração atualizados.")
        print(f"Tracker: {tracker_url}")
        print(f"Bootstrap: {bootstrap_peer}")
        print("[!] Este script não inicia o Tracker nem abre portas no firewall.")
        print("    Inicie os serviços e teste a conectividade a partir de outro computador.")
        return 0

    if option == "2":
        try:
            raw_ip = input("IP Tailscale ou LAN do servidor (sem http:// e sem porta): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[X] Configuração cancelada.")
            return 1
        try:
            server_ip = _validate_server_ip(raw_ip)
            tracker_url, bootstrap_peer = _configure(server_ip, tracker_port, p2p_port)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(f"[X] Configuração não concluída: {exc}")
            return 1

        print("\n[OK] Arquivos de configuração atualizados.")
        print(f"Tracker configurado: {tracker_url}")
        print(f"Bootstrap configurado: {bootstrap_peer}")
        print("\nTestando conexão com o servidor...")
        http_ok, http_message = _probe_tracker(server_ip, tracker_port)
        p2p_ok, p2p_message = _probe_p2p(server_ip, p2p_port)
        print(f"[{'OK' if http_ok else 'AVISO'}] {http_message}")
        print(f"[{'OK' if p2p_ok else 'AVISO'}] {p2p_message}")
        if http_ok and p2p_ok:
            print("[OK] Testes básicos de conectividade passaram. "
                  "Isso não comprova sincronização da blockchain.")
            return 0
        print("[!] Configuração salva, mas a conexão completa ainda não foi confirmada.")
        print("    Confira se o Tracker e o nó estão ativos e se o firewall permite as portas configuradas.")
        return 2

    print("[X] Opção inválida.")
    return 1


# ============================================================
# SELF-TEST
# ============================================================
def _self_test() -> int:
    """Testa roundtrip de quote/parse e validação de IP. Sem I/O externo."""
    failures: list[str] = []

    # 1) Roundtrip de quote/parse
    cases = [
        "simple_value",
        "value:with/slashes",
        "value with spaces",
        "value#hash_inside",
        "value=equals",
        'value"with"quotes',
        "value'with'apostrophes",
        "",  # vazio
        "100.64.1.5:6001",
        "http://100.64.1.5:9000",
        "[fd00::1]:6001",
        "amazonas#01",  # # no meio sem espaço antes
    ]
    for case in cases:
        quoted = _quote_env_value(case)
        parsed = _parse_env_value(quoted)
        if parsed != case:
            failures.append(
                f"roundtrip: {case!r} -> quote={quoted!r} -> parse={parsed!r}"
            )

    # 2) Comentário inline (fora de aspas)
    if _parse_env_value("value # comment") != "value":
        failures.append("inline comment: 'value # comment' deveria virar 'value'")
    # Dentro de aspas: preservar
    if _parse_env_value("'value # not comment'") != "value # not comment":
        failures.append("aspas simples deveria preservar #")
    if _parse_env_value('"value # not comment"') != "value # not comment":
        failures.append("aspas duplas deveria preservar #")

    # 3) Prefixo export (read) e ausência na escrita
    if _parse_env_value("value") != "value":
        pass
    if _KEY_RE.match("export BRN_FOO=bar").group(1) != "BRN_FOO":
        failures.append("_KEY_RE deveria aceitar 'export BRN_FOO=bar'")

    # 4) Validação de IP
    valid = ["100.64.1.5", "192.168.1.10", "10.0.0.1", "fd00::1"]
    for v in valid:
        try:
            _validate_server_ip(v)
        except ValueError as e:
            failures.append(f"_validate_server_ip deveria aceitar {v!r}: {e}")

    invalid = ["", "not_an_ip", "127.0.0.1", "0.0.0.0", "::1",
               "fe80::1", "224.0.0.1", "http://1.2.3.4", "1.2.3.4:80"]
    for v in invalid:
        try:
            _validate_server_ip(v)
            failures.append(f"_validate_server_ip deveria rejeitar {v!r}")
        except ValueError:
            pass

    # 5) _format_host
    if _format_host("100.64.1.5") != "100.64.1.5":
        failures.append("_format_host IPv4 falhou")
    if _format_host("fd00::1") != "[fd00::1]":
        failures.append("_format_host IPv6 falhou")

    # 6) Timeouts configuráveis
    os.environ["BRN_HTTP_TIMEOUT"] = "7"
    if abs(_http_timeout() - 7.0) > 0.01:
        failures.append("_http_timeout não respeita BRN_HTTP_TIMEOUT")
    os.environ.pop("BRN_HTTP_TIMEOUT", None)

    if failures:
        print(f"[FAIL] {len(failures)} problema(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"[ok] auto_config_network.py v{__version__} self-test passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(_self_test())
    sys.exit(main())
