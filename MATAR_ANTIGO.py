"""MATAR_ANTIGO.py — encerra processos BRN antigos (main.py, explorer.py, tracker.py)."""
from __future__ import annotations
import os
import sys
import time
import signal
import subprocess

TARGET_KEYWORDS = ("main.py", "explorer.py", "tracker.py", "brn_tailscale_setup.py")
EXCLUDE_PID = os.getpid()


def _is_target(cmdline: str) -> bool:
    if not cmdline:
        return False
    low = cmdline.lower()
    # evita matar o próprio MATAR_ANTIGO.py
    if "matar_antigo" in low:
        return False
    return any(kw in low for kw in TARGET_KEYWORDS)


def _listar():
    result = []
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["wmic", "process", "get", "commandline,processid", "/format:csv"],
                capture_output=True, text=True, timeout=10,
            )
            for line in r.stdout.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 3 or not parts[1]:
                    continue
                cmdline = parts[1]
                try:
                    pid = int(parts[-1])
                except ValueError:
                    continue
                if pid != EXCLUDE_PID and _is_target(cmdline):
                    result.append((pid, cmdline))
        except Exception:
            pass
    else:
        try:
            r = subprocess.run(
                ["ps", "-eo", "pid,args"],
                capture_output=True, text=True, timeout=10,
            )
            for line in r.stdout.splitlines()[1:]:
                parts = line.strip().split(None, 1)
                if len(parts) != 2:
                    continue
                try:
                    pid = int(parts[0])
                except ValueError:
                    continue
                cmdline = parts[1]
                if pid != EXCLUDE_PID and _is_target(cmdline):
                    result.append((pid, cmdline))
        except Exception:
            pass
    return result


def main() -> int:
    print("=" * 60)
    print("  MATAR_ANTIGO — encerra processos BRN antigos")
    print("=" * 60)

    procs = _listar()
    if not procs:
        print("\nNenhum processo BRN rodando. Nada a fazer.")
        return 0

    print(f"\nEncontrei {len(procs)} processo(s) BRN:")
    for pid, cmd in procs:
        print(f"  PID {pid:>6}: {cmd[:100]}")

    print("\n[1/2] SIGTERM (gracioso)...")
    for pid, _ in procs:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                               capture_output=True, timeout=5)
            else:
                os.kill(pid, signal.SIGTERM)
            print(f"  PID {pid}: SIGTERM")
        except Exception as e:
            print(f"  PID {pid}: {e}")

    time.sleep(3)
    restantes = _listar()

    if restantes:
        print(f"\n[2/2] {len(restantes)} ainda vivo(s). SIGKILL...")
        for pid, _ in restantes:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, timeout=5)
                else:
                    os.kill(pid, signal.SIGKILL)
                print(f"  PID {pid}: SIGKILL")
            except Exception as e:
                print(f"  PID {pid}: {e}")
        time.sleep(1)
    else:
        print("\n[2/2] Todos terminaram graciosamente.")

    final = _listar()
    if final:
        print(f"\n[!] {len(final)} processo(s) resistiram:")
        for pid, cmd in final:
            print(f"  PID {pid}: {cmd[:80]}")
        return 1

    print("\n[OK] Todos os processos BRN encerrados.")
    print("[*] Agora rode: python3 main.py --headless")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(130)
