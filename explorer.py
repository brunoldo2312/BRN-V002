"""
explorer.py — Backend do Explorer + Painéis + Block Explorer (v3.3)
================================================================
v3.3 (2026-11-04):
  [FIX CRÍTICO] Removido {% extends "layout" %} que quebrava as
        rotas /, /block/<h> e /address/<a> com TemplateNotFound.
        Agora usa um helper render_page() que injeta o layout.
  [FIX] /tx/<txid> agora renderiza página HTML bonita (antes era JSON).
  [FIX] Reordenado /api/<path>-proxy ANTES de /api/<path> para
        evitar conflito de matching de rotas no Flask.
  [NEW] Suporte a respostas não-JSON no proxy (ex: /health).
  [KEEP] Todas as rotas existentes (dashboard, panel, monitor).
================================================================
"""
from __future__ import annotations

__version__ = "3.3"

from flask import Flask, render_template_string, request, jsonify, send_file, Response
from markupsafe import Markup
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
# LAYOUT (usado pelo helper render_page)
# ============================================================
LAYOUT_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>BRN Explorer</title>
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
    {{ content }}
</div>
</body>
</html>
"""

def render_page(content_html: str, **context) -> str:
    """Renderiza o conteúdo interno e injeta no layout."""
    content = render_template_string(content_html, **context)
    return render_template_string(LAYOUT_HTML, content=Markup(content))


# ============================================================
# CONTEÚDOS DAS PÁGINAS
# ============================================================
HOME_CONTENT = """
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
"""

BLOCK_CONTENT = """
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
"""

ADDRESS_CONTENT = """
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
"""

TX_CONTENT = """
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
"""


# ============================================================
# HELPERS DB
# ============================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_latest_blocks(n=10):
    conn = get_db()
    rows = conn.execute("SELECT height, raw FROM blocks ORDER BY height DESC LIMIT ?", (n,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            b = orjson.loads(zlib.decompress(r["raw"]))
            out.append({
                "height": b["height"],
                "hash": b["hash"],
                "txs": len(b["transactions"]),
                "time": datetime.fromtimestamp(b["timestamp"]).strftime("%H:%M:%S")
            })
        except Exception:
            pass
    return out


def get_latest_txs(n=10):
    conn = get_db()
    rows = conn.execute("SELECT raw FROM transactions ORDER BY block_height DESC LIMIT ?", (n,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            tx = orjson.loads(zlib.decompress(r["raw"]))
            if tx["inputs"][0]["txid"] == "0"*64:
                continue
            val = sum(o["amount"] for o in tx["outputs"]) / 1e8
            out.append({
                "txid": tx["txid"],
                "value": f"{val:.2f}",
                "time": datetime.fromtimestamp(tx["timestamp"]).strftime("%H:%M")
            })
        except Exception:
            pass
    return out


# ============================================================
# ROTAS DO BLOCK EXPLORER
# ============================================================
@app.route("/")
def home():
    blocks = get_latest_blocks(10)
    txs = get_latest_txs(10)
    return render_page(HOME_CONTENT, blocks=blocks, txs=txs)


@app.route("/block/<int:height>")
def block_detail(height):
    conn = get_db()
    row = conn.execute("SELECT raw FROM blocks WHERE height=?", (height,)).fetchone()
    conn.close()
    if not row:
        return "Bloco não encontrado", 404

    try:
        b = orjson.loads(zlib.decompress(row["raw"]))
        b["time"] = datetime.fromtimestamp(b["timestamp"]).strftime("%d/%m/%Y %H:%M:%S")
        for tx in b["transactions"]:
            tx["value"] = f"{sum(o['amount'] for o in tx['outputs'])/1e8:.8f}"
    except Exception as e:
        return f"Erro ao decodificar bloco: {e}", 500

    return render_page(BLOCK_CONTENT, block=b)


@app.route("/address/<address>")
def address_detail(address):
    if not address.startswith("brn1"):
        return "Endereço inválido", 400

    conn = get_db()
    row = conn.execute("SELECT SUM(amount) as s FROM utxos WHERE address=? AND spent=0", (address,)).fetchone()
    balance = (row["s"] or 0) / 1e8

    row2 = conn.execute("SELECT SUM(amount) as s FROM utxos WHERE address=?", (address,)).fetchone()
    total_received = (row2["s"] or 0) / 1e8

    txs = []
    rows = conn.execute("SELECT txid, amount, spent, block_height FROM utxos WHERE address=? ORDER BY block_height DESC LIMIT 50", (address,)).fetchall()
    seen_txids = set()
    for r in rows:
        if r["txid"] in seen_txids:
            continue
        seen_txids.add(r["txid"])
        txs.append({
            "txid": r["txid"],
            "amount": f"{r['amount']/1e8:.8f}",
            "direction": "received",
            "status": "confirmed",
            "block_height": r["block_height"]
        })

    conn.close()
    return render_page(
        ADDRESS_CONTENT,
        address=address,
        balance=f"{balance:.8f}",
        total_received=f"{total_received:.8f}",
        txs=txs
    )


@app.route("/tx/<txid>")
def tx_detail(txid):
    conn = get_db()
    row = conn.execute("SELECT raw, block_height FROM transactions WHERE txid=?", (txid,)).fetchone()
    conn.close()
    if not row:
        return "Transação não encontrada", 404

    try:
        tx = orjson.loads(zlib.decompress(row["raw"]))
        tx_time = datetime.fromtimestamp(tx.get("timestamp", 0)).strftime("%d/%m/%Y %H:%M:%S")
    except Exception as e:
        return f"Erro ao decodificar tx: {e}", 500

    return render_page(TX_CONTENT, tx=tx, block_height=row["block_height"], tx_time=tx_time)


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
        return "Formato não reconhecido. Use altura (número), endereço (brn1...) ou TxID (64 chars).", 400


# ============================================================
# SERVIDOR DE ARQUIVOS (Dashboard, Panel e Monitor)
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
    if p.exists():
        return send_file(p)
    return "panel.html não encontrado", 404


@app.route("/monitor")
def serve_monitor():
    p = BASE_DIR / "monitor.html"
    if p.exists():
        return send_file(p)
    return "monitor.html não encontrado", 404


# ============================================================
# PROXY GENÉRICO
# ============================================================
def _do_api_proxy(path: str):
    """Repassa requisição /api/* para o nó principal."""
    if request.method == "OPTIONS":
        return "", 200

    target_url = f"{API_BASE}/api/{path}"

    try:
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            r = requests.post(target_url, json=data, timeout=API_TIMEOUT)
        else:
            r = requests.get(target_url, params=request.args, timeout=API_TIMEOUT)

        # Se a resposta for JSON, retorna JSON
        try:
            return jsonify(r.json()), r.status_code
        except ValueError:
            # Não-JSON (ex: texto puro de /health)
            return Response(r.content, status=r.status_code,
                            content_type=r.headers.get("Content-Type", "text/plain"))

    except requests.exceptions.ConnectionError:
        return jsonify({"ok": False, "error": f"Nó principal offline ({API_BASE})"}), 502
    except requests.exceptions.Timeout:
        return jsonify({"ok": False, "error": f"Timeout ao contactar {API_BASE} ({API_TIMEOUT}s)"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# IMPORTANTE: -proxy registrado PRIMEIRO para evitar conflito
@app.route("/api/<path:path>-proxy", methods=["GET", "POST", "OPTIONS"])
def api_proxy_compat(path):
    return _do_api_proxy(path)


@app.route("/api/<path:path>", methods=["GET", "POST", "OPTIONS"])
def api_proxy(path):
    return _do_api_proxy(path)


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
    print(f"BRN Explorer + Painéis rodando em http://127.0.0.1:{port}")
    print(f"  → Block Explorer: http://127.0.0.1:{port}/")
    print(f"  → Dashboard:      http://127.0.0.1:{port}/dashboard")
    print(f"  → Monitor:        http://127.0.0.1:{port}/monitor")
    print(f"  → Painel:         http://127.0.0.1:{port}/panel")
    print(f"  → API backend:    {API_BASE} (timeout={API_TIMEOUT}s)")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
