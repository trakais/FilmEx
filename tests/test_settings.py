"""Settings persistence, validation and runtime side effects."""

from __future__ import annotations

from fastapi.testclient import TestClient

from filmex.services import exiftool_locator as locator
from filmex.services import settings as settings_service


def test_defaults_are_returned_when_nothing_is_stored(client):
    data = client.get("/api/settings").json()
    assert data["theme"] == "dark"
    assert data["recent_folders"] == []
    assert data["library_root"] is None
    assert data["naming_template"] == "{date}-{camera}-{film}"
    assert set(data) == set(settings_service.SPECS)


def test_patch_merges_and_persists_across_restart(client, data_dir):
    res = client.patch("/api/settings", json={"theme": "paper", "last_folder": "/tmp/roll"})
    assert res.status_code == 200
    assert res.json()["theme"] == "paper"
    assert res.json()["last_folder"] == "/tmp/roll"
    assert res.json()["preview_invert"] is False  # untouched default

    from filmex import db as db_module
    from filmex.main import app

    db_module.reset_engine()
    with TestClient(app) as fresh:
        again = fresh.get("/api/settings").json()
    assert again["theme"] == "paper"
    assert again["last_folder"] == "/tmp/roll"


def test_unknown_key_is_rejected(client):
    res = client.patch("/api/settings", json={"colour": "red"})
    assert res.status_code == 400
    assert "unknown setting" in res.json()["detail"]


def test_wrong_types_are_rejected(client):
    assert client.patch("/api/settings", json={"theme": "neon"}).status_code == 400
    assert client.patch("/api/settings", json={"preview_invert": "yes"}).status_code == 400
    assert client.patch("/api/settings", json={"recent_folders": "a,b"}).status_code == 400
    assert client.patch("/api/settings", json={"window_geometry": 12}).status_code == 400
    # A rejected patch leaves nothing half-written.
    assert client.get("/api/settings").json()["theme"] == "dark"


def test_recent_folders_are_deduped_and_capped(client):
    folders = [f"/rolls/r{i:02d}" for i in range(30)] + ["/rolls/r00", " ", ""]
    res = client.patch("/api/settings", json={"recent_folders": folders})
    stored = res.json()["recent_folders"]
    assert len(stored) == settings_service.RECENT_FOLDERS_MAX
    assert stored[0] == "/rolls/r00"
    assert len(set(stored)) == len(stored)


def test_exiftool_path_setting_configures_locator(client, monkeypatch, tmp_path):
    seen: list[str | None] = []
    monkeypatch.setattr(locator, "configure", lambda path: seen.append(path))
    client.patch("/api/settings", json={"exiftool_path": "  /opt/tools/exiftool "})
    assert seen == ["/opt/tools/exiftool"]
    client.patch("/api/settings", json={"exiftool_path": ""})
    assert seen[-1] is None


def test_startup_applies_persisted_exiftool_path(client, data_dir, monkeypatch):
    client.patch("/api/settings", json={"exiftool_path": "/opt/tools/exiftool"})
    seen: list[str | None] = []
    monkeypatch.setattr(locator, "configure", lambda path: seen.append(path))

    from filmex import db as db_module
    from filmex.main import app

    db_module.reset_engine()
    with TestClient(app):
        pass
    assert "/opt/tools/exiftool" in seen


def test_remember_folder_helper_moves_folder_to_front(client, data_dir):
    from sqlmodel import Session

    from filmex.db import get_engine

    with Session(get_engine()) as session:
        settings_service.remember_folder(session, "/a")
        settings_service.remember_folder(session, "/b")
        result = settings_service.remember_folder(session, "/a")
    assert result["recent_folders"] == ["/a", "/b"]
    assert result["last_folder"] == "/a"
