"""Split: plan, apply (moves with sidecars and pairs), EXDEV refusal, undo."""

from __future__ import annotations

import errno
import json
import os
import subprocess
from pathlib import Path

import pytest
from sqlmodel import Session

from filmex.db import get_engine
from filmex.services import fileops, operations
from filmex.services import frames as fr
from filmex.services import manifest as mf


def _touch(folder: Path, name: str, size: int = 4) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture
def library(tmp_path: Path, client, make_jpeg):
    """``root/2026/EUR-04`` holds two rolls' worth of frames: raw+jpg pairs
    with sidecars, then lab JPEGs."""
    root = tmp_path / "Filma"
    mixed = root / "2026" / "EUR-04"
    for i in range(1, 5):
        make_jpeg(f"DSC{i:04d}.JPG", long_edge=64, folder=mixed)
        _touch(mixed, f"DSC{i:04d}.ARW", 16)
        if i < 3:
            _touch(mixed, f"DSC{i:04d}.xmp", 2)
    _touch(mixed, "DSC0003.ARW.xmp", 2)  # legacy sidecar form
    for i in range(1, 4):
        make_jpeg(f"lab_{i:03d}.jpg", long_edge=64, folder=mixed)
        _touch(mixed, f"lab_{i:03d}.thm", 2)
    _touch(mixed, "notes.txt", 3)
    _touch(mixed, ".DS_Store", 1)
    client.patch("/api/settings", json={"library_root": str(root)})
    return root


def _stub_exiftool(monkeypatch, written):
    from filmex.services import fileops as fo
    from filmex.services import tagging as tg

    def fake_apply(path, intent):
        written.append((Path(path).parent.name, Path(path).name, list(intent.hierarchical_keywords)))
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(fo, "exiftool_available", lambda: True)
    monkeypatch.setattr(tg, "exiftool_available", lambda: True)
    monkeypatch.setattr(tg, "read_managed_tags", lambda path: {})
    monkeypatch.setattr(tg, "apply_intent", fake_apply)


def _segments(listing: fr.FrameListing):
    raw = [f.key for f in listing.frames if f.has_raw]
    lab = [f.key for f in listing.frames if not f.has_raw]
    return [
        fileops.Segment(name="2026-04-01-Fed2-Gold", frames=raw, tagging={"roll_date": "2026-04-01"}),
        fileops.Segment(name="2026-04-09-Fed2-HP5", frames=lab, tagging={"roll_date": "2026-04-09"}),
    ]


def test_plan_split_siblings_for_roll_folder_and_subfolders_for_year(library: Path):
    mixed = library / "2026" / "EUR-04"
    listing = fr.list_frames(mixed)
    plan = fileops.plan_split(mixed, _segments(listing), root=str(library))
    assert plan.ok, plan.to_dict()
    assert plan.layout == "siblings"
    assert plan.target_parent == str(library / "2026")
    assert plan.remove_source
    assert plan.other_files == ["notes.txt"]
    assert [i.name for i in plan.items] == ["2026-04-01-Fed2-Gold", "2026-04-09-Fed2-HP5"]
    first = plan.items[0]
    assert first.image_count == 8 and len(first.files) == 11  # 4 pairs + 3 sidecars
    assert "DSC0003.ARW.xmp" in first.files
    assert plan.items[1].files == ["lab_001.jpg", "lab_001.thm", "lab_002.jpg", "lab_002.thm", "lab_003.jpg", "lab_003.thm"]

    # A flat year folder (loose files directly under the root) is cut into subfolders.
    year = library / "2011"
    _touch(year, "a.jpg")
    _touch(year, "b.jpg")
    plan_year = fileops.plan_split(year, [fileops.Segment(name="R1", frames=["a"]), fileops.Segment(name="R2", frames=["b"])], root=str(library))
    assert plan_year.layout == "subfolders"
    assert plan_year.target_parent == str(year)
    assert not plan_year.remove_source


