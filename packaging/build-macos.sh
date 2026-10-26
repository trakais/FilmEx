#!/usr/bin/env bash
# Build FilmEx.app and a DMG on macOS. No signing identity needed: the app is
# ad-hoc signed, so recipients approve it once via System Settings ▸ Privacy &
# Security ▸ Open Anyway (or `xattr -dr com.apple.quarantine FilmEx.app`).
#
# Optional: run packaging/fetch-exiftool.sh first to bundle ExifTool.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(uv run python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
ARCH=$(uname -m)

uv sync --group build
uv run python packaging/make_icon.py packaging/FilmEx.icns
rm -rf build dist
uv run --group build pyinstaller --noconfirm --clean packaging/filmex.spec
codesign --force --deep --sign - dist/FilmEx.app

DMG="dist/FilmEx-${VERSION}-${ARCH}.dmg"
rm -f "$DMG"
hdiutil create -volname "FilmEx" -srcfolder dist/FilmEx.app -ov -format UDZO "$DMG" >/dev/null
echo "Built $DMG"
