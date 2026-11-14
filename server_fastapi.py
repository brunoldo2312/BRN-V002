"""server_fastapi.py — Ponte FastAPI para o servidor Flask legado (v13.9.20)
================================================================================
v13.9.20 (2026-11-13) — [NOVO] Rotas de carteira com peer_id único no caminho.
  - GET /wallet            → redireciona 307 pra /wallet/<peer_id-deste-nó>
  - GET /wallet/{peer_id}  → serve a carteira, valida que é ESTE peer
  - GET /peers             → menu HTML com os peers conhecidos
  Essas rotas ficam ANTES do mount do Flask — interceptam primeiro.
  O PEER_ID vem do main.py via os.environ["BRN_PEER_ID"].
  O restante das rotas continua indo pro Flask (compatibilidade total).
================================================================================
"""
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.wsgi import WSGIMiddleware
from server import app as flask_app
from pathlib import Path
import hashlib
import logging
import os

log = logging.getLogger("fastapi_bridge")

# Cria a aplicação FastAPI principal
app = FastAPI(
    title="BRN Node API",
    description="API de produção BRN (FastAPI + Uvicorn + Flask WSGI)",
    version="13.9.20",
)


# ============================================================
# Helpers
# ============================================================

def _peer_id() -> str:
    """Lê o PEER_ID injetado pelo main.py via env."""
    return (os.environ.get("BRN_PEER_ID") or "").strip()


def _wallet_html_path() -> Path:
    """Localiza o HTML da carteira no disco."""
    base = Path(__file__).resolve().parent
    for name in ("index_wallet.html", "panel.html"):
        p = base / name
        if p.exists():
            return p
    return None


def _valid_peer_id(pid: str) -> bool:
    return (
        isinstance(pid, str)
        and len(pid) == 16
        and all(c in "0123456789abcdef" for c in pid)
    )


# ============================================================
# ROTAS DE CARTEIRA — Solução 2 (caminho único por peer)
# Ficam ANTES do mount do Flask para interceptar.
# ============================================================

@app.get("/wallet")
def wallet_root():
    """
    Quem abrir /wallet cai na carteira DESTE nó.
    Mantém compatibilidade com GUI/scripts antigos (que só chamam /wallet).
    """
    pid = _peer_id()
    if not pid:
        # Se main.py ainda não injetou (boot em andamento),
        # deixa o Flask responder (ele serve a página vazia padrão).
        raise HTTPException(
            status_code=503,
            detail="peer_id não inicializado (main.py ainda não injetou)",
        )
    return RedirectResponse(url=f"/wallet/{pid}", status_code=307)


@app.get("/wallet/{peer_id}", response_class=HTMLResponse)
def wallet_by_peer(peer_id: str):
    """
    Abre a carteira DO PEER indicado no caminho.
    Só funciona se peer_id == ID deste nó (política estrita).
    """
    # 1) valida formato
    if not _valid_peer_id(peer_id):
        raise HTTPException(
            status_code=400,
            detail="peer_id inválido (16 hex chars)",
        )

    # 2) valida que é este peer
    meu = _peer_id()
    if not meu:
        raise HTTPException(
            status_code=503,
            detail="peer_id não inicializado (main.py ainda não injetou)",
        )
    if peer_id != meu:
        raise HTTPException(
            status_code=404,
            detail=f"peer_id {peer_id} não é deste nó (é {meu})",
        )

    # 3) lê o HTML da carteira do disco
    path = _wallet_html_path()
    if path is None:
        raise HTTPException(
            status_code=500,
            detail="index_wallet.html (ou panel.html) não encontrado",
        )
    html = path.read_text(encoding="utf-8", errors="replace")

    # 4) injeta o peer_id como meta tag (útil pro JS saber de quem é)
    if "</head>" in html:
        html = html.replace(
            "</head>",
            f'<meta name="brn-peer-id" content="{peer_id}">\n</head>',
            1,
        )

    return HTMLResponse(html)


@app.get("/peers", response_class=HTMLResponse)
def peers_menu():
    """
    Menu HTML com os peers conhecidos — prático para abrir no celular.
    Sempre mostra o peer local; se houver peers no DB, adiciona.
    """
    meu = _peer_id()
    if not meu:
        return HTMLResponse(
            "<h1>peer_id não inicializado</h1>"
            "<p>O main.py ainda não injetou BRN_PEER_ID.</p>",
            status_code=503,
        )

    peers = [{"id": meu, "url": f"/wallet/{meu}", "local": True}]

    # Tenta ler peers remotos do Flask (que tem a chain global)
    try:
        from server import chain as _chain  # noqa: F401
        if _chain is not None:
            for p in _chain.db.listar_peers(apenas_ativos=False) or []:
                pub = (p.get("pubkey") or p.get("pub") or "").strip()
                if not pub:
                    continue
                try:
                    pid = hashlib.sha256(bytes.fromhex(pub)).hexdigest()[:16]
                except ValueError:
                    continue
                if pid != meu:
                    peers.append(
                        {"id": pid, "url": f"/wallet/{pid}", "local": False}
                    )
    except Exception as e:
        log.debug("[peers] não consegui ler peers do Flask: %s", e)

    linhas = "".join(
        f'<li><a href="{p["url"]}">{p["id"]}</a>'
        f'{" 🏠 (este nó)" if p["local"] else ""}</li>'
        for p in peers
    )
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Peers BRN</title>"
        "<style>body{font-family:sans-serif;padding:20px}"
        "li{margin:8px 0}a{color:#06c;text-decoration:none}"
        "a:hover{text-decoration:underline}</style></head><body>"
        "<h1>Peers BRN</h1>"
        f"<ul>{linhas}</ul>"
        "<p><small>Total: " + str(len(peers)) + " peer(s)</small></p>"
        "</body></html>"
    )


# ============================================================
# FLASK (compatibilidade retroativa)
# ============================================================
# Envolve TODO o Flask existente.
# Qualquer rota que NÃO seja /wallet, /wallet/{id} ou /peers
# continua indo pro Flask como antes (API, /health, /explorer, etc.).
app.mount("/", WSGIMiddleware(flask_app))

log.info(
    "FastAPI Bridge inicializado. "
    "Flask montado em '/'. "
    "Rotas /wallet, /wallet/{peer_id} e /peers sobrescritas pelo FastAPI."
)