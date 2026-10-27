"""Renditions: RAW and JPG subfolders of one roll share its identity."""

from __future__ import annotations

from pathlib import Path

from filmex.services import library as lib
from filmex.services import manifest as mf
from filmex.services import renditions as rd
from filmex.services.folder_scan import scan_folder


def test_rendition_folders_take_their_roll_from_the_parent(tmp_path: Path):
    roll = tmp_path / "2026" / "2026-04-16-Fed2-Fuji"
    raw = roll / "RAW"
    raw.mkdir(parents=True)
    assert rd.is_rendition_dir("RAW") and rd.is_rendition_dir("jpg") and rd.is_rendition_dir("Positives")
    assert not rd.is_rendition_dir("2026-04-16-Fed2-Fuji") and not rd.is_rendition_dir("00424644-Portra")
    assert rd.rendition_name(raw) == "RAW" and rd.rendition_name(roll) is None
    assert rd.roll_folder(raw) == roll and rd.roll_folder(roll) == roll
    assert rd.roll_id_for(raw) == "2026-04-16-Fed2-Fuji"


def test_sibling_renditions_are_the_other_image_bearing_rendition_folders(tmp_path: Path, make_jpeg):
    roll = tmp_path / "2026-04-16-Fed2-Fuji"
    make_jpeg("DSC0001.jpg", long_edge=64, folder=roll / "RAW")
    make_jpeg("000001.jpg", long_edge=64, folder=roll / "JPG")
    (roll / "TIFF").mkdir()  # a rendition name, but nothing in it
    make_jpeg("x.jpg", long_edge=64, folder=roll / "contact-sheets")  # not a rendition name
    assert [p.name for p in rd.rendition_dirs(roll)] == ["JPG", "RAW"]
    assert [p.name for p in rd.sibling_renditions(roll / "RAW")] == ["JPG"]
    assert rd.sibling_renditions(roll) == []


def test_tagging_a_rendition_writes_the_parent_roll_keyword(client, tmp_path: Path, make_jpeg, fake_exiftool):
    raw = tmp_path / "2026-04-16-Fed2-Fuji" / "RAW"
    make_jpeg("DSC0001.jpg", long_edge=64, folder=raw)
    assert scan_folder(raw).roll_id == "2026-04-16-Fed2-Fuji"
    cam = client.get("/api/cameras").json()[0]
    res = client.post("/api/metadata/apply", json={"folder": str(raw), "camera_id": cam["id"]})
    assert res.status_code == 200, res.text
    assert res.json()["roll_id"] == "2026-04-16-Fed2-Fuji"
    assert mf.read_manifest(raw)["roll_id"] == "2026-04-16-Fed2-Fuji"
    written = [intent for path, intent in fake_exiftool.writes if path.name == "DSC0001.jpg"]
    assert written and "Roll|2026-04-16-Fed2-Fuji" in written[0].hierarchical_keywords
    assert not any(k.startswith("Roll|RAW") for k in written[0].hierarchical_keywords)


def test_a_roll_kept_as_renditions_is_one_library_row(tmp_path: Path, make_jpeg, client, fake_exiftool):
    root = tmp_path / "Filma"
    roll = root / "2026" / "2026-04-16-Fed2-Fuji"
    make_jpeg("000001.jpg", long_edge=64, folder=roll / "JPG")
    make_jpeg("000002.jpg", long_edge=64, folder=roll / "JPG")
    make_jpeg("DSC0001.jpg", long_edge=64, folder=roll / "RAW")
    client.patch("/api/settings", json={"library_root": str(root)})
    # Tag the RAW rendition only: the roll is partial, the gear shows on the roll.
    cam = client.get("/api/cameras").json()[0]
    res = client.post("/api/metadata/apply", json={"folder": str(roll / "RAW"), "camera_id": cam["id"]})
    assert res.status_code == 200, res.text

    rows = {r.display_name: r for r in lib.scan_library(root)}
    row = rows["2026-04-16-Fed2-Fuji"]
    assert row.status == lib.PARTIAL and row.image_count == 3 and row.untagged_count == 2
    assert row.roll_id == "2026-04-16-Fed2-Fuji" and row.camera_display
    by_name = {r["rendition"]: r for r in row.renditions}
    assert by_name["RAW"]["status"] == lib.TAGGED and by_name["JPG"]["status"] == lib.UNTAGGED
    assert by_name["JPG"]["untagged_count"] == 2 and "untagged_files" not in by_name["JPG"]

    # The folder endpoint answers the same for the roll and for one rendition.
    data = client.get("/api/library/folder", params={"path": str(roll)}).json()
    assert data["status"] == lib.PARTIAL and len(data["renditions"]) == 2
    part = client.get("/api/library/folder", params={"path": str(roll / "JPG")}).json()
    assert part["rendition"] == "JPG" and part["untagged_files"] == ["000001.jpg", "000002.jpg"]
    assert part["display_name"] == "2026-04-16-Fed2-Fuji/JPG"

    # The frames endpoint names the roll and the sibling rendition.
    frames = client.get("/api/folder/frames", params={"path": str(roll / "JPG")}).json()
    assert frames["roll_id"] == "2026-04-16-Fed2-Fuji" and frames["rendition"] == "JPG"
    assert frames["sibling_renditions"] == [str(roll / "RAW")]

    # Normalize names leaves such a roll alone.
    proposals = client.post("/api/naming/propose", json={}).json()["proposals"]
    skipped = [p for p in proposals if p["current"] == "2026-04-16-Fed2-Fuji"]
    assert skipped and "renditions" in skipped[0]["reason"]
