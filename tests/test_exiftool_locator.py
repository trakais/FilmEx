"""ExifTool discovery order and invocation shape."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from filmex.services import exiftool as et
from filmex.services import exiftool_locator as locator


def _fake_binary(path: Path, *, perl: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "#!/usr/bin/perl\nprint 1;\n" if perl else "#!/bin/sh\necho 13.55\n"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Every test starts with nothing discoverable and a clean cache."""
    monkeypatch.delenv(locator.ENV_VAR, raising=False)
    monkeypatch.setattr(locator.shutil, "which", lambda name: None)
    monkeypatch.setattr(locator, "candidates", lambda: ())
    monkeypatch.setattr(locator, "bundle_resource_dir", lambda: None)
    locator.configure(None)
    yield
    locator.configure(None)


def test_nothing_found_means_unavailable():
    assert locator.locate() is None
    assert locator.available() is False
    assert locator.command() is None


def test_path_lookup(monkeypatch, tmp_path):
    binary = _fake_binary(tmp_path / "bin" / "exiftool")
    monkeypatch.setattr(locator.shutil, "which", lambda name: str(binary))
    locator.invalidate()
    loc = locator.locate()
    assert loc is not None
    assert loc.source == "path"
    assert loc.command == (str(binary),)


def test_candidate_used_when_path_is_empty(monkeypatch, tmp_path):
    binary = _fake_binary(tmp_path / "opt" / "homebrew" / "bin" / "exiftool")
    monkeypatch.setattr(locator, "candidates", lambda: (str(tmp_path / "missing"), str(binary)))
    locator.invalidate()
    loc = locator.locate()
    assert loc is not None
    assert loc.source == "candidate"
    assert loc.path == str(binary)


def test_env_beats_path(monkeypatch, tmp_path):
    on_path = _fake_binary(tmp_path / "bin" / "exiftool")
    from_env = _fake_binary(tmp_path / "env" / "exiftool")
    monkeypatch.setattr(locator.shutil, "which", lambda name: str(on_path))
    monkeypatch.setenv(locator.ENV_VAR, str(from_env))
    locator.invalidate()
    loc = locator.locate()
    assert loc is not None
    assert (loc.source, loc.path) == ("env", str(from_env))


def test_setting_beats_everything(monkeypatch, tmp_path):
    on_path = _fake_binary(tmp_path / "bin" / "exiftool")
    from_env = _fake_binary(tmp_path / "env" / "exiftool")
    configured = _fake_binary(tmp_path / "custom" / "exiftool")
    monkeypatch.setattr(locator.shutil, "which", lambda name: str(on_path))
    monkeypatch.setenv(locator.ENV_VAR, str(from_env))
    locator.configure(str(configured))
    loc = locator.locate()
    assert loc is not None
    assert (loc.source, loc.path) == ("setting", str(configured))


def test_unusable_setting_falls_through(monkeypatch, tmp_path):
    on_path = _fake_binary(tmp_path / "bin" / "exiftool")
    monkeypatch.setattr(locator.shutil, "which", lambda name: str(on_path))
    locator.configure(str(tmp_path / "does-not-exist"))
    loc = locator.locate()
    assert loc is not None
    assert loc.source == "path"


def test_setting_accepts_distribution_directory(tmp_path):
    script = _fake_binary(tmp_path / "Image-ExifTool-13.55" / "exiftool", perl=True)
    locator.configure(str(script.parent))
    loc = locator.locate()
    assert loc is not None
    assert loc.command[-1] == str(script)
    assert loc.command[0].endswith("perl")


def test_perl_script_runs_through_perl(monkeypatch, tmp_path):
    script = _fake_binary(tmp_path / "vendor" / "exiftool" / "exiftool", perl=True)
    monkeypatch.setattr(locator, "bundle_resource_dir", lambda: tmp_path / "vendor")
    monkeypatch.setattr(locator, "_find_perl", lambda: "/usr/bin/perl")
    locator.invalidate()
    loc = locator.locate()
    assert loc is not None
    assert loc.source == "bundled"
    assert loc.command == ("/usr/bin/perl", str(script))


def test_perl_script_without_interpreter_is_unusable(monkeypatch, tmp_path):
    script = _fake_binary(tmp_path / "vendor" / "exiftool" / "exiftool", perl=True)
    monkeypatch.setattr(locator, "bundle_resource_dir", lambda: tmp_path / "vendor")
    monkeypatch.setattr(locator, "_find_perl", lambda: None)
    locator.invalidate()
    assert locator.locate() is None
    assert script.is_file()


@pytest.mark.skipif(os.name == "nt", reason="exec bit is POSIX-only")
def test_non_executable_file_is_ignored(monkeypatch, tmp_path):
    binary = tmp_path / "bin" / "exiftool"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(locator.shutil, "which", lambda name: str(binary))
    locator.invalidate()
    assert locator.locate() is None


def test_builder_uses_command_prefix():
    prefix = ("/usr/bin/perl", "/app/vendor/exiftool/exiftool")
    read = et.build_read_argv("photo.jpg", ["Make"], command=prefix)
    assert read[:2] == list(prefix)
    seed, edit = et.build_argv("photo.arw", et.WriteIntent(camera_make="FED"), command=prefix)
    assert seed[:2] == list(prefix)
    assert edit[:2] == list(prefix)
    (embed,) = et.build_argv("photo.jpg", et.WriteIntent(camera_make="FED"), command=prefix)
    assert embed[:2] == list(prefix)


def test_builder_default_prefix_is_bare_name():
    (embed,) = et.build_argv("photo.jpg", et.WriteIntent(camera_make="FED"))
    assert embed[0] == "exiftool"
