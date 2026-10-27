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
from dataclasses import asdict, dataclass, field, replace
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
    """A request that cannot be turned into a tagging plan (bad ids, bad folder)."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class CaptureDate:
    """The roll date as the user gave it (``YYYY-MM-DD``), or none."""

    value: str | None = None

    @property
    def precision(self) -> str:
        return "day" if self.value else "unknown"

    @property
    def source(self) -> str | None:
        return "user_override" if self.value else None

    @property
    def exif(self) -> str | None:
        """``YYYY:MM:DD 12:00:00``: noon local stands in for day precision."""
        return self.value.replace("-", ":") + " 12:00:00" if self.value else None


@dataclass
class TaggingPlan:
    """What a tagging run writes: the folder's files, snapshots of the gear,
    film, providers and methods, and the per-file overrides. Built once by
    ``plan_tagging``; ``preview_all`` and ``apply_all`` only read it."""

    folder: Path
    roll_id: str
    source_kind: str
    scan_tier: str
    files: list[Path]
    camera_snapshot: mf.GearSnapshot
    lens_snapshot: mf.GearSnapshot
    film_snapshot: mf.FilmSnapshot
    dev_provider_snapshot: mf.NamedSnapshot
    dev_method_snapshot: mf.NamedSnapshot
    scan_provider_snapshot: mf.NamedSnapshot
    scan_method_snapshot: mf.NamedSnapshot
    keyword_context: kw.WriteContext
    capture_date: CaptureDate = field(default_factory=CaptureDate)
    file_keywords_by_name: dict[str, list[str]] = field(default_factory=dict)
    file_lens_by_name: dict[str, Lens] = field(default_factory=dict)
    roll_notes: str | None = None
    place: str | None = None
    replace_managed_metadata: bool = False


@dataclass
class ApplyResult:
    """Outcome of ``apply_all``; ``to_dict`` is what the apply job reports."""

    folder: str
    roll_id: str
    manifest_path: str
    applied_files: list[str]
    errors: list[str]
    cancelled: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Gear:
    camera: Camera | None
    lens: Lens | None
    film: FilmStock | None


@dataclass
class _Processing:
    """Who developed and scanned the roll, and how."""

    dev_provider: Provider | None
    dev_method: DevelopmentMethod | None
    scan_provider: Provider | None
    scan_method: ScanMethod | None


def plan_tagging(req: DryRunRequest, session: Session, *, roll_id: str | None = None) -> TaggingPlan:
    """Resolve a request into a plan, or raise ``TaggingError``.

    ``roll_id`` overrides the folder's name as the roll id; only a dry run
    passes it, to preview a roll under the folder name it is about to get.
    """
    scan = _scan(req.folder)
    names = [f.name for f in scan.files]
    gear = _resolve_gear(session, req)
    processing = _resolve_processing(session, req)
    roll_id = roll_id or scan.roll_id
    return TaggingPlan(
        folder=scan.folder,
        roll_id=roll_id,
        source_kind=scan.source_kind,
        scan_tier=scan.scan_tier,
        camera_snapshot=_camera_snapshot(gear.camera),
        lens_snapshot=_lens_snapshot(gear.lens),
        film_snapshot=_film_snapshot(gear.film),
        dev_provider_snapshot=_named_snapshot(processing.dev_provider),
        dev_method_snapshot=_named_snapshot(processing.dev_method),
        scan_provider_snapshot=_named_snapshot(processing.scan_provider),
        scan_method_snapshot=_named_snapshot(processing.scan_method),
        keyword_context=_keyword_context(roll_id, gear, processing, req.extra_keywords),
        file_keywords_by_name=_normalize_file_keywords(req.file_keywords, names),
        file_lens_by_name=_normalize_file_lenses(session, req.file_lens_ids, names, gear.camera),
        capture_date=CaptureDate(_normalize_roll_date(req.roll_date)),
        roll_notes=_clean_text(req.roll_notes),
        place=_clean_text(req.place),
        replace_managed_metadata=req.replace_managed_metadata,
        files=select_files(scan, req.files),
    )


def _scan(folder: str) -> fs.ScanResult:
    try:
        return fs.scan_folder(folder)
    except FileNotFoundError as e:
        raise TaggingError(str(e), status_code=404) from e


def _resolve_gear(session: Session, req: DryRunRequest) -> _Gear:
    """Camera, lens and film are each optional: knowing only one of them is
    still worth tagging. A fixed-lens camera always brings its own lens."""
    camera = _get_optional(session, Camera, req.camera_id, "Camera")
    if camera and camera.fixed_lens_id:
        lens = session.get(Lens, camera.fixed_lens_id)
    else:
        lens = _get_optional(session, Lens, req.lens_id, "Lens")
    film = _get_optional(session, FilmStock, req.film_stock_id, "Film stock")
    return _Gear(camera=camera, lens=lens, film=film)


def _resolve_processing(session: Session, req: DryRunRequest) -> _Processing:
    processing = _Processing(
        dev_provider=_get_optional(session, Provider, req.development_provider_id, "Development provider"),
        dev_method=_get_optional(session, DevelopmentMethod, req.development_method_id, "Development method"),
        scan_provider=_get_optional(session, Provider, req.scan_provider_id, "Scan provider"),
        scan_method=_get_optional(session, ScanMethod, req.scan_method_id, "Scan method"),
    )
    _require_provider_kind(processing.dev_provider, "development", "Development provider")
    _require_provider_kind(processing.scan_provider, "scan", "Scan provider")
    return processing


def _keyword_context(roll_id: str, gear: _Gear, processing: _Processing, extra_keywords: list[str]) -> kw.WriteContext:
    def name_of(row) -> str | None:
        return row.name if row else None

    return kw.WriteContext(
        roll_id=roll_id,
        film_display_name=gear.film.display_name if gear.film else None,
        film_type=gear.film.film_type if gear.film else None,
        camera_display_name=gear.camera.display_name if gear.camera else None,
        lens_display_name=gear.lens.display_name if gear.lens else None,
        development_provider_name=name_of(processing.dev_provider),
        development_method_name=name_of(processing.dev_method),
        scan_provider_name=name_of(processing.scan_provider),
        scan_method_name=name_of(processing.scan_method),
        extra_keywords=list(extra_keywords),
    )


def _camera_snapshot(camera: Camera | None) -> mf.GearSnapshot:
    if camera is None:
        return mf.GearSnapshot()
    return mf.GearSnapshot(
        id=camera.id, make=camera.make, model=camera.model, display_name=camera.display_name, mount=camera.mount
    )


def _lens_snapshot(lens: Lens | None) -> mf.GearSnapshot:
    if lens is None:
        return mf.GearSnapshot()
    return mf.GearSnapshot(
        id=lens.id,
        make=lens.make,
        model=lens.model,
        display_name=lens.display_name,
        mount=lens.mount,
        lens_model_write_value=lens.lens_model_write_value,
    )


def _film_snapshot(film: FilmStock | None) -> mf.FilmSnapshot:
    if film is None:
        return mf.FilmSnapshot()
    return mf.FilmSnapshot(
        id=film.id,
        brand=film.brand,
        display_name=film.display_name,
        box_speed_iso=film.box_speed_iso,
        film_type=film.film_type,
        process_type=film.process_type,
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
        camera_make=plan.camera_snapshot.make,
        camera_model=plan.camera_snapshot.model,
        lens_model=(lens.lens_model_write_value if lens else plan.lens_snapshot.lens_model_write_value),
        hierarchical_keywords=hierarchical,
        flat_keywords=flat,
        capture_date=plan.capture_date.exif,
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
) -> ApplyResult:
    """Write every file, report each one to ``progress``, return the outcome.

    ``cancelled`` is checked between files. A cancelled or failed roll still
    gets its manifest written with the files that were actually applied, so
    tagged files are never left unrecorded.
    """
    emit = progress or (lambda e: None)
    applied: list[str] = []
    errors: list[str] = []
    diffs: list[dict] = []
    was_cancelled = False
    finished = False

    try:
        for file_path in plan.files:
            if cancelled():
                was_cancelled = True
                break
            written = exiftool.write(
                file_path, intent_for_file(plan, file_path.name), replace_managed=plan.replace_managed_metadata
            )
            if written.ok:
                applied.append(file_path.name)
                diffs.append({"file": file_path.name, "changes": [asdict(c) for c in written.changes]})
                emit({"type": "progress", "file": file_path.name, "ok": True})
            else:
                error = f"{file_path.name}: {written.error}"
                errors.append(error)
                emit({"type": "progress", "file": file_path.name, "ok": False, "error": error})
        finished = True
    finally:
        # Files already written must be on record even when the loop died.
        if finished or applied:
            manifest_path = _write_apply_manifest(plan, applied, diffs)

    return ApplyResult(
        folder=str(plan.folder),
        roll_id=plan.roll_id,
        manifest_path=str(manifest_path),
        applied_files=applied,
        errors=errors,
        cancelled=was_cancelled,
    )


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
        capture_date_value=plan.capture_date.value,
        capture_date_precision=plan.capture_date.precision,
        capture_date_source=plan.capture_date.source,
        notes=plan.roll_notes,
        place=plan.place,
    )
    merged = mf.merge_existing_manifest(existing_manifest, fresh) if existing_manifest else fresh
    mf.set_file_overrides(
        merged,
        extra_keywords_by_file=plan.file_keywords_by_name,
        lens_by_file={name: _lens_snapshot(lens) for name, lens in plan.file_lens_by_name.items()},
    )
    mf.append_apply_history(merged, applied_files=applied, diffs=all_diffs, dry_run=False)
    return mf.write_manifest(plan.folder, merged)