def test_plan_split_reports_conflicts_and_bad_segments(library: Path, monkeypatch):
    mixed = library / "2026" / "EUR-04"
    listing = fr.list_frames(mixed)
    segs = _segments(listing)
    (library / "2026" / "2026-04-01-Fed2-Gold").mkdir()
    segs[1].frames.append("DSC0001")  # also in the first segment
    segs.append(fileops.Segment(name="EUR-04", frames=[]))
    segs.append(fileops.Segment(name="bad/name", frames=["nope"]))
    plan = fileops.plan_split(mixed, segs, root=str(library))
    assert not plan.ok
    assert plan.items[0].errors == ["a folder with this name already exists"]
    assert any("also in" in e for e in plan.errors)
    assert any("no frames" in e for e in plan.errors)
    assert any("source folder's own name" in e for e in plan.items[2].errors)
    assert any("invalid folder name" in e for e in plan.errors)
    assert any("not in the folder" in e for e in plan.errors)

    monkeypatch.setattr(fileops, "same_volume", lambda a, b: False)
    plan2 = fileops.plan_split(mixed, _segments(listing), root=str(library))
    assert any("different volume" in e for e in plan2.errors)
    with pytest.raises(fileops.PlanError):
        fileops.apply_split(Session(get_engine()), mixed, _segments(listing), root=str(library))


def test_apply_split_moves_frames_tags_new_rolls_and_undo_restores(library: Path, monkeypatch):
    written: list = []
    _stub_exiftool(monkeypatch, written)
    mixed = library / "2026" / "EUR-04"
    listing = fr.list_frames(mixed)
    segs = _segments(listing)
    # A manifest on the source stands in for a previously tagged first roll.
    old_manifest = mf.build_new_manifest(
        folder=mixed, roll_id="EUR-04", source_kind="lab_scan_jpeg", scan_tier="standard",
        camera=mf.GearSnapshot(), lens=mf.GearSnapshot(), film=mf.FilmSnapshot(),
        development_provider=mf.NamedSnapshot(), development_method=mf.NamedSnapshot(),
        scan_provider=mf.NamedSnapshot(), scan_method=mf.NamedSnapshot(),
    )
    mf.write_manifest(mixed, old_manifest)
    events: list = []

    with Session(get_engine()) as session:
        result = fileops.apply_split(session, mixed, segs, root=str(library), progress=events.append)

    gold = library / "2026" / "2026-04-01-Fed2-Gold"
    hp5 = library / "2026" / "2026-04-09-Fed2-HP5"
    assert sorted(p.name for p in gold.iterdir()) == sorted(
        [f"DSC{i:04d}.{ext}" for i in range(1, 5) for ext in ("JPG", "ARW")] + ["DSC0001.xmp", "DSC0002.xmp", "DSC0003.ARW.xmp", "roll_manifest.json"]
    )
    assert sorted(p.name for p in hp5.iterdir()) == ["lab_001.jpg", "lab_001.thm", "lab_002.jpg", "lab_002.thm", "lab_003.jpg", "lab_003.thm", "roll_manifest.json"]
    # notes.txt keeps the source folder alive; its manifest moved into the journal.
    assert mixed.is_dir() and sorted(p.name for p in mixed.iterdir()) == ["notes.txt"]
    assert not result["removed_source"]
    assert result["stashed_manifest"]["roll_id"] == "EUR-04"
    assert not result["errors"]
    assert len(result["moves"]) == 17
    assert [len(c["applied"]) for c in result["created"]] == [8, 3]
    assert mf.read_manifest(gold)["roll_id"] == "2026-04-01-Fed2-Gold"
    assert mf.read_manifest(gold)["capture_date_value"] == "2026-04-01"
    assert mf.read_manifest(hp5)["capture_date_value"] == "2026-04-09"
    assert any(kw == ["Roll|2026-04-01-Fed2-Gold"] for folder, name, kw in written if folder == gold.name)
    steps = {e.get("step") for e in events}
    assert {"move", "tag", "segment"} <= steps

    with Session(get_engine()) as session:
        op = operations.get(session, result["operation_id"])
        assert op.kind == "split" and op.status == "done"
        undo = fileops.undo_split(session, result["operation_id"])
        assert operations.get(session, result["operation_id"]).status == "undone"

    assert len(undo["restored"]) == 17
    assert sorted(undo["removed_folders"]) == sorted([str(gold), str(hp5)])
    assert not gold.exists() and not hp5.exists()
    names = sorted(p.name for p in mixed.iterdir())
    assert "DSC0001.ARW" in names and "lab_003.thm" in names and "DSC0003.ARW.xmp" in names
    assert mf.read_manifest(mixed)["roll_id"] == "EUR-04"
    with Session(get_engine()) as session, pytest.raises(ValueError):
        fileops.undo_split(session, result["operation_id"])


