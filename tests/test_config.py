"""Data directory resolution rules."""

from __future__ import annotations

from pathlib import Path

from filmex import config


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("FILMEX_DATA_DIR", str(tmp_path / "override"))
    monkeypatch.setattr(config, "is_source_checkout", lambda: True)
    assert config.data_dir() == tmp_path / "override"
    assert config.db_path() == tmp_path / "override" / "filmex.sqlite"


def test_source_checkout_uses_project_data_dir(monkeypatch):
    monkeypatch.delenv("FILMEX_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "is_source_checkout", lambda: True)
    assert config.data_dir() == config.PROJECT_ROOT / "data"


def test_installed_or_frozen_uses_user_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("FILMEX_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "is_source_checkout", lambda: False)
    monkeypatch.setattr(config, "user_data_dir", lambda: tmp_path / "AppSupport" / "FilmEx")
    assert config.data_dir() == tmp_path / "AppSupport" / "FilmEx"


def test_frozen_is_never_a_source_checkout(monkeypatch):
    monkeypatch.setattr(config, "is_frozen", lambda: True)
    assert config.is_source_checkout() is False


def test_user_data_dir_is_platform_specific():
    path = config.user_data_dir()
    assert isinstance(path, Path)
    assert path.name == "FilmEx"
