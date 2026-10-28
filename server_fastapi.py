"""server_fastapi.py — Ponte FastAPI para o servidor Flask legado (v1.0)"""
from fastapi import FastAPI
from fastapi.middleware.wsgi import WSGIMiddleware
from server import app as flask_app
import logging

log = logging.getLogger("fastapi_bridge")

# Cria a aplicação FastAPI principal
app = FastAPI(
    title="BRN Node API",
    description="API de produção BRN (FastAPI + Uvicorn + Flask WSGI)",
    version="9.9.18"
)

# Envolve todo o Flask existente no FastAPI.
# Tudo que ia para o Flask agora passa pelo Uvicorn primeiro.
app.mount("/", WSGIMiddleware(flask_app))

log.info("FastAPI Bridge inicializado. Flask montado em '/'")
