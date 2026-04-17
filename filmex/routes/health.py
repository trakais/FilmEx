"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from filmex.services.exiftool_runner import exiftool_available

router = APIRouter()


@router.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "exiftool_available": exiftool_available(),
    }
