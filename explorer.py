"""explorer.py — Backend do Explorer + Painéis + Block Explorer (v3.6)
===============================================================
v3.6 (2026-11-20) — ADAPTADO AO db.py v9.8.2 (schema novo):
  [FIX] blocks agora têm coluna `data` (JSON texto completo);
        removeu-se o zlib.decompress de `raw` (mantido só como
        fallback de DB legado).
  [FIX] Não existe tabela `transactions`; txs confirmadas vêm dos
        blocos, pendentes da tabela `mempool`.
  [FIX] utxos não têm coluna `spent`/`block_height`: saldo = SUM
        das linhas vivas; histórico = utxos + mempool envolvendo
        o endereço.
  [FIX] tx_detail: tx_index → bloco; fallback mempool; fallback
        legado (tabela transactions).
v3.5 (2026-11-17) — SEGURANÇA XSS/SSTI E LEITURA ZERO-LOCK:
  [FIX] Substituído render_template_string aninhado + Markup() por
        DictLoader nativo do Flask com herança {% extends "base.html" %}
        e auto-escape ativo em 100% das variáveis.
  [FIX] Conexão SQLite alterada para ?mode=ro (Read-Only) com
        PRAGMA query_only=ON para eliminar disputa de lock com o main.py.
  [KEEP] v3.4: Proxy da API, Waitress em produção, timeout=30s.
===============================================================
"""
from __future__ import annotations

__version__ = "3.6"

from flask import Flask, render_template, request, jsonify, send_file, Response
from jinja2 import DictLoader
import sqlite3
import zlib
import orjson
import os
import requests
from datetime import datetime
from pathlib import Path

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / "brn_v2_chain.db")

API_BASE = os.environ.get("BRN_API_URL", "http://127.0.0.1:5000").rstrip("/")

try:
    API_TIMEOUT = max(1.0, float(os.environ.get("BRN_EXPLORER_API_TIMEOUT", "15")))
except (TypeError, ValueError):
    API_TIMEOUT = 15.0

