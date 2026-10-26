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
from filmex.services.fileops import Cancelled, PlanError, Progress, Segment

CHUNK = 4 * 1024 * 1024
HEADROOM = 64 * 1024 * 1024
MODES = ("copy", "move")


class VerifyError(RuntimeError):
    """The destination does not match the source after the copy."""


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


def copy_verify(src: Path, dst: Path, *, progress: Progress | None = None, chunk: int = CHUNK) -> dict[str, Any]:
    """Copy ``src`` to a new ``dst`` and prove the copy by reading it back.

    Returns ``{"size", "digest"}``. On mismatch the bad copy is removed and
    ``VerifyError`` raised, so a corrupt file never stays at the destination.
    """
    emit = progress or (lambda e: None)
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
            f"{src.name}: destination differs from source"
            + (f" (size {landed} != {size})" if landed != size else " (hash mismatch)")
        )
    return {"size": size, "digest": expected}


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
    def ok(self) -> bool:
        return not self.errors and not any(i.errors for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        data["file_count"] = sum(len(i.files) for i in self.items)
        return data


def resolve_year(segment: Segment, source: Path, frames: list[fr.Frame]) -> str | None:
    """Year folder for a roll: explicit, else from the roll date, else from
    the oldest file in it."""
    if segment.year and segment.year.isdigit() and len(segment.year) == 4:
        return segment.year
    date = (segment.tagging or {}).get("roll_date") or ""
    if len(date) >= 4 and date[:4].isdigit():
        return date[:4]
    earliest = fr.earliest_file_date(source, frames)
    return earliest[:4] if earliest else None


def plan_import(source: Path | str, segments: list[Segment], *, root: str | None, mode: str) -> ImportPlan:
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
    resolved, unassigned, seg_errors = fileops.resolve_segments(listing, segments)
    errors.extend(seg_errors)

    items: list[ImportItem] = []
    taken: list[str] = []
    for seg, frames in resolved:
        item_errors: list[str] = []
        year = resolve_year(seg, source_path, frames)
        if year is None:
            item_errors.append("no roll date and no file dates; set a date to pick the year folder")
        target = root_path / (year or "_") / seg.name
        if year is not None:
            if target.exists() or target.is_symlink():
                item_errors.append("a folder with this name already exists in that year")
            if any(fileops.same_name(str(target), t) for t in taken):
                item_errors.append("two rolls would land in the same folder")
            if fileops._same_path(target.parent, source_path) or _is_under(target, source_path):
                item_errors.append("the destination is inside the source folder; use Split instead")
            taken.append(str(target))
        files = [{"name": name, "bytes": _size(source_path / name)} for f in frames for name in f.files]
        items.append(
            ImportItem(
                name=seg.name,
                year=year,
                target=str(target),
                frames=[f.key for f in frames],
                files=files,
                image_count=sum(len(f.images) for f in frames),
                bytes=sum(f["bytes"] for f in files),
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


def apply_import(
    session: Session,
    source: Path | str,
    segments: list[Segment],
    *,
    root: str | None,
    mode: str,
    progress: Progress | None = None,
    cancelled: Cancelled = lambda: False,
) -> dict[str, Any]:
    emit = progress or (lambda e: None)
    plan = plan_import(source, segments, root=root, mode=mode)
    if not plan.ok:
        raise PlanError(plan.errors + [f"{i.name}: {e}" for i in plan.items for e in i.errors])
    source_path = Path(plan.source)
    by_name = {seg.name: seg for seg in segments}

    op = operations.start(
        session,
        "import",
        {"source": plan.source, "root": plan.root, "mode": mode,
         "items": [{"name": i.name, "year": i.year, "target": i.target, "frames": i.frames} for i in plan.items]},
        root=root,
    )
    created: list[dict[str, Any]] = []
    removed_sources: list[str] = []
    errors: list[str] = []
    was_cancelled = False
    total_bytes = plan.total_bytes
    total_files = sum(len(i.files) for i in plan.items)
    bytes_before = 0
    files_done = 0

    for item in plan.items:
        if cancelled():
            was_cancelled = True
            break
        target = Path(item.target)
        year_dir = target.parent
        record: dict[str, Any] = {
            "name": item.name, "path": item.target, "year_dir": str(year_dir), "year_dir_created": False,
            "files": [], "verified": False, "applied": [], "errors": [], "tag_skipped": None,
        }
        created.append(record)
        try:
            if not year_dir.exists():
                year_dir.mkdir(parents=False)
                record["year_dir_created"] = True
            target.mkdir(parents=False, exist_ok=False)
        except OSError as exc:
            record["errors"].append(f"could not create the folder: {exc}")
            errors.append(f"{item.name}: could not create the folder: {exc}")
            break

        roll_ok = True
        for entry in item.files:
            if cancelled():
                was_cancelled = True
                break
            name = entry["name"]
            src, dst = source_path / name, target / name
            file_bytes_before = bytes_before

            def chunk_progress(
                e: dict[str, Any],
                _base: int = file_bytes_before,
                _name: str = name,
                _segment: str = item.name,
                _files_done: int = files_done,
            ) -> None:
                emit({
                    **e, "segment": _segment, "file": _name,
                    "files_done": _files_done, "files_total": total_files,
                    "bytes_done": _base + e["bytes"], "bytes_total": total_bytes,
                })

            try:
                info = copy_verify(src, dst, progress=chunk_progress)
            except (OSError, VerifyError) as exc:
                roll_ok = False
                msg = f"{name}: {exc}" if not isinstance(exc, VerifyError) else str(exc)
                record["errors"].append(msg)
                errors.append(f"{item.name}: {msg}")
                emit({"type": "progress", "step": "verify", "segment": item.name, "file": name, "ok": False, "error": msg})
                break
            record["files"].append({"name": name, "from": str(src), "to": str(dst), **info})
            bytes_before += info["size"]
            files_done += 1
            emit({"type": "progress", "step": "verify", "segment": item.name, "file": name, "ok": True,
                  "files_done": files_done, "files_total": total_files, "bytes_done": bytes_before, "bytes_total": total_bytes})
        if was_cancelled or not roll_ok:
            break

        record["verified"] = True
        tagged = fileops.tag_folder(session, target, by_name[item.name].tagging, progress=emit, cancelled=cancelled)
        record["applied"] = tagged["applied"]
        record["errors"].extend(tagged["errors"])
        record["tag_skipped"] = tagged.get("skipped")
        errors.extend(f"{item.name}: {e}" for e in tagged["errors"])
        _annotate_manifest(target, source=source_path, mode=mode, files=record["files"])

        if mode == "move":
            for entry in record["files"]:
                try:
                    os.remove(entry["from"])
                    removed_sources.append(entry["from"])
                    emit({"type": "progress", "step": "remove", "segment": item.name, "file": entry["name"], "ok": True})
                except OSError as exc:
                    msg = f"{entry['name']}: source could not be removed: {exc}"
                    record["errors"].append(msg)
                    errors.append(f"{item.name}: {msg}")
        emit({"type": "progress", "step": "segment", "segment": item.name, "files": len(record["files"]),
              "applied": len(record["applied"]), "verified": True})

    result = {
        "created": created,
        "removed_sources": removed_sources,
        "errors": errors,
        "cancelled": was_cancelled,
        "mode": mode,
    }
    any_file = any(c["files"] for c in created)
    status = operations.FAILED if errors and not any_file else operations.DONE
    operations.finish(session, op.id, status, result)
    result["operation_id"] = op.id
    return result


def _annotate_manifest(folder: Path, *, source: Path, mode: str, files: list[dict[str, Any]]) -> None:
    manifest = mf.read_manifest(folder)
    if not manifest:
        return
    manifest["import_source"] = {
        "path": str(source),
        "imported_at": datetime.now(UTC).isoformat(),
        "verified": True,
        "mode": mode,
        "files": [{"name": f["name"], "size": f["size"], "blake2b": f["digest"]} for f in files],
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
) -> dict[str, Any]:
    """Reverse an import: moved files go back (verified), copies are removed
    once the source is confirmed present, created folders are removed."""
    op = operations.get(session, op_id)
    if op is None:
        raise KeyError(op_id)
    if op.kind != "import":
        raise ValueError(f"cannot undo an operation of kind {op.kind}")
    if op.status == operations.UNDONE:
        raise ValueError("operation already undone")
    data = operations.to_dict(op)
    result = data["result"]
    mode = result.get("mode") or data["plan"].get("mode") or "copy"
    emit = progress or (lambda e: None)
    restored: list[str] = []
    removed: list[str] = []
    removed_folders: list[str] = []
    errors: list[str] = []

    for record in reversed(result.get("created", [])):
        folder = Path(record["path"])
        for entry in reversed(record.get("files", [])):
            if cancelled():
                errors.append("cancelled before every file was handled")
                break
            dst, src = Path(entry["to"]), Path(entry["from"])
            name = entry["name"]
            if not dst.exists():
                continue
            try:
                if src.exists():
                    if os.stat(src).st_size != entry["size"]:
                        raise OSError("source differs in size; destination kept")
                else:
                    if mode != "move":
                        raise OSError("source is gone; destination kept")
                    src.parent.mkdir(parents=True, exist_ok=True)
                    info = copy_verify(dst, src)
                    if info["digest"] != entry["digest"]:
                        os.remove(src)
                        raise OSError("destination no longer matches the imported file; kept")
                    restored.append(str(src))
                os.remove(dst)
                removed.append(str(dst))
                emit({"type": "progress", "step": "restore", "file": name, "ok": True})
            except (OSError, VerifyError) as exc:
                errors.append(f"{name}: {exc}")
                emit({"type": "progress", "step": "restore", "file": name, "ok": False, "error": str(exc)})
        if folder.is_dir():
            copied_names = {entry["name"] for entry in record.get("files", [])}
            for sidecar in fileops.tagging_sidecars(folder, sorted(copied_names)):
                if sidecar.name in copied_names:
                    continue
                try:
                    if mode == "move":
                        # The raw went back to the source; its metadata goes with it.
                        back = Path(record["files"][0]["from"]).parent / sidecar.name
                        if back.exists():
                            raise OSError("a sidecar with that name is back in the source already")
                        copy_verify(sidecar, back)
                    os.remove(sidecar)
                    removed.append(str(sidecar))
                except (OSError, VerifyError) as exc:
                    errors.append(f"{sidecar.name}: {exc}")
            manifest_path = mf.manifest_path_for(folder)
            if manifest_path.is_file():
                try:
                    manifest_path.unlink()
                except OSError as exc:
                    errors.append(f"{folder.name}: could not remove its manifest: {exc}")
            if fileops.remove_folder_if_empty(folder):
                removed_folders.append(str(folder))
                if record.get("year_dir_created"):
                    year_dir = Path(record["year_dir"])
                    if fileops.remove_folder_if_empty(year_dir):
                        removed_folders.append(str(year_dir))
            else:
                errors.append(f"{folder.name}: left in place, it still holds files")

    undo_result = {"restored": restored, "removed": removed, "removed_folders": removed_folders, "errors": errors}
    if removed or removed_folders:
        operations.mark_undone(session, op_id, undo_result)
    undo_result["operation_id"] = op_id
    return undo_result
