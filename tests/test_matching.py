"""Lab scans matched to raws by picture, linked as positives, and undone."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageOps

from filmex.services import frames as fr
from filmex.services import manifest as mf
from filmex.services import matching
from tests.test_previews import write_scanner_dng

SIZE = (300, 200)


def scene(seed: int) -> Image.Image:
    """A frame of the roll: a few shapes in colour, different for every seed."""
    rnd = random.Random(seed)
    image = Image.new("RGB", SIZE, tuple(rnd.randrange(256) for _ in range(3)))
    draw = ImageDraw.Draw(image)
    for _ in range(7):
        x, y = rnd.randrange(SIZE[0]), rnd.randrange(SIZE[1])
        box = (x, y, x + rnd.randrange(40, 180), y + rnd.randrange(30, 120))
        fill = tuple(rnd.randrange(256) for _ in range(3))
        (draw.rectangle if rnd.random() < 0.5 else draw.ellipse)(box, fill=fill)
    return image


def home_scan(image: Image.Image, *, mirror: bool = False, turn: int = 0) -> Image.Image:
    """What a camera scan of that frame looks like: a negative under the
    orange mask, with the film's clear border around it."""
    neg = ImageOps.invert(image)
    if mirror:
        neg = ImageOps.mirror(neg)
    if turn:
        neg = neg.rotate(turn, expand=True)
    r, g, b = neg.split()
    neg = Image.merge("RGB", (r.point(lambda v: 70 + v * 0.7), g.point(lambda v: 40 + v * 0.55), b.point(lambda v: 10 + v * 0.4)))
    w, h = neg.size
    framed = Image.new("RGB", (int(w * 1.12), int(h * 1.12)), (235, 150, 90))
    framed.paste(neg, ((framed.width - w) // 2, (framed.height - h) // 2))
    return framed


def grids(images: list[tuple[str, Image.Image]], *, crop: float) -> list[tuple[str, matching.Grid]]:
    return [(key, matching.grid_of(image, crop=crop)) for key, image in images]


def labs(seeds: list[int]) -> list[tuple[str, matching.Grid]]:
    return grids([(f"L{s:02d}", scene(s)) for s in seeds], crop=matching.LAB_CROP)


def raws(images: list[Image.Image]) -> list[tuple[str, matching.Grid]]:
    return grids([(f"R{i:02d}", image) for i, image in enumerate(images)], crop=matching.RAW_CROP)


def pairs_of(result: matching.Matching) -> dict[str, str]:
    return {p.raw: p.lab for p in result.pairs}


def test_a_reversed_mirrored_subset_pairs_by_picture():
    # The home scan skipped frames and ran from the end of the roll, through the base.
    shot = [10, 8, 7, 5, 2, 1]
    result = matching.match_grids(raws([home_scan(scene(s), mirror=True) for s in shot]), labs(list(range(12))))
    assert (result.direction, result.orientation, result.negative) == ("reversed", "m0", True)
    assert pairs_of(result) == {f"R{i:02d}": f"L{s:02d}" for i, s in enumerate(shot)}
    assert "unsure" not in {p.level for p in result.pairs}
    assert result.unmatched_labs == ["L00", "L03", "L04", "L06", "L09", "L11"]
    assert result.leftovers == []


def test_order_breaks_ties_between_two_shots_of_one_scene():
    # Frames 2 and 3 of the roll are the same scene: only the order tells them apart.
    lab_images = [(f"L{i:02d}", scene(s)) for i, s in enumerate([0, 1, 2, 2, 4, 5])]
    raw_images = [home_scan(scene(s)) for s in [0, 1, 2, 2, 4, 5]]
    result = matching.match_grids(raws(raw_images), grids(lab_images, crop=matching.LAB_CROP))
    assert pairs_of(result) == {f"R{i:02d}": f"L{i:02d}" for i in range(6)}


def test_out_of_order_capture_retake_and_blank_frame():
    # Frames 3 and 4 captured the wrong way round, frame 5 captured twice,
    # and one capture of nothing (lens cap on).
    shot = [home_scan(scene(s)) for s in [1, 2, 4, 3, 5, 5]] + [Image.new("RGB", (336, 224), (20, 20, 20)), home_scan(scene(7))]
    result = matching.match_grids(raws(shot), labs(list(range(8))))
    assert (result.direction, result.orientation) == ("forward", "0")
    pairs = pairs_of(result)
    assert pairs["R02"] == "L04" and pairs["R03"] == "L03"
    assert any(p.note == "captured out of order" for p in result.pairs)
    assert {p.raw: p.lab for p in result.pairs if p.raw in ("R04", "R05")} in ({"R04": "L05"}, {"R05": "L05"})
    reasons = {left.raw: left.reason for left in result.leftovers}
    assert reasons["R06"] == matching.NO_PICTURE
    retake = next(left for left in result.leftovers if left.reason == matching.ANOTHER_CAPTURE)
    assert retake.lab == "L05" and retake.holder in ("R04", "R05")
    assert pairs["R07"] == "L07"


def test_a_roll_turned_upside_down_is_found():
    shot = [1, 3, 4, 6]
    result = matching.match_grids(raws([home_scan(scene(s), turn=180) for s in shot]), labs(list(range(8))))
    assert result.orientation == "180"
    assert pairs_of(result) == {f"R{i:02d}": f"L{s:02d}" for i, s in enumerate(shot)}


def test_positive_name_joins_the_raw_frame():
    assert fr.positive_name("DSC0001", "R1-09034-024A.JPG") == "DSC0001-R1-09034-024A.JPG"
    assert fr.positive_name("DSC0001", "_0001.tif") == "DSC0001-0001.tif"


# ---------- folder, routes, journal ----------


@pytest.fixture
def scanned_twice(tmp_path: Path):
    """A roll folder with the lab's JPEGs (one with its .thm) and a home
    scan of four of its frames as raws, captured from the end of the roll."""
    folder = tmp_path / "Filma" / "2026" / "2026-04-16-Fed2-Fuji"
    folder.mkdir(parents=True)
    for n in range(1, 7):
        image = scene(n)
        image.save(folder / f"R1-{n:04d}.JPG", "JPEG", quality=92)
        if n == 2:
            thumb = image.copy()
            thumb.thumbnail((160, 120))
            thumb.save(folder / "R1-0002.thm", "JPEG")
    for i, n in enumerate([6, 5, 2, 1]):
        full = home_scan(scene(n), mirror=True)
        small = full.copy()
        small.thumbnail((48, 48))
        write_scanner_dng(folder / f"DSC{100 + i:04d}.dng", full, small)
    return folder


def _stream(client, url, payload=None):
    with client.stream("POST", url, json=payload) as res:
        assert res.status_code == 200, res.read()
        return [json.loads(line) for line in res.iter_lines() if line.strip()]


def _tagged_manifest(folder: Path) -> None:
    manifest = mf.build_new_manifest(
        folder=folder, roll_id=folder.name, source_kind="lab_scan_jpeg", scan_tier="standard",
        camera=mf.GearSnapshot(), lens=mf.GearSnapshot(), film=mf.FilmSnapshot(),
        development_provider=mf.NamedSnapshot(), development_method=mf.NamedSnapshot(),
        scan_provider=mf.NamedSnapshot(), scan_method=mf.NamedSnapshot(),
    )
    mf.append_apply_history(manifest, applied_files=["R1-0006.JPG", "R1-0003.JPG"], diffs=[{"file": "R1-0006.JPG", "changes": []}])
    manifest["file_overrides"] = {"R1-0006.JPG": {"extra_keywords": ["dog"]}}
    mf.write_manifest(folder, manifest)


def test_match_link_and_undo_through_the_api(client, scanned_twice):
    folder = scanned_twice
    _tagged_manifest(folder)

    plan = client.post("/api/folder/matches", json={"folder": str(folder)}).json()
    assert plan["problems"] == []
    assert (plan["raw_count"], plan["lab_count"], plan["direction"], plan["orientation"]) == (4, 6, "reversed", "m0")
    pairs = {p["raw"]: p["lab"] for p in plan["pairs"]}
    assert pairs == {"DSC0100": "R1-0006", "DSC0101": "R1-0005", "DSC0102": "R1-0002", "DSC0103": "R1-0001"}
    thm_pair = next(p for p in plan["pairs"] if p["lab"] == "R1-0002")
    assert thm_pair["files"] == [
        {"from": "R1-0002.JPG", "to": "DSC0102-R1-0002.JPG"},
        {"from": "R1-0002.thm", "to": "DSC0102-R1-0002.thm"},
    ]
    assert plan["unmatched_labs"] == ["R1-0003", "R1-0004"]

    links = [{"raw": raw, "lab": lab} for raw, lab in pairs.items()]
    done = _stream(client, "/api/folder/link/apply", {"folder": str(folder), "links": links})[-1]
    assert done["type"] == "done", done
    assert done["errors"] == [] and done["manifest_updated"] is True
    assert (folder / "DSC0102-R1-0002.thm").is_file() and not (folder / "R1-0002.JPG").exists()

    # Each lab scan is now a positive of its raw frame; unpaired ones stay frames of their own.
    listing = client.get("/api/folder/frames", params={"path": str(folder)}).json()
    by_key = {f["key"]: f for f in listing["frames"]}
    assert sorted(by_key) == ["DSC0100", "DSC0101", "DSC0102", "DSC0103", "R1-0003", "R1-0004"]
    assert by_key["DSC0100"]["positives"] == ["DSC0100-R1-0006.JPG"]
    assert by_key["DSC0102"]["companions"] == ["DSC0102-R1-0002.thm"]

    manifest = mf.read_manifest(folder)
    assert manifest["apply_history"][-1]["applied_files"] == ["DSC0100-R1-0006.JPG", "R1-0003.JPG"]
    assert manifest["apply_history"][-1]["diffs"][0]["file"] == "DSC0100-R1-0006.JPG"
    assert manifest["file_overrides"] == {"DSC0100-R1-0006.JPG": {"extra_keywords": ["dog"]}}
    assert {e["raw"] for e in manifest["linked_scans"]} == set(pairs)

    # Linked raws take no part in the next match.
    again = client.post("/api/folder/matches", json={"folder": str(folder)}).json()
    assert again["raw_count"] == 0 and again["problems"]

    op = client.get("/api/operations?kind=link").json()[0]
    assert op["id"] == done["operation_id"] and op["status"] == "done"
    undone = _stream(client, f"/api/operations/{done['operation_id']}/undo")[-1]
    assert undone["type"] == "done" and undone["errors"] == []
    assert len(undone["restored"]) == 5
    assert (folder / "R1-0002.JPG").is_file() and (folder / "R1-0002.thm").is_file()
    manifest = mf.read_manifest(folder)
    assert manifest["apply_history"][-1]["applied_files"] == ["R1-0006.JPG", "R1-0003.JPG"]
    assert manifest["file_overrides"] == {"R1-0006.JPG": {"extra_keywords": ["dog"]}}
    assert "linked_scans" not in manifest
    assert client.get("/api/operations?kind=link").json()[0]["status"] == "undone"


def test_link_refuses_collisions_and_strangers_before_renaming(client, scanned_twice):
    folder = scanned_twice
    (folder / "DSC0100-R1-0006.JPG").write_bytes(b"in the way")
    res = client.post("/api/folder/link/apply", json={"folder": str(folder), "links": [
        {"raw": "DSC0100", "lab": "R1-0006"},
        {"raw": "DSC0101", "lab": "DSC0102"},
        {"raw": "nope", "lab": "R1-0001"},
        {"raw": "DSC0103", "lab": "R1-0001"},
    ]})
    assert res.status_code == 400
    errors = res.json()["detail"]["errors"]
    assert any("already exists" in e for e in errors)
    assert any("no lab scan" in e for e in errors)
    assert any("no raw frame" in e for e in errors)
    assert (folder / "R1-0001.JPG").is_file() and (folder / "R1-0006.JPG").is_file()
    assert client.get("/api/operations?kind=link").json() == []
    assert client.post("/api/folder/link/apply", json={"folder": str(folder), "links": []}).status_code == 400


def test_nothing_to_match_says_so(client, tmp_path, make_jpeg):
    make_jpeg("R1-0001.JPG", long_edge=64, folder=tmp_path / "labonly")
    plan = client.post("/api/folder/matches", json={"folder": str(tmp_path / "labonly")}).json()
    assert plan["problems"] and "pairs" not in plan
    assert client.post("/api/folder/matches", json={"folder": str(tmp_path / "missing")}).status_code == 404
