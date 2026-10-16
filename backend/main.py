"""Aplicação FastAPI. Executar:  python -m backend.main

Sobe desconectado; conectar e varrer é feito pelo painel (ou pela API REST).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import config
from .api import router, session_error_handler, source_error_handler
from .hub import Hub
from .session import Session, SessionError
from .sources.base import LidarSource, SourceError
from .sources.factory import create_source_for_port

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class NoCacheStaticFiles(StaticFiles):
    """Sempre revalida (ETag): sem isso o navegador pode reusar JS/HTML antigo logo após uma mudança."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(source_for_port: Callable[[str], LidarSource] = create_source_for_port) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub = Hub()
        session = Session(hub, source_for_port=source_for_port)
        app.state.hub, app.state.session = hub, session
        await hub.start()
        try:
            yield
        finally:
            logging.getLogger("lidar").info("encerrando: parando varredura e fechando a fonte")
            await asyncio.to_thread(session.shutdown)   # o join da thread não trava o loop
            await hub.stop()

    app = FastAPI(title="Lidar 2D", lifespan=lifespan)
    app.add_exception_handler(SessionError, session_error_handler)
    app.add_exception_handler(SourceError, source_error_handler)
    app.include_router(router)
    app.mount("/", NoCacheStaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")   # por último
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host=config.HOST, port=config.PORT)
