"""Inbox import: copy rolls into ``<root>/<year>/<name>``, verify, tag, journal.

Order of operations per roll, and why:
1. create the destination (refuse if it exists; a collision stops the batch
   during planning, before any copy)
2. copy every frame file in chunks, hashing the source bytes on the way
3. read the destination back and compare size and BLAKE2b, so what is
   checked is what landed on the share, not what left the buffer
4. run the tagging pipeline at the destination; the manifest records the
   source folder under ``import_source``
5. in move mode, delete the sources, and only once every file of the roll
   verified

Undo copies moved files back (verified against the recorded hash) or
removes verified duplicates, then removes the folders the import created.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session

from filmex.services import fileops, operations
from filmex.services import frames as fr
from filmex.services import manifest as mf
from filmex.services.fileops import Cancelled, PlanError, Progress, RollDraft, no_progress

CHUNK = 4 * 1024 * 1024
HEADROOM = 64 * 1024 * 1024
MODES = ("copy", "move")


class VerifyError(RuntimeError):
    """The destination does not match the source after the copy."""


@dataclass(frozen=True)
class VerifiedCopy:
    """A copy that was read back and matched its source."""

    size: int
    digest: str  # BLAKE2b, 32 bytes, hex


# ---------- copy and verify ----------


def hash_file(path: Path, *, chunk: int = CHUNK) -> str:
    digest = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def copy_verify(src: Path, dst: Path, *, progress: Progress | None = None, chunk: int = CHUNK) -> VerifiedCopy:
    """Copy ``src`` to a new ``dst`` and prove the copy by reading it back.

    On mismatch the bad copy is removed and ``VerifyError`` raised, so a
    corrupt file never stays at the destination.
    """
    emit = progress or no_progress
    size = os.stat(src).st_size
    digest = hashlib.blake2b(digest_size=32)
    done = 0
    with open(src, "rb") as reader, open(dst, "xb") as writer:  # x: never overwrite
        while True:
            block = reader.read(chunk)
            if not block:
                break
            digest.update(block)
            writer.write(block)
            done += len(block)
            emit({"type": "progress", "step": "copy", "file": src.name, "bytes": done, "size": size})
        writer.flush()
        os.fsync(writer.fileno())
    expected = digest.hexdigest()
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass  # timestamps are nice to keep, not a reason to fail
    landed = os.stat(dst).st_size
    actual = hash_file(dst, chunk=chunk) if landed == size else None
    if landed != size or actual != expected:
        try:
            os.remove(dst)
        except OSError:
            pass
        raise VerifyError(
            "destination differs from source"
            + (f" (size {landed} != {size})" if landed != size else " (hash mismatch)")
        )
    return VerifiedCopy(size=size, digest=expected)


# ---------- plan ----------


@dataclass
class ImportItem:
    name: str
    year: str | None
    target: str
    frames: list[str]
    files: list[dict[str, Any]]  # {name, bytes}
    image_count: int
    bytes: int
    tagging: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class ImportPlan:
    source: str
    root: str
    mode: str
    items: list[ImportItem]
    unassigned: list[str]
    other_files: list[str]
    errors: list[str]
    total_bytes: int
    free_bytes: int | None

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
        data["file_count"] = sum(len(i.files) for i in self.items)
        return data


def resolve_year(roll: RollDraft, source: Path, frames: list[fr.Frame]) -> str | None:
    """Year folder for a roll: explicit, else from the roll date, else from
    the oldest file in it."""
    if roll.year and roll.year.isdigit() and len(roll.year) == 4:
        return roll.year
    date = (roll.tagging or {}).get("roll_date") or ""
    if len(date) >= 4 and date[:4].isdigit():
        return date[:4]
    earliest = fr.earliest_file_date(source, frames)
    return earliest[:4] if earliest else None


def plan_import(source: Path | str, rolls: list[RollDraft], *, root: str | None, mode: str) -> ImportPlan:
    source_path = Path(source).expanduser()
    errors: list[str] = []
    if not source_path.is_dir():
        raise PlanError([f"folder not found: {source_path}"])
    if not root:
        raise PlanError(["no library root configured"])
    root_path = Path(root).expanduser()
    if not root_path.is_dir():
        raise PlanError([f"library root not reachable: {root}"])
    if mode not in MODES:
        raise PlanError([f"unknown import mode: {mode}"])

    listing = fr.list_frames(source_path)
    resolved, unassigned, roll_errors = fileops.resolve_rolls(listing, rolls)
    errors.extend(roll_errors)

    items: list[ImportItem] = []
    taken: list[str] = []
    for roll, frames in resolved:
        item_errors: list[str] = []
        year = resolve_year(roll, source_path, frames)
        if year is None:
            item_errors.append("no roll date and no file dates; set a date to pick the year folder")
        target = root_path / (year or "_") / roll.name
        if year is not None:
            if target.exists() or target.is_symlink():
                item_errors.append("a folder with this name already exists in that year")
            if any(fileops.same_name(str(target), t) for t in taken):
                item_errors.append("two rolls would land in the same folder")
            if fileops.same_path(target.parent, source_path) or _is_under(target, source_path):
                item_errors.append("the destination is inside the source folder; use Split instead")
            taken.append(str(target))
        files = [{"name": name, "bytes": _size(source_path / name)} for f in frames for name in f.files]
        items.append(
            ImportItem(
                name=roll.name,
                year=year,
                target=str(target),
                frames=[f.key for f in frames],
                files=files,
                image_count=sum(len(f.images) for f in frames),
                bytes=sum(f["bytes"] for f in files),
                tagging=dict(roll.tagging),
                errors=item_errors,
            )
        )
    if not items:
        errors.append("no rolls to import")

    total = sum(i.bytes for i in items)
    free: int | None
    try:
        free = shutil.disk_usage(root_path).free
    except OSError:
        free = None
    if free is not None and total + HEADROOM > free:
        errors.append(f"not enough free space in the library: {_human(total)} needed, {_human(free)} free")
    return ImportPlan(
        source=str(source_path),
        root=str(root_path),
        mode=mode,
        items=items,
        unassigned=unassigned,
        other_files=listing.other_files,
        errors=errors,
        total_bytes=total,
        free_bytes=free,
    )


def _size(path: Path) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def _is_under(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


# ---------- apply ----------


# ---------- results ----------


@dataclass
class ImportedFile:
    """One file copied into the library. Journaled with ``from``/``to``."""

    name: str
    source: str
    target: str
    size: int
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "from": self.source, "to": self.target, "size": self.size, "digest": self.digest}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImportedFile:
        return cls(name=data["name"], source=data["from"], target=data["to"], size=data["size"], digest=data["digest"])


@dataclass
class ImportedRoll:
    """One roll folder an import created, and how far it got."""

    name: str
    path: str
    year_dir: str
    year_dir_created: bool = False
    files: list[ImportedFile] = field(default_factory=list)
    verified: bool = False
    applied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tag_skipped: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "year_dir": self.year_dir,
            "year_dir_created": self.year_dir_created,
            "files": [f.to_dict() for f in self.files],
            "verified": self.verified,
            "applied": list(self.applied),
            "errors": list(self.errors),
            "tag_skipped": self.tag_skipped,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImportedRoll:
        return cls(
            name=data["name"],
            path=data["path"],
            year_dir=data.get("year_dir") or str(Path(data["path"]).parent),
            year_dir_created=bool(data.get("year_dir_created")),
            files=[ImportedFile.from_dict(f) for f in data.get("files") or []],
            verified=bool(data.get("verified")),
            applied=list(data.get("applied") or []),
            errors=list(data.get("errors") or []),
            tag_skipped=data.get("tag_skipped"),
        )


@dataclass
class ImportResult:
    """What an import did. ``to_dict`` is both the journal entry and what the
    import job reports; ``from_dict`` reads the journal back for undo."""

    mode: str
    created: list[ImportedRoll] = field(default_factory=list)
    removed_sources: list[str] = field(default_factory=list)
    cancelled: bool = False
    operation_id: str | None = None

    @property
    def errors(self) -> list[str]:
        return [f"{roll.name}: {e}" for roll in self.created for e in roll.errors]

    def to_dict(self) -> dict[str, Any]:
        data = {
            "created": [roll.to_dict() for roll in self.created],
            "removed_sources": list(self.removed_sources),
            "errors": self.errors,
            "cancelled": self.cancelled,
            "mode": self.mode,
        }
        if self.operation_id:
            data["operation_id"] = self.operation_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImportResult:
        return cls(
            mode=data.get("mode") or "",
            created=[ImportedRoll.from_dict(r) for r in data.get("created") or []],
            removed_sources=list(data.get("removed_sources") or []),
            cancelled=bool(data.get("cancelled")),
        )


@dataclass
class ImportUndoResult:
    restored: list[str] = field(default_factory=list)  # moved files copied back to the source
    removed: list[str] = field(default_factory=list)  # files removed from the library
    removed_folders: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    operation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if not self.operation_id:
            data.pop("operation_id")
        return data


# ---------- apply ----------


def apply_import(
    session: Session,
    source: Path | str,
    rolls: list[RollDraft],
    *,
    root: str | None,
    mode: str,
    progress: Progress | None = None,
    cancelled: Cancelled = lambda: False,
) -> ImportResult:
    plan = plan_import(source, rolls, root=root, mode=mode)
    if not plan.ok:
        raise PlanError(plan.problems)
    journal = {
        "source": plan.source,
        "root": plan.root,
        "mode": mode,
        "items": [{"name": i.name, "year": i.year, "target": i.target, "frames": i.frames} for i in plan.items],
    }
    op = operations.start(session, "import", journal, root=root)
    result = _Import(session, plan, progress or no_progress, cancelled).run()
    copied_any = any(roll.files for roll in result.created)
    status = operations.FAILED if result.errors and not copied_any else operations.DONE
    operations.finish(session, op.id, status, result.to_dict())
    result.operation_id = op.id
    return result


class _Meter:
    """Running file and byte totals behind an import's progress events."""

    def __init__(self, emit: Progress, *, files_total: int, bytes_total: int) -> None:
        self.emit = emit
        self.files_total = files_total
        self.bytes_total = bytes_total
        self.files_done = 0
        self.bytes_done = 0

    def copying(self, roll: str, name: str) -> Progress:
        """Progress callback for ``copy_verify`` while ``name`` copies."""
        return lambda event: self.emit({
            **event, "roll": roll, "file": name,
            "files_done": self.files_done, "files_total": self.files_total,
            "bytes_done": self.bytes_done + event["bytes"], "bytes_total": self.bytes_total,
        })

    def verified(self, roll: str, name: str, size: int) -> None:
        self.files_done += 1
        self.bytes_done += size
        self.emit({"type": "progress", "step": "verify", "roll": roll, "file": name, "ok": True,
                   "files_done": self.files_done, "files_total": self.files_total,
                   "bytes_done": self.bytes_done, "bytes_total": self.bytes_total})


