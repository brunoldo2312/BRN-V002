import os
from dotenv import load_dotenv
from infisical_sdk import InfisicalSDKClient
from bitcoinlib.wallets import Wallet

load_dotenv()

REDE = 'testnet'
NOME_CARTEIRA = "carteira_segura"
ENDERECO_DESTINO = "tb1qSEU_DESTINO_AQUI..." # PREENCHA AQUI
VALOR_SATOSHIS = 50000
FEE_SATOSHIS = 1000

# Conecta no Infisical
client = InfisicalSDKClient(
    host="https://app.infisical.com",
    token=os.getenv("INFISICAL_TOKEN")
)

# Busca os segredos criptografados
def get_secret(nome):
    secret = client.getSecret(
        secretName=nome,
        projectId=os.getenv("INFISICAL_PROJECT_ID"),
        environment=os.getenv("INFISICAL_ENV", "dev"),
        secretPath="/"
    )
    return secret.secretValue

wif = get_secret("BTC_WIF")
seed = get_secret("BTC_SEED")
chave = wif if wif else seed

if not chave:
    raise Exception("BTC_WIF / BTC_SEED não encontrado no Infisical")

print("✓ Chave carregada do Infisical com sucesso")

try:
    w = Wallet(NOME_CARTEIRA, network=REDE)
except:
    w = Wallet.create(NOME_CARTEIRA, keys=chave, network=REDE)

w.scan()
print(f"Endereço: {w.get_key().address} | Saldo: {w.balance()} sats")

if w.balance() >= VALOR_SATOSHIS + FEE_SATOSHIS:
    tx = w.send_to(ENDERECO_DESTINO, VALOR_SATOSHIS, fee=FEE_SATOSHIS)
    print(f"ENVIADO! TXID: {tx.txid}")
else:
    print(f"Sem saldo. Envie testnet para: {w.get_key().address}")