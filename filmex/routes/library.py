"""Library endpoints: cached index, rescan job, folder details."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session

from filmex.db import get_engine, get_session
from filmex.services import frames as fr
from filmex.services import jobs
from filmex.services import library as lib
from filmex.services import library_cache as cache
from filmex.services import settings as settings_service

router = APIRouter()


class RescanRequest(BaseModel):
    root: str | None = None  # defaults to the library_root setting


def _configured_root(session: Session) -> str | None:
    return settings_service.get(session, "library_root")


@router.get("/api/library")
def read_library(session: Session = Depends(get_session)) -> dict[str, Any]:
    root = _configured_root(session)
    if not root:
        return {
            "root": None,
            "reachable": False,
            "scanned_at": None,
            "stale": True,
            "folders": [],
            "summary": lib.summarize([]),
        }
    rows = cache.rows_for(session, root)
    scanned_at = cache.scanned_at(session, root)
    return {
        "root": root,
        "reachable": Path(root).expanduser().is_dir(),
        "scanned_at": scanned_at,
        "stale": scanned_at is None,
        "folders": rows,
        "summary": lib.summarize(rows),
    }


def _scan_job(job_ctx: jobs.JobContext, root: str) -> dict[str, Any]:
    def progress(count: int) -> None:
        if count % 10 == 0:
            job_ctx.emit({"type": "progress", "scanned": count})

    rows = lib.scan_library(root, progress=progress)
    with Session(get_engine()) as session:
        scanned_at = cache.replace(session, root, rows)
    done = {
        "type": "done",
        "root": root,
        "folders": len(rows),
        "scanned_at": scanned_at,
        "summary": lib.summarize(rows),
    }
    job_ctx.emit(done)
    return done


@router.post("/api/library/rescan")
def rescan_library(
    req: RescanRequest | None = None,
    session: Session = Depends(get_session),
) -> StreamingResponse:
    root = (req.root if req and req.root else None) or _configured_root(session)
    if not root:
        raise HTTPException(status_code=400, detail="No library root configured")
    if not Path(root).expanduser().is_dir():
        raise HTTPException(status_code=404, detail=f"Library root not reachable: {root}")

    job = jobs.get_manager().submit("library_scan", lambda ctx: _scan_job(ctx, root))

    def gen():
        yield json.dumps({"type": "start", "root": root, "job_id": job.id}) + "\n"
        for event in jobs.get_manager().follow(job.id):
            if event["type"] == jobs.STATUS_EVENT:
                if event["status"] == jobs.FAILED:
                    yield json.dumps({"type": "error", "error": event["error"]}) + "\n"
                continue
            yield json.dumps(event) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.get("/api/library/folder")
def read_library_folder(path: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Live details for one folder, including the untagged file names."""
    root = _configured_root(session)
    folder = Path(path).expanduser()
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail=f"Folder not found: {path}")
    if root and not lib.folder_is_under(root, folder):
        raise HTTPException(status_code=400, detail="Folder is outside the library root")
    parent = folder.parent.name if root and folder.parent != Path(root).expanduser() else None
    depth = 1 if root and folder.parent == Path(root).expanduser() else 2
    row = lib.inspect_folder(
        folder, depth=depth, parent=fr.display_name(parent) if parent else None
    )
    row.is_flat_year = depth == 1 and row.image_count > 0
    return row.to_dict()
