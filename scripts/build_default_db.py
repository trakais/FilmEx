"""Regenerate the bundled `filmex/default_data.sqlite` from the demo data below.

This file is what a fresh install starts from: `init_db()` copies it to
`data/filmex.sqlite` on first run, then it's 100% user-owned. The demo data
is deliberately neutral — no region-specific labs, no specific camera gear —
just the universal dev/scan methods, generic fallback providers, and a
handful of ubiquitous film stocks so pickers aren't empty on first launch.

Run:
    uv run python scripts/build_default_db.py

This wipes and rewrites `filmex/default_data.sqlite`. Commit the result.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the filmex package importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from filmex.models import (  # noqa: E402
    Camera,
    DevelopmentMethod,
    FilmStock,
    Lens,
    Provider,
    ScanMethod,
)
from filmex.migrations import migrate_db  # noqa: E402

OUTPUT = Path(__file__).resolve().parent.parent / "filmex" / "default_data.sqlite"

DEV_METHODS = ["C-41", "B&W", "E-6", "ECN-2"]
SCAN_METHODS = ["Lab scan", "DSLR capture", "Flatbed"]

# Generic fallbacks only — specific labs are personal and get added by the user.
PROVIDERS = [
    {"name": "Home", "kinds": ["development"], "favorite": False},
    {"name": "Self", "kinds": ["scan"], "favorite": False},
]

# Two demo cameras (one mountable, one fixed-lens) + their lenses. Chosen for
# being iconic and widely recognized; every field is renameable/deletable.
# The lens paired with the fixed-lens body is inserted first so its real UUID
# can be wired into the camera's `fixed_lens_id`.
DEMO_LENSES = [
    {
        "make": "Pentax",
        "model": "SMC 50mm f/1.4",
        "display_name": "Pentax SMC 50mm f/1.4",
        "lens_model_write_value": "Pentax SMC 50mm f/1.4",
        "focal_length_mm": 50,
        "max_aperture": 1.4,
        "mount": "K",
    },
    {
        # Built-in lens for the Yashica Electro 35 GSN; mount stays "Fixed-Lens"
        # so the mount-filter picker excludes it from mountable-camera pickers.
        "make": "Yashica",
        "model": "Yashinon DX 45mm f/1.7",
        "display_name": "Yashinon DX 45mm f/1.7",
        "lens_model_write_value": "Yashinon DX 45mm f/1.7",
        "focal_length_mm": 45,
        "max_aperture": 1.7,
        "mount": "Fixed-Lens",
    },
]

# Cameras reference lenses by list index — resolved to real IDs in build().
DEMO_CAMERAS = [
    {
        "make": "Pentax",
        "model": "K1000",
        "display_name": "Pentax K1000",
        "mount": "K",
        "camera_type": "slr",
        "fixed_lens_idx": None,
    },
    {
        "make": "Yashica",
        "model": "Electro 35 GSN",
        "display_name": "Yashica Electro 35 GSN",
        "mount": "Fixed-Lens",
        "camera_type": "rangefinder",
        "fixed_lens_idx": 1,  # index into DEMO_LENSES
    },
]

# A small starter shelf of common films so first launch is useful immediately.
# User renames/archives/extends freely.
FILM_STOCKS = [
    {
        "brand": "Kodak",
        "name": "Portra 400",
        "display_name": "Kodak Portra 400",
        "box_speed_iso": 400,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak",
        "name": "Gold 200",
        "display_name": "Kodak Gold 200",
        "box_speed_iso": 200,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak",
        "name": "ColorPlus 200",
        "display_name": "Kodak ColorPlus 200",
        "box_speed_iso": 200,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak",
        "name": "Ultramax 400",
        "display_name": "Kodak Ultramax 400",
        "box_speed_iso": 400,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak",
        "name": "Ektar 100",
        "display_name": "Kodak Ektar 100",
        "box_speed_iso": 100,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Ilford",
        "name": "HP5 Plus 400",
        "display_name": "Ilford HP5 Plus 400",
        "box_speed_iso": 400,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Ilford",
        "name": "Delta 100",
        "display_name": "Ilford Delta 100",
        "box_speed_iso": 100,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Ilford",
        "name": "Delta 400",
        "display_name": "Ilford Delta 400",
        "box_speed_iso": 400,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Kodak",
        "name": "Tri-X 400",
        "display_name": "Kodak Tri-X 400",
        "box_speed_iso": 400,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Kentmere",
        "name": "Pan 400",
        "display_name": "Kentmere Pan 400",
        "box_speed_iso": 400,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Fujifilm",
        "name": "Fujicolor C200",
        "display_name": "Fujifilm Fujicolor C200",
        "box_speed_iso": 200,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Fujifilm",
        "name": "Superia X-TRA 400",
        "display_name": "Fujifilm Superia X-TRA 400",
        "box_speed_iso": 400,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak",
        "name": "Vision3 50D",
        "display_name": "Kodak Vision3 50D",
        "box_speed_iso": 50,
        "film_type": "color_negative",
        "process_type": "ecn2",
    },
    {
        "brand": "Kodak",
        "name": "Vision3 250D",
        "display_name": "Kodak Vision3 250D",
        "box_speed_iso": 250,
        "film_type": "color_negative",
        "process_type": "ecn2",
    },
    {
        "brand": "Kodak",
        "name": "Vision3 500T",
        "display_name": "Kodak Vision3 500T",
        "box_speed_iso": 500,
        "film_type": "color_negative",
        "process_type": "ecn2",
    },
    {
        "brand": "Kodak",
        "name": "Ektachrome E100",
        "display_name": "Kodak Ektachrome E100 100",
        "box_speed_iso": 100,
        "film_type": "slide",
        "process_type": "e6",
    },
]


def build(output: Path) -> None:
    if output.exists():
        output.unlink()
    engine = create_engine(f"sqlite:///{output}")
    SQLModel.metadata.create_all(engine)
    migrate_db(engine)

    with Session(engine) as session:
        for name in DEV_METHODS:
            session.add(DevelopmentMethod(name=name))
        for name in SCAN_METHODS:
            session.add(ScanMethod(name=name))
        for p in PROVIDERS:
            session.add(Provider(name=p["name"], kinds=p["kinds"], favorite=p["favorite"]))
        for f in FILM_STOCKS:
            session.add(FilmStock(**f))

        lens_rows: list[Lens] = []
        for ldef in DEMO_LENSES:
            lens = Lens(**ldef)
            session.add(lens)
            lens_rows.append(lens)
        session.flush()  # assign lens IDs so cameras can reference them

        for cdef in DEMO_CAMERAS:
            fixed_idx = cdef.pop("fixed_lens_idx", None)
            fixed_id = lens_rows[fixed_idx].id if fixed_idx is not None else None
            session.add(Camera(fixed_lens_id=fixed_id, **cdef))

        session.commit()

    engine.dispose()
    print(f"Wrote {output} ({output.stat().st_size} bytes)")


if __name__ == "__main__":
    build(OUTPUT)
