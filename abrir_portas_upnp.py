
import upnpclient
import socket
import sys
import urllib.request

PORTAS = [(5000, "BRN_API_HTTP"), (6001, "BRN_P2P"), (8080, "BRN_Explorer")]

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()

def get_public_ip():
    try:
        req = urllib.request.Request("https://api.ipify.org", headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode().strip()
    except Exception:
        return None

def main():
    print("=" * 60)
    print(" BRN — Configurador Automático de Portas (UPnP)")
    print("=" * 60)

    local_ip = get_local_ip()
    print(f"[i] IP local: {local_ip}")

    try:
        devices = upnpclient.discover()
    except Exception as e:
        print(f"[X] Erro ao buscar UPnP: {e}")
        sys.exit(1)

    if not devices:
        print("[X] Nenhum roteador UPnP encontrado.")
        sys.exit(1)

    router = None
    for d in devices:
        if 'WANIPConnection' in d.services or 'WANPPPConnection' in d.services:
            router = d
            break

    if not router:
        print("[X] Roteador encontrado, mas sem suporte a UPnP WANIP.")
        sys.exit(1)

    print(f"[OK] Roteador: {router.friendly_name}")

    for port, desc in PORTAS:
        print(f"[*] Abrindo porta {port} ({desc})...")
        try:
            try:
                router.WANIPConnection.DeletePortMapping(NewRemoteHost='', NewExternalPort=port, NewProtocol='TCP')
            except Exception:
                pass

            router.WANIPConnection.AddPortMapping(
                NewRemoteHost='', NewExternalPort=port, NewProtocol='TCP',
                NewInternalPort=port, NewInternalClient=local_ip, NewEnabled='1',
                NewPortMappingDescription=desc, NewLeaseDuration=0
            )
            print(f"    [OK] Porta {port} aberta.")
        except Exception as e:
            print(f"    [X] Falha na porta {port}: {e}")
            sys.exit(1) # Se falhar uma, já era.

    public_ip = get_public_ip()
    if public_ip:
        # Verifica se é CGNAT
        partes = public_ip.split('.')
        if len(partes) >= 2:
            if int(partes[0]) == 100 and 64 <= int(partes[1]) <= 127:
                print(f"\n[!] AVISO: IP {public_ip} parece ser CGNAT. UPnP não vai funcionar na internet.")
                sys.exit(1)

        print(f"\n[OK] IP Público: {public_ip}")
        # Salva o IP para o .bat ler
        with open("brn_public_ip.txt", "w") as f:
            f.write(public_ip)
        print("[OK] Sucesso absoluto! Rede pública configurada.")
        sys.exit(0)
    else:
        print("[X] Não consegui descobrir o IP público.")
        sys.exit(1)

if __name__ == "__main__":
    main()
