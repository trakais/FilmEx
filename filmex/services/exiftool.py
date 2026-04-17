"""Pure ExifTool command builder.

This module contains NO subprocess calls. It is a unit-testable string
layer. The actual invocation lives in `exiftool_runner.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

# Extensions we know how to write. Embed-in-place for JPG/TIFF; sidecar for raw.
_EMBED_EXTS = {".jpg", ".jpeg", ".tif", ".tiff"}
_SIDECAR_EXTS = {".arw", ".dng"}

ModeName = Literal["embed", "sidecar"]


@dataclass
class WriteIntent:
    """The set of managed fields to write for a single file.

    `hierarchical_keywords` are full paths like `Film|Kodak Portra 400`.
    `flat_keywords` are leaves like `Kodak Portra 400`.
    """

    camera_make: Optional[str] = None
    camera_model: Optional[str] = None
    lens_model: Optional[str] = None
    hierarchical_keywords: list[str] = field(default_factory=list)
    flat_keywords: list[str] = field(default_factory=list)
    capture_date: Optional[str] = None  # YYYY:MM:DD HH:MM:SS, optional


@dataclass
class TagChange:
    tag: str
    action: str  # "set" | "merge" | "skip"
    before: Any = None
    after: Any = None


def detect_mode(path: Path | str) -> ModeName:
    """Return 'embed' for JPG/TIFF, 'sidecar' for raw. Unsupported raises."""
    suffix = Path(path).suffix.lower()
    if suffix in _EMBED_EXTS:
        return "embed"
    if suffix in _SIDECAR_EXTS:
        return "sidecar"
    raise NotImplementedError(f"Unsupported extension: {suffix}")


def sidecar_path_for(raw_path: Path | str) -> Path:
    """Return the canonical XMP sidecar path for a raw file.

    Lightroom and Bridge expect the *replaced-extension* form: `photo.arw` →
    `photo.xmp` (not `photo.arw.xmp`). ExifTool's own default `-o` behavior
    appends instead of replacing, so we pass the path explicitly every time.

    Early FilmEx builds wrote the appended form; `find_existing_sidecar`
    tolerates that on read so re-apply merges into the right file without the
    user having to rename anything by hand.
    """
    p = Path(raw_path)
    return p.with_suffix(".xmp")


def legacy_sidecar_path_for(raw_path: Path | str) -> Path:
    """Return the pre-v1 appended-form sidecar path (`photo.arw.xmp`).

    Kept around purely so re-apply can pick up sidecars written by earlier
    FilmEx versions. Never a write target for new files.
    """
    p = Path(raw_path)
    return p.with_suffix(p.suffix + ".xmp")


def find_existing_sidecar(raw_path: Path | str) -> Optional[Path]:
    """Return whichever sidecar is on disk, preferring the canonical form.

    Order:
      1. `photo.xmp` — what we write today and what Lightroom looks for.
      2. `photo.arw.xmp` — legacy form written by pre-v1 FilmEx builds.
      3. None — no sidecar yet; caller will seed one at the canonical path.
    """
    canonical = sidecar_path_for(raw_path)
    if canonical.is_file():
        return canonical
    legacy = legacy_sidecar_path_for(raw_path)
    if legacy.is_file():
        return legacy
    return None


def build_read_argv(path: Path | str, tags: list[str]) -> list[str]:
    """Build argv for reading specified tags as JSON."""
    argv = [
        "exiftool",
        "-charset",
        "filename=utf8",
        "-j",
        "-n",
    ]
    for tag in tags:
        argv.append(f"-{tag}")
    # Multi-value tags: use `-sep` so JSON arrays come back split on the separator.
    # For reads we use "||" to avoid conflict with hierarchy pipes; but to read
    # lists as JSON arrays exiftool already returns arrays with -j for list-type
    # tags. We still request the plain read here; parse_read_output handles both.
    argv.append(str(path))
    return argv


def parse_read_output(data: bytes) -> dict[str, Any]:
    """Parse `exiftool -j` output into a dict (first file only)."""
    if not data:
        return {}
    try:
        parsed = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError:
        return {}
    if not parsed:
        return {}
    return parsed[0]


def build_argv(
    target: Path | str,
    intent: WriteIntent,
    *,
    dry_run: bool = False,
    overwrite_original: bool = True,
    sidecar_exists: bool = False,
    sidecar_path: Optional[Path | str] = None,
) -> list[str]:
    """Build the exiftool argv for applying `intent` to `target`.

    Embed mode (JPG/TIFF): writes into the file in place.

    Sidecar mode (ARW/DNG): two sub-paths —
      * If a sidecar already exists, edit the XMP directly so we merge with any
        Lightroom-written XMP content.
      * If no sidecar exists yet, use `-o` to seed one from the raw + intent.

    `sidecar_path` lets the caller pin the exact sidecar file to read/write.
    The runner passes this when it found a legacy-named sidecar so we keep
    editing the existing file instead of silently creating a second one.
    """
    path = Path(target)
    mode = detect_mode(path)

    argv: list[str] = [
        "exiftool",
        "-charset",
        "filename=utf8",
        # `|` is our hierarchical separator, so keyword values themselves cannot
        # contain `|`. Using `||` as the CLI list separator keeps values intact.
        "-sep",
        "||",
    ]

    if overwrite_original and not dry_run:
        argv.append("-overwrite_original")

    # Gear fields — silent overwrite of managed whitelist.
    if intent.camera_make is not None:
        argv += [f"-Make={intent.camera_make}"]
    if intent.camera_model is not None:
        argv += [f"-Model={intent.camera_model}"]
    if intent.lens_model is not None:
        argv += [f"-LensModel={intent.lens_model}", f"-Lens={intent.lens_model}"]

    # Keywords — write both forms. Exiftool's `+=` appends even if the item
    # already exists, so we first `-=` (no-op if absent) and then `+=` the same
    # set. Net effect: each value appears exactly once regardless of current
    # state. This is what keeps re-apply idempotent.
    if intent.flat_keywords:
        joined = "||".join(intent.flat_keywords)
        argv += [
            f"-XMP-dc:Subject-={joined}",
            f"-XMP-dc:Subject+={joined}",
            f"-IPTC:Keywords-={joined}",
            f"-IPTC:Keywords+={joined}",
        ]
    if intent.hierarchical_keywords:
        joined = "||".join(intent.hierarchical_keywords)
        argv += [
            f"-XMP-lr:HierarchicalSubject-={joined}",
            f"-XMP-lr:HierarchicalSubject+={joined}",
        ]

    if intent.capture_date:
        # Write all three date tags in one shot so Lightroom, Bridge, and raw
        # EXIF readers all agree. Day-precision timestamps are stamped at noon
        # local (handled upstream) to signal "roll-level approximation".
        argv += [
            f"-DateTimeOriginal={intent.capture_date}",
            f"-CreateDate={intent.capture_date}",
            f"-XMP-photoshop:DateCreated={intent.capture_date}",
        ]

    if mode == "sidecar":
        xmp_path = Path(sidecar_path) if sidecar_path is not None else sidecar_path_for(path)
        if sidecar_exists:
            # Direct edit of the existing sidecar. IPTC:Keywords does not apply
            # in XMP-only files, so drop the IPTC arg to avoid exiftool warnings.
            argv = [a for a in argv if not a.startswith("-IPTC:Keywords")]
            argv.append(str(xmp_path))
        else:
            # Seed a fresh sidecar from the raw. `-o` refuses to overwrite, so
            # the caller must only reach this branch when the xmp is absent.
            argv = [a for a in argv if not a.startswith("-IPTC:Keywords")]
            argv += ["-o", str(xmp_path), str(path)]
    else:
        argv.append(str(path))

    if dry_run:
        argv.insert(1, "-n")  # no numerical conversions; no-op
        # Remove -overwrite_original just in case (defensive)
        argv = [a for a in argv if a != "-overwrite_original"]

    return argv


def diff(existing: dict[str, Any], intent: WriteIntent) -> list[TagChange]:
    """Compute what would change given current tag values and the intent."""
    changes: list[TagChange] = []

    def _gear(tag: str, new_value: Optional[str]) -> None:
        if new_value is None:
            return
        current = existing.get(tag)
        if current == new_value:
            changes.append(TagChange(tag=tag, action="skip", before=current, after=new_value))
        else:
            changes.append(TagChange(tag=tag, action="set", before=current, after=new_value))

    _gear("Make", intent.camera_make)
    _gear("Model", intent.camera_model)
    _gear("LensModel", intent.lens_model)
    _gear("DateTimeOriginal", intent.capture_date)

    if intent.flat_keywords:
        current_flat = _as_list(existing.get("Subject")) or _as_list(existing.get("Keywords"))
        merged = _merge_lists(current_flat, intent.flat_keywords)
        if merged == current_flat:
            changes.append(
                TagChange(tag="Subject", action="skip", before=current_flat, after=current_flat)
            )
        else:
            changes.append(
                TagChange(tag="Subject", action="merge", before=current_flat, after=merged)
            )

    if intent.hierarchical_keywords:
        current_h = _as_list(existing.get("HierarchicalSubject"))
        merged = _merge_lists(current_h, intent.hierarchical_keywords)
        if merged == current_h:
            changes.append(
                TagChange(
                    tag="HierarchicalSubject",
                    action="skip",
                    before=current_h,
                    after=current_h,
                )
            )
        else:
            changes.append(
                TagChange(
                    tag="HierarchicalSubject",
                    action="merge",
                    before=current_h,
                    after=merged,
                )
            )

    return changes


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _merge_lists(existing: list[str], new: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in list(existing) + list(new):
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
