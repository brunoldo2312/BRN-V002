"""
analyze_audit.py — Calculadora de Resultados do Teste A/B (BRN)
================================================================
Lê o audit.log e gera o relatório completo para o Sheet 06 (Readout Gates).
Não requer dependências externas (usa apenas a biblioteca padrão do Python).

Como rodar:
    python analyze_audit.py
    python analyze_audit.py --log audit_teste.log
"""
from __future__ import annotations

import json
import math
import argparse
from collections import defaultdict
from pathlib import Path

DEFAULT_LOG = "audit.log"

def safe_div(num, den):
    return (num / den) if den > 0 else 0.0

def analyze(log_file: str):
    path = Path(log_file)
    if not path.exists():
        print(f"[X] Arquivo '{log_file}' não encontrado.")
        return

    # Dicionários para acumular os dados
    node_to_variant = {}         # node_id_hash -> "A" ou "B"
    exposures = {"A": 0, "B": 0}
    unique_nodes = {"A": set(), "B": set()}
    
    # Eventos de resultado por variante
    events = {
        "A": defaultdict(int),
        "B": defaultdict(int)
    }
    
    # Controle diário para calcular o 'r' (novas carteiras/dia)
    daily_nodes = defaultdict(set)
    unattributed_events = 0
    total_lines = 0

    print(f"[*] Lendo {log_file}...")

    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # ---------------------------------------------------------
    # PASSO 1: Mapear node_id_hash -> Variante (apenas quem viu a tela)
    # ---------------------------------------------------------
    for line in lines:
        line = line.strip()
        if not line: continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        total_lines += 1

        ev = data.get("event")
        node = data.get("node_id_hash", "unknown")
        ts = data.get("ts", "")[:10] # YYYY-MM-DD

        if ev == "exposure_log":
            variant = data.get("variant")
            if variant in ("A", "B") and node != "unknown":
                if node not in node_to_variant:
                    node_to_variant[node] = variant
                    exposures[variant] += 1
                    unique_nodes[variant].add(node)
                daily_nodes[ts].add(node)

    # ---------------------------------------------------------
    # PASSO 2: Atribuir os resultados (outcomes) às variantes
    # ---------------------------------------------------------
    for line in lines:
        line = line.strip()
        if not line: continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        ev = data.get("event")
        node = data.get("node_id_hash", "unknown")

        # Ignora eventos de sistema que não têm a ver com o usuário
        if ev in ("node_start", "node_stop", "rate_limit_hit", "exposure_log"):
            if ev == "rate_limit_hit":
                # Rate limit não tem node_id necessariamente, mas se tiver, conta
                if node in node_to_variant:
                    events[node_to_variant[node]][ev] += 1
            continue

        # Se o evento foi gerado por um node que vimos na exposição, conta
        if node in node_to_variant:
            variant = node_to_variant[node]
            events[variant][ev] += 1
        elif ev in ("miner_start_ok", "miner_start_failed", "transfer_ok", "transfer_rejected"):
            # Evento importante, mas sem exposição registrada (ex: rodou antes do teste)
            unattributed_events += 1

    # ---------------------------------------------------------
    # PASSO 3: Calcular Métricas e Imprimir Relatório
    # ---------------------------------------------------------
    print("\n" + "="*70)
    print(" 📊 RELATÓRIO DE READOUT — TESTE A/B (ATIVAÇÃO DE MINERAÇÃO)")
    print("="*70)

    exp_a = exposures["A"]
    exp_b = exposures["B"]
    total_exp = exp_a + exp_b

    if total_exp == 0:
        print("\n[!] Nenhuma exposição (exposure_log) encontrada no log.")
        print("    O teste ainda não começou ou a carteira não registrou o evento.")
        return

    # ---- Gate 1: Validade dos Dados (SRM) ----
    print("\n[Gate 1] Validade dos Dados (Sample Ratio Mismatch)")
    pct_a = (exp_a / total_exp) * 100
    pct_b = (exp_b / total_exp) * 100
    print(f"  Exposições Totais : {total_exp}")
    print(f"  Controle (A)      : {exp_a} ({pct_a:.1f}%)")
    print(f"  Tratamento (B)    : {exp_b} ({pct_b:.1f}%)")
    
    # SRM check: se um lado for < 45% ou > 55%, há alerta.
    srm_ok = True
    if abs(pct_a - 50) > 5:
        print("  ⚠️  [ALERTA SRM] A proporção está fora de 50/50. Revise o hash do ab_test.py.")
        srm_ok = False
    else:
        print("  ✅ [OK] Distribuição balanceada.")
        
    if unattributed_events > 0:
        print(f"  ⚠️  [ALERTA] {unattributed_events} eventos importantes (miner_start_ok, etc) ocorreram sem exposição registrada.")

    # ---- Gate 2: Evidência Estatística (Ativação) ----
    print("\n[Gate 2] Evidência Estatística (Métrica Primária: Ativação)")
    act_a = events["A"]["miner_start_ok"]
    act_b = events["B"]["miner_start_ok"]
    
    rate_a = safe_div(act_a, exp_a)
    rate_b = safe_div(act_b, exp_b)
    lift = rate_b - rate_a
    
    print(f"  Ativação A (Controle)   : {act_a}/{exp_a} = {rate_a*100:.2f}%")
    print(f"  Ativação B (Tratamento) : {act_b}/{exp_b} = {rate_b*100:.2f}%")
    print(f"  Lift Absoluto           : {lift*100:+.2f} pontos percentuais")
    print(f"  Lift Relativo           : {(safe_div(rate_b, rate_a) - 1)*100:+.2f}%")

    # Cálculo do Z-score (Teste de Hipótese para Duas Proporções)
    if exp_a > 0 and exp_b > 0:
        # Proporção agrupada (pooled proportion)
        p_pool = (act_a + act_b) / (exp_a + exp_b)
        se = math.sqrt(p_pool * (1 - p_pool) * ((1/exp_a) + (1/exp_b)))
        
        if se > 0:
            z_score = lift / se
            print(f"  Z-score                 : {z_score:+.3f}")
            if abs(z_score) >= 1.96:
                print("  ✅ [SIGNIFICANTE] A diferença é estatisticamente significativa (p < 0.05).")
            else:
                print("  ⏳ [NÃO SIGNIFICANTE] Ainda não há confiança de 95%. Continue coletando dados.")
        else:
            print("  (Erro padrão zero, impossível calcular Z-score)")

    # ---- Gate 3: Guarda-Rails ----
    print("\n[Gate 3] Guarda-Rails (Segurança)")
    
    # Calcula taxas de falha por variante
    def print_guardrail(name, ev_key):
        a_count = events["A"][ev_key]
        b_count = events["B"][ev_key]
        a_rate = safe_div(a_count, exp_a)
        b_rate = safe_div(b_count, exp_b)
        
        diff = b_rate - a_rate
        symbol = "🔴" if diff > 0.05 else "🟢" # Vermelho se B for 5pp pior
        
        print(f"  {symbol} {name:<22} | A: {a_count:>3} ({a_rate*100:>5.1f}%) | B: {b_count:>3} ({b_rate*100:>5.1f}%)")

    print_guardrail("Falha ao Minerar", "miner_start_failed")
    print_guardrail("Transferência Rejeitada", "transfer_rejected")
    print_guardrail("Rate Limit Atingido", "rate_limit_hit")
    
    # ---- Métricas Secundárias e Aceleração ----
    print("\n[Métricas Secundárias]")
    print_guardrail("Transferência com Sucesso", "transfer_ok")
    
    print("\n[Taxa de Novas Carteiras (r)]")
    dias = len(daily_nodes)
    total_unicos = len(set.union(*daily_nodes.values())) if daily_nodes else 0
    print(f"  Dias com Tráfego : {dias}")
    print(f"  Carteiras Únicas : {total_unicos}")
    if dias > 0:
        r = total_unicos / dias
        print(f"  Aceleração (r)   : {r:.1f} carteiras/dia")
        if r < 30:
            print("  ⚠️  (Volume baixo para A/B clássico. Considere adotar o CTA para todos se o Lift for positivo).")

    # ---- Veredito Final ----
    print("\n" + "="*70)
    print(" 📝 VEREDITO (Sugestão baseada nos 4 Gates)")
    print("="*70)
    
    z = locals().get('z_score', 0)
    
    if total_exp < 50:
        print(f"  ⏳ HOLD: Amostra muito pequena ({total_exp} exposições).")
        print("     Continue coletando dados até ter pelo menos 50 usuários por variante.")
    elif not srm_ok:
        print("  ❌ RERUN: O split A/B está desbalanceado. Corrija o hash antes de continuar.")
    elif abs(z) >= 1.96 and lift > 0:
        print("  ✅ SHIP: A Variante B (Banner) aumentou a ativação de forma significativa.")
        print("     Recomendação: Desligar o A/B test e deixar o Banner ligado para 100% dos usuários.")
    elif abs(z) >= 1.96 and lift < 0:
        print("  ❌ KILL: A Variante B DIMINUIU a ativação. Reverta para a tela original.")
    else:
        print("  ⏳ HOLD: Nenhuma diferença estatisticamente significativa observada até agora.")
        print("     Recomendação: Manter o teste rodando.")
    
    print("="*70)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analisa o audit.log do Teste A/B")
    parser.add_argument("--log", default=DEFAULT_LOG, help="Caminho do arquivo de log")
    args = parser.parse_args()
    
    analyze(args.log)
