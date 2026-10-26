"""ExifTool as the rest of FilmEx sees it: is it there, what managed tags a
file carries, what a write would change, and the write itself.

Callers import the module (``from filmex.services import exiftool_runner as
exiftool``) and use the four operations below. They never build argv, run
subprocesses, pick between a raw file and its sidecar, or decode stderr;
the pure command builder in ``exiftool.py`` and the two primitives at the
bottom of this module own that.

Tests replace ExifTool in one place: the ``fake_exiftool`` fixture swaps
``available``, ``read_managed_tags`` and ``apply_intent`` here, and every
caller follows because they look these names up through the module.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filmex.services import exiftool as et
from filmex.services import exiftool_locator as locator

_log = logging.getLogger(__name__)


# Every tag FilmEx manages; read before a diff or write so merges see the
# current state.
MANAGED_READ_TAGS = [
    "Make",
    "Model",
    "LensModel",
    "Subject",
    "Keywords",
    "HierarchicalSubject",
    "Description",
    "Caption-Abstract",
]


class ExifToolMissing(RuntimeError):
    """Raised when a write is attempted without a usable ExifTool."""


@dataclass
class WriteResult:
    """Outcome of one ``write``: the per-tag changes, or why ExifTool refused."""

    changes: list[et.TagChange] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------- the interface ----------


def available() -> bool:
    return locator.available()


def managed_tags(path: Path | str) -> dict[str, Any]:
    """The managed tags as they are on disk.

    Empty when ExifTool is missing (so a dry run still shows what would be
    written) and for a raw file that has no sidecar yet.
    """
    if not available():
        return {}
    return read_managed_tags(path)


def preview(path: Path | str, intent: et.WriteIntent, *, replace_managed: bool = False) -> list[et.TagChange]:
    """What ``write`` would change on ``path``, without touching it."""
    return et.diff(managed_tags(path), intent, replace_managed=replace_managed)


def write(path: Path | str, intent: et.WriteIntent, *, replace_managed: bool = False) -> WriteResult:
    """Merge ``intent`` into the file's managed tags and write them.

    User keywords outside FilmEx's taxonomy are kept; managed families are
    replaced (all of them with ``replace_managed``). Raw files are written
    through their sidecar. Returns the changes, or the error ExifTool gave.
    Raises ``ExifToolMissing`` when there is no ExifTool.
    """
    if not available():
        raise ExifToolMissing("exiftool not found; set its path in Settings")
    existing = read_managed_tags(path)
    changes = et.diff(existing, intent, replace_managed=replace_managed)
    resolved = et.resolve_intent(existing, intent, replace_managed=replace_managed)
    proc = apply_intent(path, resolved)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        return WriteResult(changes=changes, error=stderr or f"exiftool exited with status {proc.returncode}")
    return WriteResult(changes=changes)


# ---------- primitives ----------


def read_tags(path: Path | str, tags: list[str]) -> dict[str, Any]:
    """Read `tags` from `path` via exiftool, returning a dict.

    Non-zero exit with stdout still parses (exiftool warns on benign issues
    like missing optional tags but still emits valid JSON), but the stderr is
    surfaced via the logger so failures aren't entirely silent.
    """
    argv = et.build_read_argv(path, tags, command=_command())
    result = subprocess.run(argv, capture_output=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        _log.warning("exiftool read_tags(%s) exit=%s stderr=%s", path, result.returncode, stderr)
        if not result.stdout:
            return {}
    return et.parse_read_output(result.stdout)


def read_managed_tags(path: Path | str) -> dict[str, Any]:
    """The managed tag set as currently on disk.

    For raw files this reads the XMP sidecar (canonical or legacy name), never
    the raw itself, so the "before" state reflects the metadata layer FilmEx
    manages rather than the camera EXIF inside the raw. No sidecar yet means
    nothing is managed yet.
    """
    target = Path(path)
    if et.detect_mode(target) == "sidecar":
        sidecar = et.find_existing_sidecar(target)
        if sidecar is None:
            return {}
        return read_tags(sidecar, list(MANAGED_READ_TAGS))
    return read_tags(target, list(MANAGED_READ_TAGS))


def apply_intent(path: Path | str, intent: et.WriteIntent) -> subprocess.CompletedProcess:
    """Run exiftool to write an already-resolved ``intent`` to ``path``.

    For sidecar-mode files (ARW/DNG), checks whether the XMP already exists so
    `build_argv` picks the correct exiftool invocation (direct edit vs -o seed).
    The sidecar-create path returns two invocations (seed + edit); we stop on
    the first non-zero exit so the caller's check sees the failure.
    """
    sidecar_exists = False
    sidecar_path = None
    if et.detect_mode(path) == "sidecar":
        # Prefer an existing sidecar (canonical OR legacy-named) so re-applies
        # merge into the file Lightroom is already reading, and we don't
        # produce a second sidecar next to the first.
        existing = et.find_existing_sidecar(path)
        if existing is not None:
            sidecar_exists = True
            sidecar_path = existing
    invocations = et.build_argv(
        path,
        intent,
        overwrite_original=True,
        sidecar_exists=sidecar_exists,
        sidecar_path=sidecar_path,
        command=_command(),
    )
    result: subprocess.CompletedProcess | None = None
    for argv in invocations:
        result = subprocess.run(argv, capture_output=True, check=False)
        if result.returncode != 0:
            return result
    assert result is not None  # build_argv never returns an empty list
    return result


def _command() -> list[str]:
    cmd = locator.command()
    if cmd is None:
        raise ExifToolMissing("exiftool not found; set its path in Settings")
    return cmd
