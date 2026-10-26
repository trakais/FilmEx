"""Naming system endpoints: propose renames, apply them as a job, undo."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session

from filmex.db import get_engine, get_session
from filmex.services import jobs, naming, operations, renames
from filmex.services import settings as settings_service

router = APIRouter()


class ProposeRequest(BaseModel):
    root: str | None = None
    template: str | None = None


class RenameItem(BaseModel):
    path: str
    proposed: str


class ApplyRenamesRequest(BaseModel):
    items: list[RenameItem] = Field(default_factory=list)
    root: str | None = None


def _root(session: Session, override: str | None) -> str:
    root = override or settings_service.get(session, "library_root")
    if not root:
        raise HTTPException(status_code=400, detail="No library root configured")
    if not Path(root).expanduser().is_dir():
        raise HTTPException(status_code=404, detail=f"Library root not reachable: {root}")
    return root


@router.post("/api/naming/propose")
def propose_renames(req: ProposeRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    root = _root(session, req.root)
    settings = settings_service.get_all(session)
    template = (req.template or settings["naming_template"] or naming.DEFAULT_TEMPLATE).strip()
    try:
        proposals = renames.propose(
            session, root, template=template, place_suffix=bool(settings["naming_place_suffix"])
        )
    except naming.NamingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    counts: dict[str, int] = {}
    for p in proposals:
        counts[p.status] = counts.get(p.status, 0) + 1
    return {
        "root": root,
        "template": template,
        "proposals": [p.to_dict() for p in proposals],
        "counts": counts,
    }


def _stream_job(job: jobs.Job, first: dict[str, Any]) -> StreamingResponse:
    def gen():
        yield json.dumps({**first, "job_id": job.id}) + "\n"
        for event in jobs.get_manager().follow(job.id):
            if event["type"] == jobs.STATUS_EVENT:
                if event["status"] == jobs.FAILED:
                    yield json.dumps({"type": "error", "error": event["error"]}) + "\n"
                continue
            yield json.dumps(event) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post("/api/naming/apply")
def apply_renames(req: ApplyRenamesRequest, session: Session = Depends(get_session)) -> StreamingResponse:
    if not req.items:
        raise HTTPException(status_code=400, detail="No renames selected")
    root = req.root or settings_service.get(session, "library_root")
    items = [item.model_dump() for item in req.items]

    def body(job_ctx: jobs.JobContext) -> dict[str, Any]:
        with Session(get_engine()) as job_session:
            result = renames.apply_renames(
                job_session, items, root=root, progress=job_ctx.emit, cancelled=job_ctx.cancelled
            )
        job_ctx.emit({"type": "done", **result})
        return result

    job = jobs.get_manager().submit("rename", body, exclusive=True)
    return _stream_job(job, {"type": "start", "total": len(items)})


@router.get("/api/operations")
def list_operations(kind: str | None = None, limit: int = 20, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return operations.recent(session, kind=kind, limit=limit)


@router.post("/api/operations/{op_id}/undo")
def undo_operation(op_id: str, session: Session = Depends(get_session)) -> StreamingResponse:
    op = operations.get(session, op_id)
    if op is None:
        raise HTTPException(status_code=404, detail="Unknown operation")
    if op.status == operations.UNDONE:
        raise HTTPException(status_code=409, detail="Operation already undone")
    if op.kind != "rename":
        raise HTTPException(status_code=400, detail=f"Cannot undo operations of kind {op.kind}")

    def body(job_ctx: jobs.JobContext) -> dict[str, Any]:
        with Session(get_engine()) as job_session:
            result = renames.undo(job_session, op_id, progress=job_ctx.emit, cancelled=job_ctx.cancelled)
        job_ctx.emit({"type": "done", **result})
        return result

    job = jobs.get_manager().submit("undo", body, exclusive=True)
    return _stream_job(job, {"type": "start", "operation_id": op_id})
