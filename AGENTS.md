# AGENTS.md

Working notes for humans and LLMs contributing to FilmEx — what the project is,
how to run it, the conventions data must follow, and how to add gear/film
entries in bulk from an LLM session.

---

## Project

**FilmEx** is a local-first browser tool for tagging folders of film scans
before Lightroom import. It is not a DAM, not an editor, and not a cloud
service. Its only job is:

1. let the user pick a folder of scans
2. choose analog camera, lens, and film from controlled vocabularies
3. write metadata safely via ExifTool (XMP sidecars for raw, embedded for JPG/TIFF)
4. leave a `roll_manifest.json` next to the images

Stack: FastAPI + SQLModel + SQLite, served at `localhost`. ExifTool runs as a
subprocess on the same machine as the files. Python 3.11+. Managed with `uv`.

---

## Run, test, lint

```bash
uv sync                  # first time only
uv run filmex            # desktop window (pywebview), no Terminal needed
./filmex-run             # browser mode: macOS/Linux (Windows: filmex-run.cmd)
uv run pytest            # run the test suite
uv run ruff check .      # lint
packaging/build-macos.sh # FilmEx.app + DMG (see packaging/)
```

Browser mode binds `127.0.0.1:8765` by default (override with `FILMEX_PORT`).
The desktop shell picks a random loopback port and a per-launch token.
`FILMEX_SMOKE_SECONDS=8 uv run filmex` opens the window, checks the page
initialised, and closes it: use it to verify a build without a human.

The phased plan for the desktop upgrade lives in `docs/upgrade-plan.md`.
Tick tasks there as they land.

---

## Project layout

```
filmex/
├── filmex/
│   ├── main.py               # FastAPI app factory (+ loopback guard, token exchange)
│   ├── desktop.py            # `filmex` entry point: server thread + pywebview window
│   ├── security.py           # LoopbackGuard middleware (Host/Origin/token)
│   ├── db.py                 # engine, session, bootstrap from default_data.sqlite
│   ├── config.py             # data dir resolution (env / checkout / per-user)
│   ├── migrations.py         # PRAGMA user_version app-managed migrations
│   ├── models/               # SQLModel tables (+ app_setting)
│   ├── schemas/              # Pydantic request/response DTOs
│   ├── routes/               # entities, workflow, health, settings, jobs,
│   │                         # library, naming, fileops (split, import)
│   ├── services/             # exiftool (+locator), keywords, folder scan,
│   │                         # manifest, previews, settings, jobs, tagging,
│   │                         # library, naming, renames, frames, fileops,
│   │                         # importer, operations
│   ├── static/               # vanilla-JS single-page UI
│   ├── cli.py                # `python -m filmex.cli` — batch entity add / export
│   └── default_data.sqlite   # bundled starter DB; copied to the data dir on first run
├── packaging/                # PyInstaller spec, icon generator, build + vendoring scripts
├── docs/upgrade-plan.md      # phased plan with status checkboxes
├── data/                     # gitignored — checkout's live DB + runtime state
├── examples/
│   └── entities-batch.json   # sample batch for the CLI
├── scripts/
│   └── build_default_db.py   # regenerates the bundled starter DB
└── tests/
```

The SQLite file in the data directory is the single source of truth at
runtime. Edits to `filmex/default_data.sqlite` only affect fresh installs.

Data directory resolution (`filmex/config.py`): `FILMEX_DATA_DIR` if set;
otherwise `<checkout>/data` when running from a source checkout; otherwise
the platform per-user directory (`~/Library/Application Support/FilmEx` on
macOS) for the packaged app and `uv tool install`ed copies.

---

## Data model

All entities are soft-archived, never hard-deleted. Archived rows stay
queryable so past roll manifests remain meaningful.

### Camera
- `make`, `model`, `display_name`
- `mount` — string, nullable. For built-in lens bodies use `"Fixed-Lens"`.
- `fixed_lens_id` — FK to Lens. When set, the lens picker auto-fills and locks.
- `camera_type` — free text; UI uppercases common values (`slr` → `SLR`).
- `aliases`, `notes`, `favorite`, `archived`.

