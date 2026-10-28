🚀 BRN (BrunoCoin): A Nova Era da Mineração Descentralizada e Segura
 
É com enorme satisfação que anunciamos a maior atualização da rede BRN até hoje! Depois de dias de testes intensivos, correções profundas no consenso e reestruturação do ecossistema, a BRN acaba de entrar em sua fase de maturidade operacional.
 
A rede evoluiu de um único servidor para uma malha descentralizada com múltiplos validadores, capaz de sincronizar e minerar de forma coordenada, segura e à prova de falhas.
 
🛡️ O que mudou nesta nova evolução?
 
1. Segurança de Nível Institucional (Multi-Sig)
 
O consenso da rede não aceita mais blocos "vazios". A blockchain agora exige a assinatura criptográfica (Ed25519) dos validadores da rede (Top Validators). Se um bloco for forjado ou não tiver o quórum de assinaturas, ele é sumariamente rejeitado. Isso torna a rede imune a ataques de falsificação.
 
2. Sincronização à Prova de Falhas (HTTP Sync v2.1)
 
Abandonamos o sincronizador antigo baseado em P2P/Multicast (que falhava atrás de firewalls e do Tailscale). O novo sync_manager.py baixa a blockchain via HTTP (Port 5000). Agora, qualquer computador no mundo, conectado via VPN ou Tailscale, consegue baixar os mais de 5.000 blocos da rede em segundos, sem travar.
 
3. Pool de Mineração Otimizado (Vardiff)
 
O servidor de Pool (rodando no Ubuntu) foi reescrito para coordenar múltiplos mineradores (Windows, Linux, Mac) sem gargalos no banco de dados. Implementamos Vardiff (dificuldade variável), onde mineradores mais lentos recebem desafios mais fáceis, garantindo que todos recebam sua recompensa de forma justa.
 
4. Automação e Facilidade (Zero Config)
 
Esqueça comandos complexos no terminal. A rede agora conta com scripts de inicialização automatizados que preparam o ambiente, instalam as dependências, configuram o Tailscale e conectam seu nó ao Pool instantaneamente. 
 
💻 Como iniciar o seu Cliente BRN e começar a minerar agora
 
A rede está aberta. Se você deseja participar, minerar e garantir sua parte das recompensas, o processo foi reduzido a um clique.
 
O que você precisa:
• Ter o Python instalado.
• Estar conectado à rede (via Tailscale ou LAN).
• Ter a pasta do projeto BRN. 
O Passo a Passo para o Futuro:
1. Baixe e extraia o código-fonte atualizado da BRN.
2. Localize o arquivo iniciar_cliente.bat na pasta principal.
3. Dê um duplo clique no arquivo iniciar_cliente.bat.
4. O script fará tudo sozinho: baixará as dependências, verificará a sincronização com o servidor Origem, configurará o pool e abrirá a sua Carteira Gráfica.
5. Assim que a carteira abrir, clique no botão azul "Minerar Pool". 
Não digite comandos. Não configure arquivos. Apenas clique em iniciar_cliente.bat e faça parte da nova economia.
 
A BRN está pronta para escalar. O código está validado. A rede está no ar. Junte-se a nós! 



Para instalar o BRN no seu computador com Windows, comece baixando e instalando o Python pelo site oficial em python.org, e durante a instalação não se esqueça de marcar a opção "Adicionar Python ao PATH". Depois, baixe o projeto BRN-V002 do GitHub, seja clonando com git clone https://github.com/brunoldo2312/BRN-V002.git ou baixando o arquivo ZIP diretamente da página do repositório e extraindo os arquivos. Em seguida, abra o Prompt de Comando dentro da pasta do projeto que você acabou de baixar e execute o comando pip install flask flask-cors cryptography argon2-cffi orjson requests mnemonic pywebview para instalar todas as bibliotecas necessárias. Quando terminar a instalação, basta digitar python main.py ou simplesmente dar dois cliques no arquivo iniciar_cliente.bat e o BRN vai abrir pronto para você usar como minerador e carteira. Se em algum momento aparecer uma mensagem dizendo que o Python não foi encontrado, provavelmente é porque você não marcou a opção de adicionar ao PATH — basta reinstalar o Python marcando essa caixinha que resolve! 😊
[ Interface / pywebview ]
                  │
                  ▼
         ┌────────────────────────┐
         │   app_wallet_v3.py     │  ◄── Ponte principal (Bridge)
         └──────────┬─────────────┘
                    │
      ┌─────────────┼──────────────┬──────────────┐
      ▼             ▼              ▼              ▼
[secure_store]  [ab_test.py]  [security.py]  [Nó HTTP :5000]
(Criptografia   (Atribuição    (Logs de       (Blockchain,
 e Wallet)       A/B 50/50)     Auditoria)     Mempool, Mineração)
https://diagnostic-work-596.notion.site/BRN-BrunoCoin-Documenta-o-do-Projeto-3fef4e870c4a81f3acfbe4d4f77e0dda

https://alguma-coisa-aleatoria.trycloudflare.com

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
Há módulos experimentais para acompanhar depósitos BTC e criar ordens BTC→BRN, mas a ponte **não está operacional para conversão real**: o método `Blockchain.add_l2_transaction()` ainda não existe na blockchain principal e, por isso, não há um caminho de crédito BTC→BRN concluído e validado por consenso. Ao iniciar com `--l2`, o nó deve informar que a integração está indisponível.

O watcher foi endurecido para exigir o valor exato da ordem, falhar fechado quando a verificação anti-replay não está disponível e expirar ordens no instante do vencimento. A fábrica agora recusa criar ordens enquanto o endereço de recebimento for um placeholder.

**Não envie BTC para esta ponte** até a integração on-chain, a proteção contra reutilização de txid, os testes de reorg/confirmacões e a emissão de BRN terem sido implementados e auditados.

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


## 9. VM declarativa de contratos (experimental)

O pacote `contracts/` contém uma máquina virtual declarativa limitada, que não é EVM:

- `contracts/vm.py`: valida e executa métodos declarativos com operações `set`, `add`, `sub`, `emit`, `require_eq`, `require_ge`, `transfer` e `inc_nonce`.
- `contracts/escrow_btc_brn.py` e `contracts/factory.py`: modelos e criação de ordens de escrow BTC→BRN.
- `contracts/__init__.py`: expõe `ContractVM`, `deploy` e `call`.

### Teste local da validação

Na raiz do repositório, execute:

```bash
python -m contracts.vm
```

O comando executa autotestes básicos da validação. Isso **não** substitui testes de integração com a base de dados, API, consenso ou rede.

### Exemplo de contrato

```python
from contracts.vm import ContractVM

code = {
    "version": 1,
    "name": "Counter",
    "initial_state": {"count": 0},
    "methods": {
        "increment": [
            {"op": "add", "path": "count", "value": 1},
            {"op": "emit", "event": "Incremented"},
        ]
    },
}
print(ContractVM.validate(code))
```

### Avisos de segurança

A VM é experimental e não deve ser considerada pronta para fundos reais. As funções `deploy` e `call` são APIs Python; a integração com uma API pública precisa autenticar a identidade do chamador e aplicar autorização. A execução de contratos ainda não foi demonstrada como parte do consenso validado por todos os nós. Transferências, eventos e estado precisam de testes de atomicidade e concorrência antes de produção.

Para a ponte BTC→BRN, configure e verifique o endereço real de recebimento e a fonte de dados do watcher. Não considere uma ordem criada como prova de depósito BTC nem como crédito confirmado de BRN.
