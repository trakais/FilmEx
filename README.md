# FilmEx

FilmEx is a local-first browser tool for tagging film scan folders before import into Lightroom or other DAM software.

It is not a catalog, not an editor, and not a cloud service.

## What It Does

- scans a single film-roll folder
- detects supported files: `JPG`, `TIFF`, `DNG`, `ARW`
- lets you choose analog camera, lens, and film stock
- adds development and scan metadata
- shows a dry-run preview before writing anything
- writes metadata with ExifTool
- creates a `roll_manifest.json` next to the images

For raw files (`DNG`, `ARW`), FilmEx writes to XMP sidecars.

For `JPG` and `TIFF`, FilmEx writes metadata into the file itself.

## Main Workflow

1. Choose one folder containing a single film roll.
2. Scan the folder.
3. Pick camera, lens, and film.
4. Add optional development / scan info.
5. Preview the metadata changes.
6. Apply the changes.

## Requirements

- Python 3.11+
- `uv`
- `exiftool` available on `PATH`

## Run Locally

```bash
uv sync
uv run uvicorn filmex.main:app --reload --port 8765
```

Then open:

```text
http://127.0.0.1:8765
```

## Notes

- The app stores runtime data in `data/`.
- A starter SQLite database is bundled with sample cameras, lenses, films, and methods.
- Your own working database lives in `data/filmex.sqlite`.
- Temporary and private local files should stay in `tmp/`, which is ignored by git.

## Testing

```bash
uv run pytest
```
