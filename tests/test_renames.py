"""Managed renames: proposals from the template, apply, keyword swap, undo."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from filmex.services import manifest as mf
from tests.helpers import ensure_film


def _manifest(folder: Path, *, film: str, camera: str, date: str | None, applied: list[str], place: str | None = None, camera_id=None, film_id=None):
    manifest = mf.build_new_manifest(
        folder=folder,
        roll_id=folder.name,
        source_kind="lab_scan_jpeg",
        scan_tier="standard",
        camera=mf.GearSnapshot(id=camera_id, display_name=camera),
        lens=mf.GearSnapshot(),
        film=mf.FilmSnapshot(id=film_id, display_name=film),
        development_provider=mf.NamedSnapshot(),
        development_method=mf.NamedSnapshot(),
        scan_provider=mf.NamedSnapshot(),
        scan_method=mf.NamedSnapshot(),
        capture_date_value=date,
        capture_date_precision="day" if date else "unknown",
        place=place,
    )
    mf.append_apply_history(manifest, applied_files=applied, diffs=[], dry_run=False)
    mf.write_manifest(folder, manifest)


@pytest.fixture
def rename_tree(tmp_path: Path, make_jpeg, client):
    cam = client.get("/api/cameras").json()[0]  # FED-2 from conftest
    client.patch(f"/api/cameras/{cam['id']}", json={"short_name": "Fed2"})
    film = ensure_film(client, brand="Kodak", name="Gold 200", display_name="Kodak Gold 200", iso=200, short_name="Gold")

    root = tmp_path / "Filma"
    y = root / "2026"
    old_style = y / "EUR-02-Gold"
    for i in range(2):
        make_jpeg(f"DSC0{i}.jpg", long_edge=64, folder=old_style)
    _manifest(old_style, film="Kodak Gold 200", camera="FED-2", date="2026-07-02", applied=["DSC00.jpg", "DSC01.jpg"], camera_id=cam["id"], film_id=film["id"])

    conforming = y / "2026-05-14-Fed2-Gold"
    make_jpeg("a.jpg", long_edge=64, folder=conforming)
    _manifest(conforming, film="Kodak Gold 200", camera="FED-2", date="2026-05-14", applied=["a.jpg"], camera_id=cam["id"], film_id=film["id"])

    undated = y / "mystery"
    make_jpeg("b.jpg", long_edge=64, folder=undated)
    _manifest(undated, film="Kodak Gold 200", camera="FED-2", date=None, applied=["b.jpg"], camera_id=cam["id"], film_id=film["id"])

    placed = y / "jani-2026"
    make_jpeg("c.jpg", long_edge=64, folder=placed)
    _manifest(placed, film="Kodak Gold 200", camera="FED-2", date="2026-06-23", applied=["c.jpg"], place="Jāņos", camera_id=cam["id"], film_id=film["id"])

    make_jpeg("u.jpg", long_edge=64, folder=y / "untagged-roll")
    (y / "2026-07-02-Fed2-Gold").mkdir()  # occupies the name old_style would get
    make_jpeg("x.jpg", long_edge=64, folder=y / "2026-07-02-Fed2-Gold")
    for i in range(3):
        make_jpeg(f"loose{i}.jpg", long_edge=64, folder=root / "2011")
    client.patch("/api/settings", json={"library_root": str(root)})
    return root


def test_propose_classifies_every_folder(client, rename_tree):
    res = client.post("/api/naming/propose", json={})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["template"] == "{date}-{camera}-{film}"
    by_name = {p["current"]: p for p in data["proposals"]}

    assert by_name["EUR-02-Gold"]["status"] == "conflict"  # target exists
    assert by_name["EUR-02-Gold"]["proposed"] == "2026-07-02-Fed2-Gold"
    assert by_name["2026-05-14-Fed2-Gold"]["status"] == "conforms"
    assert by_name["mystery"]["status"] == "skipped"
    assert "{date}" in by_name["mystery"]["reason"]
    assert by_name["jani-2026"]["status"] == "rename"
    assert by_name["jani-2026"]["proposed"] == "2026-06-23-Fed2-Gold-Jāņos"
    assert by_name["untagged-roll"]["status"] == "skipped"
    assert by_name["2011"]["status"] == "skipped"
    assert "loose" in by_name["2011"]["reason"]
    assert by_name["2026-07-02-Fed2-Gold"]["status"] == "skipped"  # no manifest
    assert data["counts"]["rename"] == 1


def test_propose_with_custom_template_and_roll_numbers(client, rename_tree):
    res = client.post("/api/naming/propose", json={"template": "{year}-{month}_R{roll}_{camera}_{film}"})
    by_name = {p["current"]: p for p in res.json()["proposals"]}
    # Date order within 2026: 05-14 (R01), 06-23 (R02), 07-02 (R03).
    assert by_name["2026-05-14-Fed2-Gold"]["proposed"] == "2026-05_R01_Fed2_Gold"
    assert by_name["jani-2026"]["proposed"] == "2026-06_R02_Fed2_Gold-Jāņos"
    assert by_name["EUR-02-Gold"]["proposed"] == "2026-07_R03_Fed2_Gold"
    assert client.post("/api/naming/propose", json={"template": "{nope}"}).status_code == 400


def _fake_exiftool(monkeypatch, written):
    from filmex.services import renames as r

    def fake_read(path):
        return {"HierarchicalSubject": [f"Roll|{Path(path).parent.name}", "Film|Kodak Gold 200", "People|Anna"],
                "Subject": [Path(path).parent.name, "Kodak Gold 200", "Anna"]}

    def fake_apply(path, intent):
        written.append((Path(path).name, list(intent.hierarchical_keywords), list(intent.flat_keywords)))
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(r, "read_managed_tags", fake_read)
    monkeypatch.setattr(r, "apply_intent", fake_apply)


def test_apply_rename_updates_manifest_and_roll_keyword_then_undo(client, rename_tree, monkeypatch):
    written: list = []
    _fake_exiftool(monkeypatch, written)
    source = rename_tree / "2026" / "jani-2026"
    target = rename_tree / "2026" / "2026-06-23-Fed2-Gold-Jāņos"

    with client.stream("POST", "/api/naming/apply", json={"items": [{"path": str(source), "proposed": target.name}]}) as res:
        assert res.status_code == 200, res.text
        events = [json.loads(line) for line in res.iter_lines() if line.strip()]
    done = events[-1]
    assert done["type"] == "done", events
    assert done["errors"] == []
    assert done["renamed"] == [{"from": str(source), "to": str(target)}]
    assert done["keyword_files"] == 1
    assert target.is_dir() and not source.exists()

    manifest = mf.read_manifest(target)
    assert manifest["roll_id"] == target.name
    assert manifest["folder_relative_path"] == target.name
    assert manifest["renames"][-1]["from"] == "jani-2026"
    # Old Roll keyword dropped, new one added, unrelated keywords kept.
    name, hier, flat = written[-1]
    assert name == "c.jpg"
    assert f"Roll|{target.name}" in hier and "Roll|jani-2026" not in hier
    assert "Film|Kodak Gold 200" in hier and "People|Anna" in hier
    assert target.name in flat and "jani-2026" not in flat and "Anna" in flat

    ops = client.get("/api/operations?kind=rename").json()
    assert ops[0]["status"] == "done"
    assert ops[0]["id"] == done["operation_id"]

    with client.stream("POST", f"/api/operations/{done['operation_id']}/undo") as res:
        events = [json.loads(line) for line in res.iter_lines() if line.strip()]
    assert events[-1]["type"] == "done"
    assert events[-1]["restored"] == [{"from": str(target), "to": str(source)}]
    assert source.is_dir() and not target.exists()
    assert mf.read_manifest(source)["roll_id"] == "jani-2026"
    assert written[-1][1] == ["Film|Kodak Gold 200", "People|Anna", "Roll|jani-2026"] or "Roll|jani-2026" in written[-1][1]
    assert client.get("/api/operations?kind=rename").json()[0]["status"] == "undone"
    assert client.post(f"/api/operations/{done['operation_id']}/undo").status_code == 409


def test_apply_refuses_conflicts_and_bad_targets(client, rename_tree, monkeypatch):
    written: list = []
    _fake_exiftool(monkeypatch, written)
    source = rename_tree / "2026" / "EUR-02-Gold"
    items = [
        {"path": str(source), "proposed": "2026-07-02-Fed2-Gold"},  # exists
        {"path": str(rename_tree / "2026" / "missing"), "proposed": "whatever"},
        {"path": str(rename_tree / "2026" / "mystery"), "proposed": "../escape"},
    ]
    with client.stream("POST", "/api/naming/apply", json={"items": items}) as res:
        events = [json.loads(line) for line in res.iter_lines() if line.strip()]
    done = events[-1]
    assert done["renamed"] == []
    assert len(done["errors"]) == 3
    assert source.is_dir()
    assert written == []
    assert client.get("/api/operations").json()[0]["status"] == "failed"
    assert client.post("/api/naming/apply", json={"items": []}).status_code == 400
    assert client.post("/api/operations/nope/undo").status_code == 404
