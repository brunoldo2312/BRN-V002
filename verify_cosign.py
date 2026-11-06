#!/usr/bin/env python3
"""
verify_cosign.py — Auditor de Co-Assinatura (Multi-Sig) BRN
================================================================
Verifica criptograficamente se os blocos recentes foram assinados
corretamente pelos validadores ativos da Trust Network.

Uso:
    python verify_cosign.py
    python verify_cosign.py --blocks 50
================================================================
"""
import os
import sys
import time
import argparse
from pathlib import Path

# Cores ANSI para o terminal
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    GRAY   = "\033[90m"

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# Tenta carregar o .env para garantir as chaves de rede
ENV_FILE = BASE_DIR / "brn_network.env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def short_pk(pk):
    if not pk: return "?"
    return pk[:12] + "..." + pk[-4:]

def main():
    parser = argparse.ArgumentParser(description="Auditor de Co-Assinatura BRN")
    parser.add_argument("--blocks", type=int, default=15, help="Número de blocos para auditar (default: 15)")
    parser.add_argument("--db", type=str, default="brn_v2_chain.db", help="Caminho do banco de dados")
    args = parser.parse_args()

    print(f"{C.CYAN}{'=' * 80}{C.RESET}")
    print(f"{C.BOLD} 🛡️  AUDITOR DE CO-ASSINATURA (MULTI-SIG) BRN{C.RESET}")
    print(f"{C.CYAN}{'=' * 80}{C.RESET}")

    try:
        from blockchain import Blockchain, verify_block_signature
        from trust_network import TrustNetwork
    except ImportError as e:
        print(f"{C.RED}[X] Falha ao importar módulos: {e}{C.RESET}")
        sys.exit(1)

    try:
        bc = Blockchain(args.db, auto_genesis=False)
        trust = TrustNetwork("trust_network.json")
        bc.set_trust_net(trust)
    except Exception as e:
        print(f"{C.RED}[X] Falha ao abrir blockchain ou trust: {e}{C.RESET}")
        sys.exit(1)

    # 1. Descobre os validadores ativos e o quórum
    validators = bc._get_active_validators()
    required = bc._required_signatures(validators)
    
    print(f"\n{C.BOLD}▶ ESTADO DA REDE{C.RESET}")
    print(f"  Altura atual        : {bc.db.height()}")
    print(f"  Validadores ativos  : {len(validators)}")
    print(f"  Quórum exigido      : {C.YELLOW}{required}{C.RESET}")
    
    if not validators:
        print(f"\n{C.YELLOW}[!] Nenhum validador ativo no Trust Network.{C.RESET}")
        print("    O modo Multi-Sig está desativado (bootstrap).")
        sys.exit(0)

    print(f"\n  Validadores esperados:")
    for v in validators:
        print(f"    - {short_pk(v)}")

    # 2. Itera sobre os blocos recentes
    current_h = bc.db.height()
    if current_h <= 0:
        print(f"\n{C.RED}[X] Blockchain vazia.{C.RESET}")
        sys.exit(0)

    start_h = max(1, current_h - args.blocks + 1)
    end_h = current_h

    print(f"\n{C.BOLD}▶ ANALISANDO BLOCOS #{start_h} a #{end_h}{C.RESET}")
    print(f"{C.GRAY}{'Bloco':<8} {'Assinaturas':<12} {'Válidas':<10} {'Faltando':<10} {'Status':<15}{C.RESET}")
    print(f"{C.GRAY}{'-' * 80}{C.RESET}")

    blocos_ok = 0
    blocos_falha = 0
    total_sigs_validas = 0

    for h in range(start_h, end_h + 1):
        block = bc.db.get_block(h)
        if not block:
            continue

        sigs = block.get("validators_sig", []) or []
        
        # Verifica cada assinatura criptograficamente
        valid_sigs = []
        for entry in sigs:
            if not isinstance(entry, dict): continue
            pk = entry.get("pubkey", "").strip()
            sg = entry.get("signature", "").strip()
            
            if not pk or not sg: continue
            
            # Verifica a assinatura usando a chave pública
            is_valid = verify_block_signature(pk, block, sg)
            
            if is_valid and pk in validators:
                valid_sigs.append(pk)
            elif is_valid and pk not in validators:
                # Assinou, mas não é um validador ativo (estrangeiro)
                pass

        # Deduplica
        valid_sigs = list(set(valid_sigs))
        n_valid = len(valid_sigs)
        total_sigs_validas += n_valid
        
        # Quem faltou?
        faltando = [v for v in validators if v not in valid_sigs]

        # Verifica se atingiu o quórum
        if n_valid >= required:
            status = f"{C.GREEN}✓ CONSENSO OK{C.RESET}"
            blocos_ok += 1
        elif required == 0:
            status = f"{C.GRAY}- BOOTSTRAP{C.RESET}"
        else:
            status = f"{C.RED}✗ QUÓRUM FALHOU{C.RESET}"
            blocos_falha += 1

        # Printa a linha
        h_str = f"#{h:<7}"
        sigs_str = f"{n_valid}/{len(validators)}"
        faltando_str = str(len(faltando)) if faltando else "0"
        
        print(f"{h_str:<8} {sigs_str:<12} {n_valid:<10} {faltando_str:<10} {status:<15}")
        
        # Se falhou, mostra quem faltou
        if n_valid < required and required > 0:
            for f in faltando:
                print(f"         {C.RED}└─ Faltou: {short_pk(f)}{C.RESET}")

    # 3. Veredito Final
    print(f"\n{C.CYAN}{'=' * 80}{C.RESET}")
    print(f"{C.BOLD}▶ VEREDITO FINAL{C.RESET}")
    
    taxa_sucesso = (blocos_ok / (blocos_ok + blocos_falha)) * 100 if (blocos_ok + blocos_falha) > 0 else 0
    
    print(f"  Blocos analisados : {blocos_ok + blocos_falha}")
    print(f"  Blocos com quórum : {C.GREEN}{blocos_ok}{C.RESET}")
    print(f"  Blocos sem quórum : {C.RED}{blocos_falha}{C.RESET}")
    print(f"  Taxa de sucesso   : {C.BOLD}{taxa_sucesso:.1f}%{C.RESET}")
    print(f"  Total de assinaturas válidas: {total_sigs_validas}")
    
    print(f"\n{C.BOLD}  -> {C.GREEN}REDE SEGURA (MULTI-SIG ATIVO){C.RESET}" if blocos_falha == 0 and required > 0 else f"\n{C.BOLD}  -> {C.RED}ATENÇÃO (QUÓRUM NÃO ATINGIDO){C.RESET}")
    
    print(f"{C.CYAN}{'=' * 80}{C.RESET}")

    try:
        bc.db.close()
    except Exception:
        pass

if __name__ == "__main__":
    main()