# ============================================================
# TEMPLATES JINJA2 COM HERANÇA NATIVA E AUTO-ESCAPE SEGURO
# ============================================================
TEMPLATES = {
    "base.html": """
<!DOCTYPE html>
<html>
<head>
    <title>{% block title %}BRN Explorer{% endblock %}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { background: #0d1117; color: #c9d1d9; font-family: -apple-system, sans-serif; margin: 0; padding: 20px; }
        .container { max-width: 1100px; margin: 0 auto; }
        header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #30363d; padding-bottom: 15px; margin-bottom: 20px; flex-wrap: wrap; gap: 10px; }
        h1 { margin: 0; color: #58a6ff; font-size: 1.5rem; }
        .search-box { display: flex; gap: 10px; margin-bottom: 20px; }
        .search-box input { flex: 1; padding: 10px; border-radius: 6px; border: 1px solid #30363d; background: #161b22; color: #fff; }
        .search-box button { padding: 10px 20px; background: #238636; border: none; border-radius: 6px; color: #fff; cursor: pointer; font-weight: bold; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin-bottom: 20px; }
        .card h2 { font-size: 1.1rem; border-bottom: 1px solid #30363d; padding-bottom: 10px; margin-top: 0; color: #8b949e; }
        table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid #21262d; }
        th { color: #8b949e; font-size: 0.8rem; text-transform: uppercase; }
        tr:hover { background: #1c2128; }
        .mono { font-family: ui-monospace, monospace; font-size: 0.85rem; color: #79c0ff; word-break: break-all; }
        .tag { padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
        .tag.ok { background: rgba(63,185,80,0.15); color: #3fb950; }
        .tag.warn { background: rgba(210,153,34,0.15); color: #d29922; }
        a { color: #58a6ff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        @media (max-width: 700px) { .grid { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
<div class="container">
    <header>
        <h1>🔷 BRN Explorer</h1>
        <div><a href="/">Home</a> | <a href="/dashboard">Dashboard ao Vivo</a> | <a href="/monitor">Monitor</a></div>
    </header>
    <form class="search-box" action="/search" method="GET">
        <input type="text" name="q" placeholder="Pesquisar por Bloco, TxID ou Endereço (brn1...)">
        <button type="submit">Pesquisar</button>
    </form>
    {% block content %}{% endblock %}
</div>
</body>
</html>
""",

    "home.html": """
{% extends "base.html" %}
{% block title %}BRN Explorer — Início{% endblock %}
{% block content %}
<div class="grid">
    <div class="card">
        <h2>Últimos Blocos</h2>
        <table>
            <tr><th>Altura</th><th>Hash</th><th>TXs</th><th>Tempo</th></tr>
            {% for b in blocks %}
            <tr>
                <td><a href="/block/{{ b.height }}"><b>{{ b.height }}</b></a></td>
                <td class="mono">{{ b.hash[:16] }}...</td>
                <td>{{ b.txs }}</td>
                <td>{{ b.time }}</td>
            </tr>
            {% endfor %}
        </table>
    </div>
    <div class="card">
        <h2>Últimas Transações</h2>
        <table>
            <tr><th>TxID</th><th>Valor</th><th>Hora</th></tr>
            {% for tx in txs %}
            <tr>
                <td><a href="/tx/{{ tx.txid }}" class="mono">{{ tx.txid[:16] }}...</a></td>
                <td>{{ tx.value }} BRN</td>
                <td>{{ tx.time }}</td>
            </tr>
            {% endfor %}
        </table>
    </div>
</div>
{% endblock %}
""",

    "block.html": """
{% extends "base.html" %}
{% block title %}Bloco #{{ block.height }} — BRN Explorer{% endblock %}
{% block content %}
<div class="card">
    <h2>Bloco #{{ block.height }}</h2>
    <table>
        <tr><td><b>Hash</b></td><td class="mono">{{ block.hash }}</td></tr>
        <tr><td><b>Prev Hash</b></td><td class="mono">{{ block.prev_hash }}</td></tr>
        <tr><td><b>Merkle Root</b></td><td class="mono">{{ block.merkle }}</td></tr>
        <tr><td><b>Timestamp</b></td><td>{{ block.time }}</td></tr>
        <tr><td><b>Dificuldade</b></td><td>{{ block.difficulty }}</td></tr>
        <tr><td><b>Nonce</b></td><td>{{ block.nonce }}</td></tr>
    </table>
</div>
<div class="card">
    <h2>Transações ({{ block.transactions|length }})</h2>
    <table>
        <tr><th>TxID</th><th>Tipo</th><th>Valor (BRN)</th></tr>
        {% for tx in block.transactions %}
        <tr>
            <td><a href="/tx/{{ tx.txid }}" class="mono">{{ tx.txid[:20] }}...</a></td>
            <td>{% if tx.inputs[0].txid == "0"*64 %}<span class="tag warn">Coinbase</span>{% else %}Transferência{% endif %}</td>
            <td>{{ tx.value }}</td>
        </tr>
        {% endfor %}
    </table>
</div>
{% endblock %}
""",

    "address.html": """
{% extends "base.html" %}
{% block title %}Endereço {{ address[:16] }}... — BRN Explorer{% endblock %}
{% block content %}
<div class="card">
    <h2>Endereço {{ address }}</h2>
    <p><b>Saldo:</b> <span style="font-size:1.5rem; color:#3fb950">{{ balance }} BRN</span></p>
    <p><b>Total Recebido:</b> {{ total_received }} BRN</p>
</div>
<div class="card">
    <h2>Transações</h2>
    <table>
        <tr><th>TxID</th><th>Direção</th><th>Valor</th><th>Status</th></tr>
        {% for tx in txs %}
        <tr>
            <td><a href="/tx/{{ tx.txid }}" class="mono">{{ tx.txid[:20] }}...</a></td>
            <td>{% if tx.direction == 'received' %}<span style="color:#3fb950">Recebido</span>{% elif tx.direction == 'sent' %}<span style="color:#f85149">Enviado</span>{% else %}Próprio{% endif %}</td>
            <td>{{ tx.amount }} BRN</td>
            <td>{% if tx.status == 'confirmed' %}<span class="tag ok">Confirmado</span>{% else %}<span class="tag warn">Pendente</span>{% endif %}</td>
        </tr>
        {% endfor %}
    </table>
</div>
{% endblock %}
""",

    "tx.html": """
{% extends "base.html" %}
{% block title %}Transação {{ tx.txid[:16] }}... — BRN Explorer{% endblock %}
{% block content %}
<div class="card">
    <h2>Transação {{ tx.txid[:32] }}...</h2>
    <table>
        <tr><td><b>TxID</b></td><td class="mono">{{ tx.txid }}</td></tr>
        <tr><td><b>Bloco</b></td><td>{% if block_height %}<a href="/block/{{ block_height }}">#{{ block_height }}</a>{% else %}<span class="tag warn">Mempool</span>{% endif %}</td></tr>
        <tr><td><b>Timestamp</b></td><td>{{ tx_time }}</td></tr>
        <tr><td><b>Entradas</b></td><td>{{ tx.inputs|length }}</td></tr>
        <tr><td><b>Saídas</b></td><td>{{ tx.outputs|length }}</td></tr>
    </table>
</div>
<div class="card">
    <h2>Saídas ({{ tx.outputs|length }})</h2>
    <table>
        <tr><th>#</th><th>Endereço</th><th>Valor (BRN)</th></tr>
        {% for out in tx.outputs %}
        <tr>
            <td>{{ loop.index0 }}</td>
            <td>{% if out.address %}<a href="/address/{{ out.address }}" class="mono">{{ out.address[:24] }}...</a>{% else %}(sem endereço){% endif %}</td>
            <td>{{ '%.8f' % (out.amount / 1e8) }}</td>
        </tr>
        {% endfor %}
    </table>
</div>
{% endblock %}
"""
}

