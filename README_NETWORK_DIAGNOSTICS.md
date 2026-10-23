# Diagnóstico de rede BRN — notas de operação

Este documento acompanha as correções de `check_network.py`.

## Alterações
- A porta HTTP dos peers é configurável por `BRN_PEER_API_PORT` (padrão 5000).
- Timeout configurável por `BRN_HTTP_TIMEOUT`.
- A comparação consulta `/api/work` remotamente quando o endpoint está disponível e mostra a diferença de trabalho acumulado.
- `--json` silencia o texto de diagnóstico para manter o stdout como JSON.
- Um possível fork não recomenda apagar a base de dados. Faça backup e valide a cadeia antes de qualquer ação.

## Teste
```bash
python -m pip install requests
python -m py_compile check_network.py
python check_network.py
python check_network.py --json
```

## Limitações
O script é somente leitura e não executa sincronização nem valida todos os blocos criptograficamente. A comparação remota depende de os peers exporem `/api/chain-info` e `/api/work` na porta HTTP configurada. O fingerprint calculado localmente não prova que os peers compartilham o mesmo segredo; essa comparação ainda precisa ser feita por mecanismo autenticado.

Não apague nem recrie `brn_v2_chain.db` para corrigir um alerta. Pare a mineração, faça uma cópia de segurança e investigue com os logs e a validação integral da cadeia.

# BRN — configuração automática de rede

## O que faz

Execute \`python auto_config_network.py\` em cada computador:
- **Opção 1**: configura este computador como endereço de referência do Tracker/peer.
- **Opção 2**: grava o endereço do servidor informado e testa HTTP \`/health\` e TCP P2P.
- Atualiza \`brn_network.env\`, \`bootstrap_peers.json\` e \`brn_tailscale_peer.txt\`.
- Mantém as demais variáveis, incluindo segredos locais; não imprime nem substitui segredos.
- Valida IPs e nunca usa \`127.0.0.1\` como endereço remoto automaticamente.
- Grava os arquivos com substituição atômica para reduzir o risco de configuração parcialmente escrita.

## Portas padrão

- Tracker HTTP: \`9000\` (configurável por \`BRN_TRACKER_PORT\`).
- P2P: \`6001\` (configurável por \`BRN_P2P_PORT\`).
- API HTTP local para diagnóstico: \`5000\` (configurável por \`BRN_PEER_API_PORT\`).

As portas precisam corresponder à configuração e aos serviços realmente iniciados. O configurador **não** inicia serviços, não altera firewall/roteador e não garante sincronização. A mensagem de teste bem-sucedido confirma apenas conectividade HTTP/TCP básica, não a validade da cadeia ou consenso.

## Como usar

1. Instale e conecte o Tailscale em todos os computadores, ou use IPs LAN alcançáveis na mesma rede.
2. No computador que hospedará o Tracker, execute o script e escolha a opção 1.
3. Inicie o Tracker e o nó BRN; permita as portas necessárias no firewall.
4. Em cada computador cliente, execute o script, escolha a opção 2 e informe o IP do servidor sem esquema HTTP e sem porta.
5. Confira as mensagens HTTP e TCP. Se falharem, não considere o cliente conectado: confira serviços, endereços, portas e firewall.
6. Execute \`python check_network.py\` para diagnóstico adicional.

## Segurança e operação

- Não coloque segredos reais em \`brn_network.env.example\` nem no Git.
- \`brn_network.env\` é uma configuração local e deve permanecer ignorada pelo Git.
- Não apague nem recrie a base de dados da blockchain para corrigir problemas de rede.
