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


def test_every_folder_view_skips_junk_the_same_way(tmp_path: Path, data_dir, make_jpeg):
    """The Tag view scan, the thumbnail builder, the Library index and the
    contact sheet all list a folder through frames, so AppleDouble files and
    NAS litter never show up as images anywhere."""
    from filmex.services import folder_scan, library, previews

    folder = tmp_path / "2026" / "roll"
    make_jpeg("DSC0001.jpg", long_edge=64, folder=folder)
    make_jpeg("DSC0002.jpg", long_edge=64, folder=folder)
    _touch(folder, "._DSC0001.jpg")  # AppleDouble resource fork on SMB / FAT
    _touch(folder, "smbdelete0001.jpg")
    _touch(folder, ".hidden.jpg")
    _touch(folder, "Thumbs.db")
    _touch(folder, "DSC0002.xmp")
    _touch(folder, "._DSC0002.xmp")

    expected = ["DSC0001.jpg", "DSC0002.jpg"]
    assert fr.list_frames(folder).images == expected
    assert [f.name for f in folder_scan.scan_folder(folder).files] == expected
    assert [p.name for p in previews.previewable_entries(folder)] == expected
    row = library.inspect_folder(folder, depth=2, parent="2026")
    assert row.image_count == 2 and row.untagged_files == expected
    assert row.sidecar_count == 1


def test_thumbnail_for_ignores_appledouble_twins(tmp_path: Path):
    folder = tmp_path / "lab"
    image = _touch(folder, "master_001.tif")
    _touch(folder, "._master_001.thm")
    assert fr.thumbnail_for(image) is None
    _touch(folder, "master_001.thm")
    assert fr.thumbnail_for(image) == folder / "master_001.thm"


def test_source_kind_and_litter_rules():
    assert fr.source_kind_for("a.JPEG") == "lab_scan_jpeg"
    assert fr.source_kind_for("a.tif") == "positive_master_tiff"
    assert fr.source_kind_for("a.ARW") == "camera_scan_raw"
    assert fr.source_kind_for("a.xmp") is None
    # Junk is skipped when listing; only OS litter may be deleted by a cleanup.
    assert fr.is_junk_file("smbdelete123.jpg") and not fr.is_litter("smbdelete123.jpg")
    assert fr.is_litter(".DS_Store") and fr.is_litter("._a.jpg")
    assert fr.is_junk_dir("@eaDir") and not fr.is_junk_dir("2026")


def test_tag_view_scan_endpoint_skips_appledouble_files(client, tmp_path: Path, make_jpeg):
    folder = tmp_path / "smb-roll"
    make_jpeg("DSC0001.jpg", long_edge=64, folder=folder)
    _touch(folder, "._DSC0001.jpg")
    res = client.post("/api/folder/scan", json={"folder": str(folder)})
    assert res.status_code == 200, res.text
    assert [f["name"] for f in res.json()["files"]] == ["DSC0001.jpg"]
