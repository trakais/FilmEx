"""Split a folder into per-roll folders, plus the helpers the importer shares.

Rules (docs/upgrade-plan.md, cross-cutting):
- every batch is planned first and only runs what the plan showed
- never overwrite: an existing target stops the whole batch
- moves only within one volume (``os.rename``); anything else is refused
- frames move as a unit: images, sidecars and ``.thm`` together
- only one file-mutating job at a time (the caller submits it exclusive)
- every executed batch is journaled in ``operation_log`` and can be undone

Split layout: a roll folder under ``<root>/<year>/`` is cut into siblings
(``2026/EUR-04`` becomes ``2026/<roll A>`` and ``2026/<roll B>``); a flat
year folder, or any folder outside the library, is cut into subfolders.
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

from filmex.schemas import DryRunRequest
from filmex.services import frames as fr
from filmex.services import manifest as mf
from filmex.services import operations
from filmex.services import tagging as tg
from filmex.services.exiftool_runner import exiftool_available

Progress = Callable[[dict[str, Any]], None]
Cancelled = Callable[[], bool]

SIBLINGS = "siblings"
SUBFOLDERS = "subfolders"



class PlanError(ValueError):
    """The batch cannot run as requested; ``errors`` lists why."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Segment:
    """One roll draft from the contact sheet."""

    name: str
    frames: list[str]
    tagging: dict[str, Any] = field(default_factory=dict)
    year: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Segment:
        return cls(
            name=str(data.get("name") or ""),
            frames=[str(k) for k in data.get("frames") or []],
            tagging=dict(data.get("tagging") or {}),
            year=(str(data["year"]) if data.get("year") else None),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------- shared helpers ----------


def clean_name(name: str) -> str:
    """NFC, trimmed folder name; raises ``ValueError`` when unusable."""
    cleaned = unicodedata.normalize("NFC", name or "").strip().rstrip(".")
    if not cleaned:
        raise ValueError("empty folder name")
    if "/" in cleaned or "\\" in cleaned or cleaned in (".", ".."):
        raise ValueError(f"invalid folder name: {name!r}")
    if any(ord(ch) < 32 for ch in cleaned) or any(ch in ':*?"<>|' for ch in cleaned):
        raise ValueError(f"folder name contains characters that are illegal on some volumes: {name!r}")
    return cleaned


def same_name(a: str, b: str) -> bool:
    return unicodedata.normalize("NFC", a).casefold() == unicodedata.normalize("NFC", b).casefold()


def same_volume(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def remove_folder_if_empty(folder: Path) -> bool:
    """Delete OS litter (``frames.is_litter``) and remove the folder when
    nothing else is left."""
    try:
        with os.scandir(folder) as it:
            entries = list(it)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    for entry in entries:
        if fr.is_litter(entry.name):
            try:
                os.remove(entry.path)
            except OSError:
                return False
        else:
            return False
    try:
        os.rmdir(folder)
    except OSError:
        return False
    return True


def tagging_sidecars(folder: Path, image_names: list[str]) -> list[Path]:
    """Sidecars in ``folder`` that belong to ``image_names`` but were not
    among the files a batch put there: the tagging step wrote them for raw
    files that had none. Undo must carry them along, or the folder stays."""
    stems = {fr.stem_key(name) for name in image_names}
    found: list[Path] = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if os.path.splitext(entry.name)[1].lower() != fr.SIDECAR_EXT:
                    continue
                if fr.stem_key(entry.name) in stems:
                    found.append(Path(entry.path))
    except OSError:
        return []
    return found


def resolve_segments(
    listing: fr.FrameListing, segments: list[Segment]
) -> tuple[list[tuple[Segment, list[fr.Frame]]], list[str], list[str]]:
    """Match segment frame keys against the listing.

    Returns ``(resolved, unassigned_keys, errors)``. Names are cleaned in
    place. A frame may belong to one segment only; unknown keys and empty
    segments are errors.
    """
    by_key = fr.frames_by_key(listing)
    errors: list[str] = []
    seen_frames: dict[str, str] = {}
    seen_names: list[str] = []
    resolved: list[tuple[Segment, list[fr.Frame]]] = []
    for index, seg in enumerate(segments, start=1):
        label = seg.name or f"roll {index}"
        try:
            seg.name = clean_name(seg.name)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
        if any(same_name(seg.name, other) for other in seen_names):
            errors.append(f"{seg.name}: two rolls would get the same folder name")
        seen_names.append(seg.name)
        if not seg.frames:
            errors.append(f"{label}: no frames")
        frames: list[fr.Frame] = []
        for key in seg.frames:
            frame = by_key.get(key)
            if frame is None:
                errors.append(f"{label}: frame {key} is not in the folder any more")
                continue
            if key in seen_frames:
                errors.append(f"{label}: frame {key} is also in {seen_frames[key]}")
                continue
            seen_frames[key] = label
            frames.append(frame)
        frames.sort(key=lambda f: f.display_key.lower())
        resolved.append((seg, frames))
    unassigned = [f.key for f in listing.frames if f.key not in seen_frames]
    return resolved, unassigned, errors


def tag_folder(
    session: Session,
    folder: Path,
    tagging: dict[str, Any],
    *,
    progress: Progress | None = None,
    cancelled: Cancelled = lambda: False,
) -> dict[str, Any]:
    """Run the apply pipeline on every image in ``folder``.

    Without ExifTool the files are left untagged and the result says so; the
    Library then shows the folder as untagged, ready for the Tag view.
    """
    emit = progress or (lambda e: None)
    if not exiftool_available():
        return {"applied": [], "errors": [], "skipped": "ExifTool not available: files were moved but not tagged"}
    req = DryRunRequest(folder=str(folder), **tagging)
    try:
        ctx = tg.build_pipeline(req, session)
    except tg.TaggingError as exc:
        return {"applied": [], "errors": [f"tagging: {exc.detail}"], "skipped": None}
    def forward(event: dict[str, Any]) -> None:
        # The pipeline's own `done` stays internal: the job emits one `done`
        # for the whole batch and a `segment` event per roll.
        if event.get("type") == "done":
            return
        emit({**event, "step": "tag", "segment": folder.name})

    final = tg.apply_all(ctx, progress=forward, cancelled=cancelled)
    return {
        "applied": final["applied_files"],
        "errors": final["errors"],
        "manifest_path": final["manifest_path"],
        "skipped": None,
    }


# ---------- split: plan ----------


@dataclass
class SplitItem:
    name: str
    target: str
    frames: list[str]
    files: list[str]
    image_count: int
    bytes: int
    errors: list[str] = field(default_factory=list)


@dataclass
class SplitPlan:
    source: str
    target_parent: str
    layout: str  # siblings | subfolders
    items: list[SplitItem]
    unassigned: list[str]
    other_files: list[str]
    errors: list[str]
    remove_source: bool  # the source folder will be empty of images afterwards

    @property
    def ok(self) -> bool:
        return not self.errors and not any(i.errors for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        data["file_count"] = sum(len(i.files) for i in self.items)
        data["bytes"] = sum(i.bytes for i in self.items)
        return data


def target_parent_for(source: Path, root: str | None) -> tuple[Path, str]:
    """Where the new roll folders go: next to a roll folder, inside a year
    folder or any folder outside the library."""
    if root:
        root_path = Path(root).expanduser()
        try:
            rel = source.resolve().relative_to(root_path.resolve())
        except (ValueError, OSError):
            rel = None
        if rel is not None and len(rel.parts) == 2:
            return source.parent, SIBLINGS
    return source, SUBFOLDERS


def plan_split(source: Path | str, segments: list[Segment], *, root: str | None) -> SplitPlan:
    source_path = Path(source).expanduser()
    errors: list[str] = []
    if not source_path.is_dir():
        raise PlanError([f"folder not found: {source_path}"])
    listing = fr.list_frames(source_path)
    parent, layout = target_parent_for(source_path, root)
    if not same_volume(source_path, parent):
        errors.append("the target is on a different volume; a split only moves files within one volume")

    resolved, unassigned, seg_errors = resolve_segments(listing, segments)
    errors.extend(seg_errors)
    items: list[SplitItem] = []
    for seg, frames in resolved:
        target = parent / seg.name
        item_errors: list[str] = []
        if target.exists() or target.is_symlink():
            if layout == SUBFOLDERS or not _same_path(target, source_path):
                item_errors.append("a folder with this name already exists")
            else:
                item_errors.append("this is the source folder's own name")
        files = [name for f in frames for name in f.files]
        items.append(
            SplitItem(
                name=seg.name,
                target=str(target),
                frames=[f.key for f in frames],
                files=files,
                image_count=sum(len(f.images) for f in frames),
                bytes=sum(f.bytes for f in frames),
                errors=item_errors,
            )
        )
    if not items:
        errors.append("no rolls to create")
    return SplitPlan(
        source=str(source_path),
        target_parent=str(parent),
        layout=layout,
        items=items,
        unassigned=unassigned,
        other_files=listing.other_files,
        errors=errors,
        remove_source=layout == SIBLINGS and not unassigned,
    )


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return False


# ---------- split: apply ----------


def apply_split(
    session: Session,
    source: Path | str,
    segments: list[Segment],
    *,
    root: str | None,
    progress: Progress | None = None,
    cancelled: Cancelled = lambda: False,
) -> dict[str, Any]:
    """Create the roll folders, move the frames, tag each new folder, journal it.

    The plan is recomputed right before running so a change on disk since
    the preview stops the batch instead of moving the wrong files.
    """
    emit = progress or (lambda e: None)
    plan = plan_split(source, segments, root=root)
    if not plan.ok:
        raise PlanError(plan.errors + [e for i in plan.items for e in (f"{i.name}: {x}" for x in i.errors)])
    source_path = Path(plan.source)
    by_name = {seg.name: seg for seg in segments}

    op = operations.start(
        session,
        "split",
        {"source": plan.source, "target_parent": plan.target_parent, "layout": plan.layout,
         "items": [{"name": i.name, "target": i.target, "frames": i.frames} for i in plan.items]},
        root=root,
    )
    moves: list[dict[str, str]] = []
    created: list[dict[str, Any]] = []
    errors: list[str] = []
    was_cancelled = False
    total = sum(len(i.files) for i in plan.items)
    done = 0

    try:
        for item in plan.items:
            if cancelled():
                was_cancelled = True
                break
            target = Path(item.target)
            target.mkdir(parents=False, exist_ok=False)
            record: dict[str, Any] = {"name": item.name, "path": item.target, "files": [], "applied": [], "errors": [], "tag_skipped": None}
            created.append(record)
            for name in item.files:
                if cancelled():
                    was_cancelled = True
                    break
                src = source_path / name
                dst = target / name
                _move_file(src, dst)
                moves.append({"from": str(src), "to": str(dst)})
                record["files"].append(name)
                done += 1
                emit({"type": "progress", "step": "move", "segment": item.name, "file": name, "done": done, "total": total})
            if was_cancelled:
                break
            tagged = tag_folder(session, target, by_name[item.name].tagging, progress=emit, cancelled=cancelled)
            record["applied"] = tagged["applied"]
            record["errors"] = tagged["errors"]
            record["tag_skipped"] = tagged.get("skipped")
            errors.extend(f"{item.name}: {e}" for e in tagged["errors"])
            emit({"type": "progress", "step": "segment", "segment": item.name, "files": len(record["files"]), "applied": len(record["applied"])})
    except OSError as exc:
        errors.append(f"stopped: {exc}")

    # A source with no images left keeps nothing worth a folder: its manifest
    # goes into the journal (undo restores it) and the folder is removed when
    # only litter remains. In the subfolders layout the folder stays, since
    # it now holds the new rolls, but the stale manifest still goes.
    stashed, removed_source = _retire_source(source_path, remove_folder=plan.layout == SIBLINGS)

    result = {
        "created": created,
        "moves": moves,
        "removed_source": removed_source,
        "stashed_manifest": stashed,
        "errors": errors,
        "cancelled": was_cancelled,
    }
    status = operations.FAILED if errors and not moves else operations.DONE
    operations.finish(session, op.id, status, result)
    result["operation_id"] = op.id
    return result


def _move_file(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        raise OSError(f"{dst.name}: target already exists")
    os.rename(src, dst)  # same volume by plan; EXDEV surfaces as OSError


def _no_images_left(folder: Path) -> bool:
    try:
        return not fr.list_frames(folder).frames
    except OSError:
        return False


def _retire_source(folder: Path, *, remove_folder: bool) -> tuple[dict[str, Any] | None, bool]:
    """When no images remain: keep the old manifest in the journal, drop it
    from disk, and (optionally) remove the folder if only litter is left."""
    if not _no_images_left(folder):
        return None, False
    stashed = None
    manifest_path = mf.manifest_path_for(folder)
    if manifest_path.is_file():
        try:
            stashed = mf.read_manifest(folder)
            manifest_path.unlink()
        except (OSError, ValueError):
            return None, False
    if not remove_folder:
        return stashed, False
    return stashed, remove_folder_if_empty(folder)


# ---------- split: undo ----------


def undo_split(
    session: Session,
    op_id: str,
    *,
    progress: Progress | None = None,
    cancelled: Cancelled = lambda: False,
) -> dict[str, Any]:
    """Move every frame back, restore the source folder and its manifest,
    remove the roll folders the split created.

    Metadata written by the split's tagging step stays on the files; the
    result says so, and re-tagging the source folder replaces it.
    """
    op = operations.get(session, op_id)
    if op is None:
        raise KeyError(op_id)
    if op.kind != "split":
        raise ValueError(f"cannot undo an operation of kind {op.kind}")
    if op.status == operations.UNDONE:
        raise ValueError("operation already undone")
    data = operations.to_dict(op)
    result = data["result"]
    emit = progress or (lambda e: None)
    source = Path(data["plan"]["source"])
    restored: list[dict[str, str]] = []
    errors: list[str] = []

    source.mkdir(parents=True, exist_ok=True)
    for move in reversed(result.get("moves", [])):
        if cancelled():
            errors.append("cancelled before every file was moved back")
            break
        current, original = Path(move["to"]), Path(move["from"])
        try:
            if not current.exists():
                raise OSError("file is no longer where the split put it")
            _move_file(current, original)
        except OSError as exc:
            errors.append(f"{current.name}: {exc}")
            emit({"type": "progress", "step": "move", "file": current.name, "ok": False, "error": str(exc)})
            continue
        restored.append({"from": str(current), "to": str(original)})
        emit({"type": "progress", "step": "move", "file": current.name, "ok": True})

    removed_folders: list[str] = []
    for record in result.get("created", []):
        folder = Path(record["path"])
        if not folder.is_dir():
            continue
        # Sidecars the tagging step wrote go back with their raw files, so
        # the metadata stays with the frame and the folder can be removed.
        moved_names = {Path(m["to"]).name for m in result.get("moves", []) if Path(m["to"]).parent == folder}
        for sidecar in tagging_sidecars(folder, sorted(moved_names)):
            if sidecar.name in moved_names:
                continue
            try:
                _move_file(sidecar, source / sidecar.name)
                restored.append({"from": str(sidecar), "to": str(source / sidecar.name)})
            except OSError as exc:
                errors.append(f"{sidecar.name}: {exc}")
        manifest_path = mf.manifest_path_for(folder)
        if manifest_path.is_file():
            try:
                manifest_path.unlink()
            except OSError as exc:
                errors.append(f"{folder.name}: could not remove its manifest: {exc}")
        if remove_folder_if_empty(folder):
            removed_folders.append(str(folder))
        else:
            errors.append(f"{folder.name}: left in place, it still holds files")

    if result.get("stashed_manifest") and not mf.manifest_path_for(source).exists():
        mf.write_manifest(source, result["stashed_manifest"])

    undo_result = {
        "restored": restored,
        "removed_folders": removed_folders,
        "errors": errors,
        "note": "Metadata written by the split stays on the files; re-tag the folder to replace it.",
    }
    if restored or removed_folders:
        operations.mark_undone(session, op_id, undo_result)
    undo_result["operation_id"] = op_id
    return undo_result
