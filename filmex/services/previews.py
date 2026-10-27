"""Cache-backed preview generation for scan files.

Preview images are lightweight JPEGs stored under the app data directory, not
next to the original scans. Existing lab-supplied `.thm` files are reused when
possible. Raw files give their embedded JPEG preview; a scanner DNG without
one gives the scan it stores (see _dng_image).
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, TiffImagePlugin

from filmex.config import ensure_data_dir
from filmex.services import exiftool_locator as locator
from filmex.services import frames as fr

PREVIEW_SIZE = (320, 240)
# The in-app viewer's size: big enough to judge a frame on a laptop screen,
# small enough to make on demand.
LARGE_SIZE = (2048, 2048)
RAW_PREVIEW_TAGS = ("PreviewImage", "JpgFromRaw", "ThumbnailImage")
DNG_EXTS = frozenset({".dng"})

# TIFF/DNG tags read by _dng_image.
_NEW_SUBFILE_TYPE, _WIDTH, _HEIGHT, _BITS, _COMPRESSION = 254, 256, 257, 258, 259
_PHOTOMETRIC, _STRIP_OFFSETS, _ORIENTATION, _SAMPLES = 262, 273, 274, 277
_STRIP_BYTES, _PLANAR, _SUB_IFDS, _LINEARIZATION = 279, 284, 330, 50712
_RGB, _LINEAR_RAW = 2, 34892
_UPRIGHT = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


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
    return [folder_path / name for name in fr.list_frames(folder_path).images]


def preview_cache_dir() -> Path:
    path = ensure_data_dir() / "preview_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def has_preview(file_path: str | Path, *, invert: bool = False) -> bool:
    return existing_preview_path(file_path, invert=invert) is not None


def existing_preview_path(file_path: str | Path, *, invert: bool = False) -> Path | None:
    path = Path(file_path)
    if not invert:
        thm = fr.thumbnail_for(path)
        if thm is not None:
            return thm
    cached = cached_preview_path_for(path, invert=invert)
    if cached.is_file():
        return cached
    return None


def cached_preview_path_for(file_path: str | Path, *, invert: bool, large: bool = False) -> Path:
    path = Path(file_path).expanduser().resolve()
    st = path.stat()
    size = "|large" if large else ""
    digest = hashlib.sha256(
        f"{path}|{st.st_size}|{st.st_mtime_ns}|invert={int(invert)}{size}|v1".encode()
    ).hexdigest()
    return preview_cache_dir() / f"{digest}.jpg"


def ensure_preview(
    file_path: str | Path, *, invert: bool = False, large: bool = False
) -> tuple[Path, bool]:
    """Return a preview path and whether it was generated in this call.

    ``large`` is the viewer's size (LARGE_SIZE); a lab's ``.thm`` is too
    small for it, so it is made from the file itself.
    """
    path = Path(file_path).expanduser().resolve()
    thm = None if large else fr.thumbnail_for(path)
    if thm is not None and not invert:
        return thm, False

    target = cached_preview_path_for(path, invert=invert, large=large)
    if target.is_file():
        return target, False

    size = LARGE_SIZE if large else PREVIEW_SIZE
    image = _load_preview_image(path, thm=thm)
    try:
        # A JPEG can decode at a fraction of its size, much faster than a
        # full decode followed by a resize.
        image.draft("RGB", size)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        if image.mode == "L":
            image = image.convert("RGB")
        if invert:
            image = ImageOps.invert(image)
        image.thumbnail(size)
        image.save(target, "JPEG", quality=86 if large else 80, optimize=True)
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
    if suffix in fr.JPEG_EXTS | fr.TIFF_EXTS:
        return Image.open(path)
    if suffix in fr.RAW_EXTS:
        try:
            blob = _extract_embedded_preview_bytes(path)
        except PreviewUnavailable:
            image = _dng_image(path) if suffix in DNG_EXTS else None
            if image is None:
                raise
            return image
        return Image.open(BytesIO(blob))
    raise PreviewUnavailable(f"Unsupported preview source: {suffix}")


def _dng_image(path: Path) -> Image.Image | None:
    """The picture a scanner DNG stores, upright, or None.

    Scanner software such as VueScan embeds no JPEG preview. It stores the
    scan as uncompressed RGB, tagged Linear Raw with a linearization table
    because the values are already display-encoded, and a small RGB
    thumbnail in IFD0. The full image is used when it can be shown as
    stored, else the thumbnail; the IFD0 orientation turns either upright.
    A camera DNG's sensor mosaic, or truly linear data, needs raw processing
    and gives None.
    """
    try:
        with path.open("rb") as fp:
            header = fp.read(8)
            ifd0 = _read_ifd(fp, header, None)
            subs = ifd0.get(_SUB_IFDS) or ()
            subs = subs if isinstance(subs, tuple) else (subs,)
            ifds = [ifd0, *(_read_ifd(fp, header, offset) for offset in subs)]

            def by_area(ifd: TiffImagePlugin.ImageFileDirectory_v2) -> int:
                return int(ifd.get(_WIDTH, 0)) * int(ifd.get(_HEIGHT, 0))

            full = sorted((i for i in ifds if i.get(_NEW_SUBFILE_TYPE, 0) == 0), key=by_area, reverse=True)
            reduced = sorted((i for i in ifds if i.get(_NEW_SUBFILE_TYPE, 0) == 1), key=by_area, reverse=True)
            for ifd in full + reduced:
                image = _stored_rgb(fp, ifd, little_endian=header[:2] == b"II")
                if image is not None:
                    transpose = _UPRIGHT.get(ifd0.get(_ORIENTATION, 1))
                    return image.transpose(transpose) if transpose is not None else image
    except (OSError, ValueError, KeyError, SyntaxError) as e:
        raise PreviewUnavailable(f"unreadable DNG: {e}") from e
    return None


def _read_ifd(fp, header: bytes, offset: int | None) -> TiffImagePlugin.ImageFileDirectory_v2:
    ifd = TiffImagePlugin.ImageFileDirectory_v2(ifh=header)
    fp.seek(ifd.next if offset is None else offset)
    ifd.load(fp)
    return ifd


def _stored_rgb(fp, ifd: TiffImagePlugin.ImageFileDirectory_v2, *, little_endian: bool) -> Image.Image | None:
    """An uncompressed, chunky 8- or 16-bit RGB image that shows as stored."""
    photometric = ifd.get(_PHOTOMETRIC)
    shows_as_stored = photometric == _RGB or (photometric == _LINEAR_RAW and _LINEARIZATION in ifd)
    bits = ifd.get(_BITS)
    bits = bits if isinstance(bits, tuple) else (bits,)
    if not (
        shows_as_stored
        and ifd.get(_COMPRESSION, 1) == 1
        and ifd.get(_SAMPLES) == 3
        and ifd.get(_PLANAR, 1) == 1
        and len(set(bits)) == 1
        and bits[0] in (8, 16)
        and _STRIP_OFFSETS in ifd
        and _STRIP_BYTES in ifd
    ):
        return None
    width, height = int(ifd[_WIDTH]), int(ifd[_HEIGHT])
    offsets, counts = ifd[_STRIP_OFFSETS], ifd[_STRIP_BYTES]
    offsets = offsets if isinstance(offsets, tuple) else (offsets,)
    counts = counts if isinstance(counts, tuple) else (counts,)
    data = bytearray()
    for offset, count in zip(offsets, counts, strict=True):
        fp.seek(offset)
        data += fp.read(count)
    needed = width * height * 3 * bits[0] // 8
    if len(data) < needed:
        return None
    if bits[0] == 8:
        return Image.frombytes("RGB", (width, height), bytes(data[:needed]))
    raw_mode = "RGB;16L" if little_endian else "RGB;16B"
    return Image.frombytes("RGB", (width, height), bytes(data[:needed]), "raw", raw_mode)


def _extract_embedded_preview_bytes(path: Path) -> bytes:
    cmd = locator.command()
    if cmd is None:
        raise PreviewUnavailable("exiftool not available for raw preview extraction")
    for tag in RAW_PREVIEW_TAGS:
        proc = subprocess.run(
            [*cmd, "-b", f"-{tag}", str(path)],
            capture_output=True,
            check=False,
        )
        if proc.stdout:
            return proc.stdout
    raise PreviewUnavailable("no embedded preview found")
