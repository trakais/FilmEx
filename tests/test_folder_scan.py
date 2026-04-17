"""Folder scan: extension detection, tier derivation, mixed folders."""

from __future__ import annotations

from pathlib import Path

import pytest

from filmex.services.folder_scan import HIGH_RES_JPG_THRESHOLD, scan_folder


def test_missing_folder_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_folder(tmp_path / "does-not-exist")


def test_high_res_jpg_tier(make_jpeg, tmp_path):
    folder = tmp_path / "2026-04-17_R001"
    folder.mkdir()
    make_jpeg("a.jpg", long_edge=HIGH_RES_JPG_THRESHOLD, folder=folder)
    result = scan_folder(folder)
    assert result.source_kind == "lab_scan_jpeg"
    assert result.scan_tier == "high-res"
    assert result.roll_id == "2026-04-17_R001"
    assert [f.name for f in result.files] == ["a.jpg"]


def test_standard_tier_below_threshold(make_jpeg, tmp_path):
    folder = tmp_path / "roll_std"
    folder.mkdir()
    make_jpeg("a.jpg", long_edge=HIGH_RES_JPG_THRESHOLD - 500, folder=folder)
    result = scan_folder(folder)
    assert result.scan_tier == "standard"


def test_tiff_folder_always_high_res(tmp_path):
    folder = tmp_path / "tiff_roll"
    folder.mkdir()
    # PIL can write TIFF directly
    from PIL import Image

    Image.new("RGB", (1000, 800)).save(folder / "a.tif", "TIFF")
    result = scan_folder(folder)
    assert result.source_kind == "positive_master_tiff"
    assert result.scan_tier == "high-res"


def test_tiff_with_thm_marks_thumbnail(make_thm, tmp_path):
    folder = tmp_path / "tiff_with_thm"
    folder.mkdir()
    from PIL import Image

    Image.new("RGB", (1000, 800)).save(folder / "004243560012.tif", "TIFF")
    make_thm("004243560012.thm", folder=folder)

    result = scan_folder(folder)
    assert len(result.files) == 1
    assert result.files[0].name == "004243560012.tif"
    assert result.files[0].has_thumbnail is True


def test_raw_folder_detection(make_dng, tmp_path):
    folder = tmp_path / "raw_roll"
    folder.mkdir()
    make_dng("frame.dng", folder=folder)
    result = scan_folder(folder)
    assert result.source_kind == "camera_scan_raw"
    # Raw files skip PIL dimension read; long_edge_px is None.
    assert result.files[0].long_edge_px is None


def test_mixed_folder_unsupported_skipped(make_jpeg, tmp_path):
    folder = tmp_path / "mixed"
    folder.mkdir()
    make_jpeg("a.jpg", long_edge=3200, folder=folder)
    (folder / "README.txt").write_text("ignore me")
    (folder / ".DS_Store").write_bytes(b"\x00")
    result = scan_folder(folder)
    assert [f.name for f in result.files] == ["a.jpg"]


def test_symlinks_not_followed(make_jpeg, tmp_path):
    src = make_jpeg("a.jpg", long_edge=3200)
    folder = tmp_path / "linked"
    folder.mkdir()
    (folder / "link.jpg").symlink_to(src)
    result = scan_folder(folder)
    # The symlink itself should be skipped.
    assert [f.name for f in result.files] == []


def test_mixed_kinds_majority_wins(make_jpeg, make_dng, tmp_path):
    folder = tmp_path / "mixed_kinds"
    folder.mkdir()
    make_jpeg("a.jpg", long_edge=3200, folder=folder)
    make_jpeg("b.jpg", long_edge=3200, folder=folder)
    make_dng("c.dng", folder=folder)
    result = scan_folder(folder)
    assert result.source_kind == "lab_scan_jpeg"
    # All files listed, each with its own per-file source_kind.
    kinds_by_name = {f.name: f.source_kind for f in result.files}
    assert kinds_by_name == {
        "a.jpg": "lab_scan_jpeg",
        "b.jpg": "lab_scan_jpeg",
        "c.dng": "camera_scan_raw",
    }


def test_empty_folder_defaults(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    result = scan_folder(folder)
    assert result.files == []
    assert result.source_kind == "lab_scan_jpeg"
    assert result.scan_tier == "standard"
    assert result.suggested_roll_date is None


def test_suggested_roll_date_from_earliest_file(make_jpeg, tmp_path):
    import re
    from datetime import date

    folder = tmp_path / "dated_roll"
    folder.mkdir()
    make_jpeg("a.jpg", long_edge=3200, folder=folder)
    make_jpeg("b.jpg", long_edge=3200, folder=folder)

    result = scan_folder(folder)
    # Freshly-created files: date is today (avoids platform quirks between
    # birthtime/mtime) and is a well-formed ISO date.
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", result.suggested_roll_date or "")
    assert result.suggested_roll_date == date.today().isoformat()
