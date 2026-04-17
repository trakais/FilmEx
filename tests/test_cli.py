"""CLI batch add/export tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from filmex.cli import apply_batch, export_database, main


@pytest.fixture
def blank_db(data_dir: Path, monkeypatch):
    """Start from an empty schema (no bundled default_data.sqlite bootstrap)."""
    import filmex.db as db_module

    # Point the bootstrap copy at a non-existent file so init_db creates
    # empty tables instead of seeding from the bundled default.
    monkeypatch.setattr(db_module, "DEFAULT_DB", Path("/does-not-exist.sqlite"))

    sqlite_path = data_dir / "filmex.sqlite"
    if sqlite_path.exists():
        sqlite_path.unlink()
    db_module.reset_engine()
    yield data_dir
    db_module.reset_engine()


def test_apply_batch_inserts_and_is_idempotent(blank_db):
    batch = {
        "providers": [{"name": "ExampleLab", "kinds": ["development", "scan"]}],
        "development_methods": [{"name": "C-41"}],
        "scan_methods": [{"name": "DSLR capture"}],
        "film_brands": [
            {"name": "Kodak", "color_primary": "#f7ad03", "color_secondary": "#c9489e"},
        ],
        "film_stocks": [
            {
                "brand": "Kodak",
                "name": "Portra 400",
                "display_name": "Kodak Portra 400",
                "box_speed_iso": 400,
                "film_type": "color_negative",
                "process_type": "c41",
            }
        ],
        "cameras": [
            {
                "make": "Minolta",
                "model": "X-700",
                "display_name": "Minolta X-700",
                "mount": "MD",
                "camera_type": "slr",
            }
        ],
        "lenses": [
            {
                "make": "Minolta",
                "model": "MD 50mm f/1.7",
                "display_name": "Minolta MD 50mm f/1.7",
                "lens_model_write_value": "Minolta MD 50mm f/1.7",
                "focal_length_mm": 50,
                "max_aperture": 1.7,
                "mount": "MD",
            }
        ],
    }

    first = apply_batch(batch)
    assert not first.has_errors()
    counts = {s.section: (len(s.created), len(s.skipped)) for s in first.sections}
    assert counts["cameras"] == (1, 0)
    assert counts["lenses"] == (1, 0)
    assert counts["film_brands"] == (1, 0)
    assert counts["film_stocks"] == (1, 0)
    assert counts["providers"] == (1, 0)

    # Second run: everything is skipped.
    second = apply_batch(batch)
    assert not second.has_errors()
    counts2 = {s.section: (len(s.created), len(s.skipped)) for s in second.sections}
    for section, (created, skipped) in counts2.items():
        assert created == 0, f"{section} created rows on re-run"
        assert skipped >= 1, f"{section} should have reported skips"


def test_fixed_lens_ref_links_camera_to_lens(blank_db):
    batch = {
        "cameras": [
            {
                "make": "Yashica",
                "model": "Electro 35 GSN",
                "display_name": "Yashica Electro 35 GSN",
                "mount": "Fixed-Lens",
                "camera_type": "rangefinder",
                "fixed_lens_ref": {
                    "make": "Yashica",
                    "model": "Yashinon DX 45mm f/1.7",
                },
            }
        ],
        "lenses": [
            {
                "make": "Yashica",
                "model": "Yashinon DX 45mm f/1.7",
                "display_name": "Yashica Yashinon DX 45mm f/1.7",
                "lens_model_write_value": "Yashica Yashinon DX 45mm f/1.7",
                "focal_length_mm": 45,
                "max_aperture": 1.7,
                "mount": "Fixed-Lens",
            }
        ],
    }
    report = apply_batch(batch)
    assert not report.has_errors()
    assert len(report.fixed_lens_linked) == 1
    assert "Yashica Electro 35 GSN" in report.fixed_lens_linked[0]

    # Verify the link survives.
    from sqlmodel import Session, select

    from filmex.db import get_engine
    from filmex.models import Camera, Lens

    with Session(get_engine()) as session:
        cam = session.exec(
            select(Camera).where(Camera.model == "Electro 35 GSN")
        ).one()
        assert cam.fixed_lens_id is not None
        lens = session.get(Lens, cam.fixed_lens_id)
        assert lens is not None
        assert lens.model == "Yashinon DX 45mm f/1.7"


def test_lens_without_matching_camera_mount_errors(blank_db):
    batch = {
        "lenses": [
            {
                "make": "Nikon",
                "model": "50mm f/1.8",
                "display_name": "Nikon 50mm f/1.8",
                "lens_model_write_value": "Nikon 50mm f/1.8",
                "mount": "Imaginary Mount",
            }
        ],
    }
    report = apply_batch(batch)
    assert report.has_errors()
    lens_section = next(s for s in report.sections if s.section == "lenses")
    assert lens_section.errors
    assert "Imaginary Mount" in lens_section.errors[0]


def test_film_brand_batch_normalizes_hex(blank_db):
    batch = {
        "film_brands": [
            {
                "name": "Lomography",
                "color_primary": "#BB9D70",
                "color_secondary": "292825",
            }
        ]
    }
    report = apply_batch(batch)
    assert not report.has_errors()

    from sqlmodel import Session, select

    from filmex.db import get_engine
    from filmex.models import FilmBrand

    with Session(get_engine()) as session:
        row = session.exec(
            select(FilmBrand).where(FilmBrand.name == "Lomography")
        ).one()
        # Normalized to lowercase #rrggbb regardless of input shape.
        assert row.color_primary == "#bb9d70"
        assert row.color_secondary == "#292825"


def test_film_brand_batch_rejects_garbage_hex(blank_db):
    batch = {
        "film_brands": [{"name": "BadColor", "color_primary": "not-a-color"}]
    }
    report = apply_batch(batch)
    assert report.has_errors()
    fb = next(s for s in report.sections if s.section == "film_brands")
    assert fb.errors
    assert "color" in fb.errors[0].lower()


def test_film_stock_with_unknown_brand_is_rejected(blank_db):
    # No film_brands section — the stock's brand reference has no target row.
    batch = {
        "film_stocks": [
            {
                "brand": "UnregisteredBrand",
                "name": "Color Negative 400",
                "display_name": "UnregisteredBrand Color Negative 400",
                "box_speed_iso": 400,
                "film_type": "color_negative",
                "process_type": "c41",
            }
        ]
    }
    report = apply_batch(batch)
    assert report.has_errors()
    fs = next(s for s in report.sections if s.section == "film_stocks")
    assert fs.errors
    assert "not registered" in fs.errors[0]


def test_invalid_film_type_is_rejected(blank_db):
    batch = {
        "film_brands": [{"name": "Kodak"}],
        "film_stocks": [
            {
                "brand": "Kodak",
                "name": "Made Up",
                "display_name": "Kodak Made Up",
                "box_speed_iso": 100,
                "film_type": "holographic",
                "process_type": "c41",
            }
        ],
    }
    report = apply_batch(batch)
    assert report.has_errors()
    fs = next(s for s in report.sections if s.section == "film_stocks")
    assert any("film_type" in e for e in fs.errors)


def test_dry_run_reports_without_writing(blank_db):
    batch = {
        "providers": [{"name": "OnlyInDryRun", "kinds": ["development"]}],
    }
    report = apply_batch(batch, dry_run=True)
    assert not report.has_errors()
    providers_section = next(s for s in report.sections if s.section == "providers")
    assert len(providers_section.created) == 1

    # Nothing persisted.
    from sqlmodel import Session, select

    from filmex.db import get_engine
    from filmex.models import Provider

    with Session(get_engine()) as session:
        rows = [
            p
            for p in session.exec(select(Provider)).all()
            if p.name == "OnlyInDryRun"
        ]
        assert rows == []


def test_unknown_batch_section_raises(blank_db):
    with pytest.raises(ValueError) as exc:
        apply_batch({"robots": [{"name": "HAL"}]})
    assert "robots" in str(exc.value)


def test_main_add_from_json_idempotent_exit_codes(blank_db, tmp_path):
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps(
            {
                "cameras": [
                    {
                        "make": "Pentax",
                        "model": "K1000",
                        "display_name": "Pentax K1000",
                        "mount": "K",
                        "camera_type": "slr",
                    }
                ]
            }
        )
    )
    assert main(["add-from-json", str(batch_path)]) == 0
    # Re-run: still success, just all skipped.
    assert main(["add-from-json", str(batch_path)]) == 0


def test_export_roundtrip(blank_db):
    apply_batch(
        {
            "cameras": [
                {
                    "make": "Pentax",
                    "model": "K1000",
                    "display_name": "Pentax K1000",
                    "mount": "K",
                    "camera_type": "slr",
                }
            ]
        }
    )
    exported = export_database()
    assert any(
        c.get("make") == "Pentax" and c.get("model") == "K1000"
        for c in exported.get("cameras", [])
    )
