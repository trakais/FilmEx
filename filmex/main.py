"""FastAPI application factory.

Run locally:
    uvicorn filmex.main:app --reload --port 8765
"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from filmex.db import get_engine, init_db
from filmex.routes import entities, health, jobs, library, settings, workflow
from filmex.security import TOKEN_COOKIE, TOKEN_QUERY_PARAM, LoopbackGuard
from filmex.services import jobs as job_service
from filmex.services import settings as settings_service

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    with Session(get_engine()) as session:
        settings_service.apply_runtime_settings(session)
    yield
    # Drop the job manager so a later startup (tests, restarts) gets a fresh
    # executor instead of one that has been shut down.
    job_service.reset_manager()


def create_app(token: str | None = None) -> FastAPI:
    """Build the app.

    `token` enables the per-launch session token used by the desktop shell.
    It defaults to the `FILMEX_TOKEN` environment variable so the launcher can
    set it before this module is imported.
    """
    if token is None:
        token = os.environ.get("FILMEX_TOKEN") or None

    app = FastAPI(title="FilmEx", version="0.1.0", lifespan=_lifespan)
    app.add_middleware(LoopbackGuard, token=token)
    app.include_router(health.router)
    app.include_router(settings.router)
    app.include_router(jobs.router)
    app.include_router(library.router)
    app.include_router(entities.router)
    app.include_router(workflow.router)

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def _index(request: Request):
            # Desktop shell: the window opens `/?token=...` once. Exchange it
            # for a SameSite=Strict cookie and strip it from the URL.
            offered = request.query_params.get(TOKEN_QUERY_PARAM)
            if token is not None and offered is not None and hmac.compare_digest(offered, token):
                response = RedirectResponse(url="/", status_code=303)
                response.set_cookie(
                    TOKEN_COOKIE,
                    token,
                    httponly=True,
                    samesite="strict",
                    path="/",
                )
                return response
            return FileResponse(_STATIC_DIR / "index.html")

    return app


app = create_app()
