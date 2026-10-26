"""Contact-sheet file operations: frames, split into rolls, inbox import.

Plans are synchronous so the UI can show the full operation list; applies
run as exclusive jobs and stream their events. Both re-plan right before
running, so a change on disk since the preview stops the batch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session

from filmex.db import get_engine, get_session
from filmex.services import fileops, importer, jobs
from filmex.services import frames as fr
from filmex.services import settings as settings_service

router = APIRouter()


class SegmentTagging(BaseModel):
    """The ``DryRunRequest`` fields a roll draft carries (no folder, no files)."""

    camera_id: str | None = None
    lens_id: str | None = None
    film_stock_id: str | None = None
    development_provider_id: str | None = None
    development_method_id: str | None = None
    scan_provider_id: str | None = None
    scan_method_id: str | None = None
    extra_keywords: list[str] = Field(default_factory=list)
    roll_date: str | None = None
    roll_notes: str | None = None
    place: str | None = None
    replace_managed_metadata: bool = False


class SegmentIn(BaseModel):
    name: str
    frames: list[str] = Field(default_factory=list)
    tagging: SegmentTagging = Field(default_factory=SegmentTagging)
    year: str | None = None

    def to_segment(self) -> fileops.Segment:
        return fileops.Segment(name=self.name, frames=list(self.frames), tagging=self.tagging.model_dump(), year=self.year)


class SplitRequest(BaseModel):
    folder: str
    segments: list[SegmentIn] = Field(default_factory=list)


class ImportRequest(BaseModel):
    folder: str
    segments: list[SegmentIn] = Field(default_factory=list)
    mode: str | None = None  # defaults to the import_mode setting


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


def _plan_errors(exc: fileops.PlanError) -> HTTPException:
    return HTTPException(status_code=400, detail={"errors": exc.errors})


# ---------- frames ----------


@router.get("/api/folder/frames")
def folder_frames(path: str) -> dict[str, Any]:
    folder = Path(path).expanduser()
    try:
        listing = fr.list_frames(folder)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    data = listing.to_dict()
    data["suggested_roll_date"] = fr.earliest_file_date(folder, listing.frames)
    return data


# ---------- split ----------


@router.post("/api/folder/split")
def split_plan(req: SplitRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    root = settings_service.get(session, "library_root")
    try:
        plan = fileops.plan_split(req.folder, [s.to_segment() for s in req.segments], root=root)
    except fileops.PlanError as exc:
        raise _plan_errors(exc) from exc
    return plan.to_dict()


@router.post("/api/folder/split/apply")
def split_apply(req: SplitRequest, session: Session = Depends(get_session)) -> StreamingResponse:
    root = settings_service.get(session, "library_root")
    segments = [s.to_segment() for s in req.segments]
    try:
        plan = fileops.plan_split(req.folder, segments, root=root)
    except fileops.PlanError as exc:
        raise _plan_errors(exc) from exc
    if not plan.ok:
        raise HTTPException(status_code=400, detail={"errors": plan.errors + [f"{i.name}: {e}" for i in plan.items for e in i.errors]})
    folder = req.folder

    def body(job_ctx: jobs.JobContext) -> dict[str, Any]:
        with Session(get_engine()) as job_session:
            try:
                result = fileops.apply_split(
                    job_session, folder, segments, root=root, progress=job_ctx.emit, cancelled=job_ctx.cancelled
                )
            except fileops.PlanError as exc:
                raise RuntimeError("; ".join(exc.errors)) from exc
        job_ctx.emit({"type": "done", **result})
        return result

    job = jobs.get_manager().submit("split", body, exclusive=True)
    return _stream_job(job, {"type": "start", "total": sum(len(i.files) for i in plan.items), "rolls": len(plan.items)})


# ---------- import ----------


def _import_inputs(req: ImportRequest, session: Session) -> tuple[str | None, str]:
    settings = settings_service.get_all(session)
    mode = req.mode or settings["import_mode"]
    if mode not in importer.MODES:
        raise HTTPException(status_code=400, detail={"errors": [f"unknown import mode: {mode}"]})
    return settings["library_root"], mode


@router.post("/api/import/plan")
def import_plan(req: ImportRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    root, mode = _import_inputs(req, session)
    try:
        plan = importer.plan_import(req.folder, [s.to_segment() for s in req.segments], root=root, mode=mode)
    except fileops.PlanError as exc:
        raise _plan_errors(exc) from exc
    return plan.to_dict()


@router.post("/api/import/apply")
def import_apply(req: ImportRequest, session: Session = Depends(get_session)) -> StreamingResponse:
    root, mode = _import_inputs(req, session)
    segments = [s.to_segment() for s in req.segments]
    try:
        plan = importer.plan_import(req.folder, segments, root=root, mode=mode)
    except fileops.PlanError as exc:
        raise _plan_errors(exc) from exc
    if not plan.ok:
        raise HTTPException(status_code=400, detail={"errors": plan.errors + [f"{i.name}: {e}" for i in plan.items for e in i.errors]})
    folder = req.folder

    def body(job_ctx: jobs.JobContext) -> dict[str, Any]:
        with Session(get_engine()) as job_session:
            try:
                result = importer.apply_import(
                    job_session, folder, segments, root=root, mode=mode,
                    progress=job_ctx.emit, cancelled=job_ctx.cancelled,
                )
            except fileops.PlanError as exc:
                raise RuntimeError("; ".join(exc.errors)) from exc
        job_ctx.emit({"type": "done", **result})
        return result

    job = jobs.get_manager().submit("import", body, exclusive=True)
    return _stream_job(
        job,
        {"type": "start", "total": sum(len(i.files) for i in plan.items), "rolls": len(plan.items),
         "bytes_total": plan.total_bytes, "mode": mode},
    )
