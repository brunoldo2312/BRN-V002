import subprocess
import time
import sys
import os

# Garante que estamos na pasta correta
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

def iniciar_brn():
    print("====================================================")
    print("  INICIANDO BRN 24/7 (Supervisor Ativo)")
    print("  Pressione Ctrl+C para encerrar tudo.")
    print("====================================================")
    
    while True:
        try:
            print(f"[{time.strftime('%H:%M:%S')}] Iniciando Nó (main.py)...")
            # Inicia o nó principal em modo headless (sem interface gráfica)
            node = subprocess.Popen([sys.executable, "main.py", "--headless", "--client-mode", "--no-pool-client"])
            
            print(f"[{time.strftime('%H:%M:%S')}] Iniciando Painel Web (explorer.py)...")
            # Inicia o painel web na porta 8080
            web = subprocess.Popen([sys.executable, "explorer.py"])

            # O script vai ficar travado aqui esperando um dos dois cair
            node.wait()
            web.wait()

            print(f"[{time.strftime('%H:%M:%S')}] [!] Um dos processos caiu. Reiniciando em 10 segundos...")
            time.sleep(10)

        except KeyboardInterrupt:
            print("\nEncerrando o supervisor...")
            try:
                node.terminate()
                web.terminate()
            except:
                pass
            break
        except Exception as e:
            print(f"Erro no supervisor: {e}")
            time.sleep(10)

if __name__ == "__main__":
    iniciar_brn()