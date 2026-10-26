# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for FilmEx.app. Run via packaging/build-macos.sh."""

import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 - provided by PyInstaller
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
ICON = ROOT / "packaging" / "FilmEx.icns"

datas = [
    (str(ROOT / "filmex" / "static"), "filmex/static"),
    (str(ROOT / "filmex" / "default_data.sqlite"), "filmex"),
]
datas += collect_data_files("webview")
vendor = ROOT / "packaging" / "vendor" / "exiftool"
if vendor.is_dir():
    datas.append((str(vendor), "vendor/exiftool"))

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("webview")
    + ["sqlalchemy.dialects.sqlite", "filmex.models", "filmex.routes", "filmex.services"]
)

a = Analysis(
    [str(ROOT / "filmex" / "desktop.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="FilmEx",
    console=False,
    icon=str(ICON) if ICON.is_file() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="FilmEx")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="FilmEx.app",
        icon=str(ICON) if ICON.is_file() else None,
        bundle_identifier="lv.dodies.filmex",
        info_plist={
            "CFBundleName": "FilmEx",
            "CFBundleDisplayName": "FilmEx",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "FilmEx contributors",
        },
    )
