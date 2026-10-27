"""Gather: the RAW and JPG renditions of a roll become one folder.

A roll kept as ``<roll>/RAW`` and ``<roll>/JPG`` is one roll already (see
``renditions``); gathering moves every file of every rendition up into the
roll folder, merges their manifests into one, gives files that were tagged
under an old per-subfolder roll id (``Roll|RAW``) the roll's keyword, and
removes the emptied subfolders. It is planned first, journaled, and undone
by putting everything back exactly as it was.

Lightroom knows files by path, so a gathered roll shows as missing there
until it is relinked; the UI says so before running. Nothing is copied:
every move is a rename inside the roll folder, so it is fast and atomic.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session

from filmex.services import exiftool as et
from filmex.services import exiftool_runner as exiftool
from filmex.services import frames as fr
from filmex.services import library as lib
from filmex.services import manifest as mf
from filmex.services import operations
from filmex.services import renditions as rd
from filmex.services.fileops import (
    Cancelled,
    Move,
    PlanError,
    Progress,
    no_progress,
    remove_folder_if_empty,
)

KIND = "gather"


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------- plan ----------


@dataclass
class GatherItem:
    rendition: str
    path: str
    files: list[str]  # everything that moves: frame files and loose files, never the manifest
    has_manifest: bool

    def to_dict(self) -> dict[str, Any]:
        return {"rendition": self.rendition, "path": self.path, "files": list(self.files), "has_manifest": self.has_manifest}


@dataclass
class GatherPlan:
    roll: str
    items: list[GatherItem]
    problems: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def file_count(self) -> int:
        return sum(len(i.files) for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "roll": self.roll,
            "roll_name": Path(self.roll).name,
            "items": [i.to_dict() for i in self.items],
            "file_count": self.file_count,
            "problems": list(self.problems),
            "ok": self.ok,
        }


def plan_gather(folder: Path | str) -> GatherPlan:
    """What gathering the roll that ``folder`` belongs to would move. A name
    present in two renditions, or already in the roll folder, is a problem:
    nothing is ever overwritten."""
    roll = rd.roll_folder(Path(folder).expanduser())
    if not roll.is_dir():
        raise FileNotFoundError(f"Folder not found: {roll}")
    parts = rd.rendition_dirs(roll)
    problems: list[str] = []
    if not parts:
        problems.append(f"{roll.name}: no RAW/JPG renditions to gather")
    items: list[GatherItem] = []
    seen: dict[str, str] = {}
    for part in parts:
        listing = fr.list_frames(part)
        names = {n for frame in listing.frames for n in frame.files}
        names |= {n for n in listing.other_files if n != mf.MANIFEST_FILENAME}
        files = sorted(names, key=str.lower)
        for name in files:
            if name in seen:
                problems.append(f"{name}: in both {seen[name]} and {part.name}")
            else:
                seen[name] = part.name
            if (roll / name).exists() or (roll / name).is_symlink():
                problems.append(f"{name}: already in {roll.name}")
        items.append(GatherItem(rendition=part.name, path=str(part), files=files, has_manifest=mf.manifest_path_for(part).is_file()))
    return GatherPlan(roll=str(roll), items=items, problems=problems)


# ---------- results ----------


@dataclass
class GatheredRendition:
    rendition: str
    path: str
    manifest: dict[str, Any] | None  # the rendition's manifest as it was, for undo
    files: list[str] = field(default_factory=list)  # moved
    keyword_files: list[str] = field(default_factory=list)  # Roll keyword swapped on these

    def to_dict(self) -> dict[str, Any]:
        return {"rendition": self.rendition, "path": self.path, "manifest": self.manifest,
                "files": list(self.files), "keyword_files": list(self.keyword_files)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatheredRendition:
        return cls(rendition=data["rendition"], path=data["path"], manifest=data.get("manifest"),
                   files=list(data.get("files", [])), keyword_files=list(data.get("keyword_files", [])))


@dataclass
class GatherResult:
    roll: str
    moves: list[Move] = field(default_factory=list)
    renditions: list[GatheredRendition] = field(default_factory=list)
    prior_manifest: dict[str, Any] | None = None  # the roll folder's own manifest before, if any
    removed_folders: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    operation_id: str | None = None

    @property
    def keyword_files(self) -> list[str]:
        return [name for r in self.renditions for name in r.keyword_files]

    def to_dict(self) -> dict[str, Any]:
        data = {
            "roll": self.roll,
            "moves": [m.to_dict() for m in self.moves],
            "renditions": [r.to_dict() for r in self.renditions],
            "prior_manifest": self.prior_manifest,
            "removed_folders": list(self.removed_folders),
            "keyword_files": self.keyword_files,
            "errors": list(self.errors),
            "cancelled": self.cancelled,
        }
        if self.operation_id:
            data["operation_id"] = self.operation_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatherResult:
        return cls(
            roll=data["roll"],
            moves=[Move.from_dict(m) for m in data.get("moves", [])],
            renditions=[GatheredRendition.from_dict(r) for r in data.get("renditions", [])],
            prior_manifest=data.get("prior_manifest"),
            removed_folders=list(data.get("removed_folders", [])),
            errors=list(data.get("errors", [])),
            cancelled=bool(data.get("cancelled", False)),
            operation_id=data.get("operation_id"),
        )


@dataclass
class GatherUndoResult:
    restored: list[Move] = field(default_factory=list)
    restored_manifests: list[str] = field(default_factory=list)
    keyword_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    note: str = "Metadata written while gathered stays on the files; re-tag a rendition to replace it."
    operation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "restored": [m.to_dict() for m in self.restored],
            "restored_manifests": list(self.restored_manifests),
            "keyword_files": list(self.keyword_files),
            "errors": list(self.errors),
            "note": self.note,
        }
        if self.operation_id:
            data["operation_id"] = self.operation_id
        return data


# ---------- apply ----------


def apply_gather(
    session: Session,
    folder: Path | str,
    *,
    progress: Progress | None = None,
    cancelled: Cancelled = lambda: False,
) -> GatherResult:
    """Move every rendition's files into the roll folder, merge the manifests,
    fix Roll keywords written under an old roll id, remove the empty
    subfolders, journal it. The plan is recomputed right before running."""
    plan = plan_gather(folder)
    if not plan.ok:
        raise PlanError(plan.problems)
    op = operations.start(session, KIND, plan.to_dict())
    result = _Gather(plan, progress or no_progress, cancelled).run()
    status = operations.FAILED if result.errors and not result.moves else operations.DONE
    operations.finish(session, op.id, status, result.to_dict())
    result.operation_id = op.id
    return result


class _Gather:
    def __init__(self, plan: GatherPlan, emit: Progress, cancelled: Cancelled) -> None:
        self.plan = plan
        self.emit = emit
        self.cancelled = cancelled
        self.roll = Path(plan.roll)
        self.result = GatherResult(roll=plan.roll)

    def run(self) -> GatherResult:
        self.result.prior_manifest = _read_manifest_or_none(self.roll)
        try:
            for item in self.plan.items:
                self._move_rendition(item)
                if self.result.cancelled:
                    return self.result
        except OSError as exc:
            self.result.errors.append(f"stopped: {exc}")
            return self.result
        self._merge_manifests()
        self._swap_roll_keywords()
        self._remove_subfolders()
        return self.result

    def _move_rendition(self, item: GatherItem) -> None:
        part = Path(item.path)
        record = GatheredRendition(rendition=item.rendition, path=item.path, manifest=_read_manifest_or_none(part))
        self.result.renditions.append(record)
        for name in item.files:
            if self.cancelled():
                self.result.cancelled = True
                self.result.errors.append("cancelled before every file was moved")
                return
            _move_file(part / name, self.roll / name)
            self.result.moves.append(Move(source=str(part / name), target=str(self.roll / name)))
            record.files.append(name)
            self.emit({"type": "progress", "step": "move", "rendition": item.rendition, "file": name,
                       "done": len(self.result.moves), "total": self.plan.file_count})

    def _merge_manifests(self) -> None:
        merged = merge_manifests(self.roll, self.result.renditions, prior=self.result.prior_manifest)
        if merged is None:
            return
        try:
            mf.write_manifest(self.roll, merged)
            for record in self.result.renditions:
                path = mf.manifest_path_for(record.path)
                if path.is_file():
                    path.unlink()
        except OSError as exc:
            self.result.errors.append(f"manifest: {exc}")

    def _swap_roll_keywords(self) -> None:
        """Files tagged when the subfolder was the roll carry ``Roll|RAW``;
        give them the roll's keyword, through the normal reconciliation."""
        for record in self.result.renditions:
            old = (record.manifest or {}).get("roll_id")
            if not record.manifest or not old or old == self.roll.name:
                continue
            applied = lib.applied_files_from_manifest(record.manifest)
            for name in sorted(n for n in record.files if n in applied):
                if self.cancelled():
                    self.result.errors.append("cancelled before every Roll keyword was updated")
                    return
                error = _write_roll_keyword(self.roll / name, self.roll.name)
                if error:
                    self.result.errors.append(f"{name}: {error}")
                    self.emit({"type": "progress", "step": "keywords", "file": name, "ok": False, "error": error})
                    continue
                record.keyword_files.append(name)
                self.emit({"type": "progress", "step": "keywords", "file": name, "ok": True})

    def _remove_subfolders(self) -> None:
        for record in self.result.renditions:
            part = Path(record.path)
            if remove_folder_if_empty(part):
                self.result.removed_folders.append(str(part))
            else:
                self.result.errors.append(f"{part.name}: left in place, it still holds files")