### Lens
- `make`, `model`, `display_name`
- `lens_model_write_value` — string written to EXIF `LensModel`. Usually equals `display_name`.
- `focal_length_mm` — integer, optional. Null for zoom lenses (range lives in the display name).
- `max_aperture` — float, **strictly > 0** when set.
- `mount` — **required**, and must match an existing camera's `mount`.
- `aliases`, `notes`, `favorite`, `archived`.

### FilmStock
- `brand` — string that **must match an existing `FilmBrand.name`** (case-insensitive).
  The API rejects unknown brands; the UI only offers a dropdown of registered
  brands. Create the brand row first (see FilmBrand), then the stock.
- `name`, `display_name`
- `box_speed_iso` — integer
- `film_type` — one of `color_negative`, `bw_negative`, `slide`, `other`
- `process_type` — one of `c41`, `bw`, `e6`, `ecn2`, `other`
- `aliases`, `notes`, `favorite`, `archived`.

### FilmBrand
First-class row that owns the two-color palette used by the film-picker tile's
bookmark strip. Extracting this out of FilmStock means adding another Kodak
stock automatically inherits Kodak's colors — edit the brand once.

- `name` — canonical brand name, case-insensitive unique. Renaming a brand
  cascades into `film_stock.brand` for all its stocks.
- `color_primary`, `color_secondary` — optional hex strings (`#bb9d70`,
  `#292825`). Pick a dominant and an accent color drawn from the physical
  film-box design. Accepted input: `#rrggbb`, `#rgb`, or the same without the
  leading `#`; normalized to lowercase `#rrggbb`. When both are null the tile
  falls back to a neutral gray.
- No `favorite` or `archived` — a brand is either a real brand worth keeping
  or a typo worth renaming.

Seeded on first install: Kodak, Kodak Eastman, Ilford, Kentmere, Fujifilm,
Foma, Fomapan, Agfa, Harman, CineStill, Rollei, Lomography. The migration path backfills a row for every distinct
`film_stock.brand` string found in existing databases and repairs incomplete
brand backfills on already-upgraded DBs. Known brands get pre-populated
palettes; unknown brands land with null colors (edit in Settings → Film
brands).

### AppSetting
Key/value table (`app_setting`) holding every persisted setting and the UI's
sticky state: library root, recent folders, last folder and selection, theme,
preview inversion, ExifTool path, naming template, import mode, window
geometry. Typed defaults and validation live in `services/settings.py`
(`SPECS`); unknown keys are rejected. Add a key there, never ad hoc. Nothing
user-facing is stored in browser `localStorage` any more (only a theme hint
for the first paint).

### Provider
Single table for labs and self-service providers. `kinds` is a set-ish JSON
list with at least one of `"development"`, `"scan"`. One row, one rename.

### DevelopmentMethod / ScanMethod
Coarse labels only (`C-41`, `BW`, `E-6`, `ECN-2` / `Lab scan`, `DSLR capture`,
`Flatbed`). Per-roll chemistry detail lives in the manifest `notes` field, not
as separate method rows.

---

## Nomenclature

Conventions that the UI, the database, and the CLI all assume. Violating them
produces confusing duplicates and broken filtering.

### Display-name format

- **Camera**: `Make Model` → `Pentax K1000`, `Minolta X-700`
- **Lens**: `Make Mount? Focal Aperture` → `Minolta MD 50mm f/1.7`, `Canon FD 28-80mm f/3.5-5.6`
- **Film**: `Brand Line [Variant] ISO` → `Kodak Portra 400`, `Ilford HP5 Plus 400`, `Fujifilm Superia X-TRA 400`

### Film brand colors

The two-color palette lives on the `FilmBrand` row, not on each film stock.
The UI renders it as a small SVG film cassette (`canisterSvg()` in app.js:
primary = label band, secondary = accent stripe).
Pick a dominant and an accent color drawn from the physical box — typically
the cardboard / label color plus the brand's signature tone. The picker tile
renders them as a vertical bookmark strip on the left edge.

- Hex strings (`#bb9d70` / `bb9d70` / `#ba9`). The CLI and API normalize to
  lowercase `#rrggbb`.
- Leave both null for a neutral gray fallback.
- Pick accessible contrast between the two — two very similar tones look like
  a bug. The CSS already adds a subtle separator, but don't rely on it.
- To add a new brand with its colors in one step, include a `film_brands`
  entry in the same CLI batch as the film stocks that reference it. The CLI
  inserts brands before stocks so references resolve in one pass.

