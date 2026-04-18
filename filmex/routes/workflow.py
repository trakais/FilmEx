"""Workflow endpoints: folder scan, dry-run, apply, manifest read.

The business logic lives in `filmex.services.*`. These routes only hydrate
DB snapshots, build the WriteIntent + WriteContext, and fan out to the
service layer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import asdict, replace
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session

from filmex.db import get_session
from filmex.models import (
    Camera,
    DevelopmentMethod,
    FilmStock,
    Lens,
    Provider,
    ScanMethod,
)
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
from filmex.services import exiftool as et
from filmex.services import folder_scan as fs
from filmex.services import keywords as kw
from filmex.services import manifest as mf
from filmex.services import previews as pv
from filmex.services.exiftool_runner import (
    apply_intent,
    exiftool_available,
    read_tags,
)

router = APIRouter()


class FolderPickResponse(BaseModel):
    folder: Optional[str] = None
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
    ctx = _build_pipeline(req, session)
    diffs: list[FileDiff] = []
    for file_path in ctx.files:
        existing = _read_existing_tags(file_path)
        changes = et.diff(existing, _intent_for_file(ctx, file_path.name))
        diffs.append(
            FileDiff(
                name=file_path.name,
                changes=[
                    TagChangeOut(tag=c.tag, action=c.action, before=c.before, after=c.after)
                    for c in changes
                ],
            )
        )
    return DryRunResponse(folder=str(ctx.folder), roll_id=ctx.roll_id, diffs=diffs)


def _run_apply(ctx: "_Pipeline"):
    """Generator: yields (file_name, ok, error_msg, changes) per file, then finalizes.

    Shared by the JSON and streaming endpoints. The caller decides how to surface
    progress; manifest writes happen once at the end regardless.
    """
    applied: list[str] = []
    errors: list[str] = []
    all_diffs: list[dict] = []

    for file_path in ctx.files:
        existing = _read_existing_tags(file_path)
        intent = _intent_for_file(ctx, file_path.name)
        changes = et.diff(existing, intent)
        result = apply_intent(file_path, intent)
        if result.returncode != 0:
            err = f"{file_path.name}: {result.stderr.decode('utf-8', 'replace').strip()}"
            errors.append(err)
            yield {"type": "progress", "file": file_path.name, "ok": False, "error": err}
            continue
        applied.append(file_path.name)
        all_diffs.append({"file": file_path.name, "changes": [asdict(c) for c in changes]})
        yield {"type": "progress", "file": file_path.name, "ok": True}

    existing_manifest = mf.read_manifest(ctx.folder)
    fresh = mf.build_new_manifest(
        folder=ctx.folder,
        roll_id=ctx.roll_id,
        source_kind=ctx.source_kind,
        scan_tier=ctx.scan_tier,
        camera=ctx.camera_snapshot,
        lens=ctx.lens_snapshot,
        film=ctx.film_snapshot,
        development_provider=ctx.dev_provider_snapshot,
        development_method=ctx.dev_method_snapshot,
        scan_provider=ctx.scan_provider_snapshot,
        scan_method=ctx.scan_method_snapshot,
        capture_date_value=ctx.capture_date_value,
        capture_date_precision=ctx.capture_date_precision,
        capture_date_source=ctx.capture_date_source,
        notes=ctx.roll_notes,
    )
    merged = mf.merge_existing_manifest(existing_manifest, fresh) if existing_manifest else fresh
    mf.set_file_overrides(
        merged,
        extra_keywords_by_file=ctx.file_keywords_by_name,
        lens_by_file={
            name: mf.GearSnapshot(
                id=lens.id,
                make=lens.make,
                model=lens.model,
                display_name=lens.display_name,
                mount=lens.mount,
                lens_model_write_value=lens.lens_model_write_value,
            )
            for name, lens in ctx.file_lens_by_name.items()
        },
    )
    mf.append_apply_history(merged, applied_files=applied, diffs=all_diffs, dry_run=False)
    manifest_path = mf.write_manifest(ctx.folder, merged)

    yield {
        "type": "done",
        "folder": str(ctx.folder),
        "roll_id": ctx.roll_id,
        "manifest_path": str(manifest_path),
        "applied_files": applied,
        "errors": errors,
    }


@router.post("/api/metadata/apply", response_model=ApplyResponse)
def metadata_apply(
    req: ApplyRequest,
    session: Session = Depends(get_session),
) -> ApplyResponse:
    if not exiftool_available():
        raise HTTPException(status_code=503, detail="exiftool not found on PATH")

    ctx = _build_pipeline(req, session)
    final = None
    for event in _run_apply(ctx):
        if event["type"] == "done":
            final = event
    assert final is not None
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
    if not exiftool_available():
        raise HTTPException(status_code=503, detail="exiftool not found on PATH")

    ctx = _build_pipeline(req, session)
    total = len(ctx.files)

    def gen():
        yield json.dumps({"type": "start", "total": total}) + "\n"
        for event in _run_apply(ctx):
            yield json.dumps(event) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


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


def _resolve_folder_file(folder: str, name: str) -> Path:
    folder_path = Path(folder).expanduser().resolve()
    if not folder_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Folder not found: {folder_path}")
    if Path(name).name != name:
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

_READ_TAGS = ["Make", "Model", "LensModel", "Subject", "Keywords", "HierarchicalSubject"]
_READ_TAGS += ["Description", "Caption-Abstract"]


def _read_existing_tags(file_path: Path) -> dict:
    """Read tags for a dry-run/apply diff.

    For raw files we read the XMP sidecar rather than the raw so the "before"
    state reflects the managed metadata layer, not the original digital-camera
    EXIF inside the raw (which we never touch).
    """
    if not exiftool_available():
        return {}
    if et.detect_mode(file_path) == "sidecar":
        # Tolerate legacy `photo.arw.xmp` alongside the canonical `photo.xmp`
        # so the diff view matches whatever actually exists on disk.
        sidecar = et.find_existing_sidecar(file_path)
        if sidecar is None:
            return {}
        return read_tags(sidecar, _READ_TAGS)
    return read_tags(file_path, _READ_TAGS)


class _Pipeline:
    """Resolved inputs needed by both dry-run and apply."""

    def __init__(
        self,
        *,
        folder: Path,
        roll_id: str,
        source_kind: str,
        scan_tier: str,
        files: list[Path],
        intent: et.WriteIntent,
        camera_snapshot: mf.GearSnapshot,
        lens_snapshot: mf.GearSnapshot,
        film_snapshot: mf.FilmSnapshot,
        dev_provider_snapshot: mf.NamedSnapshot,
        dev_method_snapshot: mf.NamedSnapshot,
        scan_provider_snapshot: mf.NamedSnapshot,
        scan_method_snapshot: mf.NamedSnapshot,
        capture_date_value: Optional[str],
        capture_date_precision: str,
        capture_date_source: Optional[str],
        file_keywords_by_name: dict[str, list[str]],
        file_lens_by_name: dict[str, Lens],
        keyword_context: kw.WriteContext,
        camera_make: Optional[str],
        camera_model: Optional[str],
        capture_date_exif: Optional[str],
        roll_notes: Optional[str],
    ) -> None:
        self.folder = folder
        self.roll_id = roll_id
        self.source_kind = source_kind
        self.scan_tier = scan_tier
        self.files = files
        self.intent = intent
        self.camera_snapshot = camera_snapshot
        self.lens_snapshot = lens_snapshot
        self.film_snapshot = film_snapshot
        self.dev_provider_snapshot = dev_provider_snapshot
        self.dev_method_snapshot = dev_method_snapshot
        self.scan_provider_snapshot = scan_provider_snapshot
        self.scan_method_snapshot = scan_method_snapshot
        self.capture_date_value = capture_date_value
        self.capture_date_precision = capture_date_precision
        self.capture_date_source = capture_date_source
        self.file_keywords_by_name = file_keywords_by_name
        self.file_lens_by_name = file_lens_by_name
        self.keyword_context = keyword_context
        self.camera_make = camera_make
        self.camera_model = camera_model
        self.capture_date_exif = capture_date_exif
        self.roll_notes = roll_notes


def _build_pipeline(req: DryRunRequest, session: Session) -> _Pipeline:
    try:
        scan = fs.scan_folder(req.folder)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    # All three of camera/lens/film are optional. The user may only know one
    # piece of context for a given roll — tagging just that is still useful.
    camera: Optional[Camera] = None
    if req.camera_id:
        camera = session.get(Camera, req.camera_id)
        if camera is None:
            raise HTTPException(status_code=404, detail="Camera not found")

    lens: Optional[Lens] = None
    if camera and camera.fixed_lens_id:
        lens = session.get(Lens, camera.fixed_lens_id)
    elif req.lens_id:
        lens = session.get(Lens, req.lens_id)
        if lens is None:
            raise HTTPException(status_code=404, detail="Lens not found")

    film: Optional[FilmStock] = None
    if req.film_stock_id:
        film = session.get(FilmStock, req.film_stock_id)
        if film is None:
            raise HTTPException(status_code=404, detail="Film stock not found")

    dev_provider = _maybe(session, Provider, req.development_provider_id, "Development provider")
    dev_method = _maybe(
        session, DevelopmentMethod, req.development_method_id, "Development method"
    )
    scan_provider = _maybe(session, Provider, req.scan_provider_id, "Scan provider")
    scan_method = _maybe(session, ScanMethod, req.scan_method_id, "Scan method")

    _require_provider_kind(dev_provider, "development", "Development provider")
    _require_provider_kind(scan_provider, "scan", "Scan provider")

    ctx_kw = kw.WriteContext(
        roll_id=scan.roll_id,
        film_display_name=film.display_name if film else None,
        film_type=film.film_type if film else None,
        camera_display_name=camera.display_name if camera else None,
        lens_display_name=lens.display_name if lens else None,
        development_provider_name=dev_provider.name if dev_provider else None,
        development_method_name=dev_method.name if dev_method else None,
        scan_provider_name=scan_provider.name if scan_provider else None,
        scan_method_name=scan_method.name if scan_method else None,
        extra_keywords=list(req.extra_keywords),
    )
    file_keywords_by_name = _normalize_file_keywords(req.file_keywords, [f.name for f in scan.files])
    file_lens_by_name = _normalize_file_lenses(
        session,
        req.file_lens_ids,
        [f.name for f in scan.files],
        camera,
    )

    capture_date_value = _normalize_roll_date(req.roll_date)
    capture_date_precision = "day" if capture_date_value else "unknown"
    capture_date_source = "user_override" if capture_date_value else None
    exif_date = _exif_date(capture_date_value)
    roll_notes = _normalize_roll_notes(req.roll_notes)

    files = [scan.folder / f.name for f in scan.files]

    return _Pipeline(
        folder=scan.folder,
        roll_id=scan.roll_id,
        source_kind=scan.source_kind,
        scan_tier=scan.scan_tier,
        files=files,
        intent=et.WriteIntent(),
        camera_snapshot=mf.GearSnapshot(
            id=camera.id if camera else None,
            make=camera.make if camera else None,
            model=camera.model if camera else None,
            display_name=camera.display_name if camera else None,
            mount=camera.mount if camera else None,
        ),
        lens_snapshot=mf.GearSnapshot(
            id=lens.id if lens else None,
            make=lens.make if lens else None,
            model=lens.model if lens else None,
            display_name=lens.display_name if lens else None,
            mount=lens.mount if lens else None,
            lens_model_write_value=lens.lens_model_write_value if lens else None,
        ),
        film_snapshot=mf.FilmSnapshot(
            id=film.id if film else None,
            brand=film.brand if film else None,
            display_name=film.display_name if film else None,
            box_speed_iso=film.box_speed_iso if film else None,
            film_type=film.film_type if film else None,
            process_type=film.process_type if film else None,
        ),
        dev_provider_snapshot=_named(dev_provider),
        dev_method_snapshot=_named(dev_method),
        scan_provider_snapshot=_named(scan_provider),
        scan_method_snapshot=_named(scan_method),
        capture_date_value=capture_date_value,
        capture_date_precision=capture_date_precision,
        capture_date_source=capture_date_source,
        file_keywords_by_name=file_keywords_by_name,
        file_lens_by_name=file_lens_by_name,
        keyword_context=ctx_kw,
        camera_make=camera.make if camera else None,
        camera_model=camera.model if camera else None,
        capture_date_exif=exif_date,
        roll_notes=roll_notes,
    )


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize_roll_date(raw: Optional[str]) -> Optional[str]:
    """Validate `YYYY-MM-DD`; reject anything else so garbage never hits exiftool."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if not _ISO_DATE_RE.match(s):
        raise HTTPException(status_code=400, detail=f"roll_date must be YYYY-MM-DD (got {s!r})")
    return s


