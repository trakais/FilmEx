"""Workflow endpoints: folder scan, dry-run, apply, manifest read.

The business logic lives in `filmex.services.*`. These routes only hydrate
DB snapshots, build the WriteIntent + WriteContext, and fan out to the
service layer.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session

from filmex.db import get_session
from filmex.routes import jobs as job_routes
from filmex.schemas import (
    ApplyRequest,
    ApplyResponse,
    DryRunRequest,
    DryRunResponse,
    FileDiff,
    FileEntry,
    FolderScanRequest,
    FolderScanResponse,
    ManifestResponse,
    PreviewBuildRequest,
    PreviewBuildResponse,
    TagChangeOut,
)
from filmex.services import exiftool_runner as exiftool
from filmex.services import folder_scan as fs
from filmex.services import jobs
from filmex.services import manifest as mf
from filmex.services import previews as pv
from filmex.services import tagging as tg

router = APIRouter()


class FolderPickResponse(BaseModel):
    folder: str | None = None
    cancelled: bool = False


class FileOpenResponse(BaseModel):
    ok: bool = True


# Tk on macOS opens its window behind the browser unless something else
# brings the process to the front. Rather than fight that, on macOS we use
# AppleScript's native `choose folder` dialog, which is always frontmost and
# returns the POSIX path on stdout. Other platforms keep the tkinter
# subprocess approach which works fine there.
_FOLDER_PICKER_SCRIPT_TK = textwrap.dedent(
    """
    import sys, tkinter
    from tkinter.filedialog import askdirectory
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = askdirectory(title="Select a roll folder")
    root.destroy()
    sys.stdout.write(path or "")
    """
).strip()

# AppleScript: activate first so the dialog steals focus from the browser,
# then prompt. `with invisible` is intentionally omitted so the user actually
# sees a focused window. On cancel, AppleScript returns error -128, which we
# map to "cancelled" rather than treating as a failure.
_FOLDER_PICKER_APPLESCRIPT = textwrap.dedent(
    """
    tell application "System Events" to activate
    try
        set chosen to choose folder with prompt "Select a roll folder"
        return POSIX path of chosen
    on error number -128
        return ""
    end try
    """
).strip()


@router.post("/api/folder/pick", response_model=FolderPickResponse)
def folder_pick() -> FolderPickResponse:
    """Open a native OS folder dialog and return the chosen absolute path."""
    if sys.platform == "darwin":
        cmd = ["osascript", "-e", _FOLDER_PICKER_APPLESCRIPT]
    else:
        cmd = [sys.executable, "-c", _FOLDER_PICKER_SCRIPT_TK]

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    except subprocess.TimeoutExpired as e:
        raise HTTPException(status_code=504, detail="Folder picker timed out") from e

    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip() or "folder picker failed"
        raise HTTPException(status_code=500, detail=msg)

    path = proc.stdout.decode("utf-8", "replace").strip()
    # AppleScript adds a trailing slash to folder paths; normalize so it
    # matches what the rest of the app produces.
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    if not path:
        return FolderPickResponse(cancelled=True)
    return FolderPickResponse(folder=path)


@router.post("/api/folder/scan", response_model=FolderScanResponse)
def folder_scan(req: FolderScanRequest) -> FolderScanResponse:
    try:
        result = fs.scan_folder(req.folder)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    return FolderScanResponse(
        folder=str(result.folder),
        roll_id=result.roll_id,
        files=[
            FileEntry(
                name=f.name,
                long_edge_px=f.long_edge_px,
                has_thumbnail=f.has_thumbnail,
                has_inverted_preview=f.has_inverted_preview,
            )
            for f in result.files
        ],
        source_kind=result.source_kind,
        scan_tier=result.scan_tier,
        provider_hint=result.provider_hint,
        suggested_roll_date=result.suggested_roll_date,
    )


@router.post("/api/metadata/dry-run", response_model=DryRunResponse)
def metadata_dry_run(
    req: DryRunRequest,
    session: Session = Depends(get_session),
) -> DryRunResponse:
    plan = _tagging_plan(req, session)
    diffs = [
        FileDiff(
            name=name,
            changes=[TagChangeOut(tag=c.tag, action=c.action, before=c.before, after=c.after) for c in changes],
        )
        for name, changes in tg.preview_all(plan)
    ]
    return DryRunResponse(folder=str(plan.folder), roll_id=plan.roll_id, diffs=diffs)


def _submit_apply(plan: tg.TaggingPlan) -> jobs.Job:
    return jobs.get_manager().submit(
        "apply",
        lambda job_ctx: tg.apply_all(plan, progress=job_ctx.emit, cancelled=job_ctx.cancelled).to_dict(),
        exclusive=True,
        start={"total": len(plan.files)},
    )


@router.post("/api/metadata/apply", response_model=ApplyResponse)
def metadata_apply(
    req: ApplyRequest,
    session: Session = Depends(get_session),
) -> ApplyResponse:
    if not exiftool.available():
        raise HTTPException(status_code=503, detail="exiftool not found on PATH")

    plan = _tagging_plan(req, session)
    job = jobs.get_manager().wait(_submit_apply(plan).id)
    if job.status == jobs.FAILED:
        raise HTTPException(status_code=500, detail=job.error or "apply failed")
    final = job.result
    return ApplyResponse(
        folder=final["folder"],
        roll_id=final["roll_id"],
        manifest_path=final["manifest_path"],
        applied_files=final["applied_files"],
        errors=final["errors"],
    )


@router.post("/api/metadata/apply/stream")
def metadata_apply_stream(
    req: ApplyRequest,
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """NDJSON stream of per-file progress events followed by a `done` event.

    Used by the UI so the Apply button can show `3/12` as exiftool chews through
    the roll — important when files live on a slow NAS.
    """
    if not exiftool.available():
        raise HTTPException(status_code=503, detail="exiftool not found on PATH")

    return job_routes.stream(_submit_apply(_tagging_plan(req, session)))


@router.get("/api/manifests/{roll_id}", response_model=ManifestResponse)
def get_manifest(roll_id: str, folder: str) -> ManifestResponse:
    data = mf.read_manifest(folder)
    if data is None:
        raise HTTPException(status_code=404, detail=f"No manifest in {folder}")
    if data.get("roll_id") != roll_id:
        raise HTTPException(
            status_code=404,
            detail=f"Manifest roll_id {data.get('roll_id')!r} does not match {roll_id!r}",
        )
    return ManifestResponse(roll_id=roll_id, manifest=data)


@router.get("/api/manifest", response_model=ManifestResponse)
def get_manifest_by_folder(folder: str) -> ManifestResponse:
    data = mf.read_manifest(folder)
    if data is None:
        raise HTTPException(status_code=404, detail=f"No manifest in {folder}")
    roll_id = str(data.get("roll_id") or "")
    return ManifestResponse(roll_id=roll_id, manifest=data)


@router.get("/api/files/thumbnail")
def file_thumbnail(folder: str, name: str, invert: bool = False) -> FileResponse:
    target = _resolve_folder_file(folder, name)
    thumb = pv.existing_preview_path(target, invert=invert)
    if thumb is None:
        raise HTTPException(status_code=404, detail=f"No thumbnail for {name}")
    return FileResponse(thumb, media_type="image/jpeg")


@router.get("/api/files/source")
def file_source(folder: str, name: str) -> FileResponse:
    target = _resolve_folder_file(folder, name)
    return FileResponse(target)


@router.post("/api/files/open", response_model=FileOpenResponse)
def file_open(folder: str, name: str) -> FileOpenResponse:
    target = _resolve_folder_file(folder, name)
    try:
        _open_with_default_app(target)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return FileOpenResponse()


class FolderRevealRequest(BaseModel):
    folder: str


@router.post("/api/folder/reveal", response_model=FileOpenResponse)
def folder_reveal(req: FolderRevealRequest) -> FileOpenResponse:
    """Open a folder in the system file manager."""
    target = Path(req.folder).expanduser()
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Folder not found: {req.folder}")
    try:
        _open_with_default_app(target)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return FileOpenResponse()


@router.post("/api/previews/create", response_model=PreviewBuildResponse)
def create_previews(req: PreviewBuildRequest) -> PreviewBuildResponse:
    try:
        report = pv.build_previews(req.folder, invert=req.invert)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return PreviewBuildResponse(
        folder=req.folder,
        created_files=report.created_files,
        reused_files=report.reused_files,
        failed=report.failed,
    )


def _previews_job(job_ctx: jobs.JobContext, folder: str, targets: list[Path], invert: bool) -> dict:
    created_files: list[str] = []
    failed: list[str] = []
    cancelled = False
    for entry in targets:
        if job_ctx.cancelled():
            cancelled = True
            break
        try:
            _, created = pv.ensure_preview(entry, invert=invert)
            status = "created" if created else "reused"
            if created:
                created_files.append(entry.name)
            job_ctx.emit({"type": "progress", "file": entry.name, "status": status})
        except pv.PreviewUnavailable as e:
            msg = f"{entry.name}: {e}"
            failed.append(msg)
            job_ctx.emit({"type": "progress", "file": entry.name, "status": "failed", "error": msg})
    return {
        "folder": folder,
        "created_files": created_files,
        "failed": failed,
        "cancelled": cancelled,
    }


@router.post("/api/previews/create/stream")
def create_previews_stream(req: PreviewBuildRequest) -> StreamingResponse:
    try:
        targets = pv.missing_preview_entries(req.folder, invert=req.invert)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    job = jobs.get_manager().submit(
        "previews",
        lambda job_ctx: _previews_job(job_ctx, req.folder, targets, req.invert),
        start={"total": len(targets)},
    )
    return job_routes.stream(job)


def _resolve_folder_file(folder: str, name: str) -> Path:
    folder_path = Path(folder).expanduser().resolve()
    if not folder_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Folder not found: {folder_path}")
    parts = Path(name).parts
    if len(parts) != 1 or parts[0] in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="name must be a basename, not a path")

    target = (folder_path / name).resolve()
    if target.parent != folder_path or not target.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {name}")
    return target


def _open_with_default_app(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
        return
    if sys.platform.startswith("win"):
        os.startfile(str(path))
        return
    subprocess.Popen(["xdg-open", str(path)])


# ---- internals ----


def _tagging_plan(req: DryRunRequest, session: Session) -> tg.TaggingPlan:
    try:
        return tg.plan_tagging(req, session)
    except tg.TaggingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc



