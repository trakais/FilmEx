"""The tagging pipeline: resolve a request into snapshots and intents, then
dry-run or apply it file by file.

Shared by the Tag view's endpoints and by the split and import jobs, which
tag each new roll folder right after its files land there. Nothing here
knows about HTTP: validation failures raise ``TaggingError`` with a status
code the route layer maps onto its response.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from sqlmodel import Session

from filmex.models import (
    Camera,
    DevelopmentMethod,
    FilmStock,
    Lens,
    Provider,
    ScanMethod,
)
from filmex.schemas import DryRunRequest
from filmex.services import exiftool as et
from filmex.services import exiftool_runner as exiftool
from filmex.services import folder_scan as fs
from filmex.services import keywords as kw
from filmex.services import manifest as mf


class TaggingError(ValueError):
    """A request that cannot be turned into a pipeline (bad ids, bad folder)."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class TaggingPlan:
    """Resolved inputs needed by both dry-run and apply."""

    def __init__(
        self,
        *,
        folder: Path,
        roll_id: str,
        source_kind: str,
        scan_tier: str,
        files: list[Path],
        camera_snapshot: mf.GearSnapshot,
        lens_snapshot: mf.GearSnapshot,
        film_snapshot: mf.FilmSnapshot,
        dev_provider_snapshot: mf.NamedSnapshot,
        dev_method_snapshot: mf.NamedSnapshot,
        scan_provider_snapshot: mf.NamedSnapshot,
        scan_method_snapshot: mf.NamedSnapshot,
        capture_date_value: str | None,
        capture_date_precision: str,
        capture_date_source: str | None,
        file_keywords_by_name: dict[str, list[str]],
        file_lens_by_name: dict[str, Lens],
        keyword_context: kw.WriteContext,
        camera_make: str | None,
        camera_model: str | None,
        capture_date_exif: str | None,
        roll_notes: str | None,
        place: str | None,
        replace_managed_metadata: bool,
    ) -> None:
        self.folder = folder
        self.roll_id = roll_id
        self.source_kind = source_kind
        self.scan_tier = scan_tier
        self.files = files
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
        self.place = place
        self.replace_managed_metadata = replace_managed_metadata


def plan_tagging(req: DryRunRequest, session: Session) -> TaggingPlan:
    try:
        scan = fs.scan_folder(req.folder)
    except FileNotFoundError as e:
        raise TaggingError(str(e), status_code=404) from e

    # All three of camera/lens/film are optional. The user may only know one
    # piece of context for a given roll — tagging just that is still useful.
    camera: Camera | None = None
    if req.camera_id:
        camera = session.get(Camera, req.camera_id)
        if camera is None:
            raise TaggingError("Camera not found", status_code=404)

    lens: Lens | None = None
    if camera and camera.fixed_lens_id:
        lens = session.get(Lens, camera.fixed_lens_id)
    elif req.lens_id:
        lens = session.get(Lens, req.lens_id)
        if lens is None:
            raise TaggingError("Lens not found", status_code=404)

    film: FilmStock | None = None
    if req.film_stock_id:
        film = session.get(FilmStock, req.film_stock_id)
        if film is None:
            raise TaggingError("Film stock not found", status_code=404)

    dev_provider = _get_optional(session, Provider, req.development_provider_id, "Development provider")
    dev_method = _get_optional(
        session, DevelopmentMethod, req.development_method_id, "Development method"
    )
    scan_provider = _get_optional(session, Provider, req.scan_provider_id, "Scan provider")
    scan_method = _get_optional(session, ScanMethod, req.scan_method_id, "Scan method")

    _require_provider_kind(dev_provider, "development", "Development provider")
    _require_provider_kind(scan_provider, "scan", "Scan provider")

    keyword_context = kw.WriteContext(
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
    roll_notes = _clean_text(req.roll_notes)
    place = _clean_text(req.place)

    files = select_files(scan, req.files)

    return TaggingPlan(
        folder=scan.folder,
        roll_id=scan.roll_id,
        source_kind=scan.source_kind,
        scan_tier=scan.scan_tier,
        files=files,
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
        dev_provider_snapshot=_named_snapshot(dev_provider),
        dev_method_snapshot=_named_snapshot(dev_method),
        scan_provider_snapshot=_named_snapshot(scan_provider),
        scan_method_snapshot=_named_snapshot(scan_method),
        capture_date_value=capture_date_value,
        capture_date_precision=capture_date_precision,
        capture_date_source=capture_date_source,
        file_keywords_by_name=file_keywords_by_name,
        file_lens_by_name=file_lens_by_name,
        keyword_context=keyword_context,
        camera_make=camera.make if camera else None,
        camera_model=camera.model if camera else None,
        capture_date_exif=exif_date,
        roll_notes=roll_notes,
        place=place,
        replace_managed_metadata=req.replace_managed_metadata,
    )


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def select_files(scan: fs.ScanResult, requested: list[str] | None) -> list[Path]:
    """All scanned files, or only the requested subset (validated by name)."""
    if requested is None:
        return [scan.folder / f.name for f in scan.files]
    wanted = list(dict.fromkeys(name for name in requested if name))
    if not wanted:
        raise TaggingError("files: empty selection", status_code=400)
    scanned = {f.name for f in scan.files}
    missing = [name for name in wanted if name not in scanned]
    if missing:
        shown = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
        raise TaggingError(f"files not in folder: {shown}", status_code=400)
    selected = set(wanted)
    return [scan.folder / f.name for f in scan.files if f.name in selected]


def _normalize_roll_date(raw: str | None) -> str | None:
    """Validate `YYYY-MM-DD`; reject anything else so garbage never hits exiftool."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if not _ISO_DATE_RE.match(s):
        raise TaggingError(f"roll_date must be YYYY-MM-DD (got {s!r})", status_code=400)
    return s


def _exif_date(iso_date: str | None) -> str | None:
    """Convert `YYYY-MM-DD` -> `YYYY:MM:DD 12:00:00` (noon local, day-precision proxy)."""
    if iso_date is None:
        return None
    return iso_date.replace("-", ":") + " 12:00:00"


def _get_optional(session: Session, model, entity_id: str | None, label: str):
    if not entity_id:
        return None
    row = session.get(model, entity_id)
    if row is None:
        raise TaggingError(f"{label} not found", status_code=404)
    return row


def _require_provider_kind(row: Provider | None, kind: str, label: str) -> None:
    if row is None or row.has_kind(kind):
        return
    raise TaggingError(f"{label} must support {kind}", status_code=400)


def _named_snapshot(row) -> mf.NamedSnapshot:
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
            raise TaggingError(f"Unknown file in file_keywords: {name}", status_code=400)
        cleaned = [kw.strip() for kw in keywords if kw and kw.strip()]
        if cleaned:
            out[name] = cleaned
    return out


def _clean_text(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None


def _lens_compatible_with_camera(camera: Camera | None, lens: Lens) -> bool:
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
    camera: Camera | None,
) -> dict[str, Lens]:
    valid = set(valid_file_names)
    out: dict[str, Lens] = {}
    for name, lens_id in (raw or {}).items():
        if name not in valid:
            raise TaggingError(f"Unknown file in file_lens_ids: {name}", status_code=400)
        if not lens_id:
            continue
        lens = session.get(Lens, lens_id)
        if lens is None:
            raise TaggingError(f"Lens not found for file {name}", status_code=404)
        if not _lens_compatible_with_camera(camera, lens):
            raise TaggingError(f"Lens override is not compatible with the selected camera for file {name}", status_code=400)
        out[name] = lens
    return out


def intent_for_file(plan: TaggingPlan, file_name: str) -> et.WriteIntent:
    lens = plan.file_lens_by_name.get(file_name)
    file_context = replace(
        plan.keyword_context,
        lens_display_name=lens.display_name if lens else plan.keyword_context.lens_display_name,
    )
    hierarchical = kw.build_hierarchical_paths(file_context)
    flat = kw.leaves_of(hierarchical)
    extra_keywords = plan.file_keywords_by_name.get(file_name, [])
    if extra_keywords:
        hierarchical = kw.merge_hierarchical(hierarchical, extra_keywords)
        flat = kw.merge_flat(flat, kw.leaves_of(extra_keywords))
    return et.WriteIntent(
        camera_make=plan.camera_make,
        camera_model=plan.camera_model,
        lens_model=(lens.lens_model_write_value if lens else plan.lens_snapshot.lens_model_write_value),
        hierarchical_keywords=hierarchical,
        flat_keywords=flat,
        capture_date=plan.capture_date_exif,
        description=plan.roll_notes,
    )


def preview_all(plan: TaggingPlan) -> list[tuple[str, list[et.TagChange]]]:
    """Dry run: per file, what an apply would change."""
    return [
        (
            file_path.name,
            exiftool.preview(file_path, intent_for_file(plan, file_path.name), replace_managed=plan.replace_managed_metadata),
        )
        for file_path in plan.files
    ]


def apply_all(
    plan: TaggingPlan,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Write every file, report each one to ``progress``, return the outcome.

    ``cancelled`` is checked between files. A cancelled or failed roll still
    gets its manifest written with the files that were actually applied, so
    tagged files are never left unrecorded.
    """
    emit = progress or (lambda e: None)
    applied: list[str] = []
    errors: list[str] = []
    all_diffs: list[dict] = []
    was_cancelled = False
    finished = False

    try:
        for file_path in plan.files:
            if cancelled():
                was_cancelled = True
                break
            _apply_one(plan, file_path, applied, errors, all_diffs)
            if errors and errors[-1].startswith(f"{file_path.name}:"):
                emit({"type": "progress", "file": file_path.name, "ok": False, "error": errors[-1]})
            else:
                emit({"type": "progress", "file": file_path.name, "ok": True})
        finished = True
    finally:
        # Files already written must be on record even when the loop died.
        if finished or applied:
            manifest_path = _write_apply_manifest(plan, applied, all_diffs)

    return {
        "folder": str(plan.folder),
        "roll_id": plan.roll_id,
        "manifest_path": str(manifest_path),
        "applied_files": applied,
        "errors": errors,
        "cancelled": was_cancelled,
    }


def _apply_one(
    plan: TaggingPlan,
    file_path: Path,
    applied: list[str],
    errors: list[str],
    all_diffs: list[dict],
) -> None:
    result = exiftool.write(
        file_path,
        intent_for_file(plan, file_path.name),
        replace_managed=plan.replace_managed_metadata,
    )
    if not result.ok:
        errors.append(f"{file_path.name}: {result.error}")
        return
    applied.append(file_path.name)
    all_diffs.append({"file": file_path.name, "changes": [asdict(c) for c in result.changes]})


def _write_apply_manifest(plan: TaggingPlan, applied: list[str], all_diffs: list[dict]) -> Path:
    existing_manifest = mf.read_manifest(plan.folder)
    fresh = mf.build_new_manifest(
        folder=plan.folder,
        roll_id=plan.roll_id,
        source_kind=plan.source_kind,
        scan_tier=plan.scan_tier,
        camera=plan.camera_snapshot,
        lens=plan.lens_snapshot,
        film=plan.film_snapshot,
        development_provider=plan.dev_provider_snapshot,
        development_method=plan.dev_method_snapshot,
        scan_provider=plan.scan_provider_snapshot,
        scan_method=plan.scan_method_snapshot,
        capture_date_value=plan.capture_date_value,
        capture_date_precision=plan.capture_date_precision,
        capture_date_source=plan.capture_date_source,
        notes=plan.roll_notes,
        place=plan.place,
    )
    merged = mf.merge_existing_manifest(existing_manifest, fresh) if existing_manifest else fresh
    mf.set_file_overrides(
        merged,
        extra_keywords_by_file=plan.file_keywords_by_name,
        lens_by_file={
            name: mf.GearSnapshot(
                id=lens.id,
                make=lens.make,
                model=lens.model,
                display_name=lens.display_name,
                mount=lens.mount,
                lens_model_write_value=lens.lens_model_write_value,
            )
            for name, lens in plan.file_lens_by_name.items()
        },
    )
    mf.append_apply_history(merged, applied_files=applied, diffs=all_diffs, dry_run=False)
    return mf.write_manifest(plan.folder, merged)
