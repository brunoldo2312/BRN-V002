from bitcoinlib.wallets import Wallet, WalletError

# ============ PREENCHA AQUI ============
# Crie uma carteira nova ou importe sua seed existente
# Pra teste, use testnet!

REDE = 'testnet'  # mude para 'bitcoin' quando for pra mainnet

# Se já tem seed, coloque aqui (12 palavras). Se não, deixe vazio que ele cria uma
SEED_EXISTENTE = ""  # ex: "abandon abandon abandon ..."

NOME_CARTEIRA = "minha_carteira_teste"

ENDERECO_DESTINO = "tb1qSEU_DESTINO_AQUI..."  # <--- COLOQUE O ENDEREÇO DE DESTINO
VALOR_SATOSHIS = 50000  # quanto enviar: 50000 sats = 0.0005 BTC
FEE_SATOSHIS = 1000  # taxa para minerador
# =======================================

try:
    # Cria ou abre a carteira
    if SEED_EXISTENTE:
        w = Wallet.create(NOME_CARTEIRA, keys=SEED_EXISTENTE, network=REDE)
    else:
        w = Wallet.create(NOME_CARTEIRA, network=REDE)
        print(f"Carteira nova criada! ANOTE ESSA SEED: {w.main_key}")

    w.scan() # busca UTXOs na blockchain
    print(f"Seu endereço de recebimento: {w.get_key().address}")
    print(f"Saldo: {w.balance()} sats")

    if w.balance() < VALOR_SATOSHIS + FEE_SATOSHIS:
        print("ERRO: Saldo insuficiente. Mande BTC de testnet pra esse endereço primeiro.")
        print("Faucet testnet: https://coinfaucet.eu/en/btc-testnet/")
    else:
        # Monta + Assina + Transmite
        print(f"Enviando {VALOR_SATOSHIS} para {ENDERECO_DESTINO}...")
        tx = w.send_to(ENDERECO_DESTINO, VALOR_SATOSHIS, fee=FEE_SATOSHIS, network=REDE)
        print(f"ENVIADO! TXID: {tx.txid}")
        print(f"Veja em: https://mempool.space/testnet/tx/{tx.txid}")

except WalletError as e:
    print(f"Erro: {e}")