"""Managed folder renames: propose names from the template, apply, undo.

A rename is more than ``os.rename``: the manifest's ``roll_id`` and
``folder_relative_path`` must follow the folder, and every tagged file
carries a ``Roll|<folder name>`` keyword that must be swapped, or Lightroom
keeps showing the old roll. All of that happens here, as one journaled
operation that can be undone.

Rules (see docs/upgrade-plan.md, cross-cutting):
- rename within the same parent only, never across volumes
- never overwrite: an existing target is a conflict, not a merge
- the whole plan is shown first; this module only executes what was chosen
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session

from filmex.models import Camera, FilmStock, Lens
from filmex.services import exiftool as et
from filmex.services import exiftool_runner as exiftool
from filmex.services import library as lib
from filmex.services import manifest as mf
from filmex.services import naming, operations

RENAME = "rename"
CONFORMS = "conforms"
SKIPPED = "skipped"
CONFLICT = "conflict"


@dataclass
class RenameProposal:
    path: str
    parent: str | None
    current: str  # NFC display form of the folder name on disk
    proposed: str | None
    status: str  # rename | conforms | skipped | conflict
    reason: str
    roll_id: str | None = None
    film: str | None = None
    camera: str | None = None
    date: str | None = None
    place: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RenameOutcome:
    renamed: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    keyword_files: int = 0


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------- proposals ----------


def name_context(session: Session, manifest: dict[str, Any]) -> naming.NameContext:
    """Template context from a manifest, resolving short names via the DB."""
    cam_snap = manifest.get("analog_camera_snapshot") or {}
    film_snap = manifest.get("film_stock_snapshot") or {}
    lens_snap = manifest.get("analog_lens_snapshot") or {}

    camera = _short_from(session, Camera, cam_snap)
    film = _short_from(session, FilmStock, film_snap)
    lens = _short_from(session, Lens, lens_snap, has_short=False)
    date = manifest.get("capture_date_value")
    if not naming.is_date(date):
        date = None
    return naming.NameContext(
        date=date,
        camera=camera or None,
        film=film or None,
        lens=lens or None,
        place=(manifest.get("place") or "").strip() or None,
    )


def context_from_ids(
    session: Session,
    *,
    date: str | None,
    camera_id: str | None,
    film_stock_id: str | None,
    lens_id: str | None,
    place: str | None,
) -> naming.NameContext:
    """Template context for a roll draft that has not been tagged yet."""
    camera = session.get(Camera, camera_id) if camera_id else None
    film = session.get(FilmStock, film_stock_id) if film_stock_id else None
    lens = session.get(Lens, lens_id) if lens_id else None
    date = (date or "").strip() or None
    return naming.NameContext(
        date=date,
        camera=naming.short_name_for(camera.display_name, camera.short_name) if camera else None,
        film=naming.short_name_for(film.display_name, film.short_name) if film else None,
        lens=naming.short_name_for(lens.display_name, None) if lens else None,
        place=(place or "").strip() or None,
    )


def _short_from(session: Session, model, snapshot: dict[str, Any], *, has_short: bool = True) -> str:
    display = snapshot.get("display_name")
    row = session.get(model, snapshot["id"]) if snapshot.get("id") else None
    short = getattr(row, "short_name", None) if (row is not None and has_short) else None
    display = getattr(row, "display_name", None) or display
    return naming.short_name_for(display, short)


def propose(
    session: Session,
    root: str,
    *,
    template: str,
    place_suffix: bool = True,
) -> list[RenameProposal]:
    """One proposal per roll folder under ``root`` (fresh walk, not the cache)."""
    naming.validate_template(template)
    rows = [r for r in lib.scan_library(root) if r.depth == 2 or r.is_flat_year]
    proposals: list[RenameProposal] = []
    candidates: list[tuple[lib.LibraryFolder, naming.NameContext, dict[str, Any]]] = []

    for row in rows:
        if row.is_flat_year:
            proposals.append(_skip(row, "loose year folder; carve rolls out first"))
            continue
        if row.status == lib.EMPTY:
            proposals.append(_skip(row, "no images"))
            continue
        manifest = mf.read_manifest(row.path)
        if not manifest:
            proposals.append(_skip(row, "not tagged yet (no manifest)"))
            continue
        candidates.append((row, name_context(session, manifest), manifest))

    # {roll}: sequence per year in date order among the candidates that have a date.
    needs_roll = "roll" in naming.tokens_in(template)
    roll_numbers: dict[str, int] = {}
    if needs_roll:
        dated = sorted(
            (c for c in candidates if c[1].date),
            key=lambda c: (c[1].date, c[0].display_name.lower()),
        )
        per_year: dict[str, int] = {}
        for row, ctx, _ in dated:
            year = ctx.date[:4]
            per_year[year] = per_year.get(year, 0) + 1
            roll_numbers[row.path] = per_year[year]

    taken: dict[str, str] = {}  # proposed target path -> source path
    for row, ctx, manifest in candidates:
        if needs_roll:
            ctx = naming.NameContext(**{**asdict(ctx), "roll": roll_numbers.get(row.path)})
        try:
            proposed = naming.render(template, ctx, place_suffix=place_suffix)
        except naming.NamingError as exc:
            proposals.append(_skip(row, str(exc), ctx, manifest))
            continue
        base = RenameProposal(
            path=row.path,
            parent=row.parent,
            current=row.display_name,
            proposed=proposed,
            status=RENAME,
            reason="",
            roll_id=manifest.get("roll_id"),
            film=ctx.film,
            camera=ctx.camera,
            date=ctx.date,
            place=ctx.place,
        )
        if naming.same_name(proposed, row.name):
            base.status = CONFORMS
            base.reason = "already follows the template"
            proposals.append(base)
            continue
        target = str(Path(row.path).parent / proposed)
        if _exists_other(Path(row.path), Path(target)):
            base.status = CONFLICT
            base.reason = "a folder with the proposed name already exists"
        elif target in taken:
            base.status = CONFLICT
            base.reason = f"same name as proposed for {Path(taken[target]).name}"
        else:
            taken[target] = row.path
        proposals.append(base)

    proposals.sort(key=lambda p: ((p.parent or p.current).lower(), p.current.lower()))
    return proposals


def _skip(row: lib.LibraryFolder, reason: str, ctx: naming.NameContext | None = None, manifest: dict[str, Any] | None = None) -> RenameProposal:
    return RenameProposal(
        path=row.path,
        parent=row.parent,
        current=row.display_name,
        proposed=None,
        status=SKIPPED,
        reason=reason,
        roll_id=(manifest or {}).get("roll_id"),
        film=ctx.film if ctx else None,
        camera=ctx.camera if ctx else None,
        date=ctx.date if ctx else None,
        place=ctx.place if ctx else None,
    )


def _exists_other(source: Path, target: Path) -> bool:
    """True when ``target`` exists and is not just ``source`` under another
    normalization form (APFS/SMB may treat NFC and NFD as the same entry)."""
    if not target.exists():
        return False
    try:
        return not target.samefile(source)
    except OSError:
        return True


# ---------- apply ----------


def apply_renames(
    session: Session,
    items: list[dict[str, str]],
    *,
    root: str | None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Rename the chosen folders, fix manifests, swap Roll keywords, journal it.

    ``items`` are ``{"path": ..., "proposed": ...}`` pairs from ``propose``.
    Each item is validated again right before it runs; failures are reported
    per item and never stop the others.
    """
    plan = {"items": [{"path": i["path"], "proposed": i["proposed"]} for i in items]}
    op = operations.start(session, "rename", plan, root=root)
    outcome = RenameOutcome()

    for item in items:
        if cancelled():
            outcome.errors.append("cancelled before all folders were renamed")
            break
        source = Path(item["path"])
        proposed = unicodedata.normalize("NFC", item["proposed"]).strip()
        emit = progress or (lambda e: None)
        try:
            new_path = _rename_folder(source, proposed)
        except (OSError, ValueError) as exc:
            msg = f"{source.name}: {exc}"
            outcome.errors.append(msg)
            emit({"type": "progress", "step": "rename", "path": str(source), "ok": False, "error": msg})
            continue
        outcome.renamed.append({"from": str(source), "to": str(new_path)})
        emit({"type": "progress", "step": "rename", "path": str(source), "to": str(new_path), "ok": True})

        count, errors = swap_roll_keyword(
            new_path, old_roll=source.name, new_roll=proposed,
            progress=emit, cancelled=cancelled,
        )
        outcome.keyword_files += count
        outcome.errors.extend(errors)

    result = asdict(outcome)
    operations.finish(session, op.id, operations.FAILED if outcome.errors and not outcome.renamed else operations.DONE, result)
    result["operation_id"] = op.id
    return result