# Registra os templates em memória no ambiente Jinja2 do Flask (autoescape ativo por padrão)
app.jinja_env.loader = DictLoader(TEMPLATES)

# ============================================================
# HELPERS DB (Read-Only para zero lock durante sync do main.py)
# ============================================================
def get_db():
    path_uri = Path(DB_PATH).resolve().as_posix()
    try:
        conn = sqlite3.connect(f"file:///{path_uri}?mode=ro", uri=True, timeout=30.0)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA query_only=ON;")
    conn.row_factory = sqlite3.Row
    return conn


# ---------- helpers de schema (db.py v9.8.2 + fallback legado) ----------
def _table_exists(conn, table: str) -> bool:
    try:
        r = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        return r is not None
    except Exception:
        return False


def _columns(conn, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def _parse_block_row(row) -> dict:
    """Converte linha de `blocks` no dict do bloco.
    Novo schema: coluna `data` = JSON texto do bloco completo.
    Schema legado: coluna `raw` = blob zlib (mantido para leitura antiga).
    """
    cols = set(row.keys())
    b: dict = {}
    if "data" in cols and row["data"]:
        try:
            b = orjson.loads(row["data"])
        except Exception:
            b = {}
    elif "raw" in cols and row["raw"]:
        try:
            b = orjson.loads(zlib.decompress(row["raw"]))
        except Exception:
            b = {}
    for c in ("height", "hash", "prev_hash", "timestamp", "nonce",
              "merkle", "difficulty"):
        if c in cols and row[c] is not None:
            b.setdefault(c, row[c])
    b.setdefault("transactions", [])
    return b


def _is_coinbase(tx: dict) -> bool:
    ins = tx.get("inputs") or []
    return bool(ins and ins[0].get("txid") == "0" * 64)


def _tx_value_brn(tx: dict) -> float:
    return sum(int(o.get("amount", 0) or 0) for o in (tx.get("outputs") or [])) / 1e8


def get_latest_blocks(n=10):
    conn = get_db()
    out = []
    try:
        if "data" in _columns(conn, "blocks"):
            rows = conn.execute(
                "SELECT height, hash, timestamp, data FROM blocks "
                "ORDER BY height DESC LIMIT ?", (n,)).fetchall()
            for r in rows:
                b = _parse_block_row(r)
                ts = b.get("timestamp") or r["timestamp"] or 0
                out.append({
                    "height": b.get("height", r["height"]),
                    "hash": b.get("hash") or r["hash"] or "",
                    "txs": len(b.get("transactions") or []),
                    "time": datetime.fromtimestamp(ts).strftime("%H:%M:%S"),
                })
        else:
            # fallback legado: blobs zlib em `raw`
            rows = conn.execute(
                "SELECT height, raw FROM blocks ORDER BY height DESC LIMIT ?",
                (n,)).fetchall()
            for r in rows:
                try:
                    b = orjson.loads(zlib.decompress(r["raw"]))
                    out.append({
                        "height": b["height"],
                        "hash": b["hash"],
                        "txs": len(b.get("transactions", [])),
                        "time": datetime.fromtimestamp(
                            b["timestamp"]).strftime("%H:%M:%S"),
                    })
                except Exception:
                    continue
    except Exception:
        pass
    finally:
        conn.close()
    return out


def get_latest_txs(n=10):
    conn = get_db()
    out = []
    seen = set()
    try:
        # 1) Pendentes da mempool (mais recentes primeiro)
        if _table_exists(conn, "mempool"):
            mrows = conn.execute(
                "SELECT tx, added_at FROM mempool "
                "ORDER BY added_at DESC LIMIT ?", (n,)).fetchall()
            for r in mrows:
                try:
                    tx = orjson.loads(r["tx"])
                except Exception:
                    continue
                tid = tx.get("txid")
                if not tid or tid in seen or _is_coinbase(tx):
                    continue
                seen.add(tid)
                out.append({
                    "txid": tid,
                    "value": f"{_tx_value_brn(tx):.2f}",
                    "time": datetime.fromtimestamp(
                        r["added_at"] or tx.get("timestamp", 0)).strftime("%H:%M"),
                    "pending": True,
                })
        # 2) Confirmadas: últimos blocos (não existe tabela `transactions`)
        if "data" in _columns(conn, "blocks"):
            rows = conn.execute(
                "SELECT data FROM blocks ORDER BY height DESC LIMIT 25"
            ).fetchall()
            for r in rows:
                b = _parse_block_row(r)
                for tx in b.get("transactions") or []:
                    tid = tx.get("txid")
                    if not tid or tid in seen or _is_coinbase(tx):
                        continue
                    seen.add(tid)
                    out.append({
                        "txid": tid,
                        "value": f"{_tx_value_brn(tx):.2f}",
                        "time": datetime.fromtimestamp(
                            tx.get("timestamp", 0)).strftime("%H:%M"),
                        "pending": False,
                    })
                if len(out) >= n:
                    break
        elif _table_exists(conn, "transactions"):
            # fallback legado
            rows = conn.execute(
                "SELECT raw FROM transactions ORDER BY block_height DESC LIMIT ?",
                (n,)).fetchall()
            for r in rows:
                try:
                    tx = orjson.loads(zlib.decompress(r["raw"]))
                except Exception:
                    continue
                if _is_coinbase(tx):
                    continue
                out.append({
                    "txid": tx["txid"],
                    "value": f"{_tx_value_brn(tx):.2f}",
                    "time": datetime.fromtimestamp(
                        tx.get("timestamp", 0)).strftime("%H:%M"),
                    "pending": False,
                })
    except Exception:
        pass
    finally:
        conn.close()
    return out[:n]

# ============================================================
# ROTAS DO BLOCK EXPLORER
# ============================================================
@app.route("/")
def home():
    blocks = get_latest_blocks(10)
    txs = get_latest_txs(10)
    return render_template("home.html", blocks=blocks, txs=txs)


@app.route("/block/<int:height>")
def block_detail(height):
    conn = get_db()
    b = None
    try:
        if "data" in _columns(conn, "blocks"):
            row = conn.execute(
                "SELECT data, height, hash, prev_hash, timestamp, nonce, "
                "merkle, difficulty FROM blocks WHERE height=?",
                (height,)).fetchone()
            if row:
                b = _parse_block_row(row)
        else:
            row = conn.execute(
                "SELECT raw FROM blocks WHERE height=?", (height,)).fetchone()
            if row:
                b = orjson.loads(zlib.decompress(row["raw"]))
        if not b:
            return "Bloco não encontrado", 404
        b["time"] = datetime.fromtimestamp(
            b.get("timestamp", 0) or 0).strftime("%d/%m/%Y %H:%M:%S")
        for tx in b.get("transactions") or []:
            tx["value"] = f"{_tx_value_brn(tx):.8f}"
    except Exception as e:
        return f"Erro ao decodificar bloco: {e}", 500
    finally:
        conn.close()
    return render_template("block.html", block=b)


@app.route("/address/<address>")
def address_detail(address):
    if not address.startswith("brn1"):
        return "Endereço inválido", 400
    conn = get_db()
    balance = 0.0
    total_received = 0.0
    txs = []
    try:
        ucols = _columns(conn, "utxos")
        if "amount" in ucols:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM utxos WHERE address=?",
                (address,)).fetchone()
            balance = (row["s"] or 0) / 1e8
            # Histórico: UTXOs ainda vivos (confirmados)
            rows = conn.execute(
                "SELECT txid, amount, height FROM utxos WHERE address=? "
                "ORDER BY height DESC LIMIT 50", (address,)).fetchall()
            seen_txids = set()
            for r in rows:
                if r["txid"] in seen_txids:
                    continue
                seen_txids.add(r["txid"])
                txs.append({
                    "txid": r["txid"],
                    "amount": f"{(r['amount'] or 0) / 1e8:.8f}",
                    "direction": "received",
                    "status": "confirmed",
                    "block_height": r["height"],
                })
            # Pendentes: mempool com saída para o endereço
            if _table_exists(conn, "mempool"):
                for m in conn.execute(
                        "SELECT tx FROM mempool ORDER BY added_at "
                        "DESC LIMIT 200").fetchall():
                    try:
                        tx = orjson.loads(m["tx"])
                    except Exception:
                        continue
                    tid = tx.get("txid")
                    if not tid or tid in seen_txids:
                        continue
                    if any(o.get("address") == address
                           for o in (tx.get("outputs") or [])):
                        seen_txids.add(tid)
                        txs.append({
                            "txid": tid,
                            "amount": f"{_tx_value_brn(tx):.8f}",
                            "direction": "received",
                            "status": "pending",
                            "block_height": None,
                        })
            # Aproximação de recebido: soma dos itens listados
            # (UTXOs vivos + pendentes; saídas já gastas não aparecem
            #  na tabela utxos do schema v9.8.2).
            total_received = sum(float(t["amount"]) for t in txs)
    except Exception as e:
        return f"Erro ao carregar endereço: {e}", 500
    finally:
        conn.close()
    return render_template(
        "address.html",
        address=address,
        balance=f"{balance:.8f}",
        total_received=f"{total_received:.8f}",
        txs=txs
    )


