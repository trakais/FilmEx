"""What is in a roll folder: the one place that lists a folder's files.

Every view of a folder goes through here: the Tag view's scan, the thumbnail
builder, the Library index, the contact sheet, split and import. That keeps
three rules in one place:

- **Which files are images**: JPEG, TIFF, ARW and DNG, by extension.
- **What is junk**: hidden files, macOS AppleDouble ``._*`` files that SMB
  and FAT volumes carry, NAS bookkeeping (``smbdelete*``, ``@eaDir``) and
  Windows litter. Junk is never listed, counted, tagged or copied.
- **What belongs together**: a frame is every file sharing one basename, a
  camera scan's ``DSC0001.ARW`` with its ``DSC0001.JPG``, the
  ``DSC0001.xmp`` sidecar (or the legacy ``DSC0001.ARW.xmp``) and a lab's
  ``DSC0001.thm`` thumbnail. Those move and copy together, always.
  Anything else (the manifest, notes, orphan sidecars) is reported as
  ``other_files`` and left where it is.

Frames are sorted by their NFC name, case-insensitively, which is capture
order for every scanner and lab seen so far. Names are kept as listed on
disk (SMB shares may store NFD); ``display_name`` gives the NFC form.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

JPEG_EXTS = frozenset({".jpg", ".jpeg"})
TIFF_EXTS = frozenset({".tif", ".tiff"})
RAW_EXTS = frozenset({".arw", ".dng"})
IMAGE_EXTS = JPEG_EXTS | TIFF_EXTS | RAW_EXTS
SIDECAR_EXT = ".xmp"
THUMBNAIL_EXT = ".thm"
COMPANION_EXTS = frozenset({SIDECAR_EXT, THUMBNAIL_EXT})

# Preferred file to build a frame's thumbnail from: a JPEG renders without
# ExifTool, raw needs an embedded preview, TIFF is slow to decode.
_PREVIEW_PRIORITY = (".jpg", ".jpeg", ".arw", ".dng", ".tif", ".tiff")

_JUNK_DIR_NAMES = frozenset({"@eaDir", "#recycle", "#snapshot", "lost+found", "$RECYCLE.BIN"})
_JUNK_FILE_NAMES = frozenset({"Thumbs.db", "desktop.ini"})
_JUNK_FILE_PREFIXES = ("._", "smbdelete")
# Litter the OS regenerates: the only files a cleanup may delete to let an
# emptied folder go. Everything else keeps a folder alive.
_LITTER_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_LITTER_PREFIXES = ("._",)


# ---------- names and kinds ----------


def display_name(name: str) -> str:
    """NFC form of a name for the UI and for comparisons."""
    return unicodedata.normalize("NFC", name)


def source_kind_for(name: str) -> str | None:
    """``lab_scan_jpeg`` / ``positive_master_tiff`` / ``camera_scan_raw`` for an
    image file name, None for anything else."""
    ext = os.path.splitext(name)[1].lower()
    if ext in JPEG_EXTS:
        return "lab_scan_jpeg"
    if ext in TIFF_EXTS:
        return "positive_master_tiff"
    if ext in RAW_EXTS:
        return "camera_scan_raw"
    return None


def is_image(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXTS


def is_junk_file(name: str) -> bool:
    return name.startswith(".") or name.startswith(_JUNK_FILE_PREFIXES) or name in _JUNK_FILE_NAMES


def is_junk_dir(name: str) -> bool:
    return name.startswith(".") or name in _JUNK_DIR_NAMES


def is_litter(name: str) -> bool:
    return name in _LITTER_NAMES or name.startswith(_LITTER_PREFIXES)


def stem_key(name: str) -> str | None:
    """The frame basename a file belongs to, or None for files that are not
    part of any frame.

    ``DSC0001.ARW`` → ``DSC0001``; ``DSC0001.xmp`` → ``DSC0001``;
    ``DSC0001.ARW.xmp`` → ``DSC0001`` (legacy sidecar); ``notes.txt`` → None.
    """
    base, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext in IMAGE_EXTS:
        return base
    if ext in COMPANION_EXTS:
        inner, inner_ext = os.path.splitext(base)
        if inner_ext.lower() in IMAGE_EXTS:
            return inner
        return base
    return None


# ---------- listing ----------


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

    @property
    def images(self) -> list[str]:
        """Every image file, in sheet order."""
        return [name for frame in self.frames for name in frame.images]

    @property
    def sidecar_count(self) -> int:
        """XMP sidecars in the folder, attached to a frame or not."""
        names = [n for f in self.frames for n in f.companions] + self.other_files
        return sum(1 for n in names if n.lower().endswith(SIDECAR_EXT))

    def to_dict(self) -> dict[str, Any]:
        return {
            "folder": self.folder,
            "frames": [f.to_dict() for f in self.frames],
            "other_files": self.other_files,
            "has_raw": self.has_raw,
            "frame_count": len(self.frames),
        }


def list_frames(folder: Path | str) -> FrameListing:
    """Group the folder's files into frames.

    Raises ``FileNotFoundError`` for a missing folder and ``OSError`` when the
    folder cannot be read; unreadable single entries are skipped.
    """
    folder_path = Path(folder).expanduser()
    if not folder_path.is_dir():
        raise FileNotFoundError(f"Folder not found: {folder_path}")

    by_key: dict[str, Frame] = {}
    others: list[str] = []
    with os.scandir(folder_path) as it:
        for entry in it:
            if is_junk_file(entry.name):
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
            frame = by_key.setdefault(key, Frame(key=key, display_key=display_name(key)))
            if is_image(entry.name):
                frame.images.append(entry.name)
            else:
                frame.companions.append(entry.name)
            frame.bytes += size

    frames: list[Frame] = []
    for frame in by_key.values():
        if not frame.images:
            # Orphan sidecar or thumbnail: no image to travel with. Stays put.
            others.extend(frame.companions)
            continue
        frame.images.sort(key=_sort_key)
        frame.companions.sort(key=_sort_key)
        frame.kinds = sorted({k for k in (source_kind_for(n) for n in frame.images) if k})
        frame.has_raw = any(os.path.splitext(n)[1].lower() in RAW_EXTS for n in frame.images)
        frame.preview = _preview_for(frame.images)
        frames.append(frame)

    frames.sort(key=lambda f: f.display_key.lower())
    has_raw = any(f.has_raw for f in frames)
    if has_raw:
        for frame in frames:
            frame.unmatched_jpeg = all(os.path.splitext(n)[1].lower() in JPEG_EXTS for n in frame.images)
    others.sort(key=_sort_key)
    return FrameListing(folder=str(folder_path), frames=frames, other_files=others, has_raw=has_raw)


def thumbnail_for(image: Path | str) -> Path | None:
    """The lab ``.thm`` next to an image file that shares its basename, if any."""
    path = Path(image)
    key = stem_key(path.name)
    try:
        with os.scandir(path.parent) as it:
            for entry in it:
                if is_junk_file(entry.name) or not entry.name.lower().endswith(THUMBNAIL_EXT):
                    continue
                if stem_key(entry.name) == key and entry.is_file(follow_symlinks=False):
                    return Path(entry.path)
    except OSError:
        return None
    return None


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


def _sort_key(name: str) -> str:
    return display_name(name).lower()


def _preview_for(images: Iterable[str]) -> str | None:
    def rank(name: str) -> int:
        ext = os.path.splitext(name)[1].lower()
        return _PREVIEW_PRIORITY.index(ext) if ext in _PREVIEW_PRIORITY else 99

    ranked = sorted(images, key=rank)
    return ranked[0] if ranked else None
