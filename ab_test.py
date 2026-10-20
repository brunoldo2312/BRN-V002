"""ab_test.py — Lógica de atribuição do Teste A/B (Ativação de Mineração)"""
import hashlib

EXPERIMENT_ID = "activation_v1"
SALT = "brn_ab_salt_2026"

def get_variant(node_id: str) -> str:
    """
    Retorna 'A' (Controle) ou 'B' (Tratamento).
    Hash determinístico baseado no node_id.
    """
    if not node_id:
        return "A"
    
    # Cria um hash único para o experimento e nó
    raw = f"{EXPERIMENT_ID}:{node_id}:{SALT}".encode("utf-8")
    hash_hex = hashlib.sha256(raw).hexdigest()
    
    # Pega os primeiros 8 caracteres hex, converte pra int e tira módulo 100
    val = int(hash_hex[:8], 16) % 100
    
    # 50/50 split
    return "A" if val < 50 else "B"

def get_node_id_hash(node_id: str) -> str:
    """Retorna um hash seguro do node_id para ser usado nos logs (LGPD)."""
    raw = f"{node_id}:{SALT}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]
