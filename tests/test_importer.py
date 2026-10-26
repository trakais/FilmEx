"""Inbox import: copy and verify, collisions, move after verification, undo."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from sqlmodel import Session

from filmex.db import get_engine
from filmex.services import fileops, importer, operations
from filmex.services import frames as fr
from filmex.services import manifest as mf
from filmex.services.fileops import RollDraft


def _touch(folder: Path, name: str, data: bytes = b"x" * 4) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


@pytest.fixture
def inbox(tmp_path: Path, client, make_jpeg):
    root = tmp_path / "Filma"
    root.mkdir()
    source = tmp_path / "inbox" / "scan-2026-05"
    for i in range(1, 4):
        make_jpeg(f"DSC{i:04d}.JPG", long_edge=64, folder=source)
        _touch(source, f"DSC{i:04d}.ARW", os.urandom(3000))
    _touch(source, "DSC0001.xmp", b"<x:xmpmeta/>")
    for i in range(1, 3):
        make_jpeg(f"lab_{i:03d}.jpg", long_edge=64, folder=source)
    _touch(source, "readme.txt")
    client.patch("/api/settings", json={"library_root": str(root)})
    return root, source


def _rolls(source: Path):
    listing = fr.list_frames(source)
    raw = [f.key for f in listing.frames if f.has_raw]
    lab = [f.key for f in listing.frames if not f.has_raw]
    return [
        RollDraft(name="2026-05-14-Fed2-Gold", frames=raw, tagging={"roll_date": "2026-05-14"}),
        RollDraft(name="2024-12-24-Fed2-HP5", frames=lab, tagging={"roll_date": "2024-12-24"}),
    ]


def test_copy_verify_detects_a_corrupted_destination(tmp_path: Path, monkeypatch):
    src = _touch(tmp_path, "a.bin", os.urandom(10_000))
    dst = tmp_path / "b.bin"
    info = importer.copy_verify(src, dst, chunk=4096)
    assert info["size"] == 10_000 and info["digest"] == importer.hash_file(dst)
    with pytest.raises(FileExistsError):
        importer.copy_verify(src, dst)

    # The destination lands damaged: reading it back must catch it and remove it.
    real_hash = importer.hash_file

    def wrong_hash(path, *, chunk=importer.CHUNK):
        return "0" * 64 if Path(path).name == "c.bin" else real_hash(path, chunk=chunk)

    monkeypatch.setattr(importer, "hash_file", wrong_hash)
    with pytest.raises(importer.VerifyError):
        importer.copy_verify(src, tmp_path / "c.bin")
    assert not (tmp_path / "c.bin").exists()


def test_plan_import_resolves_years_and_stops_on_collision(inbox, monkeypatch):
    root, source = inbox
    plan = importer.plan_import(source, _rolls(source), root=str(root), mode="copy")
    assert plan.ok, plan.to_dict()
    assert [i.year for i in plan.items] == ["2026", "2024"]
    assert plan.items[0].target == str(root / "2026" / "2026-05-14-Fed2-Gold")
    assert plan.items[0].image_count == 6 and len(plan.items[0].files) == 7
    assert plan.other_files == ["readme.txt"]
    assert plan.total_bytes > 9000 and plan.to_dict()["file_count"] == 9

    (root / "2026" / "2026-05-14-Fed2-Gold").mkdir(parents=True)
    plan2 = importer.plan_import(source, _rolls(source), root=str(root), mode="copy")
    assert not plan2.ok and "already exists" in plan2.items[0].errors[0]
    with Session(get_engine()) as session, pytest.raises(fileops.PlanError):
        importer.apply_import(session, source, _rolls(source), root=str(root), mode="copy")
    assert not list((root / "2026" / "2026-05-14-Fed2-Gold").iterdir())  # nothing copied
    assert not (root / "2024").exists()

    # Year from the oldest file when no date is set; no year at all is an error only when nothing works.
    undated = [RollDraft(name="x", frames=["lab_001"], tagging={})]
    assert importer.plan_import(source, undated, root=str(root), mode="copy").items[0].year == str(date.today().year)
    monkeypatch.setattr(importer.shutil, "disk_usage", lambda p: type("Usage", (), {"free": 10})())
    low = importer.plan_import(source, _rolls(source), root=str(root), mode="copy")
    assert any("free space" in e for e in low.errors)


def test_apply_import_copies_verifies_tags_and_undo_removes_copies(inbox, fake_exiftool):
    root, source = inbox
    events: list = []
    with Session(get_engine()) as session:
        result = importer.apply_import(session, source, _rolls(source), root=str(root), mode="copy", progress=events.append)

    gold = root / "2026" / "2026-05-14-Fed2-Gold"
    hp5 = root / "2024" / "2024-12-24-Fed2-HP5"
    assert not result["errors"] and not result["cancelled"]
    assert sorted(p.name for p in gold.iterdir()) == sorted([f"DSC{i:04d}.{e}" for i in range(1, 4) for e in ("JPG", "ARW")] + ["DSC0001.xmp", "roll_manifest.json"])
    assert sorted(p.name for p in hp5.iterdir()) == ["lab_001.jpg", "lab_002.jpg", "roll_manifest.json"]
    # Sources untouched in copy mode, bytes identical.
    assert (source / "DSC0002.ARW").read_bytes() == (gold / "DSC0002.ARW").read_bytes()
    assert result["removed_sources"] == []
    manifest = mf.read_manifest(gold)
    assert manifest["import_source"]["path"] == str(source)
    assert manifest["import_source"]["verified"] and manifest["import_source"]["mode"] == "copy"
    assert {f["name"] for f in manifest["import_source"]["files"]} == {f"DSC{i:04d}.{e}" for i in range(1, 4) for e in ("JPG", "ARW")} | {"DSC0001.xmp"}
    assert all(len(f["blake2b"]) == 64 for f in manifest["import_source"]["files"])
    assert manifest["capture_date_value"] == "2026-05-14"
    assert [len(c["applied"]) for c in result["created"]] == [6, 2]
    assert all(c["verified"] for c in result["created"])
    steps = [e.get("step") for e in events]
    assert "copy" in steps and "verify" in steps and "tag" in steps and "roll" in steps
    last_verify = [e for e in events if e.get("step") == "verify"][-1]
    assert last_verify["bytes_done"] == last_verify["bytes_total"] and last_verify["files_done"] == 9

    (gold / "DSC0002.xmp").write_text("<x/>")  # what tagging seeds for a raw without a sidecar
    with Session(get_engine()) as session:
        undo = importer.undo_import(session, result["operation_id"])
        assert operations.get(session, result["operation_id"]).status == "undone"
    assert not undo["errors"]
    assert sorted(undo["removed_folders"]) == sorted([str(gold), str(hp5), str(root / "2026"), str(root / "2024")])
    assert not (source / "DSC0002.xmp").exists()  # copy mode: the copy's sidecar is discarded with it
    assert not (root / "2026").exists() and not (root / "2024").exists()
    assert (source / "DSC0001.xmp").is_file()


def test_move_mode_deletes_sources_only_after_the_whole_roll_verified(inbox, monkeypatch, fake_exiftool):
    root, source = inbox
    real_copy = importer.copy_verify
    state = {"n": 0}

    def flaky_copy(src, dst, *, progress=None, chunk=importer.CHUNK):
        state["n"] += 1
        if Path(src).name == "lab_002.jpg":
            raise importer.VerifyError("lab_002.jpg: destination differs from source (hash mismatch)")
        return real_copy(src, dst, progress=progress, chunk=chunk)

    monkeypatch.setattr(importer, "copy_verify", flaky_copy)
    with Session(get_engine()) as session:
        result = importer.apply_import(session, source, _rolls(source), root=str(root), mode="move")

    gold = root / "2026" / "2026-05-14-Fed2-Gold"
    hp5 = root / "2024" / "2024-12-24-Fed2-HP5"
    # Roll one verified fully: its sources are gone and the copies carry the bytes.
    assert not (source / "DSC0001.ARW").exists() and (gold / "DSC0001.ARW").is_file()
    assert len(result["removed_sources"]) == 7
    # Roll two failed verification on its second file: nothing of it was deleted.
    assert (source / "lab_001.jpg").is_file() and (source / "lab_002.jpg").is_file()
    assert (hp5 / "lab_001.jpg").is_file() and not (hp5 / "lab_002.jpg").exists()
    assert result["errors"] and "lab_002.jpg" in result["errors"][0]
    assert not result["created"][1]["verified"]
    assert not mf.manifest_path_for(hp5).exists()

    monkeypatch.setattr(importer, "copy_verify", real_copy)
    (gold / "DSC0003.xmp").write_text("<x/>")  # tagging sidecar; in move mode it goes back with the raw
    with Session(get_engine()) as session:
        undo = importer.undo_import(session, result["operation_id"])
    assert not undo["errors"], undo
    assert (source / "DSC0001.ARW").is_file() and (source / "DSC0001.xmp").is_file()
    assert (source / "DSC0003.xmp").read_text() == "<x/>"
    assert len(undo["restored"]) == 7
    assert not gold.exists() and not hp5.exists()
    assert (source / "lab_001.jpg").is_file()


def test_undo_keeps_a_copy_whose_source_vanished(inbox, fake_exiftool):
    root, source = inbox
    rolls = [RollDraft(name="only-lab", frames=["lab_001", "lab_002"], tagging={"roll_date": "2025-01-01"})]
    with Session(get_engine()) as session:
        result = importer.apply_import(session, source, rolls, root=str(root), mode="copy")
    target = root / "2025" / "only-lab"
    (source / "lab_001.jpg").unlink()
    with Session(get_engine()) as session:
        undo = importer.undo_import(session, result["operation_id"])
    assert any("source is gone" in e for e in undo["errors"])
    assert (target / "lab_001.jpg").is_file() and not (target / "lab_002.jpg").exists()
    assert target.is_dir()
