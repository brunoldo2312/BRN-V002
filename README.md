# Verificação: quais arquivos do BRN-V002 ainda precisam ser atualizados

**Base da verificação:** commit `be60392` ("Atualizar o reorg.py") em `github.com/brunoldo2312/BRN-V002`, consultado via clone local atualizado em 27/nov.
**Objetivo:** mapear, além dos 5 arquivos do patch anterior (README, API.md, 2 workflows, requirements.txt), todo arquivo que ainda está desatualizado ou inconsistente.

## Resumo executivo

| Categoria | Itens pendentes | Situação |
|---|---|---|
| A. Versão do Python / runtime (Docker, CI, devcontainer, docs) | 9 | Mistura 3.8+ / 3.11 / 3.12; nenhum alvo único |
| B. Listas de dependências (README, scripts .sh/.bat, requirements) | 5 | `requirements.txt` está completo; listas manuais defasadas |
| C. Metadados e config (version.py, badges, workflow inválido, start.sh) | 7 | Vários factuais (erro de nome/referência) |
| D. `db.py` v9.8.x vs diagnóstico anterior (10 itens) | 4 feitos, 6 pendentes | Ver tabela D |
| E. Segurança/higiene (já conhecidos, sem mudança desde 20/nov) | 7 | Sem regressão, sem correção |

> Patch mecânico anexo (`brn_patch_correcoes_mecanicas.patch`) cobre apenas 2 correções factuais de risco zero (item C1 e C2). Os demais itens dependem de uma decisão (qual versão do Python alvo) ou de mudança de arquitetura (Tabela D/E).

---

## A. Versão do Python e runtime — hoje há 3 versões convivendo

| # | Arquivo:linha | Estado atual | Precisa |
|---|---|---|---|
| A1 | `README.md:77` | "Instale o Python **3.8+**" | Alinhar ao alvo único (README é a porta de entrada) |
| A2 | `API.md:7` | Badge `python-3.11%2B-blue` | Alinhar ao alvo único |
| A3 | `.github/workflows/BRNCOIN.yml:21` | `python-version: "3.11"` | **Workflow novo, não entrava no patch anterior**; alinhar |
| A4 | `.github/workflows/contract-checks.yml:30` | `python-version: "3.12"` | Alinhar (hoje é o único em 3.12) |
| A5 | `.github/workflows/network-config-check.yml:38` | `python-version: "3.11"` | Alinhar |
| A6 | `Dockerfile:2` | `FROM python:3.11-slim` | Alinhar à imagem do alvo |
| A7 | `.devcontainer/devcontainer.json:4` | `mcr.microsoft.com/devcontainers/python:1-3.11-bookworm` | Alinhar |
| A8 | `requirements.txt:5` | Comentário "Python alvo: **3.11** (Dockerfile); compatível com 3.10–3.13" | Atualizar comentário quando o alvo for definido |
| A9 | `iniciar_cliente.bat` / `INICIA.SH` | Não travam versão; checagem genérica `python --version` | OK, mas instalam dependências que divergem do requirements (ver Tabela B) |

**Decisão necessária (não dá para "atualizar no escuro"):**
- **Recomendação prática:** unificar em **3.12** agora (todos os pins já têm wheels: `cryptography~=50.0`, `pymongo~=4.20`, `fastapi~=0.165`, `uvicorn~=0.54`, `bitcoinlib~=0.7`) e usar matrix no CI (3.12 e 3.13).
- Migrar para **3.14** só depois que o CI verde provar que `bitcoinlib`, `pywebview` e os pins atuais instalam em 3.14; hoje não há workflow que teste isso.
- Não deixar README em "3.8+" enquanto Docker/CI usam 3.11/3.12: usuário que instalar 3.8 pode quebrar em imports modernos (o código usa `from __future__ import annotations` e sintaxe `X | Y`, compatível com ≥3.10 na prática).

## B. Dependências — `requirements.txt` está completo; os comandos manuais não

Auditoria de imports feita nos `.py`: todos os módulos de terceiros efetivamente importados (`bitcoinlib`, `pysocks`, `pywebview`, `pymongo`, etc.) estão cobertos no `requirements.txt` ✅. O problema está nas listas escritas à mão.

