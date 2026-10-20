# 📚 BRN Node — Manual da API

> Documentação completa dos endpoints HTTP do nó BRN.
> Cobre API principal (`:5000`), Explorer + proxies (`:8080`), protocolo P2P e diagnóstico.

![Version](https://img.shields.io/badge/version-9.2.0-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 📖 Sumário

- [Visão Geral das Portas](#-visão-geral-das-portas)
- [Autenticação](#-autenticação)
- [1. Status e Diagnóstico](#-seção-1--status-e-diagnóstico)
- [2. Peers e Rede](#-seção-2--peers-e-rede)
- [3. Carteira e Saldo](#-seção-3--carteira-e-saldo)
- [4. Transações](#-seção-4--transações)
- [5. Mineração](#️-seção-5--mineração)
- [6. HD Wallet (BIP39/BIP44)](#-seção-6--hd-wallet-bip39bip44)
- [7. Contratos](#-seção-7--contratos)
- [8. L2 (BTC → BRN)](#-seção-8--l2-btc--brn)
- [9. Explorer e Painel](#️-seção-9--explorer-e-painel-porta-8080)
- [10. Coleção de Testes](#-seção-10--coleção-de-testes-rápidos)
- [11. Erros Comuns](#-seção-11--erros-comuns)
- [12. Variáveis de Ambiente](#-seção-12--variáveis-de-ambiente-relevantes)
- [13. Exemplos Prontos](#-seção-13--exemplos-prontos-copiarcolar)
- [14. Interfaces Web](#-seção-14--interfaces-web)

---

## 🌐 Visão Geral das Portas

| Porta | Serviço | Uso |
|-------|---------|-----|
| **5000** | API principal (`server.py`) | Writes + reads autenticados |
| **8080** | Explorer + Painel (`explorer.py`) | Reads públicos + UI + proxies |
| **6001** | P2P (`p2p_unified.py`) | Protocolo binário BRN5 (não HTTP) |
| **50007** | Discovery UDP multicast | Descoberta LAN (não HTTP) |

### Base URLs típicas (via Tailscale)

| Máquina | API (5000) | Explorer (8080) |
|---------|-----------|-----------------|
| Windows | `http://100.84.11.48:5000` | `http://100.84.11.48:8080` |
| Ubuntu | `http://100.111.253.8:5000` | `http://100.111.253.8:8080` |
| Local | `http://127.0.0.1:5000` | `http://127.0.0.1:8080` |

> ⚠️ Troque os IPs `100.x.x.x` pelos seus, obtidos via `tailscale ip -4`.

---

## 🔐 Autenticação

Endpoints marcados com 🔒 exigem **Basic Auth** (`BRN_WEB_USER` + `BRN_WEB_PASS`).

```bash
curl -s -u admin:senha-web-2026 http://127.0.0.1:5000/api/status
```

Endpoints marcados com 🔑 exigem **Bearer Token** (`BRN_ADMIN_TOKEN`):

```bash
curl -s -H "Authorization: Bearer SEU_TOKEN" -X POST http://127.0.0.1:5000/api/faucet
```

**Rate limit:** 60 requisições/min por IP (configurável via `BRN_RATE_LIMIT`).

---

## 📊 Seção 1 — Status e Diagnóstico

### 1.1 Healthcheck

```
GET /health
```

**Exemplo:**
```bash
curl -s http://127.0.0.1:5000/health | python3 -m json.tool
```

**Resposta:**
```json
{
    "ok": true,
    "height": 4848,
    "l2_enabled": true,
    "chain_injected": true,
    "sync_injected": true,
    "mode": "modo2_aberto",
    "version": "9.5"
}
```

### 1.2 Status geral

```
GET /api/status
```
🔒 (via `:5000`) · Público via `:8080/api/status-proxy`

```bash
curl -s -u admin:senha-web-2026 http://127.0.0.1:5000/api/status | python3 -m json.tool
```

**Resposta:**
```json
{
    "name": "BrunoCoin",
    "ticker": "BRN",
    "height": 4848,
    "tip_hash": "00000001956b864e...",
    "utxos": 4664,
    "mempool": 0,
    "peers": 2,
    "reward": 5000000000,
    "difficulty": 7,
    "contracts": 0,
    "l2_enabled": true
}
```

### 1.3 Info da cadeia

```
GET /api/chain-info
```

```bash
curl -s http://127.0.0.1:5000/api/chain-info | python3 -m json.tool
```

**Resposta:**
```json
{
    "success": true,
    "name": "BrunoCoin",
    "ticker": "BRN",
    "height": 4848,
    "tip_hash": "00000001956b864e...",
    "reward": 5000000000,
    "difficulty": 7
}
```

### 1.4 Cumulative work

```
GET /api/work
```

```bash
curl -s http://127.0.0.1:5000/api/work | python3 -m json.tool
```

**Resposta:**
```json
{
    "success": true,
    "height": 4848,
    "cumulative_work": 340282366920938463463374607431768211456
}
```

### 1.5 Estimativa de taxa

```
GET /api/fee-estimate
```

**Resposta:**
```json
{
    "success": true,
    "low": 1000,
    "medium": 2000,
    "high": 5000,
    "min_relay_fee": 1000
}
```

### 1.6 Sync (formato antigo)

```
GET /api/sync-info
```

### 1.7 Sync (detalhado)

```
GET /api/sync/status
```

```bash
curl -s http://127.0.0.1:5000/api/sync/status | python3 -m json.tool
```

**Resposta:**
```json
{
    "ok": true,
    "synced": true,
    "syncing": false,
    "forking": false,
    "mode": "full",
    "progress": 100.0,
    "local_height": 4848,
    "target_height": 4848,
    "blocos_faltando": 0,
    "blocos_por_segundo": 0.0,
    "velocidade_media": 0.0,
    "eta_segundos": null,
    "eta_legivel": "sincronizado",
    "active_peer": null,
    "peers_known": 1,
    "peers_synced": 1,
    "checkpoint_violations": 0,
    "checkpoints_conhecidos": 0,
    "reorgs_total": 0,
    "last_reorg_height": -1,
    "last_error": ""
}
```

---

## 👥 Seção 2 — Peers e Rede

### 2.1 Peers com score

```
GET /api/peers/score
```

```bash
curl -s http://127.0.0.1:5000/api/peers/score | python3 -m json.tool
```

**Resposta:**
```json
{
    "success": true,
    "peers": [
        {
            "address": "100.111.253.8:6001",
            "node_id": "e3c8aa3a4531cb22893baba5ff2b2a4b...",
            "public_key": "e3c8aa3a...",
            "height": 0,
            "last_seen": 1760000000,
            "first_seen": 1791000000,
            "score": 0,
            "version": "BRN5/2.0"
        }
    ],
    "banned": []
}
```

### 2.2 Peers (formato alternativo)

```
GET /api/peers
```

**Resposta:**
```json
{
    "count": 2,
    "active": 2,
    "peers": []
}
```

---

## 💼 Seção 3 — Carteira e Saldo

### 3.1 Nova carteira

```
POST /api/nova-carteira
```

```bash
curl -s -X POST http://127.0.0.1:5000/api/nova-carteira | python3 -m json.tool
```

**Resposta:**
```json
{
    "success": true,
    "address": "brn1qabc123...",
    "private_key": "a1b2c3d4...",
    "public_key": "02abc123...",
    "warning": "Guarde a chave privada."
}
```

### 3.2 Saldo

```
GET /api/saldo/<address>
GET /api/balance/<address>
```

```bash
curl -s http://127.0.0.1:5000/api/saldo/brn1qabc123... | python3 -m json.tool
```

**Resposta:**
```json
{
    "success": true,
    "address": "brn1qabc123...",
    "balance_sats": 12345678900,
    "balance_brn": 123.45678900
}
```

### 3.3 Portfolio

```
GET /api/portfolio/<address>
```

**Resposta:**
```json
{
    "portfolio": {
        "address": "brn1qabc123...",
        "BRN": 123.456789,
        "KYC": 0,
        "balance_sats": 12345678900,
        "utxos": 5
    }
}
```

### 3.4 Histórico de transações

```
GET /api/minhas-txs/<address>
GET /api/transacoes/<address>
```

```bash
curl -s http://127.0.0.1:5000/api/minhas-txs/brn1qabc123... | python3 -m json.tool
```

**Resposta:**
```json
{
    "ok": true,
    "address": "brn1qabc123...",
    "count": 3,
    "transactions": [
        {
            "txid": "abc...",
            "direction": "received",
            "amount": 5000000000,
            "status": "confirmed",
            "block_height": 4848,
            "confirmations": 1,
            "timestamp": 1760000000
        }
    ]
}
```

**Valores de `direction`:** `received` · `sent` · `self`

### 3.5 Status de transação

```
GET /api/tx-status/<txid>
```

**Resposta:**
```json
{
    "ok": true,
    "txid": "abc...",
    "status": "confirmed",
    "block_height": 4848,
    "confirmations": 5,
    "timestamp": 1760000000
}
```

**Valores de `status`:** `confirmed` · `pending` · `not_found`

### 3.6 Verificar recebimento

```
GET /api/verificar-recebimento/<address>/<txid>
```

**Resposta:**
```json
{
    "ok": true,
    "chegou": true,
    "status": "confirmed",
    "valor": 5000000000,
    "confirmacoes": 5,
    "block_height": 4848,
    "msg": "Confirmado no bloco #4848 com 5 confirmacoes."
}
```

### 3.7 Nonce

```
GET /api/nonce/<pubkey>
```

**Resposta:**
```json
{
    "success": true,
    "pubkey": "02abc...",
    "next_nonce": 3
}
```

---

## 💸 Seção 4 — Transações

### 4.1 Transferir BRN

```
POST /api/transfer
POST /api/send
POST /api/enviar
```
🔒

**Body (JSON):**
```json
{
    "from": "brn1q...",
    "to": "brn1q...",
    "asset_id": "BRN",
    "amount": 10.5,
    "private_key": "a1b2c3...",
    "public_key": "02abc..."
}
```

**Exemplo:**
```bash
curl -s -u admin:senha-web-2026 -X POST http://127.0.0.1:5000/api/transfer \
  -H "Content-Type: application/json" \
  -d '{
    "from": "brn1qabc...",
    "to": "brn1qdef...",
    "asset_id": "BRN",
    "amount": 10.5,
    "private_key": "a1b2c3d4...",
    "public_key": "02abc..."
  }' | python3 -m json.tool
```

**Resposta:**
```json
{
    "ok": true,
    "txid": "abc123...",
    "nonce": 3,
    "msg": "Aceita na mempool."
}
```

**Erros comuns:**

| Código | Erro | Causa |
|--------|------|-------|
| 400 | `Saldo insuficiente` | Não tem UTXO suficiente |
| 400 | `Valor deve ser > 0` | Amount inválido |
| 400 | `Chave privada nao corresponde` | `from` ≠ `private_key` |
| 400 | `fee abaixo do minimo` | Taxa < 1000 sats |
| 401 | `auth required` | Sem Basic Auth |

---

## ⛏️ Seção 5 — Mineração

### 5.1 Minerar 1 bloco (síncrono)

```
POST /api/mine
```

**Body:**
```json
{
    "validator_address": "brn1q...",
    "miner_pubkey": "02abc..."
}
```

**Exemplo:**
```bash
curl -s -X POST http://127.0.0.1:5000/api/mine \
  -H "Content-Type: application/json" \
  -d '{
    "validator_address": "brn1qabc...",
    "miner_pubkey": "02abc..."
  }' | python3 -m json.tool
```

**Resposta:**
```json
{
    "ok": true,
    "msg": "Bloco #4849 minerado!",
    "block": {
        "height": 4849,
        "hash": "000000...",
        "txs": 1,
        "difficulty": 7,
        "nonce": 123456
    }
}
```

### 5.2 Iniciar miner contínuo

```
POST /api/miner/start
POST /api/miner/on
POST /api/start-mining
```

### 5.3 Parar miner

```
POST /api/miner/stop
POST /api/miner/off
POST /api/stop-mining
```

```bash
curl -s -X POST http://127.0.0.1:5000/api/miner/stop | python3 -m json.tool
```

### 5.4 Status do miner

```
GET /api/miner/status
```

```bash
curl -s http://127.0.0.1:5000/api/miner/status | python3 -m json.tool
```

**Resposta:**
```json
{
    "running": true,
    "address": "brn1qabc...",
    "pubkey": "02abc...",
    "blocks_mined": 223,
    "count": 223,
    "last_height": 4848,
    "uptime": 15234,
    "height": 4848,
    "difficulty": 7,
    "fork_aborts": 0,
    "fork_in_progress": false,
    "last_error": ""
}
```

### 5.5 Faucet

```
POST /api/faucet
```
🔑 (requer `BRN_ADMIN_TOKEN`)

**Body:**
```json
{
    "address": "brn1q...",
    "public_key": "02abc..."
}
```

> **Limite:** 3 usos por endereço, 1 hora entre cada.

---

## 🔷 Seção 6 — HD Wallet (BIP39/BIP44)

### 6.1 Criar HD wallet

```
POST /api/hd/create
```

**Body:**
```json
{ "strength": 128 }
```

**Exemplo:**
```bash
curl -s -X POST http://127.0.0.1:5000/api/hd/create \
  -H "Content-Type: application/json" \
  -d '{"strength": 128}' | python3 -m json.tool
```

**Resposta:**
```json
{
    "ok": true,
    "warning": "GUARDE o mnemonico.",
    "mnemonic": "abandon abandon abandon ...",
    "address": "brn1q...",
    "private_key": "a1b2...",
    "public_key": "02abc...",
    "index": 0,
    "path": "m/44'/0'/0'/0/0"
}
```

**Força do mnemônico:** `128`, `160`, `192`, `224`, `256` (12 a 24 palavras).

### 6.2 Derivar de mnemônico

```
POST /api/hd/derive
```

**Body:**
```json
{
    "mnemonic": "abandon abandon ...",
    "index": 5
}
```

---

## 📜 Seção 7 — Contratos

### 7.1 Deploy

```
POST /api/contract/deploy
```

**Body:**
```json
{
    "owner": "brn1q...",
    "code": {
        "version": 1,
        "name": "Counter",
        "initial_state": { "count": 0 },
        "methods": {
            "increment": [
                { "op": "add", "path": "count", "value": 1 }
            ]
        }
    },
    "metadata": {}
}
```

**Resposta:**
```json
{
    "ok": true,
    "contract_id": "ctr1abc...",
    "owner": "brn1q...",
    "name": "Counter"
}
```

### 7.2 Chamar método

```
POST /api/contract/call
```

**Body:**
```json
{
    "contract_id": "ctr1abc...",
    "caller": "brn1q...",
    "args": { "method": "increment" }
}
```

### 7.3 Info do contrato

```
GET /api/contract/<contract_id>
```

### 7.4 Listar contratos

```
GET /api/contracts?owner=brn1q...
```

---

## 🌉 Seção 8 — L2 (BTC → BRN)

### 8.1 Cotação

```
GET /api/l2/quote?btc_sats=100000
```

**Resposta:**
```json
{
    "btc_amount": "0.00100000",
    "brn_gross": "10.00000000",
    "fee_percent": 2.0,
    "fee_brn": "0.20000000",
    "brn_net": "9.80000000",
    "rate": "1 BTC = 10000.0 BRN",
    "btc_address": "bc1q...",
    "min_confirmations": 3
}
```

### 8.2 Criar ordem

```
POST /api/l2/create
```

**Body:**
```json
{
    "btc_sats": 100000,
    "buyer_brn": "brn1q..."
}
```

### 8.3 Consultar ordem

```
GET /api/l2/order/<escrow_id>
```

**Status possíveis:**

| Status | Significado |
|--------|-------------|
| `OPEN` | Aguardando BTC |
| `BTC_DETECTED` | BTC recebido, aguardando minerar |
| `RELEASED` | BRN creditado |
| `EXPIRED` | 24h passaram |
| `CANCELLED` | Cancelada |

### 8.4 Estatísticas L2

```
GET /api/l2/stats
```

### 8.5 Listar ordens

```
GET /api/l2/list?buyer=brn1q...&status=OPEN
```

### 8.6 Cancelar ordem

```
POST /api/l2/cancel/<escrow_id>
```

---

## 🖥️ Seção 9 — Explorer e Painel (porta 8080)

### 9.1 Interfaces web

| URL | O que é |
|-----|---------|
| `http://127.0.0.1:8080/` | Explorer HTML |
| `http://127.0.0.1:8080/panel` | Painel de controle |
| `http://127.0.0.1:8080/monitor` | Monitor de rede |

### 9.2 Reads do DB (locais)

| Endpoint | O que retorna |
|----------|---------------|
| `GET /api/stats` | Estatísticas do DB |
| `GET /api/block/<height>` | Bloco por altura |
| `GET /api/latest` | Últimos 10 blocos |
| `GET /api/blocks?start=0&limit=20` | Blocos paginados |
| `GET /api/balance/<address>` | Saldo |
| `GET /api/mempool` | Mempool |
| `GET /api/peers` | Peers do DB |
| `GET /api/events?n=50` | Últimos eventos |
| `GET /api/verify` | Auditoria da cadeia (cache 5 min) |

### 9.3 Proxies para o `:5000`

Úteis quando você quer chamar a API sem problema de CORS:

| Proxy | Endpoint real |
|-------|---------------|
| `GET /api/status-proxy` | `/api/status` |
| `GET /api/chain-info-proxy` | `/api/chain-info` |
| `GET /api/sync-status-proxy` | `/api/sync/status` |
| `GET /api/sync-info-proxy` | `/api/sync-info` |
| `GET /api/miner-status-proxy` | `/api/miner/status` |
| `POST /api/miner/start-proxy` | `/api/miner/start` |
| `POST /api/miner/stop-proxy` | `/api/miner/stop` |
| `GET /api/peers-score-proxy` | `/api/peers/score` |
| `GET /api/saldo-proxy/<addr>` | `/api/saldo/<addr>` |
| `GET /api/portfolio-proxy/<addr>` | `/api/portfolio/<addr>` |
| `GET /api/tx-status-proxy/<txid>` | `/api/tx-status/<txid>` |
| `GET /api/minhas-txs-proxy/<addr>` | `/api/minhas-txs/<addr>` |

### 9.4 Rendezvous

| Endpoint | Método | Uso |
|----------|--------|-----|
| `/api/rendezvous/announce` | POST | Anunciar nó |
| `/api/rendezvous/peers` | GET | Listar peers anunciados |

### 9.5 MongoDB

| Endpoint | Uso |
|----------|-----|
| `GET /api/mongo/health` | Health do Mongo |
| `GET /api/mongo/events?n=50` | Eventos no Mongo |

---

## 🧪 Seção 10 — Coleção de Testes Rápidos

### Teste 1 — Node up?

```bash
curl -s http://127.0.0.1:5000/health
```

### Teste 2 — Está sincronizado?

```bash
curl -s http://127.0.0.1:5000/api/sync/status | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print('synced:', d['synced'], '| height:', d['local_height'], '| fork:', d['forking'])"
```

### Teste 3 — Qual a altura?

```bash
curl -s http://127.0.0.1:5000/api/chain-info | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(d['height'])"
```

### Teste 4 — Os dois nós batem?

```bash
U=$(curl -s http://127.0.0.1:5000/api/chain-info | python3 -c "import sys,json; print(json.load(sys.stdin)['tip_hash'])")
W=$(curl -s http://100.111.253.8:5000/api/chain-info | python3 -c "import sys,json; print(json.load(sys.stdin)['tip_hash'])")
[ "$U" = "$W" ] && echo "✅ mesma cadeia" || echo "❌ forks"
```

### Teste 5 — Está minerando?

```bash
curl -s http://127.0.0.1:5000/api/miner/status | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print('running:', d['running'], '| blocks:', d['blocks_mined'])"
```

### Teste 6 — Latência da API

```bash
time curl -s http://127.0.0.1:5000/api/chain-info > /dev/null
# Esperado: < 0.2s (com cache) / 14s (sem cache)
```

### Teste 7 — Saldo de um endereço

```bash
curl -s http://127.0.0.1:5000/api/saldo/brn1qabc... | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(d['balance_brn'], 'BRN')"
```

---

## 🚨 Seção 11 — Erros Comuns

| Código HTTP | Mensagem | Causa provável |
|-------------|----------|----------------|
| 400 | `Endereco invalido` | `brn1...` mal formatado |
| 400 | `Saldo insuficiente` | UTXO < amount |
| 400 | `txid invalido` | Payload corrompido |
| 400 | `fee abaixo do minimo` | Taxa < 1000 sats |
| 401 | `auth required` | Sem Basic Auth |
| 401 | `unauthorized` | `BRN_ADMIN_TOKEN` errado |
| 404 | `not found` | Endpoint inexistente ou bloco não existe |
| 429 | `rate limit` | Mais de 60 req/min por IP |
| 500 | `internal error` | Bug no servidor (ver log) |
| 503 | `server offline` | Proxy `:8080` não alcança `:5000` |

---

## 🔧 Seção 12 — Variáveis de Ambiente Relevantes

| Variável | Default | Uso |
|----------|---------|-----|
| `BRN_WEB_PORT` | `5000` | Porta da API principal |
| `BRN_EXPLORER_PORT` | `8080` | Porta do explorer |
| `BRN_P2P_PORT` | `6001` | Porta P2P TCP |
| `BRN_WEB_USER` | `admin` | Usuário do Basic Auth |
| `BRN_WEB_PASS` | `senha-temp-123` | Senha do Basic Auth |
| `BRN_ADMIN_TOKEN` | (vazio) | Token Bearer para faucet |
| `BRN_WEB_HOST` | `127.0.0.1` | Bind address HTTP |
| `BRN_EXPLORER_HOST` | `127.0.0.1` | Bind address explorer |
| `BRN_CORS_ORIGIN` | (vazio) | Origin permitido no CORS |
| `BRN_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING` |
| `BRN_MIN_DIFFICULTY` | `5` | Dificuldade inicial |
| `BRN_MAX_DIFFICULTY` | `7` | Dificuldade máxima |
| `BRN_DIFF_INTERVAL` | `50` | Ajusta diff a cada N blocos |
| `BRN_BLOCK_TIME` | `30` | Alvo de tempo por bloco (s) |
| `BRN_FINALITY_DEPTH` | `100` | Confirmações para finality |
| `BRN_MAX_REORG_DEPTH` | `100` | Profundidade máxima de reorg |

---

## 📋 Seção 13 — Exemplos Prontos (copiar/colar)

### Fluxo completo: nova carteira → faucet → transferência

```bash
# 1. Cria carteira
WALLET=$(curl -s -X POST http://127.0.0.1:5000/api/nova-carteira)
ADDR=$(echo $WALLET | python3 -c "import sys,json; print(json.load(sys.stdin)['address'])")
PRIV=$(echo $WALLET | python3 -c "import sys,json; print(json.load(sys.stdin)['private_key'])")
PUB=$(echo $WALLET | python3 -c "import sys,json; print(json.load(sys.stdin)['public_key'])")
echo "Endereço: $ADDR"

# 2. Pede faucet
curl -s -X POST http://127.0.0.1:5000/api/faucet \
  -H "Content-Type: application/json" \
  -d "{\"address\":\"$ADDR\",\"public_key\":\"$PUB\"}"

# 3. Espera 30s, consulta saldo
sleep 30
curl -s http://127.0.0.1:5000/api/saldo/$ADDR | python3 -m json.tool

# 4. Envia 1 BRN para outro endereço
curl -s -u admin:senha-web-2026 -X POST http://127.0.0.1:5000/api/transfer \
  -H "Content-Type: application/json" \
  -d "{
    \"from\": \"$ADDR\",
    \"to\": \"brn1qtest...\",
    \"asset_id\": \"BRN\",
    \"amount\": 1.0,
    \"private_key\": \"$PRIV\",
    \"public_key\": \"$PUB\"
  }" | python3 -m json.tool
```

### Monitorar altura até sincronizar

```bash
while true; do
  INFO=$(curl -s http://127.0.0.1:5000/api/chain-info)
  H=$(echo $INFO | python3 -c "import sys,json; print(json.load(sys.stdin)['height'])")
  TIP=$(echo $INFO | python3 -c "import sys,json; print(json.load(sys.stdin)['tip_hash'][:16])")
  echo "[$(date +%H:%M:%S)] altura=$H tip=$TIP"
  sleep 10
done
```

### Verificar se os 2 nós estão sincronizados

```bash
echo "=== Ubuntu (local) ==="
curl -s http://127.0.0.1:5000/api/chain-info | python3 -m json.tool

echo ""
echo "=== Windows (100.84.11.48) ==="
curl -s http://100.84.11.48:5000/api/chain-info | python3 -m json.tool
```

---

## 🔗 Seção 14 — Interfaces Web

| Interface | URL Local | URL Remota (Tailscale) |
|-----------|-----------|------------------------|
| Explorer | `http://127.0.0.1:8080/` | `http://100.111.253.8:8080/` |
| Painel | `http://127.0.0.1:8080/panel` | `http://100.111.253.8:8080/panel` |
| Monitor | `http://127.0.0.1:8080/monitor` | `http://100.111.253.8:8080/monitor` |
| Carteira desktop | pywebview (local) | — |
| API raiz | `http://127.0.0.1:5000/` | — |
| Healthcheck | `http://127.0.0.1:5000/health` | — |

---

## ⚠️ Notas finais

1. **Basic Auth** — a maioria dos endpoints não exige, mas `/api/transfer`, `/api/faucet` e `/api/mine` podem exigir. Passe `-u usuario:senha` no curl.
2. **Rate limit** — 60 req/min por IP (configurável via `BRN_RATE_LIMIT`). Se estourar, recebe `HTTP 429`.
3. **CORS** — o `:8080` libera `origins="*"`. O `:5000` só libera se `BRN_CORS_ORIGIN` estiver setado.
4. **Tailscale** — os IPs `100.x.x.x` funcionam entre as máquinas logadas na mesma conta. Troque pelos seus.
5. **Latência** — a partir do v9.2.0, com cache ativo, os endpoints respondem em **< 200ms**. Antes, `/api/chain-info` demorava **14s**.
6. **Endpoints `/api/*-proxy`** — existem só no `:8080`. Servem para contornar CORS quando o HTML é servido no `:8080` e precisa consultar o `:5000`.

---

## 📝 Como Contribuir

1. Faça um fork do projeto
2. Crie uma branch (`git checkout -b feature/nova-api`)
3. Documente o novo endpoint neste arquivo seguindo o padrão
4. Commit (`git commit -am 'Add nova API'`)
5. Push (`git push origin feature/nova-api`)
6. Abra um Pull Request

---

## 📄 Licença

Este projeto está sob a licença MIT. Veja [LICENSE](LICENSE) para mais detalhes.

---

**Última atualização:** 14/10/2026 · **Versão:** 9.2.0