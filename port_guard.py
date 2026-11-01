"""port_guard.py — aborta o boot se portas do BRN já estão em uso (v1.0)

Motivo: main.py v13.7.12 engolia o erro de "Address already in use"
no P2P e continuava como se tivesse subido — resultado: HTTP, P2P e
Explorer caíam em silêncio, e PoolClient minerava para o processo
antigo sem o operador perceber.

Uso no main.py (2 linhas):

    import port_guard
    ...
    def main():
        port_guard.check_brn_ports()   # <-- primeira linha do main()
        _verificar_integridade_codigo()
        args = parse_args()
        ...

Env respeitadas:
    BRN_P2P_PORT    (default 6001)
    BRN_WEB_PORT    (default 5000)
    BRN_EXPLORER_PORT (default 8080, opcional checar)
    BRN_PORT_GUARD_SKIP=1 → desativa o guard (só para testes)
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys


def _port_em_uso(port: int, host: str = "0.0.0.0") -> bool:
    """Tenta bind; se falhar com OSError, porta está ocupada."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, int(port)))
        s.close()
        return False
    except OSError:
        s.close()
        return True


def _diagnostico_porta(port: int) -> None:
    """Mostra quem está usando a porta (best-effort, Linux/macOS/Windows)."""
    print(f"\n[port_guard] Processo(s) na porta {port}:", file=sys.stderr)

    # Linux: ss
    try:
        r = subprocess.run(
            ["ss", "-tlnp", f"sport = :{port}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip() and len(r.stdout.strip().splitlines()) > 1:
            for line in r.stdout.strip().splitlines():
                print("  " + line, file=sys.stderr)
            return
    except Exception:
        pass

    # macOS/Linux: lsof
    try:
        r = subprocess.run(
            ["lsof", "-nP", f"-i:{port}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip():
            for line in r.stdout.strip().splitlines()[:6]:
                print("  " + line, file=sys.stderr)
            return
    except Exception:
        pass

    # Windows: netstat
    try:
        r = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.splitlines():
            if f":{port} " in line or f":{port}\t" in line:
                print("  " + line.strip(), file=sys.stderr)
    except Exception:
        pass


def _sugerir_acao() -> None:
    print("\n  Como resolver:", file=sys.stderr)
    print("    1. python3 MATAR_ANTIGO.py    (mata main/explorer antigos)", file=sys.stderr)
    print("    2. ss -tlnp | grep -E ':(5000|6001|8080)\\b'  (confere)", file=sys.stderr)
    print("    3. python3 main.py --headless  (sobe o nó novo)", file=sys.stderr)


def check_brn_ports(checar_explorer: bool = False) -> None:
    """Aborta o processo se portas do BRN estiverem em uso.

    Levanta SystemExit(1) — que main.py trata como retorno de erro.
    """
    if os.environ.get("BRN_PORT_GUARD_SKIP", "0") == "1":
        print("[port_guard] desativado por BRN_PORT_GUARD_SKIP=1")
        return

    p2p = int(os.environ.get("BRN_P2P_PORT", "6001"))
    web = int(os.environ.get("BRN_WEB_PORT", "5000"))
    expl = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))

    alvos = [("P2P", p2p), ("HTTP", web)]
    if checar_explorer:
        alvos.append(("Explorer", expl))

    ocupadas = []
    for nome, porta in alvos:
        if _port_em_uso(porta):
            ocupadas.append((nome, porta))

    if not ocupadas:
        print(f"[port_guard] portas {p2p} (P2P) e {web} (HTTP) livres \u2713")
        return

    # Pelo menos uma porta ocupada — aborta com diagnóstico
    print("=" * 64, file=sys.stderr)
    for nome, porta in ocupadas:
        print(f" [port_guard] FATAL: porta {nome} {porta} JÁ EM USO", file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    print("\n Isto significa que OUTRO nó BRN está rodando (provavelmente", file=sys.stderr)
    print(" uma versão antiga em v13.6.0 ou similar). Se você não matar esse", file=sys.stderr)
    print(" processo, o nó novo vai subir em modo degradado:", file=sys.stderr)
    print("   - P2P não escuta (ninguém conecta)", file=sys.stderr)
    print("   - HTTP não responde (PoolClient mira no antigo)", file=sys.stderr)
    print("   - Explorer não sobe", file=sys.stderr)

    for _, porta in ocupadas:
        _diagnostico_porta(porta)

    _sugerir_acao()
    sys.exit(1)


def check_only(ports: list) -> dict:
    """Diagnóstico sem abortar — útil para CLI. Retorna {porta: bool_em_uso}."""
    return {int(p): _port_em_uso(int(p)) for p in ports}


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(prog="port_guard.py")
    ap.add_argument("--check", action="store_true",
                    help="só checa, não aborta (usar em CLI)")
    ap.add_argument("--explorer", action="store_true",
                    help="inclui BRN_EXPLORER_PORT (8080)")
    ap.add_argument("--json", action="store_true",
                    help="com --check: saída JSON")
    args = ap.parse_args()

    if args.check:
        p2p = int(os.environ.get("BRN_P2P_PORT", "6001"))
        web = int(os.environ.get("BRN_WEB_PORT", "5000"))
        alvos = [p2p, web]
        if args.explorer:
            alvos.append(int(os.environ.get("BRN_EXPLORER_PORT", "8080")))
        res = check_only(alvos)
        if args.json:
            print(json.dumps({str(k): v for k, v in res.items()}))
        else:
            for porta, ocupada in res.items():
                status = "OCUPADA" if ocupada else "livre"
                print(f"  {porta}: {status}")
        sys.exit(1 if any(res.values()) else 0)

    check_brn_ports(checar_explorer=args.explorer)
