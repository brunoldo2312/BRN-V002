"""
auto_config_network.py — Configurador seguro da rede BRN.

Configura os endereços do Tracker e dos peers sem substituir segredos
existentes. Não inicia serviços, não abre portas no firewall e não garante
sincronização: ao conectar como cliente, testa HTTP e TCP e informa o resultado.
"""
from __future__ import annotations

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
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                values[key] = value.strip().strip('"').strip("'")
    except OSError as exc:
        print(f"[X] Não foi possível ler {path.name}: {exc}")
    return values


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
                timeout=5,
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


def _validate_server_ip(raw: str) -> str:
    """Aceita apenas um IP literal utilizável, nunca URL, porta ou loopback."""
    candidate = raw.strip()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError("Informe somente um endereço IPv4 ou IPv6 válido.") from exc
    if address.is_loopback or address.is_unspecified or address.is_link_local or address.is_multicast or address.is_reserved:
        raise ValueError("Esse IP não é adequado para anunciar um servidor remoto.")
    return str(address)


def _format_host(ip: str) -> str:
    address = ipaddress.ip_address(ip)
    return f"[{ip}]" if address.version == 6 else ip


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


def update_env_file(tracker_url: str, bootstrap_peer: str, tracker_port: int, p2p_port: int) -> None:
    """Atualiza apenas as chaves de rede, mantendo segredos e demais opções."""
    updates = {
        "BRN_TRACKER": tracker_url,
        "BRN_TRACKER_PORT": str(tracker_port),
        "BRN_BOOTSTRAP_PEERS": bootstrap_peer,
        "BRN_P2P_PORT": str(p2p_port),
    }
    existing = ENV_FILE.read_text(encoding="utf-8-sig").splitlines() if ENV_FILE.exists() else []
    key_pattern = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    new_lines: list[str] = []
    seen: set[str] = set()

    for line in existing:
        match = key_pattern.match(line)
        if not match or match.group(1) not in updates:
            new_lines.append(line)
            continue
        key = match.group(1)
        if key in seen:
            # Evita chaves duplicadas que poderiam sobrescrever o valor atualizado.
            continue
        new_lines.append(f"{key}={updates[key]}")
        seen.add(key)

    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")

    _atomic_write(ENV_FILE, "\n".join(new_lines).rstrip() + "\n")


def update_bootstrap_json(peer_list: list[str]) -> None:
    """Atualiza o JSON de bootstrap com uma lista validada de peers."""
    if not all(isinstance(peer, str) and peer.strip() for peer in peer_list):
        raise ValueError("A lista de bootstrap contém um endereço vazio ou inválido.")
    _atomic_write(BOOTSTRAP_FILE, json.dumps(peer_list, indent=2) + "\n")


def _probe_tracker(ip: str, tracker_port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """Testa o endpoint de saúde do Tracker; não envia segredos."""
    url = f"http://{_format_host(ip)}:{tracker_port}/health"
    request = urllib.request.Request(url, headers={"User-Agent": "BRN-network-config/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 200:
                return True, f"HTTP /health respondeu em {url}"
            return False, f"Tracker respondeu HTTP {response.status}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"HTTP /health não respondeu ({exc.__class__.__name__})"


def _probe_p2p(ip: str, p2p_port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """Testa apenas se a porta TCP P2P aceita conexão."""
    try:
        with socket.create_connection((ip, p2p_port), timeout=timeout):
            return True, f"porta P2P TCP {p2p_port} acessível"
    except (OSError, TimeoutError) as exc:
        return False, f"porta P2P TCP {p2p_port} inacessível ({exc.__class__.__name__})"


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


def main() -> int:
    tracker_port = _port_from_config("BRN_TRACKER_PORT", DEFAULT_TRACKER_PORT)
    p2p_port = _port_from_config("BRN_P2P_PORT", DEFAULT_P2P_PORT)

    print("=" * 60)
    print(" BRN - Configurador de Rede")
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
            print("[OK] Testes básicos de conectividade passaram. Isso não comprova sincronização da blockchain.")
            return 0
        print("[!] Configuração salva, mas a conexão completa ainda não foi confirmada.")
        print("    Confira se o Tracker e o nó estão ativos e se o firewall permite as portas configuradas.")
        return 2

    print("[X] Opção inválida.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
