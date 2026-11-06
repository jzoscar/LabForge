from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from labforge import __version__
from labforge.api import router as api_router
from labforge.config import Settings
from labforge.db import make_engine
from labforge.monitor.sweep import Monitor
from labforge.web import router as web_router
from labforge.web_iac import router as iac_router

log = logging.getLogger("labforge")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.db_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.api_token:
            log.warning("LABFORGE_API_TOKEN is not set: API writes and agent reports are unauthenticated")
        task = asyncio.create_task(Monitor(engine, settings).run_forever()) if settings.monitor_enabled else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="LabForge", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.include_router(api_router, prefix="/api/v1")
    app.include_router(web_router)
    app.include_router(iac_router)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    return app
