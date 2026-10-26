"""FastAPI application factory.

Run locally:
    uvicorn filmex.main:app --reload --port 8765
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from filmex.db import get_engine, init_db
from filmex.routes import entities, health, settings, workflow
from filmex.services import settings as settings_service

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    with Session(get_engine()) as session:
        settings_service.apply_runtime_settings(session)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="FilmEx", version="0.1.0", lifespan=_lifespan)
    app.include_router(health.router)
    app.include_router(settings.router)
    app.include_router(entities.router)
    app.include_router(workflow.router)

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def _index() -> FileResponse:
            return FileResponse(_STATIC_DIR / "index.html")

    return app


app = create_app()