class _Import:
    """Runs a checked import plan roll by roll: create the folder, copy and
    verify every file, tag the roll, then (move mode) delete its sources.
    A failure stops the batch at that roll; later rolls are not started."""

    def __init__(self, session: Session, plan: ImportPlan, emit: Progress, cancelled: Cancelled) -> None:
        self.session = session
        self.plan = plan
        self.emit = emit
        self.cancelled = cancelled
        self.source = Path(plan.source)
        self.meter = _Meter(emit, files_total=sum(len(i.files) for i in plan.items), bytes_total=plan.total_bytes)
        self.result = ImportResult(mode=plan.mode)

    def run(self) -> ImportResult:
        for item in self.plan.items:
            if self.cancelled():
                self.result.cancelled = True
                break
            roll = self._import_roll(item)
            self.result.created.append(roll)
            if not roll.verified:
                # The roll stopped part way: an error says why, no error means a cancel.
                self.result.cancelled = not roll.errors
                break
        return self.result

    def _import_roll(self, item: ImportItem) -> ImportedRoll:
        target = Path(item.target)
        roll = ImportedRoll(name=item.name, path=item.target, year_dir=str(target.parent))
        try:
            roll.year_dir_created = _create_roll_folder(target)
        except OSError as exc:
            roll.errors.append(f"could not create the folder: {exc}")
            return roll
        for entry in item.files:
            if self.cancelled():
                return roll
            try:
                roll.files.append(self._copy(roll.name, entry["name"], target))
            except (OSError, VerifyError) as exc:
                self._copy_failed(roll, entry["name"], exc)
                return roll
        roll.verified = True
        self._tag(roll, item, target)
        if self.plan.mode == "move":
            self._remove_sources(roll)
        self.emit({"type": "progress", "step": "roll", "roll": roll.name, "files": len(roll.files),
                   "applied": len(roll.applied), "verified": True})
        return roll

    def _copy(self, roll: str, name: str, target: Path) -> ImportedFile:
        src, dst = self.source / name, target / name
        copy = copy_verify(src, dst, progress=self.meter.copying(roll, name))
        self.meter.verified(roll, name, copy.size)
        return ImportedFile(name=name, source=str(src), target=str(dst), size=copy.size, digest=copy.digest)

    def _copy_failed(self, roll: ImportedRoll, name: str, exc: Exception) -> None:
        message = f"{name}: {exc}"
        roll.errors.append(message)
        self.emit({"type": "progress", "step": "verify", "roll": roll.name, "file": name, "ok": False, "error": message})

    def _tag(self, roll: ImportedRoll, item: ImportItem, target: Path) -> None:
        outcome = fileops.tag_folder(self.session, target, item.tagging, progress=self.emit, cancelled=self.cancelled)
        roll.applied, roll.tag_skipped = outcome.applied, outcome.skipped
        roll.errors.extend(outcome.errors)
        _annotate_manifest(target, source=self.source, mode=self.plan.mode, files=roll.files)

    def _remove_sources(self, roll: ImportedRoll) -> None:
        for copied in roll.files:
            try:
                os.remove(copied.source)
            except OSError as exc:
                roll.errors.append(f"{copied.name}: source could not be removed: {exc}")
                continue
            self.result.removed_sources.append(copied.source)
            self.emit({"type": "progress", "step": "remove", "roll": roll.name, "file": copied.name, "ok": True})


