"""Thin subprocess wrapper around the pure builder in `exiftool.py`."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from filmex.services import exiftool as et

_log = logging.getLogger(__name__)


def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def read_tags(path: Path | str, tags: list[str]) -> dict[str, Any]:
    """Read `tags` from `path` via exiftool, returning a dict.

    Non-zero exit with stdout still parses (exiftool warns on benign issues
    like missing optional tags but still emits valid JSON), but the stderr is
    surfaced via the logger so failures aren't entirely silent.
    """
    argv = et.build_read_argv(path, tags)
    result = subprocess.run(argv, capture_output=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        _log.warning("exiftool read_tags(%s) exit=%s stderr=%s", path, result.returncode, stderr)
        if not result.stdout:
            return {}
    return et.parse_read_output(result.stdout)


def apply_intent(path: Path | str, intent: et.WriteIntent) -> subprocess.CompletedProcess:
    """Run exiftool to apply `intent` to `path`.

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
    )
    result: subprocess.CompletedProcess | None = None
    for argv in invocations:
        result = subprocess.run(argv, capture_output=True, check=False)
        if result.returncode != 0:
            return result
    assert result is not None  # build_argv never returns an empty list
    return result
