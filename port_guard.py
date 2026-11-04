"""port_guard.py — aborta o boot se portas do BRN já estão em uso (v1.1)

v1.1 (2026-11-04):
  [FIX CRÍTICO] print com '✓' (U+2713) crashava em Windows pt-BR
                (cp1252). Substituído por 'OK' e adicionado
                sys.stdout.reconfigure(encoding="utf-8") no topo.
  [FIX] Windows: SO_REUSEADDR deixava bind() passar mesmo com porta
        ocupada (comportamento do Winsock). Agora usa
        SO_EXCLUSIVEADDRUSE quando os.name == "nt", que detecta
        corretamente.
  [NEW] _diagnostico_porta() ganhou fallback com psutil (mais
        confiável que netstat quando a porta é IPv6 ou o PID tem
        nome longo).
  [KEEP] Mesma API pública (check_brn_ports, check_only) e CLI.
v1.0:
  Versão inicial.

Uso no main.py (2 linhas):

    import port_guard
    ...
    def main():
        port_guard.check_brn_ports()   # <-- primeira linha do main()
        _verificar_integridade_codigo()
        ...

Env respeitadas:
    BRN_P2P_PORT        (default 6001)
    BRN_WEB_PORT        (default 5000)
    BRN_EXPLORER_PORT   (default 8080, opcional checar)
    BRN_PORT_GUARD_SKIP=1 → desativa o guard (só para testes)
"""
from __future__ import annotations

import os
import sys
import socket
import subprocess


# ============================================================
# [FIX v1.1] Força UTF-8 no stdout/stderr ANTES de qualquer print
# ============================================================
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# psutil é opcional — melhora o diagnóstico se estiver instalado
_HAS_PSUTIL = False
try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except ImportError:
    pass


# ============================================================
# DETECÇÃO DE PORTA
# ============================================================
def _port_em_uso(port: int, host: str = "0.0.0.0") -> bool:
    """Tenta bind; se falhar com OSError, porta está ocupada.

    [FIX v1.1] No Windows, SO_REUSEADDR faz o bind() SUCEDER mesmo
    quando outro processo está escutando na mesma porta (comportamento
    do Winsock). Trocamos por SO_EXCLUSIVEADDRUSE, que falha nesse
    caso — que é o que queremos detectar.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            # Windows: SO_EXCLUSIVEADDRUSE impede bind compartilhado
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except (AttributeError, OSError):
                # Fallback: sem a flag, usa SO_REUSEADDR (raro)
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                except Exception:
                    pass
        else:
            # POSIX: SO_REUSEADDR permite reaproveitar TIME_WAIT
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            s.bind((host, int(port)))
            return False
        except OSError:
            return True
    finally:
        try:
            s.close()
        except Exception:
            pass


def _who_uses_port_psutil(port: int):
    """Retorna [(pid, nome), ...] para LISTEN na porta. Usa psutil."""
    if not _HAS_PSUTIL:
        return []
    out = []
    try:
        for conn in psutil.net_connections(kind="inet"):
            try:
                if (conn.status == "LISTEN"
                        and conn.laddr
                        and conn.laddr.port == port):
                    pid = conn.pid
                    try:
                        proc = psutil.Process(pid)
                        out.append((pid, proc.name()))
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        out.append((pid, "<desconhecido>"))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _diagnostico_porta(port: int) -> None:
    """Mostra quem está usando a porta (best-effort, Linux/macOS/Windows)."""
    print(f"\n[port_guard] Processo(s) na porta {port}:", file=sys.stderr)

    # 1) psutil (mais confiável, se disponível)
    if _HAS_PSUTIL:
        ps = _who_uses_port_psutil(port)
        if ps:
            for pid, pname in ps:
                print(f"  PID {pid} ({pname})", file=sys.stderr)
            return

    # 2) Linux: ss
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

    # 3) macOS/Linux: lsof
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

    # 4) Windows: netstat
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


# ============================================================
# API PÚBLICA
# ============================================================
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
        # [FIX v1.1] ASCII-safe — sem U+2713, sem crash em cp1252.
        print(f"[port_guard] portas {p2p} (P2P) e {web} (HTTP) livres OK")
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
    """Diagnóstico sem abortar — útil para CLI.

    Retorna {porta: bool_em_uso}.
    """
    return {int(p): _port_em_uso(int(p)) for p in ports}


# ============================================================
# CLI
# ============================================================
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
