import os

def patch_file(filepath, old_str, new_str):
    if not os.path.exists(filepath):
        print(f"[!] {filepath} não encontrado.")
        return False
    
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    
    if old_str not in content:
        print(f"[OK] {filepath} já está correto (não precisa mudar).")
        return True
        
    new_content = content.replace(old_str, new_str)
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(new_content)
    
    print(f"[OK] {filepath} corrigido com sucesso!")
    return True

if __name__ == "__main__":
    print("=" * 50)
    print(" CORRIGINDO ROTA DA API DO POOL")
    print("=" * 50)
    # Substitui a rota antiga pela rota que o servidor do Bruno entende
    patch_file("mining_pool.py", "/api/pool/getwork", "/api/pool/work")
    patch_file("miner_pool_client.py", "/api/pool/getwork", "/api/pool/work")
    print("=" * 50)
    print(" PRONTO! Reinicie o main.py da Mayra.")
    print("=" * 50)
