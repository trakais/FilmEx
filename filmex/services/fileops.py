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
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session

from filmex.schemas import DryRunRequest
from filmex.services import exiftool_runner as exiftool
from filmex.services import frames as fr
from filmex.services import manifest as mf
from filmex.services import operations
from filmex.services import tagging as tg

Progress = Callable[[dict[str, Any]], None]
Cancelled = Callable[[], bool]

SIBLINGS = "siblings"
SUBFOLDERS = "subfolders"


def no_progress(event: dict[str, Any]) -> None:
    pass


class PlanError(ValueError):
    """The batch cannot run as requested; ``errors`` lists why."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class RollDraft:
    """One roll draft from the contact sheet."""

    name: str
    frames: list[str]
    tagging: dict[str, Any] = field(default_factory=dict)
    year: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RollDraft:
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


def resolve_rolls(
    listing: fr.FrameListing, rolls: list[RollDraft]
) -> tuple[list[tuple[RollDraft, list[fr.Frame]]], list[str], list[str]]:
    """Match roll frame keys against the listing.

    Returns ``(resolved, unassigned_keys, errors)``. Each resolved roll is
    a copy with its folder name cleaned; the caller's rolls are left as
    they are. A frame may belong to one roll only; unknown keys and empty
    rolls are errors.
    """
    by_key = fr.frames_by_key(listing)
    errors: list[str] = []
    seen_frames: dict[str, str] = {}
    seen_names: list[str] = []
    resolved: list[tuple[RollDraft, list[fr.Frame]]] = []
    for index, roll in enumerate(rolls, start=1):
        label = roll.name or f"roll {index}"
        try:
            roll = replace(roll, name=clean_name(roll.name))
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
        if any(same_name(roll.name, other) for other in seen_names):
            errors.append(f"{roll.name}: two rolls would get the same folder name")
        seen_names.append(roll.name)
        if not roll.frames:
            errors.append(f"{label}: no frames")
        frames: list[fr.Frame] = []
        for key in roll.frames:
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
        resolved.append((roll, frames))
    unassigned = [f.key for f in listing.frames if f.key not in seen_frames]
    return resolved, unassigned, errors


@dataclass
class TagOutcome:
    """What tagging a new roll folder did: the files written, per-file or
    request errors, and ``skipped`` with the reason when nothing was tried."""

    applied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: str | None = None


def tag_folder(
    session: Session,
    folder: Path,
    tagging: dict[str, Any],
    *,
    progress: Progress | None = None,
    cancelled: Cancelled = lambda: False,
) -> TagOutcome:
    """Run the apply pipeline on every image in ``folder``.

    Without ExifTool the files are left untagged and the outcome says so; the
    Library then shows the folder as untagged, ready for the Tag view.
    """
    emit = progress or no_progress
    if not exiftool.available():
        return TagOutcome(skipped="ExifTool not available: files were moved but not tagged")
    try:
        tagging_plan = tg.plan_tagging(DryRunRequest(folder=str(folder), **tagging), session)
    except tg.TaggingError as exc:
        return TagOutcome(errors=[f"tagging: {exc.detail}"])
    applied = tg.apply_all(
        tagging_plan,
        progress=lambda event: emit({**event, "step": "tag", "roll": folder.name}),
        cancelled=cancelled,
    )
    return TagOutcome(applied=applied.applied_files, errors=applied.errors)


def remove_roll_folder(folder: Path) -> str | None:
    """Remove a roll folder a batch created: its manifest, OS litter, then the
    folder. Returns what stopped it, or None once the folder is gone."""
    manifest_path = mf.manifest_path_for(folder)
    if manifest_path.is_file():
        try:
            manifest_path.unlink()
        except OSError as exc:
            return f"{folder.name}: could not remove its manifest: {exc}"
    if remove_folder_if_empty(folder):
        return None
    return f"{folder.name}: left in place, it still holds files"


# ---------- split: plan ----------


@dataclass
class SplitItem:
    name: str
    target: str
    frames: list[str]
    files: list[str]
    image_count: int
    bytes: int
    tagging: dict[str, Any] = field(default_factory=dict)
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
    def problems(self) -> list[str]:
        """Every reason the plan cannot run; roll problems name their roll."""
        return self.errors + [f"{i.name}: {e}" for i in self.items for e in i.errors]

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        data["problems"] = self.problems
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


def plan_split(source: Path | str, rolls: list[RollDraft], *, root: str | None) -> SplitPlan:
    source_path = Path(source).expanduser()
    errors: list[str] = []
    if not source_path.is_dir():
        raise PlanError([f"folder not found: {source_path}"])
    listing = fr.list_frames(source_path)
    parent, layout = target_parent_for(source_path, root)
    if not same_volume(source_path, parent):
        errors.append("the target is on a different volume; a split only moves files within one volume")

    resolved, unassigned, roll_errors = resolve_rolls(listing, rolls)
    errors.extend(roll_errors)
    items: list[SplitItem] = []
    for roll, frames in resolved:
        target = parent / roll.name
        item_errors: list[str] = []
        if target.exists() or target.is_symlink():
            if layout == SUBFOLDERS or not same_path(target, source_path):
                item_errors.append("a folder with this name already exists")
            else:
                item_errors.append("this is the source folder's own name")
        files = [name for f in frames for name in f.files]
        items.append(
            SplitItem(
                name=roll.name,
                target=str(target),
                frames=[f.key for f in frames],
                files=files,
                image_count=sum(len(f.images) for f in frames),
                bytes=sum(f.bytes for f in frames),
                tagging=dict(roll.tagging),
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


def same_path(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return False


# ---------- split: results ----------


@dataclass
class Move:
    """One file that changed place. Journaled as ``{"from", "to"}``."""

    source: str
    target: str

    def to_dict(self) -> dict[str, str]:
        return {"from": self.source, "to": self.target}

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> Move:
        return cls(source=data["from"], target=data["to"])


@dataclass
class SplitRoll:
    """One roll folder a split created."""

    name: str
    path: str
    files: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tag_skipped: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SplitRoll:
        return cls(
            name=data["name"],
            path=data["path"],
            files=list(data.get("files") or []),
            applied=list(data.get("applied") or []),
            errors=list(data.get("errors") or []),
            tag_skipped=data.get("tag_skipped"),
        )


@dataclass
class SplitResult:
    """What a split did. ``to_dict`` is both the journal entry and what the
    split job reports; ``from_dict`` reads the journal back for undo."""

    created: list[SplitRoll] = field(default_factory=list)
    moves: list[Move] = field(default_factory=list)
    removed_source: bool = False
    stashed_manifest: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    operation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "created": [roll.to_dict() for roll in self.created],
            "moves": [move.to_dict() for move in self.moves],
            "removed_source": self.removed_source,
            "stashed_manifest": self.stashed_manifest,
            "errors": list(self.errors),
            "cancelled": self.cancelled,
        }
        if self.operation_id:
            data["operation_id"] = self.operation_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SplitResult:
        return cls(
            created=[SplitRoll.from_dict(r) for r in data.get("created") or []],
            moves=[Move.from_dict(m) for m in data.get("moves") or []],
            removed_source=bool(data.get("removed_source")),
            stashed_manifest=data.get("stashed_manifest"),
            errors=list(data.get("errors") or []),
            cancelled=bool(data.get("cancelled")),
        )


@dataclass
class SplitUndoResult:
    restored: list[Move] = field(default_factory=list)
    removed_folders: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    note: str = "Metadata written by the split stays on the files; re-tag the folder to replace it."
    operation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "restored": [move.to_dict() for move in self.restored],
            "removed_folders": list(self.removed_folders),
            "errors": list(self.errors),
            "note": self.note,
        }
        if self.operation_id:
            data["operation_id"] = self.operation_id
        return data


# ---------- split: apply ----------


def apply_split(
    session: Session,
    source: Path | str,
    rolls: list[RollDraft],
    *,
    root: str | None,
    progress: Progress | None = None,
    cancelled: Cancelled = lambda: False,
) -> SplitResult:
    """Create the roll folders, move the frames, tag each new folder, journal it.

    The plan is recomputed right before running so a change on disk since
    the preview stops the batch instead of moving the wrong files.
    """
    plan = plan_split(source, rolls, root=root)
    if not plan.ok:
        raise PlanError(plan.problems)
    journal = {
        "source": plan.source,
        "target_parent": plan.target_parent,
        "layout": plan.layout,
        "items": [{"name": i.name, "target": i.target, "frames": i.frames} for i in plan.items],
    }
    op = operations.start(session, "split", journal, root=root)
    result = _Split(session, plan, progress or no_progress, cancelled).run()
    status = operations.FAILED if result.errors and not result.moves else operations.DONE
    operations.finish(session, op.id, status, result.to_dict())
    result.operation_id = op.id
    return result


class _Split:
    """Runs a checked split plan: for each roll, create its folder, move its
    frames in, tag it. A file-system error stops the batch; everything moved
    until then is in the result, so undo can put it back."""

    def __init__(self, session: Session, plan: SplitPlan, emit: Progress, cancelled: Cancelled) -> None:
        self.session = session
        self.plan = plan
        self.emit = emit
        self.cancelled = cancelled
        self.source = Path(plan.source)
        self.total_files = sum(len(i.files) for i in plan.items)
        self.result = SplitResult()

    def run(self) -> SplitResult:
        try:
            for item in self.plan.items:
                if self.cancelled():
                    self.result.cancelled = True
                    break
                roll = self._move_roll(item)
                if self.result.cancelled:
                    break
                self._tag_roll(roll, item)
        except OSError as exc:
            self.result.errors.append(f"stopped: {exc}")
        self._retire_source()
        return self.result

    def _move_roll(self, item: SplitItem) -> SplitRoll:
        target = Path(item.target)
        target.mkdir(parents=False, exist_ok=False)
        roll = SplitRoll(name=item.name, path=item.target)
        self.result.created.append(roll)
        for name in item.files:
            if self.cancelled():
                self.result.cancelled = True
                break
            _move_file(self.source / name, target / name)
            self.result.moves.append(Move(source=str(self.source / name), target=str(target / name)))
            roll.files.append(name)
            self.emit({"type": "progress", "step": "move", "roll": roll.name, "file": name,
                       "done": len(self.result.moves), "total": self.total_files})
        return roll

    def _tag_roll(self, roll: SplitRoll, item: SplitItem) -> None:
        outcome = tag_folder(self.session, Path(roll.path), item.tagging, progress=self.emit, cancelled=self.cancelled)
        roll.applied, roll.errors, roll.tag_skipped = outcome.applied, outcome.errors, outcome.skipped
        self.result.errors.extend(f"{roll.name}: {e}" for e in outcome.errors)
        self.emit({"type": "progress", "step": "roll", "roll": roll.name, "files": len(roll.files), "applied": len(roll.applied)})

    def _retire_source(self) -> None:
        # A source with no images left keeps nothing worth a folder: its
        # manifest goes into the journal (undo restores it) and the folder is
        # removed when only litter remains. In the subfolders layout the folder
        # stays, since it now holds the new rolls, but the stale manifest goes.
        self.result.stashed_manifest, self.result.removed_source = _retire_source(
            self.source, remove_folder=self.plan.layout == SIBLINGS
        )


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
) -> SplitUndoResult:
    """Move every frame back, restore the source folder and its manifest,
    remove the roll folders the split created.

    Metadata written by the split's tagging step stays on the files; the
    result says so, and re-tagging the source folder replaces it.
    """
    entry = operations.load_for_undo(session, op_id, "split")
    recorded = SplitResult.from_dict(entry["result"])
    source = Path(entry["plan"]["source"])
    undo = _SplitUndo(recorded, source, progress or no_progress, cancelled).run()
    if undo.restored or undo.removed_folders:
        operations.mark_undone(session, op_id, undo.to_dict())
    undo.operation_id = op_id
    return undo


class _SplitUndo:
    def __init__(self, recorded: SplitResult, source: Path, emit: Progress, cancelled: Cancelled) -> None:
        self.recorded = recorded
        self.source = source
        self.emit = emit
        self.cancelled = cancelled
        self.result = SplitUndoResult()

    def run(self) -> SplitUndoResult:
        self.source.mkdir(parents=True, exist_ok=True)
        self._move_frames_back()
        for roll in self.recorded.created:
            self._remove_roll(Path(roll.path))
        if self.recorded.stashed_manifest and not mf.manifest_path_for(self.source).exists():
            mf.write_manifest(self.source, self.recorded.stashed_manifest)
        return self.result

    def _move_frames_back(self) -> None:
        for move in reversed(self.recorded.moves):
            if self.cancelled():
                self.result.errors.append("cancelled before every file was moved back")
                return
            current, original = Path(move.target), Path(move.source)
            try:
                if not current.exists():
                    raise OSError("file is no longer where the split put it")
                _move_file(current, original)
            except OSError as exc:
                self.result.errors.append(f"{current.name}: {exc}")
                self.emit({"type": "progress", "step": "move", "file": current.name, "ok": False, "error": str(exc)})
                continue
            self.result.restored.append(Move(source=str(current), target=str(original)))
            self.emit({"type": "progress", "step": "move", "file": current.name, "ok": True})

    def _remove_roll(self, folder: Path) -> None:
        if not folder.is_dir():
            return
        self._return_tagging_sidecars(folder)
        problem = remove_roll_folder(folder)
        if problem:
            self.result.errors.append(problem)
        else:
            self.result.removed_folders.append(str(folder))

    def _return_tagging_sidecars(self, folder: Path) -> None:
        """Sidecars the tagging step wrote go back with their raw files, so
        the metadata stays with the frame and the folder can be removed."""
        moved = {Path(m.target).name for m in self.recorded.moves if Path(m.target).parent == folder}
        for sidecar in tagging_sidecars(folder, sorted(moved)):
            if sidecar.name in moved:
                continue
            back = self.source / sidecar.name
            try:
                _move_file(sidecar, back)
            except OSError as exc:
                self.result.errors.append(f"{sidecar.name}: {exc}")
                continue
            self.result.restored.append(Move(source=str(sidecar), target=str(back)))