def _exif_date(iso_date: Optional[str]) -> Optional[str]:
    """Convert `YYYY-MM-DD` -> `YYYY:MM:DD 12:00:00` (noon local, day-precision proxy)."""
    if iso_date is None:
        return None
    return iso_date.replace("-", ":") + " 12:00:00"


def _maybe(session: Session, model, entity_id: Optional[str], label: str):
    if not entity_id:
        return None
    row = session.get(model, entity_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return row


def _require_provider_kind(row: Optional[Provider], kind: str, label: str) -> None:
    if row is None or row.has_kind(kind):
        return
    raise HTTPException(status_code=400, detail=f"{label} must support {kind}")


def _named(row) -> mf.NamedSnapshot:
    if row is None:
        return mf.NamedSnapshot()
    return mf.NamedSnapshot(id=row.id, name=row.name)


def _normalize_file_keywords(
    raw: dict[str, list[str]],
    valid_file_names: list[str],
) -> dict[str, list[str]]:
    valid = set(valid_file_names)
    out: dict[str, list[str]] = {}
    for name, keywords in (raw or {}).items():
        if name not in valid:
            raise HTTPException(status_code=400, detail=f"Unknown file in file_keywords: {name}")
        cleaned = [kw.strip() for kw in keywords if kw and kw.strip()]
        if cleaned:
            out[name] = cleaned
    return out


def _normalize_roll_notes(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None


def _lens_compatible_with_camera(camera: Optional[Camera], lens: Lens) -> bool:
    if camera is None:
        return True
    if camera.fixed_lens_id:
        return lens.id == camera.fixed_lens_id
    if camera.mount and lens.mount:
        return camera.mount == lens.mount
    return True


def _normalize_file_lenses(
    session: Session,
    raw: dict[str, str],
    valid_file_names: list[str],
    camera: Optional[Camera],
) -> dict[str, Lens]:
    valid = set(valid_file_names)
    out: dict[str, Lens] = {}
    for name, lens_id in (raw or {}).items():
        if name not in valid:
            raise HTTPException(status_code=400, detail=f"Unknown file in file_lens_ids: {name}")
        if not lens_id:
            continue
        lens = session.get(Lens, lens_id)
        if lens is None:
            raise HTTPException(status_code=404, detail=f"Lens not found for file {name}")
        if not _lens_compatible_with_camera(camera, lens):
            raise HTTPException(status_code=400, detail=f"Lens override is not compatible with the selected camera for file {name}")
        out[name] = lens
    return out


def _intent_for_file(ctx: _Pipeline, file_name: str) -> et.WriteIntent:
    lens = ctx.file_lens_by_name.get(file_name)
    file_kw_ctx = replace(
        ctx.keyword_context,
        lens_display_name=lens.display_name if lens else ctx.keyword_context.lens_display_name,
    )
    hierarchical = kw.build_hierarchical_paths(file_kw_ctx)
    flat = kw.leaves_of(hierarchical)
    extra_keywords = ctx.file_keywords_by_name.get(file_name, [])
    if extra_keywords:
        hierarchical = kw.merge_hierarchical(hierarchical, extra_keywords)
        flat = kw.merge_flat(flat, kw.leaves_of(extra_keywords))
    return et.WriteIntent(
        camera_make=ctx.camera_make,
        camera_model=ctx.camera_model,
        lens_model=(lens.lens_model_write_value if lens else ctx.lens_snapshot.lens_model_write_value),
        hierarchical_keywords=hierarchical,
        flat_keywords=flat,
        capture_date=ctx.capture_date_exif,
        description=ctx.roll_notes,
    )
