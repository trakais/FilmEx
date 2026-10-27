# FilmEx

FilmEx, or Film EXIF, is a local-first browser tool for tagging film scan folders before import into Lightroom or other DAM software. It relies on your system exiftool and local filesystem, so you must run it locally, not in a container or on another machine. 

![FilmEx overview](docs/screenshots/filmex-overview.webp)
_Main tagging workflow_

## What It Does

- scans a single film-roll folder
- detects supported files: `JPG`, `TIFF`, `DNG`, `ARW`
- lets you choose analog camera, lens, and film stock
- keeps film brands as a separate controlled list with per-brand colors
- adds development and scan metadata
- lets you add per-file lens overrides and extra keywords in the preview
- stores roll notes as a description/caption field, not as keywords
- shows a dry-run preview before writing anything
- can create lightweight local previews for files that do not already have `.thm` thumbnails
- writes metadata with ExifTool
- creates a `roll_manifest.json` next to the images

For raw files (`DNG`, `ARW`), FilmEx writes to XMP sidecars using the canonical
replaced-extension form like `photo.xmp`.

For `JPG` and `TIFF`, FilmEx writes metadata into the file itself.

## How it works

Open a folder of scans: drop it on the window, press Open folder, or pick
it from the recent list. Its frames appear on a contact sheet as one roll.
Pick the camera, lens and film, check the date, and press Write. That tags
the files where they are and leaves a `roll_manifest.json` beside them.

The same screen covers the rest:

- **More than one roll in the folder.** FilmEx suggests where rolls change
  from pauses in the capture times (weighed by how many frames a roll
  holds), restarting file numbers and changes of frame shape, and shows
  each seam side by side so you can confirm it. Or select the first frame
  of the next roll and press "Cut here" (the C key), or build a roll from
  a selection. Each roll gets its own gear, film, date and folder name
  from the template. "Keep here" then splits the folder into one folder
  per roll.
- **A library.** Choose a library folder once (the top folder with year
  folders inside). The Library becomes the start screen: every roll by
  year with its status, counts, film and camera, with Tag, Tag remaining,
  Open and Reveal on each row. The index is cached, so it opens even when
  the share is unmounted.
- **Importing.** With a library set, a folder opened from anywhere else
  defaults to "Copy to library" or "Move to library". Rolls land in
  `<root>/<year>/<name>`, every file is read back and verified before a
  source is ever deleted, and each new folder is tagged on the spot. The
  plan is shown before anything runs, and the last batch can be undone.
- **Changes** on a roll card shows exactly what will be written to each
  file, with a per-file lens override and extra keywords.
- **Normalize names** (Library menu) proposes a folder name for every
  tagged roll from the template in Settings ▸ Preferences, renames in
  place, updates the manifest and the `Roll|` keyword on every tagged
  file, and can undo the batch.

Raw files and their JPEGs, sidecars and `.thm` thumbnails always travel
together. Files that belong to no frame, such as notes, stay behind.

## Desktop app

FilmEx also runs as a native window with no Terminal and no background
process: the server lives inside the app and stops when you quit.

Pick one:

- **App bundle (macOS).** Download `FilmEx-<version>-arm64.dmg` from the
  releases page, drag `FilmEx.app` to Applications. The app is not notarized
  (no paid Apple developer account), so the first launch is blocked. Open it
  once, then go to System Settings ▸ Privacy & Security and click
  **Open Anyway**. Or remove the quarantine flag instead:

  ```bash
  xattr -dr com.apple.quarantine /Applications/FilmEx.app
  ```

  This is needed once per downloaded version.
- **Installed tool (no Gatekeeper prompt).** Needs `uv`; it installs Python
  itself. Replace the URL with this repository's clone URL:

  ```bash
  uv tool install "filmex @ git+https://git.dodies.lv/<user>/FilmEx"
  filmex
  ```
- **From a checkout.**

  ```bash
  uv run filmex
  ```

The desktop app keeps its database in the per-user data directory
(`~/Library/Application Support/FilmEx` on macOS). A source checkout keeps
using `data/`. `FILMEX_DATA_DIR` overrides both. ExifTool is found on `PATH`,
in the usual Homebrew locations, or at the path set in Settings ▸ Preferences.

### Building the app

```bash
packaging/build-macos.sh
```

Produces `dist/FilmEx.app` and a DMG. To bundle ExifTool inside the app, run
`packaging/fetch-exiftool.sh` first (pin the tarball checksum in
`packaging/exiftool.lock` when prompted). Releases are built locally; the
GitHub workflow in `.github/workflows/build.yml` is optional.

## Requirements

- Python 3.11+
- `uv`
- `exiftool` available on `PATH`

## Run Locally

First time only:

```bash
uv sync
```

Then, on macOS / Linux:

```bash
./filmex-run
```

On Windows:

```cmd
filmex-run.cmd
```

This starts the server on `http://127.0.0.1:8765` and opens your browser. Set
`FILMEX_PORT` to use a different port.

Or run uvicorn directly if you prefer:

```bash
uv run uvicorn filmex.main:app --port 8765
```

## Notes

- A source checkout stores runtime data in `data/`; the desktop app uses the
  per-user data directory.
- Settings, recent folders and the last selection are stored in the database,
  not in the browser.
- A starter SQLite database is bundled with sample cameras, lenses, films, film brands, providers, and methods.
- Your own working database lives in `data/filmex.sqlite`.
- Temporary and private local files should stay in `tmp/`, which is ignored by git.

## Bulk data entry

To add cameras, lenses, film brands, films, providers, or methods in bulk —
for example from an LLM-drafted batch — use the CLI. It is idempotent: rows
that already exist (matched by natural key) are skipped, never overwritten.

```bash
uv run python -m filmex.cli add-from-json examples/entities-batch.json --dry-run
uv run python -m filmex.cli add-from-json examples/entities-batch.json
uv run python -m filmex.cli add-from-json examples/entities-batch.json --upsert-film-data
uv run python -m filmex.cli export-json -o backup.json
```

See `AGENTS.md` for the batch schema and nomenclature conventions.

## Testing

```bash
uv run pytest
```

## Contributing

`AGENTS.md` at the repo root is the canonical reference for project
conventions: data model, nomenclature, metadata write policy, keyword
taxonomy, and safety rules. Read it before changing behaviour.
