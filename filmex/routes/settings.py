"""Application settings endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from filmex.db import get_session
from filmex.services import settings as settings_service

router = APIRouter()


@router.get("/api/settings")
def read_settings(session: Session = Depends(get_session)) -> dict[str, Any]:
    return settings_service.get_all(session)


@router.patch("/api/settings")
def patch_settings(
    patch: dict[str, Any],
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        return settings_service.update(session, patch)
    except settings_service.SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
