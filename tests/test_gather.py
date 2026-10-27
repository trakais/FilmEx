"""Gather: the RAW and JPG renditions of a roll become one folder, undoable."""

from __future__ import annotations

import json
from pathlib import Path

from sqlmodel import Session

from filmex.db import get_engine
from filmex.services import gather, operations
from filmex.services import manifest as mf


def _roll(tmp_path: Path, make_jpeg, client, fake_exiftool) -> Path:
    """A roll kept as RAW (tagged under the old roll id "RAW") and JPG (untagged)."""
    roll = tmp_path / "Filma" / "2026" / "2026-04-16-Fed2-Fuji"
    make_jpeg("DSC0001.jpg", long_edge=64, folder=roll / "RAW")
    (roll / "RAW" / "DSC0001.xmp").write_text("<x/>")
    make_jpeg("DSC0002.jpg", long_edge=64, folder=roll / "RAW")
    make_jpeg("000001.jpg", long_edge=64, folder=roll / "JPG")
    (roll / "JPG" / "order.txt").write_text("lab order 77")
    cam = client.get("/api/cameras").json()[0]
    res = client.post("/api/metadata/apply", json={"folder": str(roll / "RAW"), "camera_id": cam["id"], "roll_date": "2026-04-16", "place": "Riga"})
    assert res.status_code == 200, res.text
    # Pretend the RAW rendition was tagged by an older FilmEx that used the
    # subfolder as the roll id.
    manifest = mf.read_manifest(roll / "RAW")
    manifest["roll_id"] = "RAW"
    mf.write_manifest(roll / "RAW", manifest)
    fake_exiftool.writes.clear()
    return roll


def test_plan_lists_every_file_and_stops_on_a_collision(tmp_path: Path, make_jpeg, client, fake_exiftool):
    roll = _roll(tmp_path, make_jpeg, client, fake_exiftool)
    plan = gather.plan_gather(roll / "JPG")  # any rendition names the roll
    assert plan.roll == str(roll) and plan.ok
    assert [i.rendition for i in plan.items] == ["JPG", "RAW"]
    by_name = {i.rendition: i for i in plan.items}
    assert by_name["JPG"].files == ["000001.jpg", "order.txt"]
    assert by_name["RAW"].files == ["DSC0001.jpg", "DSC0001.xmp", "DSC0002.jpg"]
    assert by_name["RAW"].has_manifest and not by_name["JPG"].has_manifest
    assert plan.file_count == 5

    (roll / "JPG" / "DSC0002.jpg").write_bytes(b"dup")
    plan = gather.plan_gather(roll)
    assert not plan.ok and any("DSC0002.jpg" in p and "JPG" in p and "RAW" in p for p in plan.problems)
    (roll / "JPG" / "DSC0002.jpg").unlink()
    (roll / "000001.jpg").write_bytes(b"already here")
    plan = gather.plan_gather(roll)
    assert not plan.ok and any("000001.jpg" in p for p in plan.problems)


def test_plain_roll_has_nothing_to_gather(tmp_path: Path, make_jpeg):
    roll = tmp_path / "2026-04-16-Fed2-Fuji"
    make_jpeg("a.jpg", long_edge=64, folder=roll)
    plan = gather.plan_gather(roll)
    assert not plan.ok and plan.items == [] and "rendition" in plan.problems[0]


def test_apply_moves_merges_manifests_swaps_roll_keywords_and_undo_puts_it_back(tmp_path: Path, make_jpeg, client, fake_exiftool):
    roll = _roll(tmp_path, make_jpeg, client, fake_exiftool)
    with Session(get_engine()) as session:
        result = gather.apply_gather(session, roll)
    assert not result.errors, result.errors
    assert sorted(p.name for p in roll.iterdir() if p.is_file()) == [
        "000001.jpg", "DSC0001.jpg", "DSC0001.xmp", "DSC0002.jpg", "order.txt", "roll_manifest.json",
    ]
    assert not (roll / "RAW").exists() and not (roll / "JPG").exists()
    assert result.removed_folders == [str(roll / "JPG"), str(roll / "RAW")]

    merged = mf.read_manifest(roll)
    assert merged["roll_id"] == "2026-04-16-Fed2-Fuji" and merged["folder_relative_path"] == "2026-04-16-Fed2-Fuji"
    assert merged["place"] == "Riga" and merged["capture_date_value"] == "2026-04-16"
    assert [h["applied_files"] for h in merged["apply_history"] if not h["dry_run"]] == [["DSC0001.jpg", "DSC0002.jpg"]]
    assert [r["rendition"] for r in merged["gathered_renditions"]] == ["JPG", "RAW"]
    assert merged["gathered_renditions"][1]["roll_id"] == "RAW"

    # The files tagged under the old roll id carry the roll's keyword now.
    swapped = {path.name: intent for path, intent in fake_exiftool.writes}
    assert sorted(swapped) == ["DSC0001.jpg", "DSC0002.jpg"]
    assert "Roll|2026-04-16-Fed2-Fuji" in swapped["DSC0001.jpg"].hierarchical_keywords
    assert result.keyword_files == ["DSC0001.jpg", "DSC0002.jpg"]

    with Session(get_engine()) as session:
        row = operations.get(session, result.operation_id)
        assert row.kind == "gather" and row.status == operations.DONE
        stored = json.loads(row.result_json)
        assert gather.GatherResult.from_dict(stored).to_dict() == stored

    fake_exiftool.writes.clear()
    with Session(get_engine()) as session:
        undo = gather.undo_gather(session, result.operation_id)
        assert operations.get(session, result.operation_id).status == operations.UNDONE
    assert not undo.errors, undo.errors
    assert sorted(p.name for p in (roll / "RAW").iterdir()) == ["DSC0001.jpg", "DSC0001.xmp", "DSC0002.jpg", "roll_manifest.json"]
    assert sorted(p.name for p in (roll / "JPG").iterdir()) == ["000001.jpg", "order.txt"]
    assert not mf.manifest_path_for(roll).exists()
    assert mf.read_manifest(roll / "RAW")["roll_id"] == "RAW"
    back = {path.name: intent for path, intent in fake_exiftool.writes}
    assert "Roll|RAW" in back["DSC0001.jpg"].hierarchical_keywords
    assert len(undo.restored) == 5


def test_gather_over_http_plan_apply_undo(tmp_path: Path, make_jpeg, client, fake_exiftool):
    roll = _roll(tmp_path, make_jpeg, client, fake_exiftool)
    plan = client.post("/api/folder/gather", json={"folder": str(roll / "RAW")}).json()
    assert plan["ok"] and plan["roll"] == str(roll) and plan["file_count"] == 5

    res = client.post("/api/folder/gather/apply", json={"folder": str(roll / "RAW")})
    assert res.status_code == 200, res.text
    events = [json.loads(line) for line in res.text.splitlines() if line.strip()]
    assert events[0]["type"] == "start" and events[0]["total"] == 5
    done = events[-1]
    assert done["type"] == "done" and not done["errors"] and done["operation_id"]
    assert (roll / "000001.jpg").is_file() and not (roll / "JPG").exists()

    res = client.post(f"/api/operations/{done['operation_id']}/undo")
    assert res.status_code == 200
    undo = json.loads(res.text.splitlines()[-1])
    assert undo["type"] == "done" and not undo["errors"]
    assert (roll / "JPG" / "000001.jpg").is_file() and not (roll / "000001.jpg").exists()