def _rename_folder(source: Path, proposed: str) -> Path:
    if not source.is_dir():
        raise ValueError("folder no longer exists")
    if not proposed or "/" in proposed or "\\" in proposed or proposed in (".", ".."):
        raise ValueError("invalid target name")
    target = source.parent / proposed
    if _exists_other(source, target):
        raise ValueError("target already exists")
    os.rename(source, target)  # same parent: atomic, same volume by construction
    _update_manifest_after_rename(target, old_name=source.name, new_name=proposed)
    return target


def _update_manifest_after_rename(folder: Path, *, old_name: str, new_name: str) -> None:
    manifest = mf.read_manifest(folder)
    if not manifest:
        return
    manifest["roll_id"] = new_name
    manifest["folder_relative_path"] = new_name
    manifest.setdefault("renames", []).append({"from": old_name, "to": new_name, "timestamp": _now()})
    manifest["updated_at"] = _now()
    mf.write_manifest(folder, manifest)


def swap_roll_keyword(
    folder: Path,
    *,
    old_roll: str,
    new_roll: str,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> tuple[int, list[str]]:
    """Replace ``Roll|old`` with ``Roll|new`` on every tagged file in ``folder``.

    Uses the normal intent resolution: a new ``Roll|…`` path makes the
    reconciler drop the old Roll family and keep every other keyword, so this
    is idempotent and never touches user keywords.
    """
    manifest = mf.read_manifest(folder)
    names = sorted(lib.applied_files_from_manifest(manifest))
    emit = progress or (lambda e: None)
    errors: list[str] = []
    done = 0
    new_roll = unicodedata.normalize("NFC", new_roll)
    for name in names:
        if cancelled():
            errors.append("cancelled before all Roll keywords were updated")
            break
        path = folder / name
        if not path.is_file():
            continue
        try:
            intent = et.WriteIntent(hierarchical_keywords=[f"Roll|{new_roll}"], flat_keywords=[new_roll])
            result = exiftool.write(path, intent)
        except Exception as exc:  # noqa: BLE001 - report per file, keep going
            msg = f"{name}: {exc}"
            errors.append(msg)
            emit({"type": "progress", "step": "keywords", "file": name, "ok": False, "error": msg})
            continue
        if not result.ok:
            msg = f"{name}: {result.error}"
            errors.append(msg)
            emit({"type": "progress", "step": "keywords", "file": name, "ok": False, "error": msg})
            continue
        done += 1
        emit({"type": "progress", "step": "keywords", "file": name, "ok": True})
    return done, errors


# ---------- undo ----------


def undo(
    session: Session,
    op_id: str,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Reverse a rename operation: folders back, manifests back, keywords back."""
    op = operations.get(session, op_id)
    if op is None:
        raise KeyError(op_id)
    if op.kind != "rename":
        raise ValueError(f"cannot undo an operation of kind {op.kind}")
    if op.status == operations.UNDONE:
        raise ValueError("operation already undone")
    data = operations.to_dict(op)
    emit = progress or (lambda e: None)
    restored: list[dict[str, str]] = []
    errors: list[str] = []
    keyword_files = 0

    for entry in reversed(data["result"].get("renamed", [])):
        if cancelled():
            errors.append("cancelled before all folders were restored")
            break
        current = Path(entry["to"])
        original = Path(entry["from"])
        try:
            if not current.is_dir():
                raise ValueError("renamed folder no longer exists")
            if _exists_other(current, original):
                raise ValueError("original name is taken again")
            os.rename(current, original)
            _update_manifest_after_rename(original, old_name=current.name, new_name=original.name)
        except (OSError, ValueError) as exc:
            msg = f"{current.name}: {exc}"
            errors.append(msg)
            emit({"type": "progress", "step": "rename", "path": str(current), "ok": False, "error": msg})
            continue
        restored.append({"from": str(current), "to": str(original)})
        emit({"type": "progress", "step": "rename", "path": str(current), "to": str(original), "ok": True})
        count, kw_errors = swap_roll_keyword(
            original, old_roll=current.name, new_roll=original.name, progress=emit, cancelled=cancelled
        )
        keyword_files += count
        errors.extend(kw_errors)

    result = {"restored": restored, "errors": errors, "keyword_files": keyword_files}
    if restored:
        operations.mark_undone(session, op_id, result)
    result["operation_id"] = op_id
    return result
