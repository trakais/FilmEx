"""Roll manifest creation, merge, apply history."""

from __future__ import annotations

import json
from pathlib import Path

from filmex.services import manifest as mf


def _snap_camera() -> mf.GearSnapshot:
    return mf.GearSnapshot(
        id="cam-1",
        make="FED",
        model="2b",
        display_name="FED 2b",
        mount="M39",
    )


def _snap_film() -> mf.FilmSnapshot:
    return mf.FilmSnapshot(
        id="film-1",
        brand="Kodak",
        display_name="Kodak Portra 400",
        box_speed_iso=400,
        film_type="color_negative",
        process_type="c41",
    )


def _snap_named(name: str) -> mf.NamedSnapshot:
    return mf.NamedSnapshot(id=f"id-{name}", name=name)


def test_manifest_path_uses_constant_filename(tmp_path):
    assert mf.manifest_path_for(tmp_path).name == mf.MANIFEST_FILENAME


def test_read_manifest_returns_none_when_missing(tmp_path):
    assert mf.read_manifest(tmp_path) is None


def test_build_new_manifest_has_required_fields(tmp_path):
    data = mf.build_new_manifest(
        folder=tmp_path,
        roll_id="R001",
        source_kind="lab_scan_jpeg",
        scan_tier="high-res",
        camera=_snap_camera(),
        lens=mf.GearSnapshot(),
        film=_snap_film(),
        development_provider=_snap_named("BFS"),
        development_method=_snap_named("C-41"),
        scan_provider=_snap_named("BFS"),
        scan_method=_snap_named("Lab scan"),
    )
    for key in (
        "manifest_version",
        "roll_id",
        "created_at",
        "updated_at",
        "source_kind",
        "scan_tier",
        "folder_relative_path",
        "analog_camera_snapshot",
        "film_stock_snapshot",
        "capture_date_precision",
        "apply_history",
        "file_overrides",
    ):
        assert key in data
    assert data["manifest_version"] == mf.MANIFEST_VERSION
    assert data["roll_id"] == "R001"
    assert data["analog_camera_snapshot"]["make"] == "FED"


def test_write_and_read_roundtrip(tmp_path):
    data = mf.build_new_manifest(
        folder=tmp_path,
        roll_id="R001",
        source_kind="lab_scan_jpeg",
        scan_tier="standard",
        camera=_snap_camera(),
        lens=mf.GearSnapshot(),
        film=_snap_film(),
        development_provider=_snap_named("BFS"),
        development_method=_snap_named("C-41"),
        scan_provider=_snap_named("BFS"),
        scan_method=_snap_named("Lab scan"),
    )
    path = mf.write_manifest(tmp_path, data)
    assert path.is_file()
    roundtripped = mf.read_manifest(tmp_path)
    assert roundtripped == data


def test_merge_preserves_created_at_and_history(tmp_path):
    existing = {
        "created_at": "2026-01-01T00:00:00+00:00",
        "apply_history": [{"timestamp": "old", "applied_files": ["a.jpg"], "diffs": []}],
        "roll_id": "R001",
    }
    fresh = mf.build_new_manifest(
        folder=tmp_path,
        roll_id="R001",
        source_kind="lab_scan_jpeg",
        scan_tier="high-res",
        camera=_snap_camera(),
        lens=mf.GearSnapshot(),
        film=_snap_film(),
        development_provider=_snap_named("BFS"),
        development_method=_snap_named("C-41"),
        scan_provider=_snap_named("BFS"),
        scan_method=_snap_named("Lab scan"),
    )
    merged = mf.merge_existing_manifest(existing, fresh)
    assert merged["created_at"] == "2026-01-01T00:00:00+00:00"
    assert merged["apply_history"] == existing["apply_history"]
    # updated_at still reflects the fresh run.
    assert merged["updated_at"] == fresh["updated_at"]


def test_append_apply_history_bumps_updated_at(tmp_path):
    data = mf.build_new_manifest(
        folder=tmp_path,
        roll_id="R001",
        source_kind="lab_scan_jpeg",
        scan_tier="standard",
        camera=_snap_camera(),
        lens=mf.GearSnapshot(),
        film=_snap_film(),
        development_provider=_snap_named("BFS"),
        development_method=_snap_named("C-41"),
        scan_provider=_snap_named("BFS"),
        scan_method=_snap_named("Lab scan"),
    )
    original_updated = data["updated_at"]
    result = mf.append_apply_history(data, applied_files=["a.jpg"], diffs=[], dry_run=False)
    assert len(result["apply_history"]) == 1
    assert result["apply_history"][0]["applied_files"] == ["a.jpg"]
    assert result["apply_history"][0]["dry_run"] is False
    # updated_at should have been refreshed (different instant).
    assert result["updated_at"] >= original_updated


def test_set_file_overrides_replaces_with_cleaned_keywords(tmp_path):
    data = mf.build_new_manifest(
        folder=tmp_path,
        roll_id="R001",
        source_kind="lab_scan_jpeg",
        scan_tier="standard",
        camera=_snap_camera(),
        lens=mf.GearSnapshot(),
        film=_snap_film(),
        development_provider=_snap_named("BFS"),
        development_method=_snap_named("C-41"),
        scan_provider=_snap_named("BFS"),
        scan_method=_snap_named("Lab scan"),
    )
    result = mf.set_file_overrides(
        data,
        extra_keywords_by_file={
            "a.jpg": [" Portrait ", "", "Warm Light"],
            "b.jpg": [],
        },
    )
    assert result["file_overrides"] == {
        "a.jpg": {"extra_keywords": ["Portrait", "Warm Light"]},
    }


def test_diff_to_jsonable_accepts_dataclasses():
    from filmex.services.exiftool import TagChange

    payload = mf.diff_to_jsonable(
        [
            TagChange(tag="Make", action="set", before=None, after="FED"),
            {"tag": "x", "action": "skip", "before": None, "after": None},
        ]
    )
    assert payload[0]["tag"] == "Make"
    assert payload[0]["after"] == "FED"
    assert payload[1]["tag"] == "x"
