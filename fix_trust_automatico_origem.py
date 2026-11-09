import os

def patch_file(filepath, old_str, new_str):
    if not os.path.exists(filepath):
        print(f"[!] {filepath} não encontrado.")
        return False
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    
    if old_str not in content:
        print(f"[!] Trecho não encontrado em {filepath} (pode já estar corrigido).")
        return False
        
    content = content.replace(old_str, new_str)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[OK] {filepath} corrigido com sucesso!")
    return True

if __name__ == "__main__":
    print("=" * 60)
    print(" 🛠️  AUTO-TRUST NA ORIGEM (BRUNO)")
    print("=" * 60)
    
    # 1. Registra qualquer novo minerador com score alto
    old_reg = """            try:
                trust.register_node(miner_id, address, pubkey)
                return"""
    new_reg = """            try:
                if hasattr(trust, "register_new_validator"):
                    trust.register_new_validator(pubkey, address)
                else:
                    trust.register_node(miner_id, address, pubkey)
                return"""

    # 2. Heartbeat mantém a chave viva
    old_hb = """            if hashes_total > m.hashes_total:
                m.hashes_total = int(hashes_total)
        return {"ok": True}"""
    new_hb = """            if hashes_total > m.hashes_total:
                m.hashes_total = int(hashes_total)
            
            # [AUTO-FIX] Mantém o minerador ativo no Trust da Origem
            trust = getattr(self.chain, "trust_net", None)
            if trust and hasattr(trust, "heartbeat"):
                try:
                    trust.heartbeat(m.pubkey)
                except Exception:
                    pass
        return {"ok": True}"""

    # 3. Submit share também mantém a chave viva
    old_sub = """            m.last_seen = time.time()
            job = self.current_job
            if job is None:
                return {"ok": False, "msg": "sem job ativo"}"""
    new_sub = """            m.last_seen = time.time()
            
            # [AUTO-FIX] Mantém o minerador ativo no Trust da Origem
            trust = getattr(self.chain, "trust_net", None)
            if trust and hasattr(trust, "heartbeat"):
                try:
                    trust.heartbeat(m.pubkey)
                except Exception:
                    pass
                    
            job = self.current_job
            if job is None:
                return {"ok": False, "msg": "sem job ativo"}"""

    patch_file("mining_pool.py", old_reg, new_reg)
    patch_file("mining_pool.py", old_hb, new_hb)
    patch_file("mining_pool.py", old_sub, new_sub)
    
    print("=" * 60)
    print(" PRONTO! Reinicie o main.py da Origem.")
    print(" Agora TODOS os mineradores serão aceitos e mantidos vivos!")
    print("=" * 60)