@app.route("/tx/<txid>")
def tx_detail(txid):
    conn = get_db()
    tx = None
    block_height = None
    try:
        # 1) Confirmada: tx_index → altura → bloco
        if _table_exists(conn, "tx_index"):
            row = conn.execute(
                "SELECT height FROM tx_index WHERE txid=?", (txid,)).fetchone()
            if row:
                block_height = row["height"]
                if "data" in _columns(conn, "blocks"):
                    brow = conn.execute(
                        "SELECT data FROM blocks WHERE height=?",
                        (block_height,)).fetchone()
                    if brow:
                        b = _parse_block_row(brow)
                        tx = next((t for t in (b.get("transactions") or [])
                                   if t.get("txid") == txid), None)
                elif _table_exists(conn, "blocks"):
                    brow = conn.execute(
                        "SELECT raw FROM blocks WHERE height=?",
                        (block_height,)).fetchone()
                    if brow:
                        b = orjson.loads(zlib.decompress(brow["raw"]))
                        tx = next((t for t in (b.get("transactions") or [])
                                   if t.get("txid") == txid), None)
        # 2) Pendente: mempool
        if tx is None and _table_exists(conn, "mempool"):
            mrow = conn.execute(
                "SELECT tx FROM mempool WHERE txid=?", (txid,)).fetchone()
            if mrow:
                tx = orjson.loads(mrow["tx"])
        # 3) Legado: tabela `transactions` (blobs zlib)
        if tx is None and _table_exists(conn, "transactions"):
            row = conn.execute(
                "SELECT raw, block_height FROM transactions WHERE txid=?",
                (txid,)).fetchone()
            if row:
                tx = orjson.loads(zlib.decompress(row["raw"]))
                block_height = row["block_height"]
        if tx is None:
            return "Transação não encontrada", 404
        tx_time = datetime.fromtimestamp(
            tx.get("timestamp", 0) or 0).strftime("%d/%m/%Y %H:%M:%S")
    except Exception as e:
        return f"Erro ao decodificar tx: {e}", 500
    finally:
        conn.close()
    return render_template(
        "tx.html", tx=tx, block_height=block_height, tx_time=tx_time)


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return "Busca vazia", 400
    if q.isdigit():
        return block_detail(int(q))
    elif q.startswith("brn1"):
        return address_detail(q)
    elif len(q) == 64:
        return tx_detail(q)
    else:
        return "Formato não reconhecido. Use altura (número), endereço (brn1...) ou TxID (64 caracteres).", 400

# ============================================================
# SERVIDOR DE ARQUIVOS (Dashboard, Painel e Monitor)
# ============================================================
@app.route("/dashboard")
def serve_dashboard():
    p = BASE_DIR / "explorer.html"
    if p.exists():
        return send_file(p)
    return "explorer.html não encontrado", 404


@app.route("/panel")
def serve_panel():
    p = BASE_DIR / "panel.html"
   