| # | Arquivo:linha | Estado | Correção |
|---|---|---|---|
| B1 | `README.md:83` (Windows `pip install`) | Lista só `flask flask-cors cryptography argon2-cffi orjson requests mnemonic pywebview` | Faltam: `requests[socks]`/`pysocks`, `pymongo`, `fastapi`, `uvicorn[standard]`, `waitress`, `bitcoinlib`. Melhor: trocar por `pip install -r requirements.txt` |
| B2 | `README.md:100` (Linux `pip3 install`) | Lista ainda mais curta (sem `pywebview`, sem os do B1) | Mesma solução B1 |
| B3 | `INICIA.SH:38` | `PKGS="fastapi uvicorn uvloop flask flask-cors waitress requests orjson cryptography argon2-cffi mnemonic pymongo"` | Faltam `pysocks` (proxy SOCKS5 usado em `p2p_unified.py`/`isolation_detector.py`), `bitcoinlib` (ponte offramp), `zeroconf` (opcional, `discovery_v2.py:367`); `requests` deveria vir `requests[socks]` |
| B4 | `iniciar_cliente.bat:105` | Lista inclui `pythonnet` (**não importado em nenhum `.py`**) e `zeroconf` (só importação opcional); falta `pysocks`; linhas 98–101 fixam `anyio<4.15.0` e `starlette<0.40.0`, que **não constam do requirements.txt** | Remover `pythonnet`; adicionar `pysocks`; mover os pins de anyio/starlette para o requirements ou removê-los do script |
| B5 | `requirements.txt` | `waitress~=3.0` declarado, mas **nenhum `.py` faz `import waitress`** (o servidor usa `uvicorn`; `server.py` não referencia waitress) | Verificar uso restante ou remover da lista |

## C. Metadados e config — correções factuais (parte já no patch anexo)

| # | Arquivo:linha | Problema | Ação |
|---|---|---|---|
| C1 | `version.py:6` | `GITHUB_REPO = "LRN-L1-L2"` — nome de repo errado (o repo é `BRN-V002`) | **Corrigido no patch anexo** → `"BRN-V002"` |
| C2 | `README.md:84` e `README.md:103` | Instruem copiar `brn_network.env.exemplo`; o arquivo que existe é **`brn_network.env.example`** (comando de copy falha) | **Corrigido no patch anexo** |
| C3 | `API.md:8` vs repositório | Badge `license-MIT-green`, mas **não existe arquivo LICENSE**; `README.md:186` diz "Consulte o arquivo LICENSE para detalhes" que não existe | Ou adicionar LICENSE real, ou remover o badge e a referência |
| C4 | `.github/workflows/copilot-setup-steps.yml` | Arquivo começa com texto de conversa em prosa, **não é YAML válido de Actions**; nunca executa | Renomear para `.md` (documentação) ou excluir |
| C5 | `.devcontainer/devcontainer.json` | `postAttachCommand` roda `streamlit run main.py`: `streamlit` não está no requirements e `main.py` não é app Streamlit | Remover o comando ou trocar por `python3 main.py` |
| C6 | `Dockerfile:24` + `start.sh:25` | Docker usa `WORKDIR /app` e `CMD ["./start.sh"]`, mas `start.sh` faz `cd /opt/render/project/src` (caminho da Render) — quebra fora da Render | Detectar PATH ou parametrizar; validar `docker build` local |
| C7 | `API.md:6` vs código | Badge `version-9.2.0`, enquanto `main.py`/`server.py` andam em v9.9.x e `version.py` em 13.7.12 | Padronizar versão única (há 3 fontes de versão no repo) |

## D. `db.py` v9.8.x — status do diagnóstico anterior (10 itens)

