"""Thin subprocess wrapper around the pure builder in `exiftool.py`."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from filmex.services import exiftool as et


def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def read_tags(path: Path | str, tags: list[str]) -> dict[str, Any]:
    """Read `tags` from `path` via exiftool, returning a dict."""
    argv = et.build_read_argv(path, tags)
    result = subprocess.run(argv, capture_output=True, check=False)
    if result.returncode != 0 and not result.stdout:
        return {}
    return et.parse_read_output(result.stdout)


def apply_intent(path: Path | str, intent: et.WriteIntent) -> subprocess.CompletedProcess:
    """Run exiftool to apply `intent` to `path`.

    For sidecar-mode files (ARW/DNG), checks whether the XMP already exists so
    `build_argv` picks the correct exiftool invocation (direct edit vs -o seed).
    """
    sidecar_exists = False
    if et.detect_mode(path) == "sidecar":
        sidecar_exists = et.sidecar_path_for(path).is_file()
    argv = et.build_argv(
        path,
        intent,
        dry_run=False,
        overwrite_original=True,
        sidecar_exists=sidecar_exists,
    )
    return subprocess.run(argv, capture_output=True, check=False)