### Mounts are the source of truth for compatibility

Lens `mount` must exactly match some camera's `mount`. The UI offers the list
of existing camera mounts when adding a lens — no free-text entry. If you're
adding a lens for a mount that has no camera yet, add the camera in the same
batch (or first).

Special value: `"Fixed-Lens"` — used on both the camera and its built-in lens.

### Aliases catch duplicates

Film stocks especially: `Kodak Gold 200` vs `Gold 200` vs `Kodak GB 200` —
same film. Put the nicknames in `aliases` on a single row rather than
creating new rows.

### shot_at_iso and push/pull

`box_speed_iso` is the nominal rating. Per-roll pushing/pulling is recorded on
the roll, not on the film stock. Don't create separate `Portra 400 @ 1600`
film stock rows.

---

## Runtime: shell, jobs, guard, ExifTool

- **Desktop shell** (`filmex/desktop.py`): one process. uvicorn runs in a
  daemon thread on a random loopback port; pywebview shows the page; closing
  the window stops the server. The server never imports pywebview; desktop
  behaviour lives in `desktop.py` and behind `window.pywebview` checks in
  `app.js` (`pickFolder()`, `window.filmexDesktop.onDrop`).
- **Loopback guard** (`filmex/security.py`): `/api/*` rejects non-loopback
  `Host` headers (DNS rebinding) and foreign `Origin` headers (cross-site
  posts). In desktop mode the window opens `/?token=…` once; the server
  swaps it for a `SameSite=Strict` cookie that every API call must carry.
  Tests must use a loopback `base_url`.
