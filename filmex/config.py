"""Path resolution. All user data lives in a project-local `data/` directory."""

from __future__ import annotations

import os
from pathlib import Path

import filmex

PACKAGE_ROOT = Path(filmex.__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

# Allow tests to override DATA_DIR via env var; default is project-relative.
_env_data_dir = os.environ.get("FILMEX_DATA_DIR")
DATA_DIR: Path = Path(_env_data_dir) if _env_data_dir else PROJECT_ROOT / "data"

# Bundled "fresh install" database. Copied into DATA_DIR on first run if the
# user's DB doesn't yet exist, then never touched again. Regenerate with
# `scripts/build_default_db.py`.
DEFAULT_DB: Path = PACKAGE_ROOT / "default_data.sqlite"


def db_path() -> Path:
    """Return the SQLite database path, resolved lazily so tests can override DATA_DIR."""
    data_dir = Path(os.environ.get("FILMEX_DATA_DIR", DATA_DIR))
    return data_dir / "filmex.sqlite"


def ensure_data_dir() -> Path:
    """Create the data directory if missing and return it."""
    data_dir = Path(os.environ.get("FILMEX_DATA_DIR", DATA_DIR))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir
