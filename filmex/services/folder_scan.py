"""Folder scanning: list supported files, detect source kind and scan tier.

Source-kind detection follows CLAUDE.md's extension mapping:
    ARW / DNG  -> camera_scan_raw      (home DSLR digitization)
    JPG/JPEG   -> lab_scan_jpeg        (lab-delivered scan)
    TIF/TIFF   -> positive_master_tiff (lab-delivered high-res master)

The scan is one-level-deep and does not follow symlinks. If a folder contains
a mix of kinds, the folder-level `source_kind` is the most populous bucket;
individual files keep their per-file `source_kind` in `ScannedFile`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

from filmex.services import previews

HIGH_RES_JPG_THRESHOLD = 3000
SUPPORTED_JPG_EXTS = {".jpg", ".jpeg"}
SUPPORTED_TIF_EXTS = {".tif", ".tiff"}
SUPPORTED_RAW_EXTS = {".arw", ".dng"}

SUPPORTED_EXTS = SUPPORTED_JPG_EXTS | SUPPORTED_TIF_EXTS | SUPPORTED_RAW_EXTS


def _source_kind_for(suffix: str) -> Optional[str]:
    if suffix in SUPPORTED_JPG_EXTS:
        return "lab_scan_jpeg"
    if suffix in SUPPORTED_TIF_EXTS:
        return "positive_master_tiff"
    if suffix in SUPPORTED_RAW_EXTS:
        return "camera_scan_raw"
    return None


@dataclass
class ScannedFile:
    name: str
    long_edge_px: Optional[int]
    source_kind: str
    has_thumbnail: bool = False
    has_inverted_preview: bool = False


@dataclass
class ScanResult:
    folder: Path
    roll_id: str
    files: list[ScannedFile]
    source_kind: str
    scan_tier: str
    provider_hint: Optional[str] = None
    # Suggested roll date (ISO YYYY-MM-DD) derived from the earliest file's
    # filesystem creation time — lab JPEGs typically have no EXIF, but the
    # scanner delivery stamps a sensible birth/ctime we can use as a default.
    suggested_roll_date: Optional[str] = None


def _long_edge_px(path: Path) -> Optional[int]:
    """Read the image's long-edge pixel count. PIL handles JPG/TIFF; raw returns None."""
    try:
        with Image.open(path) as im:
            w, h = im.size
            return max(w, h)
    except Exception:
        return None


def _iter_candidates(folder: Path) -> list[Path]:
    """Non-recursive listing, no symlinks."""
    out: list[Path] = []
    for entry in sorted(folder.iterdir()):
        if entry.is_symlink():
            continue
        if not entry.is_file():
            continue
        out.append(entry)
    return out

def scan_folder(folder: str | Path) -> ScanResult:
    folder_path = Path(folder).expanduser().resolve()
    if not folder_path.is_dir():
        raise FileNotFoundError(f"Folder not found: {folder_path}")

    files: list[ScannedFile] = []
    for entry in _iter_candidates(folder_path):
        suffix = entry.suffix.lower()
        kind = _source_kind_for(suffix)
        if kind is None:
            continue
        # Long-edge only meaningful for PIL-openable formats; raw is skipped.
        long_edge = _long_edge_px(entry) if kind != "camera_scan_raw" else None
        files.append(
            ScannedFile(
                name=entry.name,
                long_edge_px=long_edge,
                source_kind=kind,
                has_thumbnail=previews.has_preview(entry, invert=False),
                has_inverted_preview=previews.has_preview(entry, invert=True),
            )
        )

    # Folder-level source_kind: majority vote, stable on ties via the
    # JPEG > TIFF > RAW preference below (JPEGs are the common main deliverable).
    if files:
        counts = Counter(f.source_kind for f in files)
        folder_kind = _pick_majority_kind(counts)
    else:
        folder_kind = "lab_scan_jpeg"

    tier = _derive_tier(files, folder_kind)
    suggested_date = _earliest_creation_date(folder_path, files)

    return ScanResult(
        folder=folder_path,
        roll_id=folder_path.name,
        files=files,
        source_kind=folder_kind,
        scan_tier=tier,
        provider_hint=None,
        suggested_roll_date=suggested_date,
    )


def _earliest_creation_date(folder: Path, files: list[ScannedFile]) -> Optional[str]:
    """Earliest file's birth time (macOS/Windows) or mtime (Linux fallback), as YYYY-MM-DD."""
    if not files:
        return None
    timestamps: list[float] = []
    for f in files:
        st = (folder / f.name).stat()
        birth = getattr(st, "st_birthtime", None)
        timestamps.append(birth if birth is not None else st.st_mtime)
    earliest = min(timestamps)
    return datetime.fromtimestamp(earliest).strftime("%Y-%m-%d")


_KIND_PRIORITY = ["lab_scan_jpeg", "positive_master_tiff", "camera_scan_raw"]


def _pick_majority_kind(counts: Counter[str]) -> str:
    max_count = max(counts.values())
    winners = {k for k, v in counts.items() if v == max_count}
    for kind in _KIND_PRIORITY:
        if kind in winners:
            return kind
    return next(iter(winners))


def _derive_tier(files: list[ScannedFile], folder_kind: str) -> str:
    """High-res vs standard. Only JPEG folders genuinely have both tiers.

    * JPEGs: compare long-edge against `HIGH_RES_JPG_THRESHOLD` (3000px).
    * TIFF from a lab: always high-res by definition.
    * Raw: tier concept does not apply; report 'standard' as a neutral default.
    """
    if folder_kind == "positive_master_tiff":
        return "high-res"
    if folder_kind == "camera_scan_raw":
        return "standard"
    jpgs = [f for f in files if f.source_kind == "lab_scan_jpeg"]
    if not jpgs:
        return "standard"
    max_long_edge = max((f.long_edge_px or 0) for f in jpgs)
    return "high-res" if max_long_edge >= HIGH_RES_JPG_THRESHOLD else "standard"
