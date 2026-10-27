"""Library index: roll folders under the library root and how far each is tagged.

The walk is shallow on purpose: ``<root>/<year>/<roll>`` is the layout, so
depth two covers every roll, and a per-file metadata read across a NAS would
take minutes. Status comes from each folder's ``roll_manifest.json``: the
union of ``applied_files`` over real (non dry-run) apply entries is the set
of tagged files; whatever else is on disk is untagged.

Folder names are taken from the directory listing byte for byte (SMB shares
may store them NFD); ``display_name`` is the NFC form for the UI.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filmex.services import frames as fr
from filmex.services import manifest as mf
from filmex.services.frames import display_name

MAX_DEPTH = 2

TAGGED = "tagged"
PARTIAL = "partial"
UNTAGGED = "untagged"
EMPTY = "empty"
STATUSES = (TAGGED, PARTIAL, UNTAGGED, EMPTY)


@dataclass
class LibraryFolder:
    path: str
    parent: str | None  # display name of the year/container folder, None at depth 1
    name: str  # as listed on disk
    display_name: str  # NFC for the UI
    depth: int
    status: str
    image_count: int
    untagged_count: int
    sidecar_count: int
    kinds: dict[str, int]
    has_manifest: bool
    roll_id: str | None
    film_display: str | None
    camera_display: str | None
    last_applied_at: str | None
    is_flat_year: bool = False
    untagged_files: list[str] = field(default_factory=list)

    def to_dict(self, *, include_files: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if not include_files:
            data.pop("untagged_files")
        return data


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _subdirs(folder: Path) -> Iterator[os.DirEntry]:
    try:
        with os.scandir(folder) as it:
            entries = [e for e in it if _is_dir(e) and not fr.is_junk_dir(e.name)]
    except OSError:
        return iter(())
    entries.sort(key=lambda e: display_name(e.name).lower())
    return iter(entries)


def _is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def applied_files_from_manifest(manifest: dict[str, Any] | None) -> set[str]:
    """Files recorded as written by a real apply. Dry-run entries don't count."""
    if not manifest:
        return set()
    applied: set[str] = set()
    for entry in manifest.get("apply_history") or []:
        if entry.get("dry_run"):
            continue
        applied.update(str(name) for name in entry.get("applied_files") or [])
    return applied


def last_applied_at(manifest: dict[str, Any] | None) -> str | None:
    if not manifest:
        return None
    stamps = [
        str(entry.get("timestamp"))
        for entry in manifest.get("apply_history") or []
        if not entry.get("dry_run") and entry.get("timestamp")
    ]
    return max(stamps) if stamps else None


def inspect_folder(folder: Path, *, depth: int, parent: str | None, label: str | None = None) -> LibraryFolder:
    """One row for ``folder``. ``label`` is the display name for a roll
    nested inside another folder (``order/roll``); default is the folder's
    own name in NFC."""
    try:
        listing = fr.list_frames(folder)
        images, sidecars = listing.images, listing.sidecar_count
    except OSError:  # vanished or unreadable mid-walk: show it as empty
        images, sidecars = [], 0
    kinds = Counter(fr.source_kind_for(n) or "other" for n in images)
    manifest = _safe_read_manifest(folder)
    applied = applied_files_from_manifest(manifest)
    # Compare on normalized names: the manifest was written from the same
    # listing, but be tolerant of NFC/NFD drift between writer and share.
    applied_norm = {display_name(n) for n in applied}
    untagged = [n for n in images if display_name(n) not in applied_norm]

    if not images:
        status = EMPTY
    elif manifest is None or len(untagged) == len(images):
        status = UNTAGGED
    elif untagged:
        status = PARTIAL
    else:
        status = TAGGED

    film = (manifest or {}).get("film_stock_snapshot") or {}
    camera = (manifest or {}).get("analog_camera_snapshot") or {}
    return LibraryFolder(
        path=str(folder),
        parent=parent,
        name=folder.name,
        display_name=label or display_name(folder.name),
        depth=depth,
        status=status,
        image_count=len(images),
        untagged_count=len(untagged),
        sidecar_count=sidecars,
        kinds=dict(kinds),
        has_manifest=manifest is not None,
        roll_id=(manifest or {}).get("roll_id"),
        film_display=film.get("display_name") or None,
        camera_display=camera.get("display_name") or None,
        last_applied_at=last_applied_at(manifest),
        untagged_files=untagged,
    )


def _safe_read_manifest(folder: Path) -> dict[str, Any] | None:
    try:
        return mf.read_manifest(folder)
    except (OSError, ValueError):
        return None


def scan_library(
    root: Path | str,
    *,
    progress: Callable[[int], None] | None = None,
) -> list[LibraryFolder]:
    """Walk ``root`` and inspect every candidate roll folder.

    The layout is ``<root>/<year>/<roll>``. Depth-one folders appear as rows
    only when they hold images themselves (the old flat year folders); their
    subfolders always appear, so an empty roll folder shows up as ``empty``
    rather than vanishing. A depth-two folder that holds no images but has
    subfolders with images (a roll kept as ``JPG`` and ``RAW``, a lab order
    with one subfolder per roll) is a container: its image-bearing
    subfolders are the rolls, listed as ``container/roll`` at depth three,
    and the container itself is not a row.
    """
    root_path = Path(root).expanduser()
    if not root_path.is_dir():
        raise FileNotFoundError(f"Library root not found: {root_path}")

    rows: list[LibraryFolder] = []
    for top in _subdirs(root_path):
        top_path = Path(top.path)
        top_row = inspect_folder(top_path, depth=1, parent=None)
        if top_row.image_count:
            top_row.is_flat_year = True
            rows.append(top_row)
            if progress:
                progress(len(rows))
        for sub in _subdirs(top_path):
            sub_path = Path(sub.path)
            row = inspect_folder(sub_path, depth=2, parent=top_row.display_name)
            nested = [
                inspect_folder(Path(e.path), depth=3, parent=top_row.display_name, label=f"{row.display_name}/{display_name(e.name)}")
                for e in _subdirs(sub_path)
            ]
            nested = [n for n in nested if n.image_count]
            if row.image_count or not nested:
                rows.append(row)
            rows.extend(nested)
            if progress:
                progress(len(rows))
    return rows


def folder_is_under(root: Path | str, folder: Path | str) -> bool:
    try:
        Path(folder).expanduser().resolve().relative_to(Path(root).expanduser().resolve())
        return True
    except (ValueError, OSError):
        return False


def summarize(rows: list[dict[str, Any]] | list[LibraryFolder]) -> dict[str, int]:
    counts = dict.fromkeys(STATUSES, 0)
    for row in rows:
        status = row["status"] if isinstance(row, dict) else row.status
        counts[status] = counts.get(status, 0) + 1
    return counts
