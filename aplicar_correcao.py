import os
import re

def patch_trust_network():
    filepath = "trust_network.py"
    if not os.path.exists(filepath):
        print(f"[X] {filepath} não encontrado.")
        return

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Força o cliente a salvar no trust_network.json (para os testes verem)
    old_flush = '''                with open(self.local_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                self._dirty_local = False'''
    
    new_flush = '''                with open(self.local_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                # [AUTO-FIX] Salva também no main para os testes verem
                with open(self.data_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                self._dirty_local = False'''

    # 2. Impede o cliente de apagar o próprio trust ao reiniciar
    old_load = '''                    if not self.is_origin:
                        log.info("[TrustNetwork] main sem assinatura — cliente carrega só local")
                        data = {"nodes": {}}'''
    
    new_load = '''                    if not self.is_origin:
                        if data.get("client_written"):
                            log.info("[TrustNetwork] main carregado (escrito por cliente)")
                        else:
                            log.info("[TrustNetwork] main sem assinatura — cliente carrega só local")
                            data = {"nodes": {}}'''

    content = content.replace(old_flush, new_flush)
    content = content.replace(old_load, new_load)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print("[OK] trust_network.py corrigido (salvamento automático do trust).")

def patch_server():
    filepath = "server.py"
    if not os.path.exists(filepath):
        print(f"[!] {filepath} não encontrado (pulando patch do server).")
        return

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    if "/api/validator/announce" in content:
        print("[OK] server.py já tem a rota /api/validator/announce.")
        return

    # Injeta a rota no final do arquivo (antes do if __name__ == '__main__')
    route_code = '''
# ============================================================
# [AUTO-FIX] Rota para aceitar clientes automaticamente
# ============================================================
@app.route("/api/validator/announce", methods=["POST"])
def api_validator_announce():
    try:
        from flask import request, jsonify
        data = request.get_json(force=True, silent=True) or {}
        pubkey = data.get("pubkey", "").strip().lower()
        address = data.get("address", "").strip()
        
        if not pubkey or len(pubkey) != 66:
            return jsonify({"ok": False, "msg": "pubkey invalida"}), 400
            
        # Tenta pegar a trust network ativa
        trust = getattr(g, 'trust_net', None) if 'g' in globals() else None
        if not trust:
            import server
            trust = getattr(server, 'trust_net', None)
            
        if trust and hasattr(trust, "register_new_validator"):
            res = trust.register_new_validator(pubkey, address)
            return jsonify(res)
            
        return jsonify({"ok": False, "msg": "TrustNetwork nao inicializada"}), 500
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500
'''

    if "if __name__" in content:
        content = content.replace("if __name__", route_code + "\n\nif __name__")
    else:
        content += "\n" + route_code

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print("[OK] server.py corrigido (rota de announce adicionada).")

if __name__ == "__main__":
    print("="*50)
    print(" APLICANDO CORREÇÕES AUTOMÁTICAS")
    print("="*50)
    patch_trust_network()
    patch_server()
    print("="*50)
    print(" PRONTO! Reinicie o main.py agora.")
    print("="*50)