def _create_roll_folder(target: Path) -> bool:
    """Create ``target``, and its year folder when missing. Returns whether
    the year folder was created too, so undo knows it may remove it."""
    year_created = False
    if not target.parent.exists():
        target.parent.mkdir(parents=False)
        year_created = True
    target.mkdir(parents=False, exist_ok=False)
    return year_created


def _annotate_manifest(folder: Path, *, source: Path, mode: str, files: list[ImportedFile]) -> None:
    manifest = mf.read_manifest(folder)
    if not manifest:
        return
    manifest["import_source"] = {
        "path": str(source),
        "imported_at": datetime.now(UTC).isoformat(),
        "verified": True,
        "mode": mode,
        "files": [{"name": f.name, "size": f.size, "blake2b": f.digest} for f in files],
    }
    manifest["updated_at"] = datetime.now(UTC).isoformat()
    mf.write_manifest(folder, manifest)


# ---------- undo ----------


def undo_import(
    session: Session,
    op_id: str,
    *,
    progress: Progress | None = None,
    cancelled: Cancelled = lambda: False,
) -> ImportUndoResult:
    """Reverse an import: moved files go back (verified), copies are removed
    once the source is confirmed present, created folders are removed."""
    entry = operations.load_for_undo(session, op_id, "import")
    recorded = ImportResult.from_dict(entry["result"])
    mode = recorded.mode or entry["plan"].get("mode") or "copy"
    source = Path(entry["plan"]["source"])
    undo = _ImportUndo(recorded, source, mode, progress or no_progress, cancelled).run()
    if undo.removed or undo.removed_folders:
        operations.mark_undone(session, op_id, undo.to_dict())
    undo.operation_id = op_id
    return undo