def merge_manifests(roll: Path, parts: list[GatheredRendition], *, prior: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """One manifest for the roll from its renditions' manifests: the one
    tagged last gives the roll-level fields, apply histories are joined in
    time order, file overrides merged, and each rendition's own scan source
    and roll id are kept under ``gathered_renditions``."""
    tagged = [r for r in parts if r.manifest]
    if prior:
        tagged = [GatheredRendition(rendition="", path=str(roll), manifest=prior), *tagged]
    if not tagged:
        return None
    base = max(tagged, key=lambda r: lib.last_applied_at(r.manifest) or "")
    merged = copy.deepcopy(base.manifest or {})
    merged["roll_id"] = roll.name
    merged["folder_relative_path"] = roll.name
    history = [h for r in tagged for h in (r.manifest or {}).get("apply_history", [])]
    merged["apply_history"] = sorted(history, key=lambda h: h.get("timestamp") or "")
    overrides: dict[str, Any] = {}
    for r in tagged:
        overrides.update((r.manifest or {}).get("file_overrides") or {})
    merged["file_overrides"] = overrides
    merged["gathered_renditions"] = [
        {
            "rendition": r.rendition,
            "roll_id": (r.manifest or {}).get("roll_id"),
            "source_kind": (r.manifest or {}).get("source_kind"),
            "scan_provider_snapshot": (r.manifest or {}).get("scan_provider_snapshot"),
            "scan_method_snapshot": (r.manifest or {}).get("scan_method_snapshot"),
            "applied_files": sorted(lib.applied_files_from_manifest(r.manifest)),
            "files": list(r.files),
        }
        for r in parts
    ]
    merged["updated_at"] = _now()
    return merged


# ---------- undo ----------


def undo_gather(
    session: Session,
    op_id: str,
    *,
    progress: Progress | None = None,
    cancelled: Cancelled = lambda: False,
) -> GatherUndoResult:
    """Put every file back into its rendition folder, restore the renditions'
    manifests and their old Roll keywords, drop the merged manifest."""
    entry = operations.load_for_undo(session, op_id, KIND)
    recorded = GatherResult.from_dict(entry["result"])
    undo = _GatherUndo(recorded, progress or no_progress, cancelled).run()
    if undo.restored or undo.restored_manifests:
        operations.mark_undone(session, op_id, undo.to_dict())
    undo.operation_id = op_id
    return undo


class _GatherUndo:
    def __init__(self, recorded: GatherResult, emit: Progress, cancelled: Cancelled) -> None:
        self.recorded = recorded
        self.emit = emit
        self.cancelled = cancelled
        self.roll = Path(recorded.roll)
        self.result = GatherUndoResult()

    def run(self) -> GatherUndoResult:
        self._move_files_back()
        self._restore_manifests()
        self._restore_roll_keywords()
        return self.result

    def _move_files_back(self) -> None:
        for move in reversed(self.recorded.moves):
            if self.cancelled():
                self.result.errors.append("cancelled before every file was moved back")
                return
            current, original = Path(move.target), Path(move.source)
            try:
                if not current.exists():
                    raise OSError("file is no longer where gathering put it")
                original.parent.mkdir(parents=True, exist_ok=True)
                _move_file(current, original)
            except OSError as exc:
                self.result.errors.append(f"{current.name}: {exc}")
                self.emit({"type": "progress", "step": "move", "file": current.name, "ok": False, "error": str(exc)})
                continue
            self.result.restored.append(Move(source=str(current), target=str(original)))
            self.emit({"type": "progress", "step": "move", "file": current.name, "ok": True})

    def _restore_manifests(self) -> None:
        for record in self.recorded.renditions:
            if not record.manifest:
                continue
            try:
                mf.write_manifest(record.path, record.manifest)
            except OSError as exc:
                self.result.errors.append(f"{record.rendition}: could not restore its manifest: {exc}")
                continue
            self.result.restored_manifests.append(str(mf.manifest_path_for(record.path)))
        merged = mf.manifest_path_for(self.roll)
        try:
            if self.recorded.prior_manifest:
                mf.write_manifest(self.roll, self.recorded.prior_manifest)
            elif merged.is_file():
                merged.unlink()
        except OSError as exc:
            self.result.errors.append(f"{self.roll.name}: could not restore its manifest: {exc}")

    def _restore_roll_keywords(self) -> None:
        for record in self.recorded.renditions:
            old = (record.manifest or {}).get("roll_id")
            if not old:
                continue
            for name in record.keyword_files:
                if self.cancelled():
                    self.result.errors.append("cancelled before every Roll keyword was restored")
                    return
                path = Path(record.path) / name
                if not path.is_file():
                    continue
                error = _write_roll_keyword(path, old)
                if error:
                    self.result.errors.append(f"{name}: {error}")
                    continue
                self.result.keyword_files.append(name)


# ---------- helpers ----------


def _move_file(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        raise OSError(f"{dst.name}: target already exists")
    os.rename(src, dst)  # inside one folder tree: same volume, atomic


def _read_manifest_or_none(folder: Path | str) -> dict[str, Any] | None:
    try:
        return mf.read_manifest(folder)
    except (OSError, ValueError):
        return None


def _write_roll_keyword(path: Path, roll: str) -> str | None:
    """Give ``path`` the ``Roll|roll`` keyword; the reconciler drops the old
    Roll family and keeps everything else. Returns the error, if any."""
    try:
        result = exiftool.write(path, et.WriteIntent(hierarchical_keywords=[f"Roll|{roll}"], flat_keywords=[roll]))
    except Exception as exc:  # noqa: BLE001 - report per file, keep going
        return str(exc)
    return None if result.ok else result.error
