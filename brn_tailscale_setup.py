"""
brn_tailscale_setup.py - Setup idempotente do Tailscale para o BRN
==================================================================

Este script resolve o problema de VLANs separadas criando uma VPN
mesh (Tailscale) entre as maquinas que rodam o no BRN.

Modos de uso:

    python brn_tailscale_setup.py
        Modo interativo. Faz o setup inicial:
        - Instala o Tailscale (se necessario)
        - Faz login
        - Descobre o IP Tailscale desta maquina
        - Pede o IP do outro PC
        - Salva em brn_tailscale_peer.txt
        - Atualiza bootstrap_peers.json

    python brn_tailscale_setup.py --auto
        Modo automatico. Usado no startup do no:
        - Verifica Tailscale instalado e autenticado
        - Le brn_tailscale_peer.txt
        - Atualiza bootstrap_peers.json se algo mudou
        - Exit 0 se tudo OK, 1 se precisa atencao

    python brn_tailscale_setup.py --check
        So verifica o estado atual. Nao modifica nada.

Arquivos usados:

    brn_tailscale_peer.txt
        Uma linha por peer (IP:porta).
        Exemplo: 100.64.1.42:6001

    bootstrap_peers.json
        Atualizado automaticamente com o peer acima.
"""
import os
import sys
import json
import subprocess
import platform
from pathlib import Path

# ============================================================
# CONFIGURACOES
# ============================================================
BASE = Path(__file__).parent
BOOTSTRAP_FILE = BASE / "bootstrap_peers.json"
PEER_FILE = BASE / "brn_tailscale_peer.txt"
P2P_PORT = 6001

TAILSCALE_WINDOWS_PATHS = [
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
]


# ============================================================
# HELPERS DE LOG
# ============================================================
def log(tag, msg):
    print("[" + tag + "] " + msg, flush=True)


def info(msg):
    log("i", msg)


def ok(msg):
    log("ok", msg)


def warn(msg):
    log("!", msg)


def err(msg):
    log("X", msg)


