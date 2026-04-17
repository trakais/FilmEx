"""Shared fixtures for the FilmEx test suite.

Every test runs against a throwaway SQLite database in a per-test temp dir so
the real `data/` directory is never touched.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture
def data_dir(tmp_path: Path) -> Iterator[Path]:
    """Point FilmEx at a temp data directory for this test."""
    prev = os.environ.get("FILMEX_DATA_DIR")
    target = tmp_path / "data"
    target.mkdir()
    os.environ["FILMEX_DATA_DIR"] = str(target)

    # Reset the cached engine so the new DATA_DIR is picked up.
    from filmex import db as db_module

    db_module.reset_engine()

    yield target

    db_module.reset_engine()
    if prev is None:
        os.environ.pop("FILMEX_DATA_DIR", None)
    else:
        os.environ["FILMEX_DATA_DIR"] = prev


@pytest.fixture
def client(data_dir: Path) -> Iterator[TestClient]:
    """Booted FastAPI client using a throwaway data directory."""
    from filmex.main import app

    with TestClient(app) as c:
        _seed_test_gear(c)
        yield c


def _seed_test_gear(client: TestClient) -> None:
    """Insert one camera + one lens via the public API when the DB is blank."""
    # Idempotent — skip if another test already populated this DB instance.
    if client.get("/api/cameras").json():
        return
    lens = client.post(
        "/api/lenses",
        json={
            "make": "Industar",
            "model": "50",
            "display_name": "Industar-50 50mm f3.5",
            "lens_model_write_value": "Industar-50 50mm f3.5",
            "focal_length_mm": 50,
            "max_aperture": 3.5,
            "mount": "M39",
        },
    ).json()
    client.post(
        "/api/cameras",
        json={
            "make": "FED",
            "model": "2",
            "display_name": "FED-2",
            "mount": "M39",
            "camera_type": "rangefinder",
        },
    )
    assert lens["id"]


@pytest.fixture
def make_jpeg(tmp_path: Path):
    """Factory for creating test JPEGs with a given long-edge size."""

    def _make(name: str, long_edge: int = 3200, folder: Path | None = None) -> Path:
        folder = folder or tmp_path
        folder.mkdir(parents=True, exist_ok=True)
        w, h = long_edge, int(long_edge * 2 / 3)
        path = folder / name
        Image.new("RGB", (w, h), color=(128, 128, 128)).save(path, "JPEG")
        return path

    return _make


@pytest.fixture
def make_dng(tmp_path: Path):
    """Factory for a DNG-looking file (TIFF container renamed to .dng).

    Real ARW is a proprietary Sony format that Pillow can't author; this TIFF
    container is sufficient for exiftool to write a sidecar against.
    """

    def _make(name: str, long_edge: int = 3000, folder: Path | None = None) -> Path:
        folder = folder or tmp_path
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (name + ".tif")
        Image.new("RGB", (long_edge, long_edge), color=(0, 0, 0)).save(tmp, "TIFF")
        final = folder / name
        shutil.move(tmp, final)
        return final

    return _make


@pytest.fixture
def make_thm(tmp_path: Path):
    """Factory for a JPEG thumbnail stored under a `.thm` extension."""

    def _make(name: str, folder: Path | None = None) -> Path:
        folder = folder or tmp_path
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        Image.new("RGB", (160, 120), color=(96, 96, 96)).save(path, "JPEG")
        return path

    return _make


@pytest.fixture
def roll_factory(tmp_path: Path, make_jpeg, make_dng, make_thm):
    """Create a realistic roll folder populated with the requested mix of files."""

    def _make(
        roll_name: str,
        *,
        jpegs: int = 1,
        dngs: int = 0,
        tiffs: int = 0,
        tiff_thumbnails: bool = False,
        long_edge: int = 3200,
    ) -> Path:
        folder = tmp_path / roll_name
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(jpegs):
            make_jpeg(f"scan_{i:03d}.jpg", long_edge=long_edge, folder=folder)
        for i in range(tiffs):
            Image.new("RGB", (long_edge, int(long_edge * 2 / 3)), color=(120, 120, 120)).save(
                folder / f"master_{i:03d}.tif", "TIFF"
            )
            if tiff_thumbnails:
                make_thm(f"master_{i:03d}.thm", folder=folder)
        for i in range(dngs):
            make_dng(f"frame_{i:03d}.dng", long_edge=long_edge, folder=folder)
        return folder

    return _make
