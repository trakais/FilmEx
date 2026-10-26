"""Find the ExifTool executable once per process.

Resolution order:

1. A path configured in settings (``exiftool_path``), via :func:`configure`.
2. The ``FILMEX_EXIFTOOL`` environment variable.
3. ``exiftool`` on ``PATH``.
4. Well-known install locations. An app launched from Finder does not
   inherit the shell's ``PATH``, so a Homebrew install is invisible to
   ``shutil.which`` there; this step is what makes it work.
5. A vendored ExifTool inside the app bundle (``vendor/exiftool/exiftool``).

The resolved command is an argv prefix: a single executable for native
builds and wrappers, or ``(perl, script)`` for the pure-Perl distribution.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from filmex.config import bundle_resource_dir

_log = logging.getLogger(__name__)

ENV_VAR = "FILMEX_EXIFTOOL"

_CANDIDATES_POSIX: tuple[str, ...] = (
    "/opt/homebrew/bin/exiftool",
    "/usr/local/bin/exiftool",
    "/usr/bin/exiftool",
)
_CANDIDATES_WINDOWS: tuple[str, ...] = (
    r"C:\Program Files\ExifTool\exiftool.exe",
    r"C:\ExifTool\exiftool.exe",
)


@dataclass(frozen=True)
class ExifToolLocation:
    """Where ExifTool was found and how to invoke it."""

    command: tuple[str, ...]
    source: str  # "setting" | "env" | "path" | "candidate" | "bundled"

    @property
    def path(self) -> str:
        return self.command[-1]


_lock = threading.Lock()
_configured_path: str | None = None
_cache: ExifToolLocation | None = None
_resolved = False


def configure(path: str | None) -> None:
    """Set (or clear) the user-configured path and drop the cached result."""
    global _configured_path
    with _lock:
        _configured_path = path.strip() if path and path.strip() else None
    invalidate()


def invalidate() -> None:
    """Forget the cached location so the next lookup re-resolves."""
    global _cache, _resolved
    with _lock:
        _cache = None
        _resolved = False


def locate() -> ExifToolLocation | None:
    """Return the cached location, resolving it on first use."""
    global _cache, _resolved
    with _lock:
        if _resolved:
            return _cache
        _cache = _resolve()
        _resolved = True
        return _cache


def command() -> list[str] | None:
    """Argv prefix for invoking ExifTool, or None when it is not installed."""
    loc = locate()
    return list(loc.command) if loc else None


def available() -> bool:
    return locate() is not None


def version(timeout: float = 10.0) -> str | None:
    """Run ``exiftool -ver`` and return the version string, or None."""
    cmd = command()
    if cmd is None:
        return None
    try:
        proc = subprocess.run(cmd + ["-ver"], capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("exiftool -ver failed: %s", exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip() or None


def candidates() -> tuple[str, ...]:
    return _CANDIDATES_WINDOWS if sys.platform.startswith("win") else _CANDIDATES_POSIX


# ---------- resolution ----------


def _resolve() -> ExifToolLocation | None:
    if _configured_path:
        loc = _from_path(_configured_path, "setting")
        if loc:
            return loc
        _log.warning("configured exiftool path is not usable: %s", _configured_path)

    env = os.environ.get(ENV_VAR)
    if env:
        loc = _from_path(env, "env")
        if loc:
            return loc
        _log.warning("%s is not usable: %s", ENV_VAR, env)

    found = shutil.which("exiftool")
    if found:
        loc = _from_path(found, "path")
        if loc:
            return loc

    for candidate in candidates():
        loc = _from_path(candidate, "candidate")
        if loc:
            return loc

    vendor = bundle_resource_dir()
    if vendor is not None:
        for name in ("exiftool", "exiftool.exe"):
            loc = _from_path(str(vendor / "exiftool" / name), "bundled")
            if loc:
                return loc
    return None


def _from_path(raw: str, source: str) -> ExifToolLocation | None:
    """Turn a user- or system-supplied path into an invocation, or None."""
    path = Path(raw).expanduser()
    if path.is_dir():
        # Accept the unpacked distribution directory itself.
        for name in ("exiftool", "exiftool.exe"):
            if (path / name).is_file():
                path = path / name
                break
        else:
            return None
    if not path.is_file():
        return None

    if _is_perl_script(path):
        perl = _find_perl()
        if perl is None:
            _log.warning("exiftool at %s is a Perl script but no perl interpreter was found", path)
            return None
        return ExifToolLocation(command=(perl, str(path)), source=source)

    if not os.access(path, os.X_OK) and not sys.platform.startswith("win"):
        return None
    return ExifToolLocation(command=(str(path),), source=source)


def _is_perl_script(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(160)
    except OSError:
        return False
    if not head.startswith(b"#!"):
        return False
    first_line = head.split(b"\n", 1)[0].lower()
    return b"perl" in first_line


def _find_perl() -> str | None:
    for candidate in ("/usr/bin/perl", shutil.which("perl")):
        if candidate and Path(candidate).is_file():
            return candidate
    return None