- **Jobs** (`services/jobs.py`): long operations (apply, preview build,
  library scan, rename, split, import, undo) run as in-process jobs with an
  ordered event log. The protocol is the job runner's, not each endpoint's:
  `start` on submission (plus the `start=` fields the route passes), body
  events (`progress`), then exactly one `done` (the body's return value)
  or `error`, both emitted by the manager. A body returns its result and
  never emits `start`, `done` or `error`. Every streaming endpoint returns
  `routes.jobs.stream(job)`; `GET /api/jobs/{id}/events?from_seq=` replays
  and follows; `POST /api/jobs/{id}/cancel` requests a stop that bodies
  check between files. In the browser, `runJob()` in `app.js` is the
  only reader of the stream. Jobs that mutate the user's
  files are `exclusive` and serialize. An apply that is cancelled or fails
  still writes its manifest with the files actually applied.
- **ExifTool** (`services/exiftool_runner.py`): callers import the module
  and use `available()`, `managed_tags()`, `preview()` and `write()`;
  `write()` reads, merges, writes and returns the changes or the error.
  Nothing else builds argv, picks raw vs sidecar or decodes stderr. Discovery
  (`services/exiftool_locator.py`): setting → `FILMEX_EXIFTOOL` → `PATH` →
  Homebrew/system candidates → bundled copy. Never spawn a bare
  `"exiftool"`.
- **Dialogs**: no `alert`/`confirm`. Use `notify()` / `ask()` in `app.js`.
- **Tagging pipeline** (`services/tagging.py`): `build_pipeline(request,
  session)` resolves ids into snapshots and intents, `preview_all` is the
  dry run, `apply_all` writes file by file and the manifest at the end. The
  workflow routes wrap it for HTTP; the split and import jobs call it
  directly on each new folder. Validation failures raise `TaggingError`
  with a status code.
- **Folder contents** (`services/frames.py`): the only code that lists a
  roll folder. It owns image extensions, source kinds, the junk rules
  (hidden files, `._*` AppleDouble, `smbdelete*`, `@eaDir`, `#recycle`,
  `Thumbs.db`), the litter a cleanup may delete, frame grouping and the
  `.thm` lookup. The Tag view scan, thumbnails, Library index, contact
  sheet, split and import all read folders through it.

## Naming system

`services/naming.py` renders roll folder names from the template in
Settings ▸ General (default `{date}-{camera}-{film}`, optional `-{place}`
suffix). Tokens: `{date} {year} {month} {camera} {film} {lens} {place}
{roll}`. `{camera}` and `{film}` use the entity's `short_name` when set
(`F3`, `Gold`), else the slugged display name. Slugs keep Unicode letters,
drop characters illegal in file names, turn whitespace into `-`, and are
NFC. `{roll}` numbers rolls per year in date order among the proposals.

`services/renames.py` proposes names for tagged rolls (untagged folders
and loose year folders are skipped), applies the chosen renames in place
(same parent, never over an existing target), fixes the manifest, swaps
the `Roll|` keyword through the normal reconciliation, journals the batch
in `operation_log`, and can undo it. Renames and undos are exclusive jobs.

## Library view

`services/library.py` indexes the library root (Settings ▸ General) with a
shallow walk: `<root>/<year>/<roll>`. Rules that must hold:

- A folder's **tagged set** is the union of `applied_files` over manifest
  `apply_history` entries with `dry_run == false`. Dry runs never count.
- **Status**: no images → `empty`; no manifest or nothing applied →
  `untagged`; some files applied → `partial`; all applied → `tagged`. A
  depth-one folder holding images itself is a flat year folder
  (`is_flat_year`, shown as "unsorted").
- Names are taken from the directory listing as-is (SMB shares store some
  NFD); `display_name` is NFC. Never retype a path from its display name.
- Junk is skipped everywhere: hidden entries, `@eaDir`, `#recycle`, `._*`
  AppleDouble files, `smbdelete*`, `Thumbs.db`. The rules live in
  `services/frames.py`; never list a roll folder any other way.
- Rows are cached in `library_folder` (per root) so the view opens
  instantly and survives an unmounted share; `POST /api/library/rescan`
  replaces the cache in one transaction as a job.
- Dry-run and apply accept `files: [...]` to restrict to a subset; names
  are validated against the scan. The UI shows the subset as a chip.

## Inbox: split and import

`services/frames.py` turns a folder into **frames**: every file sharing a
basename (`DSC0001.ARW`, `DSC0001.JPG`, `DSC0001.xmp` or the legacy
`DSC0001.ARW.xmp`, `DSC0001.thm`). Frames are the unit of every move and
copy; a frame never splits. Files that belong to no frame (the manifest,
notes, orphan sidecars) stay where they are and the plan says so. In a
folder with raw frames, a JPEG-only frame is flagged `unmatched_jpeg` so
the user can leave camera JPEGs behind.

A **roll draft** (`fileops.Segment`) is a folder name, a list of frame
keys and the tagging fields of `DryRunRequest` minus folder and files. The
Inbox view builds drafts on a contact sheet; the server never trusts them:
`resolve_segments` re-checks names, duplicates and frame membership, and
both apply paths re-plan right before running.

**Split** (`services/fileops.py`): a roll folder under `<root>/<year>/`
becomes siblings in that year folder; a flat year folder or a folder
outside the library gets subfolders. Rules: same volume only
(`os.rename`), never overwrite, a collision anywhere stops the whole
batch, frames move with sidecars and `.thm`, each new folder is tagged by
the pipeline inside the same exclusive job (`Roll|<new name>` replaces the
old Roll keyword through the normal reconciliation). When the source holds
no images afterwards, its manifest goes into the journal and the folder is
removed only if just desktop litter (`.DS_Store`, `._*`, `Thumbs.db`) is
left.

**Import** (`services/importer.py`): per roll, `<root>/<year>/<name>` with
the year from the roll date, else the oldest file. Order: create the
folder (refuse if it exists), copy in chunks hashing the source, read the
destination back and compare size and BLAKE2b, tag at the destination,
write `import_source {path, imported_at, verified, mode, files[name, size,
blake2b]}` into the manifest, and only in move mode delete the sources,
only after every file of that roll verified. A verification failure
removes the bad copy and stops that roll; later rolls are not started.
Free space is checked in the plan.

**Undo** (`operation_log`, kinds `rename`, `split`, `import`): `POST
/api/operations/{id}/undo` dispatches by kind. Split undo moves every
frame back, restores the stashed manifest and removes the created folders.
Import undo copies moved files back (verified against the recorded hash)
or removes copies once the source is confirmed present. Sidecars that the
tagging step wrote travel with their raw file on a move and are discarded
with a copy. Metadata written by the batch stays on the files; re-tagging
the folder replaces it.

Routes: `GET /api/folder/frames`, `POST /api/naming/render`, `POST
/api/folder/split` (plan), `POST /api/folder/split/apply` (job), `POST
/api/import/plan`, `POST /api/import/apply` (job). Plan failures answer
`400 {"detail": {"errors": [...]}}`.

## Metadata write policy

The app owns a **whitelist** of EXIF/XMP fields and touches nothing else:

- `Make`, `Model`, `LensModel` — silently overwritten on re-apply. (Lightroom
  does not let you hand-edit these, so there's no user edit to conflict with.)
- `Subject` / `Keywords` (flat) + `HierarchicalSubject` (tree) — merged
  without duplicates. Existing non-managed keywords are preserved.
- `Description` / `Caption-Abstract` — used for roll notes; overwritten on
  re-apply when the user supplies roll notes.
- `DateCreated` — only when the user explicitly supplies a capture date.

### By file type

- **ARW / DNG**: never modify the raw. Write an XMP sidecar next to it using
  the **replaced-extension** form (`DSC08930.ARW` → `DSC08930.xmp`) — that's
  what Lightroom and Bridge look for. Legacy FilmEx builds wrote the appended
  form (`DSC08930.ARW.xmp`); if one is on disk we keep editing it in place
  instead of silently seeding a second sidecar beside it. Merge with any
  existing sidecar content; do not wipe Lightroom edits. Roll notes land in
  `XMP-dc:Description` only for sidecars.
- **JPG / TIFF**: embed into the file. Preserve other tags.
  Roll notes are written to both `XMP-dc:Description` and
  `IPTC:Caption-Abstract`.

Re-apply must be idempotent — same inputs produce no duplicate keywords.

### Preview-time file overrides

The dry-run preview may add per-file lens overrides and extra flat keywords.
Lens overrides replace `LensModel` and the `Lens|...` hierarchical keyword for
that file only. Extra keywords are merged with the auto-generated keyword set,
de-duplicated, and stored in the manifest under `file_overrides` so re-apply
stays stable.

---

## Keyword taxonomy

Hierarchical keywords use `|` as the delimiter (Lightroom's
`HierarchicalSubject` convention). The flat `Subject` / `Keywords` tags get
leaf names; the hierarchical tag gets the full path.

Canonical paths:
- `Film|<display_name>` — e.g. `Film|Kodak Portra 400`
- `FilmType|<Color Negative | BW Negative | Slide | Other>`
- `Development|Provider|<name>`
- `Development|Method|<C-41|BW|E-6|ECN-2>`
- `Scan|Provider|<name>`
- `Scan|Method|<Lab scan|DSLR capture|Flatbed>`
- `Scan|Camera|<display_name>`
- `Scan|Lens|<display_name>`
- `Roll|<folder basename>`

Optional, off by default:
- `Camera|<display_name>`
- `Lens|<display_name>`

---

## Storage

- `data/` (gitignored) — user-owned runtime state: `filmex.sqlite` plus future
  assets. Portable: zip and move to another machine with `uv`.
- `filmex/default_data.sqlite` — bundled fresh-install seed, regenerated by
  `scripts/build_default_db.py`. Keep it generic (no private labs, no
  user-specific gear).
- One `roll_manifest.json` per roll folder. The folder basename is the
  `roll_id`. Rename folders through the app (Library ▸ Normalize names or
  the naming API), never by hand: a managed rename updates `roll_id` and
  `folder_relative_path`, records the change under `renames`, and rewrites
  the `Roll|…` keyword on every tagged file. A hand rename leaves all of
  that stale.

---

## Adding entities from an LLM session

For bulk edits, skip the UI. Hand your LLM this file plus
`examples/entities-batch.json` and ask it to produce a JSON batch. Review
the JSON, then apply it:

```bash
uv run python -m filmex.cli add-from-json path/to/batch.json
uv run python -m filmex.cli add-from-json path/to/batch.json --dry-run
uv run python -m filmex.cli add-from-json path/to/batch.json --upsert-film-data
```

The CLI is **idempotent**: rows matched by natural key are skipped with a log
line. Safe to re-run.

`--upsert-film-data` is special-case maintenance mode for film libraries: it
updates existing `film_brands` and `film_stocks` instead of skipping them, so
palette tweaks or stock-name corrections can be applied in bulk without
touching camera/lens/provider rows.

### Natural keys

- Camera: `(make, model)` — case-insensitive
- Lens: `(make, model)` — case-insensitive
- FilmStock: `(brand, name)` — case-insensitive
- FilmBrand: `(name)` — case-insensitive
- Provider / DevelopmentMethod / ScanMethod: `(name)` — case-insensitive

### Batch schema

```json
{
  "cameras":             [ /* CameraCreate-ish dicts */ ],
  "lenses":              [ /* LensCreate dicts */ ],
  "film_brands":         [ /* { "name": "...", "color_primary": "#...", "color_secondary": "#..." } */ ],
  "film_stocks":         [ /* FilmStockCreate dicts — brand must exist in film_brands */ ],
  "providers":           [ /* ProviderCreate dicts */ ],
  "development_methods": [ /* { "name": "..." } */ ],
  "scan_methods":        [ /* { "name": "..." } */ ]
}
```

Field shapes match the Pydantic `*Create` classes in `filmex/schemas/entities.py`.

### Fixed-lens cameras

Cameras with a built-in lens (Chaika, Yashica Electro, disposables) need the
lens row created first, then the camera linked to it. The batch format
accepts `fixed_lens_ref: { "make": "...", "model": "..." }` on a camera to
resolve the link after lenses are inserted:

```json
{
  "cameras": [
    {
      "make": "Yashica",
      "model": "Electro 35 GSN",
      "display_name": "Yashica Electro 35 GSN",
      "mount": "Fixed-Lens",
      "camera_type": "rangefinder",
      "fixed_lens_ref": { "make": "Yashica", "model": "Yashinon DX 45mm f/1.7" }
    }
  ],
  "lenses": [
    {
      "make": "Yashica",
      "model": "Yashinon DX 45mm f/1.7",
      "display_name": "Yashica Yashinon DX 45mm f/1.7",
      "lens_model_write_value": "Yashica Yashinon DX 45mm f/1.7",
      "focal_length_mm": 45,
      "max_aperture": 1.7,
      "mount": "Fixed-Lens"
    }
  ]
}
```

The CLI inserts in this order, so cross-references resolve in one pass:
1. Providers, development methods, scan methods
2. Film brands, then film stocks (stocks validate their brand exists)
3. Cameras (without `fixed_lens_id`)
4. Lenses (mount validated against the cameras just inserted)
5. Camera `fixed_lens_id` links patched from `fixed_lens_ref`

### What the CLI will *not* do

- overwrite existing rows (use the UI or the REST API for edits)
- delete rows
- touch file metadata (that's the roll-apply flow)

---

## Testing

`tests/` runs against a throwaway SQLite DB per test via `FILMEX_DATA_DIR`.
Fixtures in `conftest.py` seed a minimal camera + lens through the public API
so route tests don't duplicate setup. Tests that would write metadata take
the `fake_exiftool` fixture, which replaces ExifTool at the runner's seam
for every caller; never monkeypatch ExifTool functions in other modules.

Must stay covered:
- ExifTool argument builder (sidecar vs embed, merge semantics, command prefix)
- ExifTool locator resolution order
- Settings defaults, validation, persistence across engine reset
- Loopback guard (foreign Host / Origin rejected, token cookie exchange)
- Job runner (replay, cancel, exclusivity) and manifest written on a
  cancelled or failed apply
- Idempotent re-apply (no duplicate keywords)
- Existing XMP sidecar merge
- Archive flag filtering
- Manifest create / update / history
- Folder scan source-kind detection and scan-tier derivation
- Frames: pairs, both sidecar forms, `.thm`, unmatched JPEG flag, and every
  folder view (scan, thumbnails, Library, sheet) skipping the same junk
- Job protocol: start, body events, one terminal event, reserved types
- Split: layout (siblings vs subfolders), collisions, moves with
  companions, cross-volume stop with a partial undo, emptied-source
  cleanup, undo including tagging sidecars
- Import: copy-and-verify catches a corrupted destination, collision stops
  before any copy, move deletes sources only after the roll verified,
  `import_source` in the manifest, undo in both modes
- CLI batch idempotency

Have at least one integration test that uses real ExifTool if it's on PATH.

---

## Non-goals

No image editing. No color inversion. No scanner control. No tethered
capture. No cloud sync. No replacing Lightroom. No AI tagging. No mobile
support. No browser extension.