# ============================================================
# HELPERS DE SISTEMA
# ============================================================
def run(cmd, capture=True, timeout=30):
    """Roda um comando e retorna (returncode, output)."""
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            text=True,
            timeout=timeout,
        )
        return r.returncode, (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return -2, "timeout"
    except Exception as e:
        return -1, str(e)


def is_windows():
    return platform.system().lower().startswith("win")


def is_linux():
    return platform.system().lower() == "linux"


# ============================================================
# HELPERS DO TAILSCALE
# ============================================================
def tailscale_path():
    """
    Retorna o comando para invocar o Tailscale.
    Prefere o PATH. Se nao achar, procura nos caminhos default do Windows.
    Retorna None se nao instalado.
    """
    rc, _ = run("tailscale version")
    if rc == 0:
        return "tailscale"

    if is_windows():
        for p in TAILSCALE_WINDOWS_PATHS:
            if os.path.exists(p):
                return '"' + p + '"'

    return None


def tailscale_ip(ts_cmd):
    """
    Retorna o IP Tailscale da maquina (100.x.x.x) ou string vazia.
    """
    rc, out = run(ts_cmd + " ip -4")
    if rc != 0:
        return ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("100."):
            return line
    return ""


def tailscale_authenticated(ts_cmd):
    """
    Verifica se o Tailscale esta logado.
    """
    rc, out = run(ts_cmd + " status")
    if rc != 0:
        return False
    out_l = out.lower()
    if "logged out" in out_l:
        return False
    if "not logged in" in out_l:
        return False
    return True


# ============================================================
# HELPERS DE ARQUIVO
# ============================================================
def read_peer_file():
    """
    Le IPs do peer em brn_tailscale_peer.txt.
    Retorna lista de strings IP:porta.
    """
    if not PEER_FILE.exists():
        return []
    peers = []
    try:
        content = PEER_FILE.read_text(encoding="utf-8-sig")
    except Exception:
        try:
            content = PEER_FILE.read_text(encoding="latin-1")
        except Exception:
            return []

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            line = line + ":" + str(P2P_PORT)
        peers.append(line)
    return peers


def update_bootstrap(own_ip, peer_ips):
    """
    Escreve bootstrap_peers.json com o peer + proprio IP.
    Retorna True se o arquivo mudou.
    """
    existing = []
    if BOOTSTRAP_FILE.exists():
        try:
            existing = json.loads(BOOTSTRAP_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            existing = []

    if not isinstance(existing, list):
        existing = []

    entries = set()
    for e in existing:
        if isinstance(e, str):
            entries.add(e)

    for ip in peer_ips:
        entries.add(ip)

    if own_ip:
        entries.add(own_ip + ":" + str(P2P_PORT))

    new_list = sorted(entries)

    if new_list == sorted(entries - set(peer_ips) - ({own_ip + ":" + str(P2P_PORT)} if own_ip else set())) and len(new_list) == len(existing):
        pass

    if new_list == sorted([e for e in existing if isinstance(e, str)]):
        return False

    try:
        BOOTSTRAP_FILE.write_text(
            json.dumps(new_list, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        err("Erro salvando bootstrap: " + str(e))
        return False


# ============================================================
# MODO AUTO (usado no startup)
# ============================================================
def mode_auto():
    """
    Roda sem interacao. Exit codes:
        0 = tudo OK, main.py pode subir
        1 = precisa acao manual (usuario deve rodar sem --auto)
    """
    ts = tailscale_path()
    if not ts:
        err("Tailscale NAO instalado")
        info("Rode: python brn_tailscale_setup.py  (sem --auto)")
        return 1

    if not tailscale_authenticated(ts):
        err("Tailscale nao autenticado")
        if is_windows():
            info("Abra o Tailscale no menu Iniciar e faca login.")
        else:
            info("Rode: sudo tailscale up")
        return 1

    own_ip = tailscale_ip(ts)
    if not own_ip:
        err("Nao consegui obter IP Tailscale")
        info("Rode: " + ts + " ip -4")
        return 1
    ok("IP Tailscale: " + own_ip)

    peer_ips = read_peer_file()
    if not peer_ips:
        warn("Arquivo " + PEER_FILE.name + " nao existe ou esta vazio")
        info("Crie com o IP Tailscale do outro PC:")
        if is_windows():
            info('   echo 100.64.1.42:6001 > ' + PEER_FILE.name)
        else:
            info('   echo 100.64.1.42:6001 > ' + PEER_FILE.name)
        return 1

    info("Peers: " + ", ".join(peer_ips))

    changed = update_bootstrap(own_ip, peer_ips)
    if changed:
        ok("bootstrap_peers.json atualizado")
    else:
        ok("bootstrap_peers.json ja estava correto")

    return 0


# ============================================================
# MODO CHECK
# ============================================================
def mode_check():
    """
    Mostra o estado atual sem modificar nada.
    """
    print()
    print("=" * 60)
    print("  BRN - Tailscale Status")
    print("=" * 60)
    print()

    ts = tailscale_path()
    if ts:
        print("  Tailscale instalado : SIM")
        print("  Comando             : " + ts)

        if tailscale_authenticated(ts):
            print("  Autenticado         : SIM")
            ip = tailscale_ip(ts)
            print("  IP Tailscale        : " + (ip if ip else "(nenhum)"))
        else:
            print("  Autenticado         : NAO")
            print("  (rode 'tailscale up' para autenticar)")
    else:
        print("  Tailscale instalado : NAO")

    print()

    peers = read_peer_file()
    print("  Peers configurados  : " + str(len(peers)))
    for p in peers:
        print("    - " + p)

    print()

    if BOOTSTRAP_FILE.exists():
        print("  bootstrap_peers.json: existe")
        try:
            content = json.loads(BOOTSTRAP_FILE.read_text(encoding="utf-8-sig"))
            for c in content:
                print("    - " + str(c))
        except Exception as e:
            print("    (erro lendo: " + str(e) + ")")
    else:
        print("  bootstrap_peers.json: NAO existe")

    print()
    return 0


# ============================================================
# MODO INTERATIVO (setup inicial)
# ============================================================
def mode_interactive():
    """
    Fluxo interativo para primeira configuracao.
    """
    print()
    print("=" * 60)
    print("  BRN - Setup do Tailscale (interativo)")
    print("=" * 60)
    print()

    # --- 1) Verifica instalacao ---
    ts = tailscale_path()
    if not ts:
        err("Tailscale NAO instalado")
        print()
        if is_windows():
            print("  Baixe em: https://tailscale.com/download/windows")
            print("  Depois de instalar, rode este script de novo.")
            print()
        elif is_linux():
            print("  Para instalar no Ubuntu/Linux, rode:")
            print()
            print("    curl -fsSL https://tailscale.com/install.sh | sh")
            print()
            print("  Depois rode este script de novo.")
            print()
        else:
            print("  Baixe em: https://tailscale.com/download")
            print()
        return 1
    ok("Tailscale instalado")

    # --- 2) Verifica autenticacao ---
    if not tailscale_authenticated(ts):
        warn("Tailscale nao autenticado")
        print()
        if is_windows():
            print("  Abra o Tailscale no menu Iniciar e faca login.")
            print("  (use a MESMA conta do outro PC)")
        elif is_linux():
            print("  Rodando 'sudo tailscale up'...")
            print("  Uma URL vai aparecer. Copie no navegador e autorize.")
            print()
            subprocess.run("sudo tailscale up", shell=True)
        print()
        try:
            input("  Pressione ENTER depois de fazer login...")
        except EOFError:
            return 1

    if not tailscale_authenticated(ts):
        err("Ainda nao autenticado. Abortando.")
        return 1
    ok("Tailscale autenticado")

    # --- 3) Pega IP proprio ---
    own_ip = tailscale_ip(ts)
    if not own_ip:
        err("Nao consegui obter IP Tailscale.")
        info("Rode manualmente: " + ts + " ip -4")
        return 1

    print()
    print("  ------------------------------------------")
    print("  SEU IP TAILSCALE: " + own_ip)
    print("  ------------------------------------------")
    print()
    print("  Anote este IP. Voce vai precisar dele no OUTRO PC.")
    print()

    # --- 4) Pede IP do peer ---
    print("  Agora cole o IP Tailscale do OUTRO computador.")
    print("  (no outro PC, rode 'tailscale ip -4' para descobrir)")
    print()

    try:
        peer = input("  IP do peer (ex: 100.64.1.42): ").strip()
    except EOFError:
        return 1

    if not peer:
        warn("Vazio. Nada a fazer.")
        return 1

    # Limpa possiveis espacos
    peer = peer.replace(" ", "").replace("\t", "")

    if ":" not in peer:
        peer = peer + ":" + str(P2P_PORT)

    # --- 5) Salva peer ---
    try:
        PEER_FILE.write_text(peer + "\n", encoding="utf-8")
        ok("Salvo em " + PEER_FILE.name + ": " + peer)
    except Exception as e:
        err("Erro salvando " + PEER_FILE.name + ": " + str(e))
        return 1

    # --- 6) Atualiza bootstrap ---
    if update_bootstrap(own_ip, [peer]):
        ok("bootstrap_peers.json atualizado")
    else:
        info("bootstrap_peers.json ja estava correto")

    # --- 7) Instrucoes finais ---
    print()
    print("=" * 60)
    print("  CONFIGURACAO CONCLUIDA")
    print("=" * 60)
    print()
    print("  Seu IP Tailscale : " + own_ip)
    print("  Peer configurado : " + peer)
    print()
    print("  IMPORTANTE: faca a mesma configuracao no outro PC,")
    print("  usando o SEU IP (" + own_ip + ") como peer.")
    print()
    print("  Depois, reinicie o no BRN:")
    if is_windows():
        print("    iniciar.bat")
    else:
        print("    ./brn.sh")
    print()

    return 0


# ============================================================
# MAIN
# ============================================================
def main():
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0

    if "--auto" in args:
        return mode_auto()

    if "--check" in args:
        return mode_check()

    return mode_interactive()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print("Cancelado.")
        sys.exit(130)