| # | Melhoria recomendada | Estado atual | Evidência |
|---|---|---|---|
| D1 | Writer com `synchronous=FULL` | ❌ **Não feito** | `db.py:178` aplica `synchronous = NORMAL` a toda conexão, inclusive de escrita |
| D2 | Mempool sair do disco (efêmera → memória) | ❌ **Não feito** | Tabela `mempool` persistida (`db.py:263`, `:383`); `add_mempool` faz INSERT (`db.py:874`) |
| D3 | Checagem de integridade de blobs / `PRAGMA integrity_check` | ❌ **Não feito** | Nenhuma ocorrência em `db.py` ou `backup_manager.py` |
| D4 | Pool de 2–4 conexões de leitura | ❌ Não há pool | `_get_connection()` abre e fecha conexão por chamada (`db.py:142–208`); funciona, mas sem reuso |
| D5 | `PRAGMA optimize` / ANALYZE no fechamento | ✅ **Parcial** | `PRAGMA optimize` adotado no fechamento (`db.py:1392`); `ANALYZE` não |
| D6 | `wal_autocheckpoint` + `journal_size_limit` | ✅ **Feito** | `db.py:182–183`: checkpoint 1000; limite 64 MB |
| D7 | Reader em `mode=ro` | ✅ **Feito** | URI `file:...?mode=ro` com fallback (`db.py:154–171`) |
| D8 | `PRAGMA user_version` para migrations | ❌ **Não feito** | Nenhuma referência a `user_version` |
| D9 | Backup online consistente (`conn.backup()` / `VACUUM INTO`) | ⚠️ **Parcial** | `backup_manager.py` empacota arquivos `.db` + `-wal` + `-shm` em ZIP; cópia pode sair inconsistente se o nó estiver escrevendo; sem API online do SQLite |
| D10 | LMDB/RocksDB acima de ~1000 writes/s | ⏸️ Não aplicado (prematuro) | Continua SQLite; adequado ao volume atual (height ~4,8 mil) |

**Também já adotado desde o diagnóstico inicial:** índices novos (`idx_mempool_added_at`, `tx_index(height)`, `utxos(height)`), `VACUUM` fora de transação (`db.py:207–212`), UPSERT na mempool, writer com `BEGIN IMMEDIATE`.

## E. Segurança/higiene — sem mudança desde 20/nov (confirmado no código atual)

| # | Item | Estado |
|---|---|---|
| E1 | PBFT desligado por padrão (`BRN_PBFT_ENABLED`/`BRN_PBFT_ENFORCE` default "0") | Confirmado: `pbft_consensus.py:78–79`; `blockchain.py:113–114` |
| E2 | `BRN_STRICT_MULTISIG` default "0" e bypass `BOOTSTRAP_SKIP_SIGS_UNTIL`/`ASSUME_VALID_UNTIL` | Confirmado: `blockchain.py:102–118`; `main.py:379–381` os seta no fast-sync |
| E3 | Cofre BTC com **uma única WIF**, sem multisig/threshold | Confirmado: `btc_sender.py:34–48` (`Key(self.wif, ...)`); `btc_config.py:87–112` |
| E4 | Sem LICENSE no repositório (ver C3) | Confirmado: ausente |
| E5 | Binários e artefatos sensíveis rastreados no repo público | `git ls-files`: 5 DLLs, 2 `.so`, `sync_manager.py.bak`, `audit.log`, `miner_wallet.enc`; `.gitignore` ignora `*.log` mas não `*.enc`, e os arquivos já estão versionados |
| E6 | README ainda instruído a enviar BTC real, com endereço mainnet fixo | Confirmado no README (linhas ~17 e seção de ponte) |
| E7 | Taxa de ponte arbitrária (1 BTC = 10.000 BRN) sem oráculo | Inalterado em `l2_manager.py`/`btc_config.py` |

---

## Prioridade sugerida

**P0 — rápido e factual (já parcialmente no patch anexo)**
1. Aplicar `brn_patch_correcoes_mecanicas.patch` (C1, C2).
2. Trocar os comandos manuais de pip do README por `pip install -r requirements.txt` (B1, B2).
3. Resolver C3: adicionar LICENSE de verdade ou remover badge/referência.
4. Decidir e aplicar o alvo único de Python (A1–A8).

**P1 — coerência operacional**
5. Alinhar `INICIA.SH` (B3) e `iniciar_cliente.bat` (B4) ao `requirements.txt`; remover `pythonnet`/pins órfãos.
6. Corrigir C5 (devcontainer) e C6 (start.sh no Docker); validar build local.
7. C7: fonte única de versão (usar `version.py` nos badges/API).

**P2 — arquitetura e segurança (não há atalho de patch)**
8. D1/D2/D8/D9: writer FULL, mempool em memória, `user_version`, backup online.
9. E1–E3: strict multisig default, PBFT só quando houver quórum real, cofre BTC multisig.
10. E5: remover do rastreamento `miner_wallet.enc`, `audit.log`, binários compilados e `.bak`; adicionar `*.enc` ao `.gitignore`.

**Observação:** o patch anexo não toca código de consenso, banco ou ponte — é seguro para aplicar em cima de qualquer clone recente. Os itens de P2 exigem decisão de projeto e não devem ser aplicados por patch mecânico.