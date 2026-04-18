"""End-to-end workflow routes: scan, dry-run, apply, manifest.

These tests exercise real exiftool when available; they skip cleanly otherwise.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from filmex.routes import workflow as workflow_routes
from filmex.services import manifest as mf
from filmex.services.exiftool_runner import exiftool_available, read_tags

requires_exiftool = pytest.mark.skipif(not shutil.which("exiftool"), reason="exiftool not on PATH")


def _pick_pipeline(client):
    cam = client.get("/api/cameras").json()[0]
    film = client.get("/api/film-stocks").json()[0]
    lenses = client.get(f"/api/lenses?camera_id={cam['id']}").json()
    return cam, film, lenses[0] if lenses else None


def test_folder_scan_endpoint_reports_roll_id_and_tier(client, roll_factory):
    folder = roll_factory("2026-04-17_R100", jpegs=2)
    res = client.post("/api/folder/scan", json={"folder": str(folder)}).json()
    assert res["roll_id"] == "2026-04-17_R100"
    assert res["source_kind"] == "lab_scan_jpeg"
    assert res["scan_tier"] == "high-res"
    assert len(res["files"]) == 2


def test_folder_scan_reports_tiff_thumbnail_presence(client, roll_factory):
    folder = roll_factory("2026-04-17_R100B", jpegs=0, tiffs=1, tiff_thumbnails=True)
    res = client.post("/api/folder/scan", json={"folder": str(folder)}).json()
    assert res["source_kind"] == "positive_master_tiff"
    assert res["files"][0]["has_thumbnail"] is True


def test_folder_scan_404_on_missing(client, tmp_path):
    res = client.post("/api/folder/scan", json={"folder": str(tmp_path / "nope")})
    assert res.status_code == 404


def test_dry_run_produces_per_file_diffs(client, roll_factory):
    cam, film, lens = _pick_pipeline(client)
    folder = roll_factory("2026-04-17_R101", jpegs=2)
    res = client.post(
        "/api/metadata/dry-run",
        json={
            "folder": str(folder),
            "camera_id": cam["id"],
            "lens_id": lens["id"] if lens else None,
            "film_stock_id": film["id"],
        },
    ).json()
    assert len(res["diffs"]) == 2
    tags = {ch["tag"] for ch in res["diffs"][0]["changes"]}
    assert {"Make", "Model", "Subject", "HierarchicalSubject"}.issubset(tags)


def test_dry_run_file_keywords_apply_only_to_target_file(client, roll_factory):
    cam, film, lens = _pick_pipeline(client)
    folder = roll_factory("2026-04-17_R101B", jpegs=2)
    res = client.post(
        "/api/metadata/dry-run",
        json={
            "folder": str(folder),
            "camera_id": cam["id"],
            "lens_id": lens["id"] if lens else None,
            "film_stock_id": film["id"],
            "file_keywords": {"scan_000.jpg": ["Portrait", "Warm Light"]},
        },
    ).json()
    by_name = {file["name"]: file for file in res["diffs"]}
    first_subject = next(ch for ch in by_name["scan_000.jpg"]["changes"] if ch["tag"] == "Subject")
    second_subject = next(ch for ch in by_name["scan_001.jpg"]["changes"] if ch["tag"] == "Subject")
    assert "Portrait" in first_subject["after"]
    assert "Warm Light" in first_subject["after"]
    assert "Portrait" not in second_subject["after"]
    assert "Warm Light" not in second_subject["after"]


def test_dry_run_file_lens_override_applies_only_to_target_file(client, roll_factory):
    cam, film, lens = _pick_pipeline(client)
    alt = client.post(
        "/api/lenses",
        json={
            "make": "Jupiter",
            "model": "8",
            "display_name": "Jupiter-8 50mm f/2",
            "lens_model_write_value": "Jupiter-8 50mm f/2",
            "focal_length_mm": 50,
            "max_aperture": 2.0,
            "mount": cam["mount"],
        },
    ).json()
    folder = roll_factory("2026-04-17_R101C", jpegs=2)
    res = client.post(
        "/api/metadata/dry-run",
        json={
            "folder": str(folder),
            "camera_id": cam["id"],
            "lens_id": lens["id"] if lens else None,
            "film_stock_id": film["id"],
            "file_lens_ids": {"scan_001.jpg": alt["id"]},
        },
    ).json()
    by_name = {file["name"]: file for file in res["diffs"]}
    second_lens = next(ch for ch in by_name["scan_001.jpg"]["changes"] if ch["tag"] == "LensModel")
    second_hier = next(ch for ch in by_name["scan_001.jpg"]["changes"] if ch["tag"] == "HierarchicalSubject")
    first_hier = next(ch for ch in by_name["scan_000.jpg"]["changes"] if ch["tag"] == "HierarchicalSubject")
    assert second_lens["after"] == alt["lens_model_write_value"]
    assert f"Lens|{alt['display_name']}" in second_hier["after"]
    assert f"Lens|{alt['display_name']}" not in first_hier["after"]


@requires_exiftool
def test_apply_writes_metadata_and_manifest(client, roll_factory, tmp_path):
    cam, film, lens = _pick_pipeline(client)
    folder = roll_factory("2026-04-17_R102", jpegs=1)

    res = client.post(
        "/api/metadata/apply",
        json={
            "folder": str(folder),
            "camera_id": cam["id"],
            "lens_id": lens["id"] if lens else None,
            "film_stock_id": film["id"],
        },
    )
    body = res.json()
    assert res.status_code == 200
    assert body["errors"] == []
    assert len(body["applied_files"]) == 1

    # Manifest was written
    manifest = json.loads((folder / "roll_manifest.json").read_text())
    assert manifest["roll_id"] == "2026-04-17_R102"
    assert manifest["analog_camera_snapshot"]["make"] == cam["make"]
    assert len(manifest["apply_history"]) == 1

    # Real metadata landed in the JPG
    jpg = folder / "scan_000.jpg"
    tags = read_tags(jpg, ["Make", "Model", "Subject", "HierarchicalSubject"])
    assert tags["Make"] == cam["make"]
    assert film["display_name"] in tags["Subject"]


@requires_exiftool
def test_apply_persists_file_keyword_overrides_in_tags_and_manifest(client, roll_factory):
    cam, film, lens = _pick_pipeline(client)
    folder = roll_factory("2026-04-17_R102B", jpegs=2)

    res = client.post(
        "/api/metadata/apply",
        json={
            "folder": str(folder),
            "camera_id": cam["id"],
            "lens_id": lens["id"] if lens else None,
            "film_stock_id": film["id"],
            "file_keywords": {"scan_001.jpg": ["Close portrait"]},
        },
    )
    assert res.status_code == 200, res.text

    manifest = json.loads((folder / "roll_manifest.json").read_text())
    assert manifest["file_overrides"] == {
        "scan_001.jpg": {"extra_keywords": ["Close portrait"]},
    }

    tags = read_tags(folder / "scan_001.jpg", ["Subject"])
    assert "Close portrait" in tags["Subject"]


@requires_exiftool
def test_apply_writes_roll_notes_as_description_and_manifest_note(client, roll_factory):
    cam, film, lens = _pick_pipeline(client)
    folder = roll_factory("2026-04-17_R102C", jpegs=1)
    res = client.post(
        "/api/metadata/apply",
        json={
            "folder": str(folder),
            "camera_id": cam["id"],
            "lens_id": lens["id"] if lens else None,
            "film_stock_id": film["id"],
            "roll_notes": "Pushed +2 in home dev",
        },
    )
    assert res.status_code == 200, res.text

    manifest = json.loads((folder / "roll_manifest.json").read_text())
    assert manifest["notes"] == "Pushed +2 in home dev"

    tags = read_tags(folder / "scan_000.jpg", ["Description", "Caption-Abstract"])
    assert tags.get("Description") == "Pushed +2 in home dev"


@requires_exiftool
def test_apply_with_no_gear_writes_only_roll_keyword(client, roll_factory):
    """All three of camera/lens/film are optional. Roll|<id> must still land."""
    folder = roll_factory("2026-04-17_R140", jpegs=1)
    res = client.post(
        "/api/metadata/apply",
        json={"folder": str(folder)},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["errors"] == []

    manifest = json.loads((folder / "roll_manifest.json").read_text())
    assert manifest["analog_camera_snapshot"]["display_name"] is None
    assert manifest["film_stock_snapshot"]["display_name"] is None

    jpg = folder / "scan_000.jpg"
    tags = read_tags(jpg, ["Make", "LensModel", "HierarchicalSubject"])
    assert tags.get("Make") in (None, "")
    assert tags.get("LensModel") in (None, "")
    raw = tags.get("HierarchicalSubject") or []
    paths = raw if isinstance(raw, list) else [raw]  # exiftool returns scalar for single-value tags
    assert "Roll|2026-04-17_R140" in paths
    assert not any(p.startswith("Camera|") for p in paths)
    assert not any(p.startswith("Film|") for p in paths)


@requires_exiftool
def test_apply_without_lens_succeeds(client, roll_factory):
    """Point-and-shoot / disposable cameras have no lens worth cataloguing."""
    cam, film, _ = _pick_pipeline(client)
    folder = roll_factory("2026-04-17_R130", jpegs=1)
    res = client.post(
        "/api/metadata/apply",
        json={
            "folder": str(folder),
            "camera_id": cam["id"],
            "lens_id": None,
            "film_stock_id": film["id"],
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["errors"] == []
    manifest = json.loads((folder / "roll_manifest.json").read_text())
    assert manifest["analog_lens_snapshot"]["display_name"] is None
    # No Lens|... keyword should be written when lens is absent.
    jpg = folder / "scan_000.jpg"
    tags = read_tags(jpg, ["LensModel", "HierarchicalSubject"])
    assert tags.get("LensModel") in (None, "")
    raw = tags.get("HierarchicalSubject") or []
    paths = raw if isinstance(raw, list) else [raw]
    assert not any(k.startswith("Lens|") for k in paths)


@requires_exiftool
def test_apply_stream_emits_per_file_progress_and_done(client, roll_factory):
    cam, film, lens = _pick_pipeline(client)
    folder = roll_factory("2026-04-17_R120", jpegs=3)
    payload = {
        "folder": str(folder),
        "camera_id": cam["id"],
        "lens_id": lens["id"] if lens else None,
        "film_stock_id": film["id"],
    }
    with client.stream("POST", "/api/metadata/apply/stream", json=payload) as res:
        assert res.status_code == 200
        events = [json.loads(line) for line in res.iter_lines() if line.strip()]
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert events[0]["total"] == 3
    assert types.count("progress") == 3
    assert types[-1] == "done"
    assert len(events[-1]["applied_files"]) == 3


@requires_exiftool
def test_reapply_is_idempotent_keywords_not_duplicated(client, roll_factory):
    cam, film, lens = _pick_pipeline(client)
    folder = roll_factory("2026-04-17_R103", jpegs=1)
    payload = {
        "folder": str(folder),
        "camera_id": cam["id"],
        "lens_id": lens["id"] if lens else None,
        "film_stock_id": film["id"],
    }

    client.post("/api/metadata/apply", json=payload)
    client.post("/api/metadata/apply", json=payload)
    client.post("/api/metadata/apply", json=payload)

    jpg = folder / "scan_000.jpg"
    tags = read_tags(jpg, ["Subject", "HierarchicalSubject"])
    assert len(tags["Subject"]) == len(set(tags["Subject"]))
    assert len(tags["HierarchicalSubject"]) == len(set(tags["HierarchicalSubject"]))


@requires_exiftool
def test_reapply_preserves_created_at_and_appends_history(client, roll_factory):
    cam, film, lens = _pick_pipeline(client)
    folder = roll_factory("2026-04-17_R104", jpegs=1)
    payload = {
        "folder": str(folder),
        "camera_id": cam["id"],
        "lens_id": lens["id"] if lens else None,
        "film_stock_id": film["id"],
    }
    client.post("/api/metadata/apply", json=payload)
    first = json.loads((folder / "roll_manifest.json").read_text())
    client.post("/api/metadata/apply", json=payload)
    second = json.loads((folder / "roll_manifest.json").read_text())
    assert first["created_at"] == second["created_at"]
    assert len(second["apply_history"]) == len(first["apply_history"]) + 1


@requires_exiftool
def test_apply_dng_creates_then_updates_sidecar(client, roll_factory):
    cam, film, lens = _pick_pipeline(client)
    folder = roll_factory("2026-04-17_R105", jpegs=0, dngs=1)
    payload = {
        "folder": str(folder),
        "camera_id": cam["id"],
        "lens_id": lens["id"] if lens else None,
        "film_stock_id": film["id"],
    }
    # First apply: creates sidecar from raw
    res1 = client.post("/api/metadata/apply", json=payload).json()
    assert res1["errors"] == []
    # Canonical Lightroom sidecar form: frame_000.dng → frame_000.xmp.
    sidecar = folder / "frame_000.xmp"
    assert sidecar.is_file()

    # Second apply: edits sidecar in place. Re-apply must not duplicate keywords.
    res2 = client.post("/api/metadata/apply", json=payload).json()
    assert res2["errors"] == []
    tags = read_tags(sidecar, ["Subject", "HierarchicalSubject"])
    assert len(tags["Subject"]) == len(set(tags["Subject"]))
    assert len(tags["HierarchicalSubject"]) == len(set(tags["HierarchicalSubject"]))


def test_dry_run_errors_on_missing_camera(client, roll_factory):
    _, film, _ = _pick_pipeline(client)
    folder = roll_factory("2026-04-17_R106", jpegs=1)
    res = client.post(
        "/api/metadata/dry-run",
        json={
            "folder": str(folder),
            "camera_id": "nonexistent",
            "film_stock_id": film["id"],
        },
    )
    assert res.status_code == 404


def test_dry_run_rejects_wrong_provider_kind(client, roll_factory):
    cam, film, lens = _pick_pipeline(client)
    providers = {p["name"]: p for p in client.get("/api/providers").json()}
    folder = roll_factory("2026-04-17_R106B", jpegs=1)
    res = client.post(
        "/api/metadata/dry-run",
        json={
            "folder": str(folder),
            "camera_id": cam["id"],
            "lens_id": lens["id"] if lens else None,
            "film_stock_id": film["id"],
            "scan_provider_id": providers["Home"]["id"],
        },
    )
    assert res.status_code == 400
    assert "must support scan" in res.text


def test_dry_run_rejects_malformed_roll_date(client, roll_factory):
    cam, film, lens = _pick_pipeline(client)
    folder = roll_factory("2026-04-17_R107", jpegs=1)
    res = client.post(
        "/api/metadata/dry-run",
        json={
            "folder": str(folder),
            "camera_id": cam["id"],
            "lens_id": lens["id"] if lens else None,
            "film_stock_id": film["id"],
            "roll_date": "17/04/2026",
        },
    )
    assert res.status_code == 400


def test_folder_scan_suggests_roll_date(client, roll_factory):
    from datetime import date

    folder = roll_factory("2026-04-17_R108", jpegs=1)
    res = client.post("/api/folder/scan", json={"folder": str(folder)}).json()
    assert res["suggested_roll_date"] == date.today().isoformat()


@requires_exiftool
def test_apply_writes_roll_date_at_noon(client, roll_factory):
    cam, film, lens = _pick_pipeline(client)
    folder = roll_factory("2026-04-17_R109", jpegs=1)
    res = client.post(
        "/api/metadata/apply",
        json={
            "folder": str(folder),
            "camera_id": cam["id"],
            "lens_id": lens["id"] if lens else None,
            "film_stock_id": film["id"],
            "roll_date": "2024-06-15",
        },
    ).json()
    assert res["errors"] == []

    jpg = folder / "scan_000.jpg"
    tags = read_tags(jpg, ["DateTimeOriginal", "CreateDate"])
    assert tags["DateTimeOriginal"].startswith("2024:06:15 12:00:00")
    assert tags["CreateDate"].startswith("2024:06:15 12:00:00")

    manifest = json.loads((folder / "roll_manifest.json").read_text())
    assert manifest["capture_date_value"] == "2024-06-15"
    assert manifest["capture_date_precision"] == "day"
    assert manifest["capture_date_source"] == "user_override"


def test_get_manifest_404_when_missing(client, tmp_path):
    folder = tmp_path / "empty_roll"
    folder.mkdir()
    res = client.get(f"/api/manifests/empty_roll?folder={folder}")
    assert res.status_code == 404


def test_get_manifest_by_folder_reads_existing_manifest_even_if_folder_was_renamed(client, tmp_path):
    folder = tmp_path / "renamed_roll"
    folder.mkdir()
    manifest = mf.build_new_manifest(
        folder=folder,
        roll_id="2026-04-17_R999",
        source_kind="lab_scan_jpeg",
        scan_tier="high-res",
        camera=mf.GearSnapshot(make="Canon", model="AE-1", display_name="Canon AE-1"),
        lens=mf.GearSnapshot(),
        film=mf.FilmSnapshot(brand="Kodak", display_name="Kodak Gold 200"),
        development_provider=mf.NamedSnapshot(name="Home"),
        development_method=mf.NamedSnapshot(name="C-41"),
        scan_provider=mf.NamedSnapshot(name="Self"),
        scan_method=mf.NamedSnapshot(name="Lab scan"),
    )
    mf.write_manifest(folder, manifest)

    res = client.get("/api/manifest", params={"folder": str(folder)})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["roll_id"] == "2026-04-17_R999"
    assert body["manifest"]["analog_camera_snapshot"]["display_name"] == "Canon AE-1"


def test_thumbnail_endpoint_serves_tiff_sidecar_thumbnail(client, roll_factory):
    folder = roll_factory("2026-04-17_R108B", jpegs=0, tiffs=1, tiff_thumbnails=True)
    res = client.get(
        "/api/files/thumbnail",
        params={"folder": str(folder), "name": "master_000.tif"},
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/jpeg")
    assert res.content


def test_thumbnail_endpoint_404_without_preview(client, roll_factory):
    folder = roll_factory("2026-04-17_R108C", jpegs=0, tiffs=1, tiff_thumbnails=False)
    res = client.get(
        "/api/files/thumbnail",
        params={"folder": str(folder), "name": "master_000.tif"},
    )
    assert res.status_code == 404


def test_thumbnail_endpoint_serves_cached_preview_after_create(client, roll_factory, data_dir):
    folder = roll_factory("2026-04-17_R108E", jpegs=1)
    created = client.post(
        "/api/previews/create",
        json={"folder": str(folder), "invert": False},
    )
    assert created.status_code == 200, created.text
    res = client.get(
        "/api/files/thumbnail",
        params={"folder": str(folder), "name": "scan_000.jpg"},
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/jpeg")
    assert list((data_dir / "preview_cache").glob("*.jpg"))


def test_create_previews_endpoint_reports_created_and_failed(client, roll_factory, data_dir):
    folder = roll_factory("2026-04-17_R108F", jpegs=1, dngs=1)
    res = client.post(
        "/api/previews/create",
        json={"folder": str(folder), "invert": True},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "scan_000.jpg" in body["created_files"]
    assert any(line.startswith("frame_000.dng:") for line in body["failed"])
    assert list((data_dir / "preview_cache").glob("*.jpg"))


def test_open_file_endpoint_uses_default_app(client, roll_factory, monkeypatch):
    folder = roll_factory("2026-04-17_R108D", jpegs=1)
    opened = {}

    def fake_open(path: Path) -> None:
        opened["path"] = path

    monkeypatch.setattr(workflow_routes, "_open_with_default_app", fake_open)

    res = client.post(
        "/api/files/open",
        params={"folder": str(folder), "name": "scan_000.jpg"},
    )
    assert res.status_code == 200
    assert opened["path"] == folder / "scan_000.jpg"


def test_health_reports_status(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert "exiftool_available" in body