def test_apply_split_removes_an_emptied_source_and_undo_recreates_it(library: Path, monkeypatch):
    _stub_exiftool(monkeypatch, [])
    mixed = library / "2026" / "EUR-04"
    (mixed / "notes.txt").unlink()
    listing = fr.list_frames(mixed)
    with Session(get_engine()) as session:
        result = fileops.apply_split(session, mixed, _segments(listing), root=str(library))
    assert result["removed_source"]
    assert not mixed.exists()
    with Session(get_engine()) as session:
        undo = fileops.undo_split(session, result["operation_id"])
    assert not undo["errors"]
    assert mixed.is_dir() and (mixed / "DSC0004.ARW").is_file()


def test_apply_split_stops_on_cross_volume_rename_and_keeps_partial_moves_undoable(library: Path, monkeypatch):
    _stub_exiftool(monkeypatch, [])
    mixed = library / "2026" / "EUR-04"
    listing = fr.list_frames(mixed)
    real_rename = os.rename
    calls = {"n": 0}

    def flaky_rename(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(errno.EXDEV, "Cross-device link")
        real_rename(src, dst)

    monkeypatch.setattr(fileops.os, "rename", flaky_rename)
    with Session(get_engine()) as session:
        result = fileops.apply_split(session, mixed, _segments(listing), root=str(library))
    assert len(result["moves"]) == 2
    assert result["errors"] and "Cross-device" in result["errors"][0]
    assert not (library / "2026" / "2026-04-09-Fed2-HP5").exists()
    monkeypatch.setattr(fileops.os, "rename", real_rename)
    with Session(get_engine()) as session:
        undo = fileops.undo_split(session, result["operation_id"])
    assert len(undo["restored"]) == 2 and not undo["errors"]
    assert sorted(p.name for p in mixed.iterdir() if p.name.startswith("DSC0001")) == ["DSC0001.ARW", "DSC0001.JPG", "DSC0001.xmp"]


def test_tag_folder_without_exiftool_reports_skip(tmp_path: Path, monkeypatch, make_jpeg):
    from filmex.services import fileops as fo

    monkeypatch.setattr(fo, "exiftool_available", lambda: False)
    folder = tmp_path / "roll"
    make_jpeg("a.jpg", long_edge=64, folder=folder)
    with Session(get_engine()) as session:
        out = fo.tag_folder(session, folder, {})
    assert out["skipped"] and not out["applied"]
    assert not mf.manifest_path_for(folder).exists()


def test_remove_folder_if_empty_deletes_only_litter(tmp_path: Path):
    folder = tmp_path / "x"
    _touch(folder, ".DS_Store")
    _touch(folder, "._a.jpg")
    assert fileops.remove_folder_if_empty(folder)
    assert not folder.exists()
    _touch(folder, "keep.txt")
    assert not fileops.remove_folder_if_empty(folder)
    assert (folder / "keep.txt").is_file()
    assert json.dumps(fileops.Segment(name="a", frames=["b"]).to_dict())
