https://diagnostic-work-596.notion.site/BRN-BrunoCoin-Documenta-o-do-Projeto-3fef4e870c4a81f3acfbe4d4f77e0dda
🔷 BRN (BrunoCoin) — Manual Oficial do Projeto
O BRN é uma blockchain P2P completa e autónoma, escrita em Python. Ela suporta mineração local, mineração em pool, sincronização de rede (com reorg automático), ponte L2 com Bitcoin e um sistema inovador de Fila de Bootstrap (recompensa partilhada).

📌 1. Arquitetura de Portas
O sistema funciona com múltiplos processos e comunica através das seguintes portas:

Porta	Serviço	Descrição
5000	API HTTP (server.py)	Endpoints de transferência, saldo, status e mineração.
8080	Explorer/Painel (explorer.py)	Serve a UI web, painel de controlo e proxies.
6001	P2P TCP (p2p_unified.py)	Protocolo binário seguro (Ed25519 + ChaCha20) para sincronizar blocos.
50007	UDP Multicast	Descoberta automática de peers na mesma rede (LAN).
⚙️ 2. Configuração (brn_network.env)
O ficheiro .env é a alma do nó. Ele define se a máquina é a Origem (Validador) ou um Cliente (Minerador).

Máquina A (Ubuntu / Origem / Validador)
env
BRN_IS_ORIGIN=1
BRN_POOL_ENABLED=1       # O Ubuntu é o servidor de Pool
BRN_WEB_HOST=0.0.0.0     # Permite que o Windows se ligue via Tailscale
BRN_API_BASE=http://127.0.0.1:5000
Máquina B (Windows / Cliente / Minerador)
env
BRN_IS_ORIGIN=0
BRN_POOL_ENABLED=0
BRN_WEB_HOST=127.0.0.1
# ⚠️ COLOQUE O IP DO UBUNTU AQUI (Ex: 100.111.253.8)
BRN_BOOTSTRAP_PEERS=100.111.253.8:6001
BRN_API_BASE=http://100.111.253.8:5000
🚀 3. Como Iniciar o Nó
Passo 1: Instalar Dependências
Em ambas as máquinas, instale os requisitos:

bash
pip install flask flask-cors cryptography argon2-cffi orjson requests mnemonic pywebview
Passo 2: Iniciar a Máquina Ubuntu (Origem)
Abra o terminal no Ubuntu.

Inicie o nó:

bash
python3 main.py
O terminal vai mostrar Modo: ORIGEM e o nó estará à espera de conexões.

Abra o painel para ver o estado: http://127.0.0.1:8080/panel

Passo 3: Iniciar a Máquina Windows (Cliente)
Abra o Prompt de Comando (cmd) no Windows.

Navegue até à pasta do projeto:

cmd
cd C:\Users\seu_usuario\Music\brn-atualiza-main
Inicie o nó:

cmd
python main.py
A interface gráfica da carteira abrirá automaticamente.

No terminal, verifique se apareceu Sync: 1 peer(s). Se sim, a ligação via Tailscale está perfeita.

⛏️ 4. Mineração e Fase de Bootstrap
O BRN possui uma mecânica única de Bootstrap. O objetivo é incentivar a entrada de novos mineradores até a rede estar segura.

Fase 1: Bootstrap (0 a 19 Mineradores na Fila)
O Validador (Ubuntu) minera os blocos.

Recompensa: 50% vai para o Validador e os outros 50% são divididos pelos mineradores na Fila.

Como participar: Na carteira do Windows, vá à aba Enviar e clique em 🌐 Minerar Pool.

Fase 2: Rede Descentralizada (20 Mineradores)
Assim que o 20º minerador entra na fila, o terminal do Ubuntu imprime:
[BOOTSTRAP] 20 MINERADORES ATINGIDOS! REDE DESCENTRALIZADA.

Recompensa: A fila para de crescer. A recompensa volta a ser 100% (50 BRN) para quem encontrar o bloco.

💡 Dica de Teste: Para simular 20 mineradores e ver a transição, corra este script no Ubuntu:

python
import urllib.request, json, time
url = 'http://127.0.0.1:5000/api/bootstrap/add'
for i in range(1, 20):
    data = {'address': f'brn1test{i:02d}', 'pubkey': f'00fake{i:02d}'}
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(req)
    time.sleep(0.1)
🖥️ 5. Interfaces do Sistema
O projeto possui três interfaces web principais (servidas na porta 8080) e uma aplicação desktop.

Carteira Desktop (index_wallet.html)

Abre via pywebview.

Permite gerar carteiras, enviar BRN, ver histórico e iniciar a mineração (Local ou Pool).

Local: O nó mina sozinho.

Pool: O nó regista-se no Validador e recebe shares.

Painel de Controle (http://127.0.0.1:8080/panel)

Mostra a altura, peers, mempool e o estado da mineração.

Fila Bootstrap: Mostra em tempo real quantos mineradores estão na fila (X / 20).

Monitor de Rede (http://127.0.0.1:8080/monitor)

Verifica se os nós estão sincronizados.

Deteta forks (se dois nós mineraram blocos diferentes na mesma altura).

🌉 6. Ponte L2 (BTC → BRN)
O sistema inclui uma bridge nativa (Layer 2) para converter Bitcoin em BRN.

O utilizador envia BTC para o endereço do cofre (definido em btc_config.py).

O btc_watcher.py monitoriza a blockchain do Bitcoin.

Assim que o BTC atinge as confirmações necessárias, o sistema cria uma transação na mempool do BRN e credita a carteira do utilizador.

🛠️ 7. Resolução de Problemas (Troubleshooting)
Problema	Causa Provável	Solução
Pool: modulo mining_pool indisponivel	O ficheiro não está na pasta ou está como .txt.	Certifique-se que o mining_pool.py está na mesma pasta do main.py. Use o Bloco de Notas com "Salvar Como" e "Todos os arquivos (.)".
Sync: 0 peer(s)	O Windows não consegue falar com o Ubuntu.	Verifique se o IP no .env do Windows é o IP exato do tailscale ip -4 do Ubuntu. Faça ping <IP>.
Erro -5 while decompressing data	Um nó fechou a conexão abruptamente.	É um falso alarme da rede P2P. Pode ignorar ou silenciar mudando o log para DEBUG no network_guard.py.
Janela da carteira não abre	pywebview falhou.	O nó tentou abrir a GUI mas falta alguma biblioteca. Inicie com python main.py --headless e use a interface web no browser.
Tailscale falhou (código 1)	O script brn_tailscale_setup.py não encontrou o executável.	Se já fez login manual no Tailscale, ignore este aviso.
📂 8. Estrutura de Ficheiros Essenciais
Se precisar de recriar o projeto, estes são os ficheiros que não podem faltar:

Cérebro e Dados:
main.py, blockchain.py, db.py, crypto.py, wallet.py, bech32.py, version.py.

Rede e Consenso:
p2p_unified.py, p2p_auth.py, p2p_secure.py, discovery_v2.py, sync_manager.py, reorg.py, chain_validator.py.

Mineração:
miner_loop.py, mining_pool.py, miner_pool_client.py.

Servidores e Interfaces:
server.py, explorer.py, app_wallet_v3.py, index_wallet.html, panel.html, monitor.html.

Segurança e Config:
secure_store.py, checkpoints.py, brn_network.env, brn_config.py, security.py.

Este manual cobre o ciclo de vida completo do projeto. Com ele, pode inicializar a rede, conectar múltiplos PCs via Tailscale, monitorizar o progresso da fila de mineração e até fazer a ponte com o Bitcoin.
