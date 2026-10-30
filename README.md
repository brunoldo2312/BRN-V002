🚀 BRN (BrunoCoin) — Blockchain P2P
Uma blockchain Layer-1 completa e autônoma, desenvolvida em Python do zero.
📋 Sumário

• Sobre o Projeto

• Funcionalidades Principais

• Arquitetura e Portas

• Instalação no Windows

• Instalação no Linux

• Configuração da Rede

• Como Usar

• Sistema de Mineração

• Ponte com Bitcoin

• Resolução de Problemas

• Segurança
🌟 Sobre o Projeto

O BRN (BrunoCoin) é uma blockchain descentralizada projetada e implementada do zero, com foco em acessibilidade, transparência e participação comunitária. Desenvolvido em Python, combina simplicidade com um protocolo P2P robusto e criptografia moderna.
✅ Funcionalidades Principais

• ⛏️ Mineração Local e em Pool — mine sozinho ou em rede

• 🔗 Sincronização P2P — descoberta automática de peers e reorg de cadeia

• 📊 Fila de Bootstrap — incentivo compartilhado durante o crescimento da rede

• 🌉 Ponte L2 com Bitcoin — converta BTC para BRN de forma confiável

• 💰 Carteira Gráfica — interface desktop e web para transações

• 🔍 Explorer e Painel de Controle — monitore a rede em tempo real

• 🔐 Criptografia Segura — Ed25519 + ChaCha20
🏗️ Arquitetura e Portas
Porta Serviço Descrição 
5000 API HTTP Saldo, transações, status, mineração 
8080 Explorer/Painel Interface web e monitor de rede 
6001 P2P TCP Sincronização de blocos entre nós 
50007 UDP Multicast Descoberta automática na rede local 

🪟 Instalação no Windows

1. Instale o Python 3.8+ → python.org/downloads ⚠️ Marque "Add Python to PATH" durante a instalação

2. Baixe o projeto:
git clone https://github.com/brunoldo2312/BRN-V002.git
cd BRN-V002
3. Instale as dependências:
pip install flask flask-cors cryptography argon2-cffi orjson requests mnemonic pywebview
4. Configure a rede: Copie brn_network.env.exemplo para brn_network.env e ajuste os parâmetros conforme orientação da rede

5. Inicie: Dê dois cliques em Iniciar_Minerador.bat ou execute:
python main.py
🐧 Instalação no Linux (Ubuntu)
# Atualize o sistema
sudo apt update && sudo apt upgrade -y

# Instale o Python e ferramentas
sudo apt install python3 python3-pip python3-venv -y

# Clone o repositório
git clone https://github.com/brunoldo2312/BRN-V002.git
cd BRN-V002

# Instale as dependências
pip3 install flask flask-cors cryptography argon2-cffi orjson requests mnemonic

# Configure a rede
cp brn_network.env.exemplo brn_network.env
nano brn_network.env  # ajuste BRN_IS_ORIGIN=1 para o nó principal

# Inicie
python3 main.py
⚙️ Configuração da Rede

O arquivo brn_network.env define o papel do nó na rede:

🖥️ Nó Principal (Validador/Origem):
BRN_IS_ORIGIN=1
BRN_POOL_ENABLED=1
BRN_WEB_HOST=0.0.0.0
BRN_API_BASE=http://127.0.0.1:5000
💻 Nó Cliente (Minerador):
BRN_IS_ORIGIN=0
BRN_POOL_ENABLED=0
BRN_WEB_HOST=127.0.0.1
BRN_BOOTSTRAP_PEERS=IP_DO_SERVIDOR:6001
BRN_API_BASE=http://IP_DO_SERVIDOR:5000
⚠️ Nunca compartilhe ou publique o arquivo brn_network.env real. Use sempre o arquivo de exemplo para referência.
🚀 Como Usar

Após iniciar o programa:

• Carteira gráfica abre automaticamente — crie sua carteira e guarde a frase de recuperação em local seguro

• Painel de Controle: acesse http://127.0.0.1:8080/panel para ver o estado da rede

• Monitor de Rede: acesse http://127.0.0.1:8080/monitor para verificar sincronização e peers

• Explorer: consulte blocos e transações em tempo real
⛏️ Sistema de Mineração

Fase 1 — Bootstrap (0 a 19 mineradores)

• O validador mina os blocos

• 50% da recompensa é dividida entre todos os mineradores na fila

• Para participar: abra a carteira → aba "Minerar Pool"

Fase 2 — Rede Descentralizada (20+ mineradores)

• Quando o 20º minerador entra, a rede torna-se independente

• Recompensa de 100% (50 BRN) para quem encontrar o bloco

• Consenso distribuído entre os nós ativos
🌉 Ponte Layer-2: Bitcoin → BRN

Envie Bitcoin para o endereço do cofre da rede. O sistema monitora automaticamente a transação e, após confirmações, credita o valor equivalente em BRN na sua carteira. Tudo registrado na blockchain do Bitcoin e na rede BRN.
🔧 Resolução de Problemas
Problema Solução 
Sync: 0 peer(s) Verifique se o BRN_BOOTSTRAP_PEERS aponta para o IP e porta corretos do nó principal 
Pool indisponível Confirme que o servidor principal está online e acessível pela rede 
Janela não abre Execute python main.py --headless e use a interface web em http://127.0.0.1:8080 
Erro de conexão P2P É normal em redes instáveis — o sistema tenta reconectar automaticamente 
Dependências falham Atualize o pip: python -m pip install --upgrade pip e tente novamente 

🔒 Segurança

• Chaves privadas: são armazenadas criptografadas localmente — nunca são enviadas pela rede

• Comunicação P2P: cifrada ponta a ponta com ChaCha20-Poly1305

• Autenticação: cada nó possui identidade única baseada em chave pública Ed25519

• Checkpoints: proteção contra reorganizações maliciosas da cadeia
💡 Dica de segurança: Faça backup da sua carteira e da frase de recuperação. Sempre mantenha seu software atualizado. Nunca compartilhe sua chave privada ou arquivo de identidade do nó.
🤝 Contribuindo

Este é um projeto em desenvolvimento! Sinta-se bem-vindo para:

• ⭐ Dar uma estrela no repositório

• 🐛 Reportar bugs e sugerir melhorias em Issues

• 💬 Participar das discussões e testar a rede

• 🔌 Conectar seu nó e minerar para fortalecer a rede
📄 Licença

Este projeto é desenvolvido para fins educacionais e comunitários. Consulte o arquivo LICENSE para detalhes.
Desenvolvido por Bruno Labanca de Oliveira • GitHub • Projeto BRN — BrunoCoin
⚠️ Aviso: Esta é uma rede em fase de crescimento. Valores e funcionalidades estão em constante evolução. Participe, teste e contribua para construir uma rede mais forte e descentralizada! 💪
Ficou bom? Quer que eu ajuste algo ou adicione mais alguma seção? 😊