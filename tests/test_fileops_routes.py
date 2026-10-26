"""Routes for frames, name rendering, split, import and undo dispatch."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from filmex.services import manifest as mf
from tests.helpers import ensure_film


def _touch(folder: Path, name: str, data: bytes = b"x" * 4) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def _stub_exiftool(monkeypatch):
    from filmex.services import fileops as fo
    from filmex.services import tagging as tg

    monkeypatch.setattr(fo, "exiftool_available", lambda: True)
    monkeypatch.setattr(tg, "exiftool_available", lambda: True)
    monkeypatch.setattr(tg, "read_managed_tags", lambda path: {})
    monkeypatch.setattr(tg, "apply_intent", lambda path, intent: subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""))


def _ndjson(res):
    assert res.status_code == 200, res.text
    return [json.loads(line) for line in res.text.splitlines() if line.strip()]


@pytest.fixture
def tree(tmp_path: Path, client, make_jpeg):
    root = tmp_path / "Filma"
    mixed = root / "2026" / "EUR-04"
    for i in range(1, 3):
        make_jpeg(f"DSC{i:04d}.JPG", long_edge=64, folder=mixed)
        _touch(mixed, f"DSC{i:04d}.ARW", b"raw" * 100)
    _touch(mixed, "DSC0001.xmp", b"<x/>")
    make_jpeg("lab_001.jpg", long_edge=64, folder=mixed)
    inbox = tmp_path / "inbox"
    for i in range(3):
        make_jpeg(f"scan_{i:03d}.jpg", long_edge=64, folder=inbox)
    client.patch("/api/settings", json={"library_root": str(root)})
    cam = client.get("/api/cameras").json()[0]
    client.patch(f"/api/cameras/{cam['id']}", json={"short_name": "Fed2"})
    film = ensure_film(client, brand="Kodak", name="Gold 200", display_name="Kodak Gold 200", iso=200, short_name="Gold")
    return root, mixed, inbox, cam, film


def test_frames_endpoint_lists_pairs(client, tree):
    _, mixed, _, _, _ = tree
    res = client.get("/api/folder/frames", params={"path": str(mixed)})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["frame_count"] == 3 and data["has_raw"]
    first = data["frames"][0]
    assert first["key"] == "DSC0001" and first["files"] == ["DSC0001.ARW", "DSC0001.JPG", "DSC0001.xmp"]
    assert data["frames"][2]["unmatched_jpeg"]
    assert data["suggested_roll_date"]
    assert client.get("/api/folder/frames", params={"path": str(mixed / "nope")}).status_code == 404


def test_render_name_from_ids(client, tree):
    _, _, _, cam, film = tree
    res = client.post("/api/naming/render", json={"roll_date": "2026-05-14", "camera_id": cam["id"], "film_stock_id": film["id"], "place": "Jāņos"})
    assert res.json() == {"name": "2026-05-14-Fed2-Gold-Jāņos", "error": None, "template": "{date}-{camera}-{film}"}
    missing = client.post("/api/naming/render", json={"camera_id": cam["id"]}).json()
    assert missing["name"] is None and "{date}" in missing["error"]
    custom = client.post("/api/naming/render", json={"roll_date": "2026-05-14", "template": "{year}_{month}"}).json()
    assert custom["name"] == "2026_05"


def test_split_plan_apply_and_undo_over_http(client, tree, monkeypatch):
    root, mixed, _, cam, film = tree
    _stub_exiftool(monkeypatch)
    body = {
        "folder": str(mixed),
        "segments": [
            {"name": "2026-04-01-Fed2-Gold", "frames": ["DSC0001", "DSC0002"], "tagging": {"camera_id": cam["id"], "film_stock_id": film["id"], "roll_date": "2026-04-01"}},
            {"name": "2026-04-09-Lab", "frames": ["lab_001"], "tagging": {"roll_date": "2026-04-09"}},
        ],
    }
    plan = client.post("/api/folder/split", json=body)
    assert plan.status_code == 200, plan.text
    assert plan.json()["ok"] and plan.json()["layout"] == "siblings" and plan.json()["file_count"] == 6

    bad = client.post("/api/folder/split/apply", json={**body, "segments": body["segments"][:1] + [{"name": "2026-04-01-Fed2-Gold", "frames": ["lab_001"]}]})
    assert bad.status_code == 400 and "same folder name" in bad.text

    events = _ndjson(client.post("/api/folder/split/apply", json=body))
    assert events[0]["type"] == "start" and events[0]["rolls"] == 2 and events[0]["total"] == 6
    done = next(e for e in events if e["type"] == "done")
    assert len(done["moves"]) == 6 and not done["errors"] and done["removed_source"]
    assert not mixed.exists()
    gold = root / "2026" / "2026-04-01-Fed2-Gold"
    assert mf.read_manifest(gold)["analog_camera_snapshot"]["display_name"] == cam["display_name"]
    assert mf.read_manifest(gold)["film_stock_snapshot"]["display_name"] == "Kodak Gold 200"

    ops = client.get("/api/operations", params={"kind": "split"}).json()
    assert ops[0]["id"] == done["operation_id"] and ops[0]["status"] == "done"

    undo = _ndjson(client.post(f"/api/operations/{done['operation_id']}/undo"))
    undo_done = next(e for e in undo if e["type"] == "done")
    assert len(undo_done["restored"]) == 6 and not undo_done["errors"]
    assert mixed.is_dir() and (mixed / "DSC0001.xmp").is_file() and not gold.exists()
    assert client.post(f"/api/operations/{done['operation_id']}/undo").status_code == 409


def test_import_plan_apply_and_undo_over_http(client, tree, monkeypatch):
    root, _, inbox, cam, film = tree
    _stub_exiftool(monkeypatch)
    body = {
        "folder": str(inbox),
        "segments": [{"name": "2026-05-14-Fed2-Gold", "frames": ["scan_000", "scan_001", "scan_002"],
                      "tagging": {"camera_id": cam["id"], "film_stock_id": film["id"], "roll_date": "2026-05-14"}}],
    }
    plan = client.post("/api/import/plan", json=body)
    assert plan.status_code == 200, plan.text
    assert plan.json()["ok"] and plan.json()["mode"] == "copy" and plan.json()["items"][0]["year"] == "2026"

    client.patch("/api/settings", json={"import_mode": "move"})
    assert client.post("/api/import/plan", json=body).json()["mode"] == "move"
    assert client.post("/api/import/plan", json={**body, "mode": "copy"}).json()["mode"] == "copy"
    assert client.post("/api/import/plan", json={**body, "mode": "teleport"}).status_code == 400

    events = _ndjson(client.post("/api/import/apply", json={**body, "mode": "copy"}))
    assert events[0]["type"] == "start" and events[0]["mode"] == "copy" and events[0]["bytes_total"] > 0
    done = next(e for e in events if e["type"] == "done")
    target = root / "2026" / "2026-05-14-Fed2-Gold"
    assert done["created"][0]["verified"] and len(done["created"][0]["files"]) == 3
    assert mf.read_manifest(target)["import_source"]["path"] == str(inbox)
    assert (inbox / "scan_000.jpg").is_file()
    assert any(e.get("step") == "copy" for e in events) and any(e.get("step") == "verify" for e in events)

    collision = client.post("/api/import/apply", json={**body, "mode": "copy"})
    assert collision.status_code == 400 and "already exists" in collision.text

    undo = _ndjson(client.post(f"/api/operations/{done['operation_id']}/undo"))
    undo_done = next(e for e in undo if e["type"] == "done")
    assert not undo_done["errors"] and not target.exists()
    assert client.get("/api/operations", params={"kind": "import"}).json()[0]["status"] == "undone"


def test_import_without_root_is_a_plan_error(client, tmp_path: Path, make_jpeg):
    client.patch("/api/settings", json={"library_root": None})
    folder = tmp_path / "x"
    make_jpeg("a.jpg", long_edge=64, folder=folder)
    res = client.post("/api/import/plan", json={"folder": str(folder), "segments": [{"name": "r", "frames": ["a"]}]})
    assert res.status_code == 400 and "library root" in res.text
