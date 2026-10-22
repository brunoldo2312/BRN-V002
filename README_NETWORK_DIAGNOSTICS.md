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
