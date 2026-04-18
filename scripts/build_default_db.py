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
    FilmBrand,
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

# Brand palettes rendered as the bookmark strip on film picker tiles.
# Keep in sync with `_KNOWN_BRAND_COLORS` in filmex/migrations.py so a fresh DB
# (via default_data.sqlite bootstrap) and an existing DB (via v3 migration
# backfill) end up with the same seed palettes.
FILM_BRANDS = [
    {"name": "Kodak", "color_primary": "#f7ad03", "color_secondary": "#c9489e"},
    {"name": "Kodak Eastman", "color_primary": "#f31a2b", "color_secondary": "#fdb71b"},
    {"name": "Ilford", "color_primary": "#000000", "color_secondary": "#ffffff"},
    {"name": "Kentmere", "color_primary": "#2f2298", "color_secondary": "#e50163"},
    {"name": "Fujifilm", "color_primary": "#079952", "color_secondary": "#942b6f"},
    {"name": "Foma", "color_primary": "#2e4076", "color_secondary": "#f5d300"},
    {"name": "Agfa", "color_primary": "#73767c", "color_secondary": "#cf1c30"},
    {"name": "Harman", "color_primary": "#d22b19", "color_secondary": "#ffffff"},
    {"name": "CineStill", "color_primary": "#493ab4", "color_secondary": "#171918"},
    {"name": "Rollei", "color_primary": "#000000", "color_secondary": "#df3a46"},
    {"name": "Lomography", "color_primary": "#bb9d70", "color_secondary": "#292825"},
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
        "brand": "Agfa",
        "name": "APX 100",
        "display_name": "Agfa APX 100",
        "box_speed_iso": 100,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Agfa",
        "name": "Color 400",
        "display_name": "Agfa Color 400",
        "box_speed_iso": 400,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Aerocolor IV",
        "display_name": "Kodak Eastman Aerocolor IV 100",
        "box_speed_iso": 100,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak",
        "name": "Portra 160",
        "display_name": "Kodak Portra 160",
        "box_speed_iso": 160,
        "film_type": "color_negative",
        "process_type": "c41",
    },
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
        "name": "Portra 800",
        "display_name": "Kodak Portra 800",
        "box_speed_iso": 800,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak",
        "name": "Pro Image 100",
        "display_name": "Kodak Pro Image 100",
        "box_speed_iso": 100,
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
        "brand": "Kodak",
        "name": "Tri-X 400",
        "display_name": "Kodak Tri-X 400",
        "box_speed_iso": 400,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Ektachrome E100",
        "display_name": "Kodak Eastman Ektachrome E100",
        "box_speed_iso": 100,
        "film_type": "slide",
        "process_type": "e6",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Ektacolor Pro 160",
        "display_name": "Kodak Eastman Ektacolor Pro 160",
        "box_speed_iso": 160,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Ektacolor Pro 400",
        "display_name": "Kodak Eastman Ektacolor Pro 400",
        "box_speed_iso": 400,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Ektacolor Pro 800",
        "display_name": "Kodak Eastman Ektacolor Pro 800",
        "box_speed_iso": 800,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Ektapan 100",
        "display_name": "Kodak Eastman Ektapan 100",
        "box_speed_iso": 100,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Ektapan 400",
        "display_name": "Kodak Eastman Ektapan 400",
        "box_speed_iso": 400,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Ektapan P3200",
        "display_name": "Kodak Eastman Ektapan P3200",
        "box_speed_iso": 3200,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Ektar 100",
        "display_name": "Kodak Eastman Ektar 100",
        "box_speed_iso": 100,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Tri-X 400",
        "display_name": "Kodak Eastman Tri-X 400",
        "box_speed_iso": 400,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Kodacolor 100",
        "display_name": "Kodak Eastman Kodacolor 100",
        "box_speed_iso": 100,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Kodacolor 200",
        "display_name": "Kodak Eastman Kodacolor 200",
        "box_speed_iso": 200,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Gold 200",
        "display_name": "Kodak Eastman Gold 200",
        "box_speed_iso": 200,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Ultramax 400",
        "display_name": "Kodak Eastman Ultramax 400",
        "box_speed_iso": 400,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Kentmere",
        "name": "Kentmere 100",
        "display_name": "Kentmere 100",
        "box_speed_iso": 100,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Kentmere",
        "name": "Kentmere 200",
        "display_name": "Kentmere 200",
        "box_speed_iso": 200,
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
        "brand": "Foma",
        "name": "Fomapan 100",
        "display_name": "Foma Fomapan 100",
        "box_speed_iso": 100,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Foma",
        "name": "Fomapan 200",
        "display_name": "Foma Fomapan 200",
        "box_speed_iso": 200,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Foma",
        "name": "Fomapan 400",
        "display_name": "Foma Fomapan 400",
        "box_speed_iso": 400,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Foma",
        "name": "Fomapan Retro 100",
        "display_name": "Foma Fomapan Retro 100",
        "box_speed_iso": 100,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Foma",
        "name": "Ortho 400",
        "display_name": "Foma Ortho 400",
        "box_speed_iso": 400,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Fujifilm",
        "name": "Neopan Acros 100 II",
        "display_name": "Fujifilm Neopan Acros 100 II",
        "box_speed_iso": 100,
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
        "name": "Double-X",
        "display_name": "Kodak Double-X 200",
        "box_speed_iso": 200,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Vision3 50D",
        "display_name": "Kodak Eastman Vision3 50D",
        "box_speed_iso": 50,
        "film_type": "color_negative",
        "process_type": "ecn2",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Vision3 250D",
        "display_name": "Kodak Eastman Vision3 250D",
        "box_speed_iso": 250,
        "film_type": "color_negative",
        "process_type": "ecn2",
    },
    {
        "brand": "Kodak Eastman",
        "name": "Vision3 500T",
        "display_name": "Kodak Eastman Vision3 500T",
        "box_speed_iso": 500,
        "film_type": "color_negative",
        "process_type": "ecn2",
    },
    {
        "brand": "Kodak",
        "name": "Ektachrome E100",
        "display_name": "Kodak Ektachrome E100",
        "box_speed_iso": 100,
        "film_type": "slide",
        "process_type": "e6",
    },
    {
        "brand": "Ilford",
        "name": "Delta 3200",
        "display_name": "Ilford Delta 3200",
        "box_speed_iso": 3200,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Ilford",
        "name": "FP4 Plus",
        "display_name": "Ilford FP4 Plus 125",
        "box_speed_iso": 125,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Ilford",
        "name": "HP5 Plus",
        "display_name": "Ilford HP5 Plus 400",
        "box_speed_iso": 400,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Ilford",
        "name": "Ortho Plus",
        "display_name": "Ilford Ortho Plus 80",
        "box_speed_iso": 80,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Ilford",
        "name": "Pan F Plus",
        "display_name": "Ilford Pan F Plus 50",
        "box_speed_iso": 50,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Ilford",
        "name": "XP2",
        "display_name": "Ilford XP2 400",
        "box_speed_iso": 400,
        "film_type": "bw_negative",
        "process_type": "c41",
    },
    {
        "brand": "Harman",
        "name": "Red",
        "display_name": "Harman Red 125",
        "box_speed_iso": 125,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Harman",
        "name": "Phoenix II",
        "display_name": "Harman Phoenix II 200",
        "box_speed_iso": 200,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "CineStill",
        "name": "50D",
        "display_name": "CineStill 50D",
        "box_speed_iso": 50,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "CineStill",
        "name": "400D",
        "display_name": "CineStill 400D",
        "box_speed_iso": 400,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "CineStill",
        "name": "800T",
        "display_name": "CineStill 800T",
        "box_speed_iso": 800,
        "film_type": "color_negative",
        "process_type": "c41",
    },
    {
        "brand": "Rollei",
        "name": "Blackbird",
        "display_name": "Rollei Blackbird 64",
        "box_speed_iso": 64,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Rollei",
        "name": "Paul & Reinhold",
        "display_name": "Rollei Paul & Reinhold 640",
        "box_speed_iso": 640,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Rollei",
        "name": "Retro 80S",
        "display_name": "Rollei Retro 80S",
        "box_speed_iso": 80,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Rollei",
        "name": "Retro 400S",
        "display_name": "Rollei Retro 400S",
        "box_speed_iso": 400,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Rollei",
        "name": "RPX 25",
        "display_name": "Rollei RPX 25",
        "box_speed_iso": 25,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Rollei",
        "name": "RPX 100",
        "display_name": "Rollei RPX 100",
        "box_speed_iso": 100,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Rollei",
        "name": "RPX 400",
        "display_name": "Rollei RPX 400",
        "box_speed_iso": 400,
        "film_type": "bw_negative",
        "process_type": "bw",
    },
    {
        "brand": "Rollei",
        "name": "Superpan 200",
        "display_name": "Rollei Superpan 200",
        "box_speed_iso": 200,
        "film_type": "bw_negative",
        "process_type": "bw",
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
        for b in FILM_BRANDS:
            session.add(FilmBrand(**b))
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
