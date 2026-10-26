"""Path resolution for the user's data directory and bundled resources.

Resolution order for the data directory:

1. ``FILMEX_DATA_DIR`` environment variable (tests, power users).
2. The platform user-data directory (``~/Library/Application Support/FilmEx``
   on macOS, ``%LOCALAPPDATA%\\FilmEx`` on Windows, ``~/.local/share/FilmEx``
   on Linux) when running as a packaged app or an installed tool.
3. ``<checkout>/data`` when running from a source checkout, so development
   keeps using the project-local database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import filmex

APP_NAME = "FilmEx"

PACKAGE_ROOT = Path(filmex.__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

# Bundled "fresh install" database. Copied into the data dir on first run if
# the user's DB doesn't yet exist, then never touched again. Regenerate with
# `scripts/build_default_db.py`.
DEFAULT_DB: Path = PACKAGE_ROOT / "default_data.sqlite"


def is_frozen() -> bool:
    """True inside a PyInstaller (or similar) bundle."""
    return bool(getattr(sys, "frozen", False))


def is_source_checkout() -> bool:
    """True when the package is imported from a git checkout.

    An installed tool (``uv tool install``, pip) lands in site-packages with
    no ``pyproject.toml`` beside the package; a checkout always has one.
    """
    if is_frozen():
        return False
    return (PROJECT_ROOT / "pyproject.toml").is_file()


def user_data_dir() -> Path:
    """Platform-specific per-user data directory for FilmEx."""
    from platformdirs import user_data_dir as _user_data_dir

    return Path(_user_data_dir(APP_NAME, appauthor=False))


def data_dir() -> Path:
    """Resolve the data directory. Evaluated on every call so tests and the
    desktop launcher can change the environment before the engine is built."""
    env = os.environ.get("FILMEX_DATA_DIR")
    if env:
        return Path(env).expanduser()
    if is_source_checkout():
        return PROJECT_ROOT / "data"
    return user_data_dir()


# Kept for callers that still read the module constant. Prefer `data_dir()`.
DATA_DIR: Path = data_dir()


def db_path() -> Path:
    """Return the SQLite database path."""
    return data_dir() / "filmex.sqlite"


def ensure_data_dir() -> Path:
    """Create the data directory if missing and return it."""
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def static_dir() -> Path:
    """Directory holding the bundled single-page UI."""
    return PACKAGE_ROOT / "static"


def bundle_resource_dir() -> Path | None:
    """Root of extra bundled resources (vendored ExifTool) when frozen.

    PyInstaller exposes the unpacked bundle root as ``sys._MEIPASS``; the
    spec file places vendored resources under ``vendor/`` inside it.
    """
    if not is_frozen():
        return None
    root = getattr(sys, "_MEIPASS", None)
    if not root:
        return None
    return Path(root) / "vendor"
