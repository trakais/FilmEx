"""Naming system endpoints: propose renames, apply them as a job, undo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session

from filmex.db import get_engine, get_session
from filmex.routes import jobs as job_routes
from filmex.services import fileops, importer, jobs, naming, operations, renames
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


@router.post("/api/naming/apply")
def apply_renames(req: ApplyRenamesRequest, session: Session = Depends(get_session)) -> StreamingResponse:
    if not req.items:
        raise HTTPException(status_code=400, detail="No renames selected")
    root = req.root or settings_service.get(session, "library_root")
    items = [item.model_dump() for item in req.items]

    def body(job_ctx: jobs.JobContext) -> dict[str, Any]:
        with Session(get_engine()) as job_session:
            return renames.apply_renames(
                job_session, items, root=root, progress=job_ctx.emit, cancelled=job_ctx.cancelled
            )

    return job_routes.stream(jobs.get_manager().submit("rename", body, exclusive=True, start={"total": len(items)}))


_UNDO_BY_KIND = {
    "rename": renames.undo,
    "split": fileops.undo_split,
    "import": importer.undo_import,
}


class RenderRequest(BaseModel):
    roll_date: str | None = None
    camera_id: str | None = None
    film_stock_id: str | None = None
    lens_id: str | None = None
    place: str | None = None
    template: str | None = None


@router.post("/api/naming/render")
def render_name(req: RenderRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Folder name for a roll draft (contact sheet segments) from the template."""
    settings = settings_service.get_all(session)
    template = (req.template or settings["naming_template"] or naming.DEFAULT_TEMPLATE).strip()
    ctx = renames.context_from_ids(
        session,
        date=req.roll_date,
        camera_id=req.camera_id,
        film_stock_id=req.film_stock_id,
        lens_id=req.lens_id,
        place=req.place,
    )
    try:
        name = naming.render(template, ctx, place_suffix=bool(settings["naming_place_suffix"]))
    except naming.NamingError as exc:
        return {"name": None, "error": str(exc), "template": template}
    return {"name": name, "error": None, "template": template}


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
    undo_fn = _UNDO_BY_KIND.get(op.kind)
    if undo_fn is None:
        raise HTTPException(status_code=400, detail=f"Cannot undo operations of kind {op.kind}")

    def body(job_ctx: jobs.JobContext) -> dict[str, Any]:
        with Session(get_engine()) as job_session:
            return undo_fn(job_session, op_id, progress=job_ctx.emit, cancelled=job_ctx.cancelled)

    return job_routes.stream(jobs.get_manager().submit("undo", body, exclusive=True, start={"operation_id": op_id}))
