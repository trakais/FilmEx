"""Cache-backed preview generation for scan files.

Preview images are lightweight JPEGs stored under the app data directory, not
next to the original scans. Existing lab-supplied `.thm` files are reused when
possible.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from filmex.config import ensure_data_dir
from filmex.services.exiftool_runner import exiftool_available

SUPPORTED_JPG_EXTS = {".jpg", ".jpeg"}
SUPPORTED_TIF_EXTS = {".tif", ".tiff"}
SUPPORTED_RAW_EXTS = {".arw", ".dng"}
PREVIEW_SIZE = (320, 240)
RAW_PREVIEW_TAGS = ("PreviewImage", "JpgFromRaw", "ThumbnailImage")


class PreviewUnavailable(RuntimeError):
    """Raised when a preview cannot be generated for a file."""


@dataclass
class PreviewBuildReport:
    created_files: list[str] = field(default_factory=list)
    reused_files: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def available_files(self) -> list[str]:
        return self.created_files + self.reused_files


def previewable_entries(folder: str | Path) -> list[Path]:
    folder_path = Path(folder).expanduser().resolve()
    return [
        entry
        for entry in sorted(folder_path.iterdir())
        if entry.is_file()
        and not entry.is_symlink()
        and entry.suffix.lower() in SUPPORTED_JPG_EXTS | SUPPORTED_TIF_EXTS | SUPPORTED_RAW_EXTS
    ]


def preview_cache_dir() -> Path:
    path = ensure_data_dir() / "preview_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sibling_thm_path(file_path: str | Path) -> Path | None:
    path = Path(file_path)
    for sibling in path.parent.iterdir():
        if not sibling.is_file() or sibling.is_symlink():
            continue
        if sibling.stem != path.stem:
            continue
        if sibling.suffix.lower() == ".thm":
            return sibling
    return None


def has_preview(file_path: str | Path, *, invert: bool = False) -> bool:
    return existing_preview_path(file_path, invert=invert) is not None


def existing_preview_path(file_path: str | Path, *, invert: bool = False) -> Path | None:
    path = Path(file_path)
    if not invert:
        thm = sibling_thm_path(path)
        if thm is not None:
            return thm
    cached = cached_preview_path_for(path, invert=invert)
    if cached.is_file():
        return cached
    return None


def cached_preview_path_for(file_path: str | Path, *, invert: bool) -> Path:
    path = Path(file_path).expanduser().resolve()
    st = path.stat()
    digest = hashlib.sha256(
        f"{path}|{st.st_size}|{st.st_mtime_ns}|invert={int(invert)}|v1".encode("utf-8")
    ).hexdigest()
    return preview_cache_dir() / f"{digest}.jpg"


def ensure_preview(file_path: str | Path, *, invert: bool = False) -> tuple[Path, bool]:
    """Return a preview path and whether it was generated in this call."""
    path = Path(file_path).expanduser().resolve()
    thm = sibling_thm_path(path)
    if thm is not None and not invert:
        return thm, False

    target = cached_preview_path_for(path, invert=invert)
    if target.is_file():
        return target, False

    image = _load_preview_image(path, thm=thm)
    try:
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        if image.mode == "L":
            image = image.convert("RGB")
        if invert:
            image = ImageOps.invert(image)
        image.thumbnail(PREVIEW_SIZE)
        image.save(target, "JPEG", quality=80, optimize=True)
    finally:
        image.close()

    return target, True


def build_previews(folder: str | Path, *, invert: bool = False) -> PreviewBuildReport:
    report = PreviewBuildReport()
    for entry in previewable_entries(folder):
        try:
            if has_preview(entry, invert=invert):
                report.reused_files.append(entry.name)
                continue
            _, created = ensure_preview(entry, invert=invert)
        except PreviewUnavailable as e:
            report.failed.append(f"{entry.name}: {e}")
            continue
        if created:
            report.created_files.append(entry.name)
        else:
            report.reused_files.append(entry.name)
    return report


def missing_preview_entries(folder: str | Path, *, invert: bool = False) -> list[Path]:
    return [entry for entry in previewable_entries(folder) if not has_preview(entry, invert=invert)]


def _load_preview_image(path: Path, *, thm: Path | None) -> Image.Image:
    suffix = path.suffix.lower()
    if thm is not None:
        return Image.open(thm)
    if suffix in SUPPORTED_JPG_EXTS | SUPPORTED_TIF_EXTS:
        return Image.open(path)
    if suffix in SUPPORTED_RAW_EXTS:
        blob = _extract_embedded_preview_bytes(path)
        return Image.open(BytesIO(blob))
    raise PreviewUnavailable(f"Unsupported preview source: {suffix}")


def _extract_embedded_preview_bytes(path: Path) -> bytes:
    if not exiftool_available():
        raise PreviewUnavailable("exiftool not available for raw preview extraction")
    for tag in RAW_PREVIEW_TAGS:
        proc = subprocess.run(
            ["exiftool", "-b", f"-{tag}", str(path)],
            capture_output=True,
            check=False,
        )
        if proc.stdout:
            return proc.stdout
    raise PreviewUnavailable("no embedded preview found")