class _ImportUndo:
    """Puts an import back, last roll first. A copy is removed only once its
    original is confirmed in the source; a moved file is copied back and
    verified first. Folders the import created go when they are empty."""

    def __init__(self, recorded: ImportResult, source: Path, mode: str, emit: Progress, cancelled: Cancelled) -> None:
        self.recorded = recorded
        self.source = source
        self.mode = mode
        self.emit = emit
        self.cancelled = cancelled
        self.result = ImportUndoResult()

    def run(self) -> ImportUndoResult:
        for roll in reversed(self.recorded.created):
            for copied in reversed(roll.files):
                if self.cancelled():
                    self.result.errors.append("cancelled before every file was handled")
                    return self.result
                self._put_back(copied)
            self._remove_roll(roll)
        return self.result

    def _put_back(self, copied: ImportedFile) -> None:
        target = Path(copied.target)
        if not target.exists():
            return
        try:
            self._ensure_original(copied)
            os.remove(target)
        except (OSError, VerifyError) as exc:
            self.result.errors.append(f"{copied.name}: {exc}")
            self.emit({"type": "progress", "step": "restore", "file": copied.name, "ok": False, "error": str(exc)})
            return
        self.result.removed.append(str(target))
        self.emit({"type": "progress", "step": "restore", "file": copied.name, "ok": True})

    def _ensure_original(self, copied: ImportedFile) -> None:
        """The original must be in the source before its copy may go."""
        original = Path(copied.source)
        if original.exists():
            if os.stat(original).st_size != copied.size:
                raise OSError("source differs in size; destination kept")
            return
        if self.mode != "move":
            raise OSError("source is gone; destination kept")
        original.parent.mkdir(parents=True, exist_ok=True)
        if copy_verify(Path(copied.target), original).digest != copied.digest:
            os.remove(original)
            raise OSError("destination no longer matches the imported file; kept")
        self.result.restored.append(str(original))

    def _remove_roll(self, roll: ImportedRoll) -> None:
        folder = Path(roll.path)
        if not folder.is_dir():
            return
        self._settle_tagging_sidecars(folder, roll)
        problem = fileops.remove_roll_folder(folder)
        if problem:
            self.result.errors.append(problem)
            return
        self.result.removed_folders.append(str(folder))
        if roll.year_dir_created and fileops.remove_folder_if_empty(Path(roll.year_dir)):
            self.result.removed_folders.append(roll.year_dir)

    def _settle_tagging_sidecars(self, folder: Path, roll: ImportedRoll) -> None:
        """Sidecars the tagging step wrote: after a move they follow their raw
        file back to the source; after a copy they go with the copy."""
        copied = {f.name for f in roll.files}
        for sidecar in fileops.tagging_sidecars(folder, sorted(copied)):
            if sidecar.name in copied:
                continue
            try:
                if self.mode == "move":
                    back = self.source / sidecar.name
                    if back.exists():
                        raise OSError("a sidecar with that name is back in the source already")
                    copy_verify(sidecar, back)
                os.remove(sidecar)
            except (OSError, VerifyError) as exc:
                self.result.errors.append(f"{sidecar.name}: {exc}")
                continue
            self.result.removed.append(str(sidecar))
