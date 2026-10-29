"""Contact-sheet file operations: frames, split into rolls, inbox import.

Plans are synchronous so the UI can show the full operation list; applies
run as exclusive jobs and stream their events. Both re-plan right before
running, so a change on disk since the preview stops the batch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session

from filmex.db import get_engine, get_session
from filmex.routes import jobs as job_routes
from filmex.services import cuts, fileops, gather, importer, jobs, matching, renditions
from filmex.services import frames as fr
from filmex.services import settings as settings_service

router = APIRouter()


class RollTagging(BaseModel):
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
    # Per-file overrides keyed by file name; the names are the same in the
    # folder the roll ends up in, so the apply there validates them.
    file_keywords: dict[str, list[str]] = Field(default_factory=dict)
    file_lens_ids: dict[str, str] = Field(default_factory=dict)


class RollIn(BaseModel):
    name: str
    frames: list[str] = Field(default_factory=list)
    tagging: RollTagging = Field(default_factory=RollTagging)
    year: str | None = None

    def to_draft(self) -> fileops.RollDraft:
        return fileops.RollDraft(name=self.name, frames=list(self.frames), tagging=self.tagging.model_dump(), year=self.year)


class SplitRequest(BaseModel):
    folder: str
    rolls: list[RollIn] = Field(default_factory=list)


class ImportRequest(BaseModel):
    folder: str
    rolls: list[RollIn] = Field(default_factory=list)
    mode: str | None = None  # defaults to the import_mode setting


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
    data["roll_id"] = renditions.roll_id_for(folder)
    data["rendition"] = renditions.rendition_name(folder)
    data["sibling_renditions"] = [str(p) for p in renditions.sibling_renditions(folder)]
    return data


@router.get("/api/folder/cuts")
def folder_cuts(path: str, frames_per_roll: int | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Where the folder's frames probably change roll, from capture times,
    file-name counters and frame shapes. One ExifTool pass over the folder."""
    folder = Path(path).expanduser()
    try:
        listing = fr.list_frames(folder)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    rhythm = settings_service.get(session, "frames_per_roll") if frames_per_roll is None else frames_per_roll
    facts = cuts.frame_facts(folder, listing.frames)
    report = cuts.suggest_cuts(facts, frames_per_roll=max(0, rhythm))
    data = report.to_dict()
    data["facts"] = [f.to_dict() for f in facts]
    return data


# ---------- gather ----------


class GatherRequest(BaseModel):
    folder: str  # the roll folder or any of its renditions


@router.post("/api/folder/gather")
def gather_plan(req: GatherRequest) -> dict[str, Any]:
    try:
        return gather.plan_gather(req.folder).to_dict()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/folder/gather/apply")
def gather_apply(req: GatherRequest) -> StreamingResponse:
    try:
        plan = gather.plan_gather(req.folder)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not plan.ok:
        raise HTTPException(status_code=400, detail={"errors": plan.problems})
    folder = req.folder

    def body(job_ctx: jobs.JobContext) -> dict[str, Any]:
        with Session(get_engine()) as job_session:
            try:
                return gather.apply_gather(job_session, folder, progress=job_ctx.emit, cancelled=job_ctx.cancelled).to_dict()
            except fileops.PlanError as exc:
                raise RuntimeError("; ".join(exc.errors)) from exc

    start = {"total": plan.file_count, "renditions": len(plan.items)}
    return job_routes.stream(jobs.get_manager().submit("gather", body, exclusive=True, start=start))


# ---------- match lab scans to raws ----------


class MatchRequest(BaseModel):
    folder: str


class LinkPair(BaseModel):
    raw: str  # raw frame key
    lab: str  # lab frame key


class LinkRequest(BaseModel):
    folder: str
    links: list[LinkPair] = Field(default_factory=list)


