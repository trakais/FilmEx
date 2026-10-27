"""Library index: walk, status derivation, cache, routes, and subset apply."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from filmex.services import library as lib
from filmex.services import manifest as mf


def _write_manifest(
    folder: Path,
    *,
    applied: list[str],
    dry_runs: list[list[str]] = (),
    film="Kodak Gold 200",
    camera="FED-2",
):
    manifest = mf.build_new_manifest(
        folder=folder,
        roll_id=folder.name,
        source_kind="lab_scan_jpeg",
        scan_tier="standard",
        camera=mf.GearSnapshot(display_name=camera),
        lens=mf.GearSnapshot(),
        film=mf.FilmSnapshot(display_name=film),
        development_provider=mf.NamedSnapshot(),
        development_method=mf.NamedSnapshot(),
        scan_provider=mf.NamedSnapshot(),
        scan_method=mf.NamedSnapshot(),
    )
    for names in dry_runs:
        mf.append_apply_history(manifest, applied_files=names, diffs=[], dry_run=True)
    if applied is not None:
        mf.append_apply_history(manifest, applied_files=applied, diffs=[], dry_run=False)
    mf.write_manifest(folder, manifest)


NFD_NAME = unicodedata.normalize("NFD", "2026-06-C41-Fujica-Jāņos")


@pytest.fixture
def library_tree(tmp_path: Path, make_jpeg):
    root = tmp_path / "Filma"
    (root / "2024").mkdir(parents=True)
    tagged = root / "2024" / "2024-08_R1_F3_Gold"
    for i in range(2):
        make_jpeg(f"scan_{i:03d}.jpg", long_edge=64, folder=tagged)
    (tagged / "scan_000.xmp").write_text("<x/>")
    _write_manifest(tagged, applied=["scan_000.jpg", "scan_001.jpg"])

    partial = root / "2024" / "2024-09_R2_Fed2_HP5"
    for i in range(3):
        make_jpeg(f"scan_{i:03d}.jpg", long_edge=64, folder=partial)
    # A dry run listing everything must not count; one real apply of one file.
    _write_manifest(
        partial,
        applied=["scan_000.jpg"],
        dry_runs=[["scan_000.jpg", "scan_001.jpg", "scan_002.jpg"]],
        film="Ilford HP5 Plus 400",
    )

    (root / "2026").mkdir()
    untagged = root / "2026" / "EUR-04"
    make_jpeg("DSC09608.jpg", long_edge=64, folder=untagged)
    make_jpeg("DSC09609.jpg", long_edge=64, folder=untagged)
    (root / "2026" / "EUR-05-empty").mkdir()
    nfd = root / "2026" / NFD_NAME
    make_jpeg("DSC00001.jpg", long_edge=64, folder=nfd)
    (nfd / "._DSC00001.jpg").write_bytes(b"apple double")
    (nfd / "smbdeleteAAA").write_bytes(b"leftover")

    flat = root / "2010"
    for i in range(3):
        make_jpeg(f"old_{i}.jpg", long_edge=64, folder=flat)
    make_jpeg("deep.jpg", long_edge=64, folder=root / "2010" / "nested" / "too-deep")

    make_jpeg("trash.jpg", long_edge=64, folder=root / ".dtrash" / "files")
    (root / "2026" / "@eaDir").mkdir()
    make_jpeg("thumb.jpg", long_edge=64, folder=root / "2026" / "@eaDir")
    (root / ".DS_Store").write_bytes(b"")
    return root


def test_scan_library_derives_status_and_skips_junk(library_tree):
    rows = {row.display_name: row for row in lib.scan_library(library_tree)}

    assert rows["2024-08_R1_F3_Gold"].status == lib.TAGGED
    assert rows["2024-08_R1_F3_Gold"].sidecar_count == 1
    assert rows["2024-08_R1_F3_Gold"].film_display == "Kodak Gold 200"
    assert rows["2024-08_R1_F3_Gold"].camera_display == "FED-2"
    assert rows["2024-08_R1_F3_Gold"].last_applied_at
    assert rows["2024-08_R1_F3_Gold"].parent == "2024"

    partial = rows["2024-09_R2_Fed2_HP5"]
    assert partial.status == lib.PARTIAL
    assert partial.untagged_files == ["scan_001.jpg", "scan_002.jpg"]
    assert partial.untagged_count == 2

    assert rows["EUR-04"].status == lib.UNTAGGED
    assert rows["EUR-04"].has_manifest is False
    assert rows["EUR-04"].kinds == {"lab_scan_jpeg": 2}
    assert rows["EUR-05-empty"].status == lib.EMPTY

    flat = rows["2010"]
    assert flat.is_flat_year is True
    assert flat.depth == 1
    assert flat.image_count == 3
    # A folder whose images live one level down is a container, not a roll:
    # the image-bearing subfolder is the row, named through its parent.
    assert "nested" not in rows
    deep = rows["nested/too-deep"]
    assert deep.depth == 3 and deep.parent == "2010" and deep.image_count == 1 and deep.status == lib.UNTAGGED

    assert ".dtrash" not in rows and "files" not in rows
    assert "@eaDir" not in rows


def test_scan_library_keeps_disk_names_and_shows_nfc(library_tree):
    rows = lib.scan_library(library_tree)
    nfd_rows = [r for r in rows if r.name == NFD_NAME]
    assert len(nfd_rows) == 1
    row = nfd_rows[0]
    assert row.display_name == unicodedata.normalize("NFC", NFD_NAME)
    assert unicodedata.is_normalized("NFC", row.display_name)
    assert Path(row.path).is_dir()
    assert row.image_count == 1  # AppleDouble and smbdelete files ignored
    assert row.status == lib.UNTAGGED


def test_scan_library_missing_root(tmp_path):
    with pytest.raises(FileNotFoundError):
        lib.scan_library(tmp_path / "nope")


def test_applied_files_ignores_dry_runs():
    manifest = {
        "apply_history": [
            {"dry_run": True, "applied_files": ["a.jpg", "b.jpg"]},
            {"dry_run": False, "applied_files": ["a.jpg"]},
            {"dry_run": False, "applied_files": ["c.jpg"]},
        ]
    }
    assert lib.applied_files_from_manifest(manifest) == {"a.jpg", "c.jpg"}
    assert lib.applied_files_from_manifest(None) == set()


def test_library_routes_rescan_cache_and_details(client, library_tree):
    assert client.get("/api/library").json()["root"] is None
    assert client.post("/api/library/rescan").status_code == 400

    client.patch("/api/settings", json={"library_root": str(library_tree)})
    before = client.get("/api/library").json()
    assert before["reachable"] is True
    assert before["stale"] is True
    assert before["folders"] == []

    with client.stream("POST", "/api/library/rescan") as res:
        events = [json.loads(line) for line in res.iter_lines() if line.strip()]
    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "done"
    assert events[-1]["summary"] == {"tagged": 1, "partial": 1, "untagged": 4, "empty": 1}

    after = client.get("/api/library").json()
    assert after["stale"] is False
    assert after["scanned_at"]
    names = [f["display_name"] for f in after["folders"]]
    assert names[0] == "2010"  # flat year sorts under its own name
    assert "kinds" in after["folders"][0]
    assert "untagged_files" not in after["folders"][0]
    assert after["summary"]["tagged"] == 1

    partial = next(f for f in after["folders"] if f["display_name"] == "2024-09_R2_Fed2_HP5")
    details = client.get("/api/library/folder", params={"path": partial["path"]}).json()
    assert details["untagged_files"] == ["scan_001.jpg", "scan_002.jpg"]
    assert details["parent"] == "2024"

    assert (
        client.get(
            "/api/library/folder", params={"path": str(library_tree / "missing")}
        ).status_code
        == 404
    )
    outside = library_tree.parent / "elsewhere"
    outside.mkdir()
    assert client.get("/api/library/folder", params={"path": str(outside)}).status_code == 400

    # Rescan replaces the cache rather than appending.
    with client.stream("POST", "/api/library/rescan") as res:
        list(res.iter_lines())
    assert len(client.get("/api/library").json()["folders"]) == len(after["folders"])


def test_library_rescan_unreachable_root(client, tmp_path):
    client.patch("/api/settings", json={"library_root": str(tmp_path / "unmounted")})
    data = client.get("/api/library").json()
    assert data["reachable"] is False
    assert client.post("/api/library/rescan").status_code == 404


def test_folder_reveal_endpoint(client, tmp_path, monkeypatch):
    from filmex.routes import workflow as wr

    opened = {}
    monkeypatch.setattr(wr, "_open_with_default_app", lambda path: opened.setdefault("path", path))
    folder = tmp_path / "roll"
    folder.mkdir()
    assert client.post("/api/folder/reveal", json={"folder": str(folder)}).status_code == 200
    assert opened["path"] == folder
    assert (
        client.post("/api/folder/reveal", json={"folder": str(tmp_path / "nope")}).status_code
        == 404
    )


# ---------- subset apply ----------


def test_dry_run_subset_limits_diffs(client, roll_factory):
    cam = client.get("/api/cameras").json()[0]
    folder = roll_factory("2026-04-17_R201", jpegs=3)
    res = client.post(
        "/api/metadata/dry-run",
        json={
            "folder": str(folder),
            "camera_id": cam["id"],
            "files": ["scan_002.jpg", "scan_000.jpg", "scan_002.jpg"],
        },
    )
    assert res.status_code == 200, res.text
    assert [d["name"] for d in res.json()["diffs"]] == ["scan_000.jpg", "scan_002.jpg"]

    res = client.post(
        "/api/metadata/dry-run",
        json={"folder": str(folder), "camera_id": cam["id"], "files": ["nope.jpg"]},
    )
    assert res.status_code == 400
    assert "nope.jpg" in res.json()["detail"]
    res = client.post(
        "/api/metadata/dry-run", json={"folder": str(folder), "camera_id": cam["id"], "files": []}
    )
    assert res.status_code == 400


def test_apply_subset_touches_only_selected_files(client, roll_factory, fake_exiftool):
    folder = roll_factory("2026-04-17_R202", jpegs=3)
    cam = client.get("/api/cameras").json()[0]
    res = client.post(
        "/api/metadata/apply",
        json={"folder": str(folder), "camera_id": cam["id"], "files": ["scan_001.jpg"]},
    )
    assert res.status_code == 200, res.text
    assert fake_exiftool.written_names == ["scan_001.jpg"]
    assert res.json()["applied_files"] == ["scan_001.jpg"]
    row = lib.inspect_folder(folder, depth=2, parent=None)
    assert row.status == lib.PARTIAL
    assert row.untagged_files == ["scan_000.jpg", "scan_002.jpg"]


def test_rolls_kept_in_subfolders_are_listed_as_rolls(tmp_path: Path, make_jpeg):
    """A roll split into JPG and RAW folders, and a lab order holding one
    subfolder per roll: the subfolders are the rolls, the parents are not."""
    root = tmp_path / "Filma"
    year = root / "2026"
    make_jpeg("R1-004A.JPG", long_edge=64, folder=year / "2026-04-16-Fed2-Fuji" / "JPG")
    make_jpeg("DSC09040.jpg", long_edge=64, folder=year / "2026-04-16-Fed2-Fuji" / "RAW")
    (year / "2026-04-16-Fed2-Fuji" / "notes.txt").write_text("lab order")
    make_jpeg("004246440001.jpg", long_edge=64, folder=year / "2026-04-21-lab" / "00424644-Portra")
    make_jpeg("004246450001.jpg", long_edge=64, folder=year / "2026-04-21-lab" / "00424645-Praktica")
    (year / "2026-04-21-lab" / "empty-sub").mkdir()
    # A roll with its own images keeps its row even if a subfolder holds more.
    make_jpeg("a.jpg", long_edge=64, folder=year / "2026-05-01-mixed")
    make_jpeg("b.jpg", long_edge=64, folder=year / "2026-05-01-mixed" / "extra")

    rows = {row.display_name: row for row in lib.scan_library(root)}
    # JPG and RAW are renditions of one roll: one row, the renditions inside it.
    fuji = rows["2026-04-16-Fed2-Fuji"]
    assert fuji.depth == 2 and fuji.image_count == 2 and fuji.status == lib.UNTAGGED
    assert [r["rendition"] for r in fuji.renditions] == ["JPG", "RAW"]
    assert fuji.renditions[0]["display_name"] == "2026-04-16-Fed2-Fuji/JPG" and fuji.renditions[0]["depth"] == 3
    assert "2026-04-16-Fed2-Fuji/JPG" not in rows
    # A lab order holding one subfolder per roll is a container, not a roll.
    assert "2026-04-21-lab" not in rows
    assert rows["2026-04-21-lab/00424645-Praktica"].status == lib.UNTAGGED
    assert rows["2026-04-21-lab/00424645-Praktica"].rendition is None
    assert "2026-04-21-lab/empty-sub" not in rows
    assert rows["2026-05-01-mixed"].image_count == 1 and rows["2026-05-01-mixed/extra"].image_count == 1
    assert lib.summarize(list(rows.values()))["untagged"] == 5


def test_library_folder_details_for_a_nested_roll(client, tmp_path: Path, make_jpeg):
    root = tmp_path / "Filma"
    roll = root / "2026" / "2026-04-21-lab" / "00424645-Praktica"
    make_jpeg("004246450001.jpg", long_edge=64, folder=roll)
    client.patch("/api/settings", json={"library_root": str(root)})
    data = client.get("/api/library/folder", params={"path": str(roll)}).json()
    assert data["depth"] == 3 and data["parent"] == "2026"
    assert data["display_name"] == "2026-04-21-lab/00424645-Praktica"
    assert data["untagged_files"] == ["004246450001.jpg"]
    nested_rename = client.post("/api/naming/propose", json={}).json()["proposals"]
    assert nested_rename[0]["status"] == "skipped" and "inside another folder" in nested_rename[0]["reason"]
