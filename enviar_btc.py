from bitcoinlib.wallets import Wallet, WalletError
from bitcoinlib.services.services import Service

# ============ PREENCHA AQUI ============
# MUDADO PARA MAINNET
REDE = 'bitcoin'  

NOME_CARTEIRA = "minha_carteira_mainnet"

# ⚠️ ATENÇÃO: Endereço de destino deve ser MAINNET (começa com bc1, 1 ou 3)
ENDERECO_DESTINO = "bc1qSEU_DESTINO_AQUI..."  

# Valor a enviar (1 BTC = 100.000.000 sats)
# Exemplo: 10.000 sats = 0.0001 BTC
VALOR_SATOSHIS = 10000  

# Taxa de rede para mineradores (Verifique mempool.space antes!)
# 1500 sats é apenas um exemplo, pode ser pouco se a rede estiver cheia.
FEE_SATOSHIS = 1500 
# =======================================

# Configura o provedor da rede para a Mainnet (Blockstream é o mais estável)
Service(network=REDE, provider='blockstream')

def main():
    print("=" * 60)
    print(" ⚠️  ATENÇÃO: VOCÊ ESTÁ USANDO A REDE MAINNET (BITCOIN REAL) ⚠️")
    print("=" * 60)
    
    if ENDERECO_DESTINO == "bc1qSEU_DESTINO_AQUI...":
        print("\n[ERRO FATAL] Você esqueceu de preencher o ENDERECO_DESTINO no código.")
        return

    print(f"Endereço de destino: {ENDERECO_DESTINO}")
    print(f"Valor a enviar     : {VALOR_SATOSHIS} sats ({VALOR_SATOSHIS / 100000000:.8f} BTC)")
    print(f"Taxa (Fee)         : {FEE_SATOSHIS} sats\n")

    try:
        # Tenta abrir a carteira, se não existir, cria uma nova
        try:
            w = Wallet(NOME_CARTEIRA, network=REDE)
            print(f"Carteira '{NOME_CARTEIRA}' aberta com sucesso.")
        except WalletError:
            w = Wallet.create(NOME_CARTEIRA, network=REDE)
            print(f"⚠️ Carteira NOVA criada!")
            print(f"⚠️ A seed foi salva no banco local (SQLite). NÃO imprima no terminal.")

        # Sincroniza com a rede (pode demorar um pouco na primeira vez)
        print("\nSincronizando carteira com a rede Bitcoin (isso pode demorar alguns minutos)...")
        w.utxos_update()
        
        endereco_local = w.get_key().address
        saldo = w.balance()
        
        print(f"\nSeu endereço de envio: {endereco_local}")
        print(f"Saldo disponível     : {saldo} sats ({saldo / 100000000:.8f} BTC)")

        total_necessario = VALOR_SATOSHIS + FEE_SATOSHIS

        if saldo < total_necessario:
            print(f"\n[ERRO] Saldo insuficiente.")
            print(f"Você precisa de {total_necessario} sats, mas tem {saldo} sats.")
            print(f"Envie BTC para este endereço: {endereco_local}")
            return

        # Confirmação de segurança para evitar cliques acidentais
        print("\n" + "=" * 60)
        print(" RESUMO DA TRANSAÇÃO")
        print("=" * 60)
        print(f"De    : {endereco_local}")
        print(f"Para  : {ENDERECO_DESTINO}")
        print(f"Valor : {VALOR_SATOSHIS} sats")
        print(f"Taxa  : {FEE_SATOSHIS} sats")
        
        conf = input("\nDeseja realmente enviar? (Digite 'SIM' para confirmar): ")
        if conf.strip().upper() != "SIM":
            print("\n[!] Transação cancelada pelo usuário. Nenhum fundo foi movido.")
            return

        print("\nAssinando e transmitindo transação para a rede Bitcoin...")
        # O send_to já cria, assina e transmite
        tx = w.send_to(ENDERECO_DESTINO, VALOR_SATOSHIS, fee=FEE_SATOSHIS, network=REDE)
        
        print("\n" + "=" * 60)
        print(" ✅ ENVIADO COM SUCESSO!")
        print("=" * 60)
        print(f"TXID: {tx.txid}")
        print(f"Veja no explorer: https://mempool.space/tx/{tx.txid}")
        print("\nAguarde a confirmação dos blocos (geralmente de 10 a 60 minutos).")

    except WalletError as e:
        print(f"\n[ERRO] Falha na carteira: {e}")
    except Exception as e:
        print(f"\n[ERRO] Erro inesperado: {e}")

if __name__ == "__main__":
    main()
