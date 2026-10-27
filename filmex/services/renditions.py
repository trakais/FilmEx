"""Renditions: a roll kept as ``RAW`` and ``JPG`` subfolders is still one roll.

A subfolder named like a file format or a scan source (``RAW``, ``JPG``,
``positives``, ``lab``…) is a *rendition* of the roll folder above it, not
a roll of its own. The roll's identity, the ``Roll|…`` keyword and the
manifest's ``roll_id``, comes from the parent; the rendition keeps its own
files, its own manifest and its own scan provider and method, which is the
one thing that genuinely differs between a lab's JPEGs and a DSLR's raws
of the same negatives.
"""

from __future__ import annotations

import os
from pathlib import Path

from filmex.services import frames as fr

RENDITION_NAMES = frozenset({
    "raw", "raws", "jpg", "jpgs", "jpeg", "jpegs", "tif", "tifs", "tiff", "tiffs",
    "dng", "arw", "nef", "cr2", "positive", "positives", "negative", "negatives",
    "scan", "scans", "lab", "export", "exports",
})


def is_rendition_dir(name: str) -> bool:
    return name.strip().lower() in RENDITION_NAMES


def rendition_name(folder: Path | str) -> str | None:
    """The folder's own name when it is a rendition folder, else None."""
    path = Path(folder)
    return path.name if is_rendition_dir(path.name) and path.parent != path else None


def roll_folder(folder: Path | str) -> Path:
    """The folder that names the roll: the parent of a rendition folder,
    otherwise the folder itself."""
    path = Path(folder)
    return path.parent if rendition_name(path) else path


def roll_id_for(folder: Path | str) -> str:
    return roll_folder(folder).name


def rendition_dirs(roll: Path | str) -> list[Path]:
    """The rendition folders directly under ``roll`` that hold images."""
    found: list[Path] = []
    try:
        with os.scandir(roll) as it:
            entries = [e for e in it if not fr.is_junk_dir(e.name)]
    except OSError:
        return found
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False) or not is_rendition_dir(entry.name):
                continue
            if fr.list_frames(entry.path).frames:
                found.append(Path(entry.path))
        except OSError:
            continue
    return sorted(found, key=lambda p: p.name.lower())


def sibling_renditions(folder: Path | str) -> list[Path]:
    """The other renditions of the roll a rendition folder belongs to."""
    path = Path(folder)
    if not rendition_name(path):
        return []
    return [p for p in rendition_dirs(path.parent) if p != path]