@router.post("/api/folder/matches")
def folder_matches(req: MatchRequest) -> dict[str, Any]:
    """Which lab scan each raw frame shows, by picture. A proposal only:
    builds missing previews, changes nothing in the folder."""
    try:
        return matching.plan_matches(req.folder).to_dict()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/folder/link/apply")
def link_apply(req: LinkRequest) -> StreamingResponse:
    chosen = [pair.model_dump() for pair in req.links]
    try:
        links = matching.plan_links(req.folder, chosen)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except fileops.PlanError as exc:
        raise _plan_errors(exc) from exc
    folder = req.folder

    def body(job_ctx: jobs.JobContext) -> dict[str, Any]:
        with Session(get_engine()) as job_session:
            try:
                return matching.apply_links(job_session, folder, chosen, progress=job_ctx.emit, cancelled=job_ctx.cancelled).to_dict()
            except fileops.PlanError as exc:
                raise RuntimeError("; ".join(exc.errors)) from exc

    start = {"total": sum(len(link.moves) for link in links), "links": len(links)}
    return job_routes.stream(jobs.get_manager().submit("link", body, exclusive=True, start=start))


# ---------- split ----------


@router.post("/api/folder/split")
def split_plan(req: SplitRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    root = settings_service.get(session, "library_root")
    try:
        plan = fileops.plan_split(req.folder, [s.to_draft() for s in req.rolls], root=root)
    except fileops.PlanError as exc:
        raise _plan_errors(exc) from exc
    return plan.to_dict()


@router.post("/api/folder/split/apply")
def split_apply(req: SplitRequest, session: Session = Depends(get_session)) -> StreamingResponse:
    root = settings_service.get(session, "library_root")
    rolls = [s.to_draft() for s in req.rolls]
    try:
        plan = fileops.plan_split(req.folder, rolls, root=root)
    except fileops.PlanError as exc:
        raise _plan_errors(exc) from exc
    if not plan.ok:
        raise HTTPException(status_code=400, detail={"errors": plan.problems})
    folder = req.folder

    def body(job_ctx: jobs.JobContext) -> dict[str, Any]:
        with Session(get_engine()) as job_session:
            try:
                return fileops.apply_split(
                    job_session, folder, rolls, root=root, progress=job_ctx.emit, cancelled=job_ctx.cancelled
                ).to_dict()
            except fileops.PlanError as exc:
                raise RuntimeError("; ".join(exc.errors)) from exc

    start = {"total": sum(len(i.files) for i in plan.items), "rolls": len(plan.items)}
    return job_routes.stream(jobs.get_manager().submit("split", body, exclusive=True, start=start))


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
        plan = importer.plan_import(req.folder, [s.to_draft() for s in req.rolls], root=root, mode=mode)
    except fileops.PlanError as exc:
        raise _plan_errors(exc) from exc
    return plan.to_dict()


@router.post("/api/import/apply")
def import_apply(req: ImportRequest, session: Session = Depends(get_session)) -> StreamingResponse:
    root, mode = _import_inputs(req, session)
    rolls = [s.to_draft() for s in req.rolls]
    try:
        plan = importer.plan_import(req.folder, rolls, root=root, mode=mode)
    except fileops.PlanError as exc:
        raise _plan_errors(exc) from exc
    if not plan.ok:
        raise HTTPException(status_code=400, detail={"errors": plan.problems})
    folder = req.folder

    def body(job_ctx: jobs.JobContext) -> dict[str, Any]:
        with Session(get_engine()) as job_session:
            try:
                return importer.apply_import(
                    job_session, folder, rolls, root=root, mode=mode,
                    progress=job_ctx.emit, cancelled=job_ctx.cancelled,
                ).to_dict()
            except fileops.PlanError as exc:
                raise RuntimeError("; ".join(exc.errors)) from exc

    start = {"total": sum(len(i.files) for i in plan.items), "rolls": len(plan.items),
             "bytes_total": plan.total_bytes, "mode": mode}
    return job_routes.stream(jobs.get_manager().submit("import", body, exclusive=True, start=start))
