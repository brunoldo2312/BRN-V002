import requests
import time
from blockchain import Blockchain

# IP do seu Ubuntu (Máquina A)
UBUNTU_IP = "100.111.253.8"
API_URL = f"http://{UBUNTU_IP}:5000/api"

def forcar_download():
    print(f"[*] Conectando ao Ubuntu em {API_URL}...")
    
    # 1. Pega a altura do Ubuntu via HTTP
    try:
        r = requests.get(f"{API_URL}/status", timeout=5)
        target_height = r.json().get("height", 0)
    except Exception as e:
        print(f"[X] Erro ao conectar no Ubuntu: {e}")
        return

    print(f"[*] Altura do Ubuntu: {target_height}")
    
    # 2. Abre a blockchain local do Windows
    bc = Blockchain(auto_genesis=False)
    local_height = bc.db.height()
    print(f"[*] Altura local do Windows: {local_height}")

    if local_height >= target_height:
        print("[ok] O Windows já está sincronizado!")
        return

    # 3. Baixa os blocos em lotes de 100 via HTTP
    batch_size = 100
    current = local_height + 1
    
    print("[*] Iniciando download forçado via HTTP...")
    
    while current <= target_height:
        end = min(current + batch_size, target_height + 1)
        print(f"    Baixando blocos {current} até {end}...")
        
        try:
            r = requests.get(f"{API_URL}/blocks?start={current}&limit={batch_size}", timeout=10)
            data = r.json()
            blocks = data.get("blocks", [])
            
            if not blocks:
                print("    [!] Nenhum bloco recebido. Aguardando...")
                time.sleep(2)
                continue
                
            for blk in blocks:
                ok, msg = bc.accept_block(blk)
                if not ok:
                    print(f"    [X] Falha ao aceitar bloco #{blk['height']}: {msg}")
                    return
            
            current = end
            bc.db.conn.commit() # Garante que salvou no banco
            
        except Exception as e:
            print(f"    [X] Erro no lote: {e}")
            time.sleep(2)

    print("\n[SUCESSO] Todos os blocos foram baixados e aplicados!")
    print(f"Nova altura do Windows: {bc.db.height()}")

if __name__ == "__main__":
    forcar_download()
