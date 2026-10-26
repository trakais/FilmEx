"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from filmex.services import exiftool_locator as locator

router = APIRouter()


@router.get("/api/health")
def health() -> dict:
    loc = locator.locate()
    return {
        "status": "ok",
        "exiftool_available": loc is not None,
        "exiftool_path": loc.path if loc else None,
        "exiftool_source": loc.source if loc else None,
        "exiftool_version": locator.version() if loc else None,
    }
