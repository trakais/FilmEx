"""Frames: same-basename groups of images, sidecars and thumbnails."""

from __future__ import annotations

from pathlib import Path

from filmex.services import frames as fr


def _touch(folder: Path, name: str, size: int = 4) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"x" * size)
    return path


def test_stem_key_handles_images_sidecars_and_legacy_forms():
    assert fr.stem_key("DSC0001.ARW") == "DSC0001"
    assert fr.stem_key("DSC0001.jpg") == "DSC0001"
    assert fr.stem_key("DSC0001.xmp") == "DSC0001"
    assert fr.stem_key("DSC0001.ARW.xmp") == "DSC0001"
    assert fr.stem_key("master_001.thm") == "master_001"
    assert fr.stem_key("roll_manifest.json") is None
    assert fr.stem_key("notes.txt") is None


def test_list_frames_groups_pairs_and_companions(tmp_path: Path):
    folder = tmp_path / "roll"
    _touch(folder, "DSC0001.ARW", 10)
    _touch(folder, "DSC0001.JPG", 3)
    _touch(folder, "DSC0001.xmp", 1)
    _touch(folder, "DSC0002.ARW", 10)
    _touch(folder, "DSC0002.ARW.xmp", 1)
    _touch(folder, "DSC0003.JPG", 3)  # unmatched JPEG in a raw folder
    _touch(folder, "orphan.xmp", 1)
    _touch(folder, "roll_manifest.json", 2)
    _touch(folder, ".DS_Store", 1)
    _touch(folder, "._DSC0001.ARW", 1)

    listing = fr.list_frames(folder)
    assert listing.has_raw
    keys = [f.key for f in listing.frames]
    assert keys == ["DSC0001", "DSC0002", "DSC0003"]
    by_key = fr.frames_by_key(listing)

    one = by_key["DSC0001"]
    assert one.images == ["DSC0001.ARW", "DSC0001.JPG"]
    assert one.companions == ["DSC0001.xmp"]
    assert one.has_raw and not one.unmatched_jpeg
    assert one.preview == "DSC0001.JPG"
    assert one.bytes == 14
    assert set(one.kinds) == {"camera_scan_raw", "lab_scan_jpeg"}

    two = by_key["DSC0002"]
    assert two.companions == ["DSC0002.ARW.xmp"]
    assert two.preview == "DSC0002.ARW"

    three = by_key["DSC0003"]
    assert three.unmatched_jpeg
    assert not three.has_raw

    assert listing.other_files == ["orphan.xmp", "roll_manifest.json"]
    assert listing.to_dict()["frame_count"] == 3
    assert listing.to_dict()["frames"][0]["files"] == ["DSC0001.ARW", "DSC0001.JPG", "DSC0001.xmp"]


def test_jpeg_only_folder_has_no_unmatched_frames(tmp_path: Path):
    folder = tmp_path / "lab"
    for i in range(3):
        _touch(folder, f"scan_{i:03d}.jpg")
    _touch(folder, "scan_000.thm")
    listing = fr.list_frames(folder)
    assert not listing.has_raw
    assert all(not f.unmatched_jpeg for f in listing.frames)
    assert listing.frames[0].companions == ["scan_000.thm"]
    assert fr.earliest_file_date(folder, listing.frames)


def test_missing_folder_raises(tmp_path: Path):
    try:
        fr.list_frames(tmp_path / "nope")
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError")
