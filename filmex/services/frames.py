"""Frames: the unit the contact sheet, split and import work with.

A frame is every file in a folder that shares one basename: a camera scan's
``DSC0001.ARW`` with its ``DSC0001.JPG``, the ``DSC0001.xmp`` sidecar (or
the legacy ``DSC0001.ARW.xmp``) and a lab's ``DSC0001.thm`` thumbnail. Those
move and copy together, always. Anything else in the folder (the manifest,
stray text files, junk) is reported separately and left where it is.

Frames are sorted by name the way the Library sorts folders, so the sheet
shows them in capture order for every scanner and lab we have seen.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from filmex.services import library as lib
from filmex.services.folder_scan import (
    SUPPORTED_EXTS,
    SUPPORTED_JPG_EXTS,
    SUPPORTED_RAW_EXTS,
    _source_kind_for,
)

COMPANION_EXTS = frozenset({".xmp", ".thm"})
# Preferred file to build the thumbnail from: a JPEG renders without
# ExifTool, raw needs an embedded preview, TIFF is slow to decode.
_PREVIEW_PRIORITY = (".jpg", ".jpeg", ".arw", ".dng", ".tif", ".tiff")


@dataclass
class Frame:
    key: str  # basename as listed on disk
    display_key: str  # NFC for the UI
    images: list[str] = field(default_factory=list)
    companions: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    has_raw: bool = False
    unmatched_jpeg: bool = False  # JPEG-only frame in a folder that has raw frames
    preview: str | None = None  # image file to build the thumbnail from
    bytes: int = 0

    @property
    def files(self) -> list[str]:
        return [*self.images, *self.companions]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["files"] = self.files
        return data


@dataclass
class FrameListing:
    folder: str
    frames: list[Frame]
    other_files: list[str]  # non-frame files that stay behind (manifest, notes…)
    has_raw: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "folder": self.folder,
            "frames": [f.to_dict() for f in self.frames],
            "other_files": self.other_files,
            "has_raw": self.has_raw,
            "frame_count": len(self.frames),
        }


def stem_key(name: str) -> str | None:
    """The frame basename a file belongs to, or None for files that are not
    part of any frame.

    ``DSC0001.ARW`` → ``DSC0001``; ``DSC0001.xmp`` → ``DSC0001``;
    ``DSC0001.ARW.xmp`` → ``DSC0001`` (legacy sidecar); ``notes.txt`` → None.
    """
    base, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext in SUPPORTED_EXTS:
        return base
    if ext in COMPANION_EXTS:
        inner, inner_ext = os.path.splitext(base)
        if inner_ext.lower() in SUPPORTED_EXTS:
            return inner
        return base
    return None


def list_frames(folder: Path | str) -> FrameListing:
    folder_path = Path(folder).expanduser()
    if not folder_path.is_dir():
        raise FileNotFoundError(f"Folder not found: {folder_path}")

    by_key: dict[str, Frame] = {}
    others: list[str] = []
    with os.scandir(folder_path) as it:
        for entry in it:
            if lib.is_junk_file(entry.name):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            key = stem_key(entry.name)
            if key is None:
                others.append(entry.name)
                continue
            frame = by_key.setdefault(key, Frame(key=key, display_key=lib.display_name(key)))
            ext = os.path.splitext(entry.name)[1].lower()
            if ext in SUPPORTED_EXTS:
                frame.images.append(entry.name)
                frame.bytes += size
            else:
                frame.companions.append(entry.name)
                frame.bytes += size

    frames: list[Frame] = []
    for frame in by_key.values():
        if not frame.images:
            # Orphan sidecar or thumbnail: no image to travel with. Stays put.
            others.extend(frame.companions)
            continue
        frame.images.sort(key=lambda n: lib.display_name(n).lower())
        frame.companions.sort(key=lambda n: lib.display_name(n).lower())
        frame.kinds = sorted({k for k in (_source_kind_for(os.path.splitext(n)[1].lower()) for n in frame.images) if k})
        frame.has_raw = any(os.path.splitext(n)[1].lower() in SUPPORTED_RAW_EXTS for n in frame.images)
        frame.preview = _preview_for(frame.images)
        frames.append(frame)

    frames.sort(key=lambda f: f.display_key.lower())
    has_raw = any(f.has_raw for f in frames)
    if has_raw:
        for frame in frames:
            jpeg_only = all(os.path.splitext(n)[1].lower() in SUPPORTED_JPG_EXTS for n in frame.images)
            frame.unmatched_jpeg = jpeg_only
    others.sort(key=lambda n: lib.display_name(n).lower())
    return FrameListing(folder=str(folder_path), frames=frames, other_files=others, has_raw=has_raw)


def _preview_for(images: Iterable[str]) -> str | None:
    ranked = sorted(
        images,
        key=lambda n: (_PREVIEW_PRIORITY.index(os.path.splitext(n)[1].lower())
                       if os.path.splitext(n)[1].lower() in _PREVIEW_PRIORITY else 99),
    )
    return ranked[0] if ranked else None


def frames_by_key(listing: FrameListing) -> dict[str, Frame]:
    return {f.key: f for f in listing.frames}


def earliest_file_date(folder: Path, frames: Iterable[Frame]) -> str | None:
    """YYYY-MM-DD of the oldest image in ``frames`` (birth time, else mtime)."""
    stamps: list[float] = []
    for frame in frames:
        for name in frame.images:
            try:
                st = (folder / name).stat()
            except OSError:
                continue
            birth = getattr(st, "st_birthtime", None)
            stamps.append(birth if birth else st.st_mtime)
    if not stamps:
        return None
    return datetime.fromtimestamp(min(stamps)).strftime("%Y-%m-%d")
