# FilmEx desktop upgrade plan

Status-tracking plan for turning FilmEx from a "server in Terminal, UI in a
browser tab" tool into a desktop film-library manager. This file is the
source of truth across sessions: tick boxes as tasks land, add notes under a
task when reality diverges, and update AGENTS.md at the end of every phase.

Decided 2026-10-26. Branch for the work: `desktop-upgrade`. Commit per task,
never push unless asked.

---

## 0. Decisions already made

These are settled. Do not reopen them in later sessions.

- **Shell**: pywebview window around the existing FastAPI server. The server
  runs as a thread inside the app process on a random loopback port with a
  per-launch token. One process, one Dock icon, Cmd+Q ends everything.
  No Terminal windows, no background processes, no daemons.
- **Packaging**: PyInstaller windowed build to `FilmEx.app`, DMG via
  `hdiutil`, built on GitHub Actions. Unsigned and unnotarized (no paid Apple
  account). README documents the Gatekeeper "Open Anyway" step and the
  quarantine-free `uv tool install` path. macOS first; Windows later if free.
- **Budget**: zero. No paid developer accounts, no Qt. PySide6 is LGPL and
  free but irrelevant: the HTML UI stays.
- **Persistence**: all sticky UI state and settings move from browser
  localStorage into a settings table in the SQLite DB.
- **One folder is one roll. Always.** `roll_id` stays the folder basename,
  one `roll_manifest.json` per folder, `Roll|<folder>` keyword unchanged.
  Splitting a mixed folder MOVES files into per-roll folders. There will be
  no multi-roll manifest and no per-file roll assignment.
- **Phase order**: 1 foundation, 2 library view, 3 naming system, 4 inbox
  import and split. Each phase ships usable on its own.
- **Default naming template**: `{date}-{camera}-{film}` with an optional
  `-{place}` suffix, matching the user's 2026 folders
  (`2026-05-14-Fed2-Gold`). Overridable in settings.
- **Import copies by default**; the source is left alone until verification
  passes. Deleting the source is a separate explicit action.
- **Hosting**: the repo lives on the user's Forgejo at git.dodies.lv; GitHub
  is a possible later mirror; Codeberg is ruled out. Forgejo has no hosted
  macOS runners, so releases are built locally with
  `packaging/build-macos.sh`. The Actions workflow is optional and only
  runs on a GitHub mirror or a self-hosted runner on the user's Mac.

## 1. What the real library looks like

Observed on the user's NAS share `/Volumes/homes/normis/Photos/Filma` (SMB,
likely Synology). These facts shaped the design; re-check them if the share
changes.

- Layout is `<root>/<year>/<roll folder>`. 44 folders at depth <= 2.
- 20 roll folders have manifests. 9 roll folders are untagged. 5 flat year
  folders (2008, 2009, 2010, 2011, 2014) hold 21 to 454 loose files directly
  with no roll folders at all.
- Three naming styles coexist: `2024-08_R2_F3_Gold_Laivas`,
  `2026-05-14-Fed2-Gold`, `EUR-03`.
- A 71-frame untagged folder (`2026/EUR-04`) is two rolls in one scan session.
  Frames are sequential camera names (`DSC09608.ARW` to `DSC09678.ARW`), so a
  "split here" cut on a sorted contact sheet is the right tool.
- Some folder names are stored NFD on the share (`Jāņos`). Opening them with
  an NFC literal fails. Rule: take names from directory listings, never
  retype them; create new names in NFC; normalize keywords to NFC.
- Mixed folders exist: `2026-06-C41-Fujica-Jāņos` has 38 ARW and 33 JPG.
  Same-basename pairs travel together; unmatched JPEGs are listed for the
  user to include or exclude. Overlap not yet verified.
- Tagged raw rolls have one `.xmp` per `.ARW` (replaced-extension form).
  Lab TIFF scans come with `.thm` siblings.
- Junk present: `.dtrash` (digiKam) at root, an `smbdelete*` leftover in a
  roll, `.DS_Store`. Skip hidden entries, `@eaDir`, `#recycle`, `._*`,
  `smbdelete*`.
- Manifest `apply_history` entries carry a `dry_run` flag. Tagged status must
  count only entries with `dry_run == false`.
- A shallow `find` over the whole share takes well under a second. The
  library view can scan live; the DB cache exists for display while the NAS
  is unmounted.

## 2. Target architecture

```
FilmEx.app (one process)
├── pywebview window (WKWebView)  ──loads──▶  http://127.0.0.1:<random>/?token=…
├── uvicorn thread: FastAPI app
│   ├── routes/        entities, workflow, health, settings, library, jobs,
│   │                  naming, imports
│   ├── services/      exiftool*, folder_scan, manifest, previews, keywords,
│   │                  settings, jobs, library, naming, fileops, importer
│   └── SQLite in Application Support/FilmEx/filmex.sqlite
└── job runner thread pool (apply, previews, rename, split, import)
```

Browser mode (`./filmex-run`) keeps working for development and Linux.
Everything desktop-specific is behind "is pywebview present" checks in
app.js and a `desktop.py` entry point; the server never imports pywebview.

Views in `index.html`: Library, Tag (the current workflow), Inbox, Settings.
Vanilla JS, imperative rendering, follow STYLE_MANIFEST.md.

---

## Phase 1: Foundation

Goal: `uv run filmex` opens a window with no Terminal, settings survive
relaunch, ExifTool is found when launched from Finder, long operations
survive a view change, and `uv run pytest` stays green.

### P1.1 Per-user data directory
- [x] `filmex/config.py`: resolution order `FILMEX_DATA_DIR` env, then
      platformdirs `user_data_dir("FilmEx")` when `sys.frozen` or when
      launched via the `filmex` script, else project `data/` for source runs.
      Keep `db_path()` / `ensure_data_dir()` signatures.
- [x] Add `platformdirs` to dependencies.
- [x] `preview_cache` follows automatically (it lives under the data dir).
- [x] Tests: env override wins; frozen resolves to platformdirs; source run
      resolves to project `data/`.

### P1.2 ExifTool resolution in one place
- [x] New `filmex/services/exiftool_locator.py`: `exiftool_command() ->
      list[str] | None`. Order: `exiftool_path` setting, `FILMEX_EXIFTOOL`
      env, `shutil.which`, candidates `/opt/homebrew/bin/exiftool`,
      `/usr/local/bin/exiftool`, `C:\Program Files\ExifTool\exiftool.exe`,
      then a bundled Perl distribution at `<bundle>/exiftool/exiftool` run
      through `/usr/bin/perl`. Cached per process; invalidated when the
      setting changes.
- [x] `services/exiftool.py` builder: argv[0] is hardcoded `"exiftool"` in
      three places (read, seed, edit). Make the builder take a command
      prefix; tests pass a fixed prefix so the builder stays pure.
- [x] `services/previews.py:163` raw preview extraction uses the same prefix.
- [x] `services/exiftool_runner.py`: use the locator; `exiftool_available()`
      stays as the public check.
- [x] `/api/health` reports resolved path and `exiftool -ver`.
- [x] Tests: locator order with monkeypatched PATH and candidates; builder
      with a two-element prefix.

### P1.3 Settings in the database
- [x] Migration v5: table `app_setting(key TEXT PRIMARY KEY, value_json TEXT
      NOT NULL, updated_at TEXT NOT NULL)`. Bump `CURRENT_SCHEMA_VERSION`.
- [x] Model `AppSetting`; `services/settings.py` with typed defaults:
      `library_root`, `recent_folders` (max 20), `theme`, `preview_invert`,
      `last_selection` (camera/lens/film ids + advanced block),
      `exiftool_path`, `naming_template`, `naming_place_suffix`,
      `import_mode` (`copy`|`move`), `window_geometry`.
- [x] Routes: `GET /api/settings`, `PATCH /api/settings` (partial merge,
      validated per key).
- [x] app.js: replace `loadStickyState`, `saveStickyState`, `recentFolders`,
      `pushRecentFolder` and the theme functions with API calls, debounced.
      On first run, import existing localStorage values once, then clear
      them.
- [x] Settings view gets a "General" tab: library root (with native picker),
      ExifTool path (with "detected: …" readout), theme, naming template
      (placeholder until phase 3).
- [x] Tests: defaults, partial patch, persistence across `reset_engine()`,
      unknown key rejected.

### P1.4 Loopback hardening
- [x] Middleware on `/api/*`: reject when `Host` is not loopback (DNS
      rebinding) and when an `Origin` header is present and not our own
      origin. Always on.
- [x] Desktop mode adds a per-launch token: the window opens
      `/?token=<t>` once, the server exchanges it for a `SameSite=Strict`
      cookie, and `/api/*` requests without the cookie are rejected. (Done
      as a cookie rather than a header so `<img>` thumbnails and streams
      need no JS changes.) Browser mode (`filmex-run`) has no token.
- [x] Tests: foreign Origin rejected, missing token rejected in desktop
      mode, normal requests pass.

### P1.5 Job runner
- [x] `services/jobs.py`: `JobManager` (thread pool, 2 workers; jobs flagged
      `exclusive` serialize so only one file-mutating job runs at a time).
      `Job`: id, kind, status (`queued`/`running`/`done`/`failed`/
      `cancelled`), events list, result, error, cancel flag. Job bodies get
      a context with `emit(event)` and `cancelled()`.
- [x] Routes: `GET /api/jobs/{id}`, `GET /api/jobs/{id}/events` (NDJSON:
      replays past events, then follows until terminal), `POST
      /api/jobs/{id}/cancel`, `GET /api/jobs` (recent).
- [x] Apply becomes a job. `_run_apply` is the body; cancellation is checked
      between files; the manifest is always written at the end with the
      files that were actually applied, even on cancel or failure. The
      existing `/api/metadata/apply/stream` becomes "enqueue + follow" so
      app.js changes are minimal. Keep the sync `/api/metadata/apply` for
      the CLI and tests.
- [x] Preview build becomes a job the same way.
- [x] app.js: a small `followJob(id, onEvent)` helper; reconnecting after a
      reload shows the replayed progress.
- [x] Tests: event replay, cancel between files, manifest written on cancel,
      exclusive serialization.

### P1.6 Desktop shell
- [x] Add `pywebview` dependency. `[project.scripts] filmex =
      "filmex.desktop:main"`.
- [x] `filmex/desktop.py`: pick a free port, generate token, run
      `uvicorn.Server` in a daemon thread, poll `/api/health` up to 10 s,
      create the window with saved geometry (min 1100x760), on `closed`
      save geometry and set `server.should_exit`, join the thread, exit.
- [x] JS API exposed to the page: `pick_folder()` via
      `window.create_file_dialog(FOLDER_DIALOG)`, `open_path(path)`,
      `reveal_path(path)`.
- [x] Drag and drop: subscribe to the document drop event on the Python
      side (pywebview exposes the dropped item's full path there, see its
      changelog; verify the field name against the installed version) and
      push the path into the page via `evaluate_js`. The existing uri-list
      hack in `setupWindowDrop` stays as the browser fallback.
- [x] app.js: when `window.pywebview` exists, Browse calls the JS API
      instead of `/api/folder/pick`.
- [x] Replace the seven `alert`/`confirm` call sites with a small in-page
      modal (also fixes browsers that suppress repeated dialogs).
- [x] Verify in WKWebView: NDJSON streaming via fetch, thumbnails, theme
      switch, settings persistence, quit behaviour, no lingering process.
      (Smoke mode checked page init through the cookie, the bridge, geometry
      save and clean shutdown; thumbnails and streams verified in a browser.)

### P1.7 Packaging and CI
- [x] `packaging/filmex.spec` (windowed, name FilmEx, icon
      `packaging/FilmEx.icns`, datas: `filmex/static`,
      `filmex/default_data.sqlite`, optional `packaging/vendor/exiftool`).
- [x] `packaging/fetch-exiftool.sh`: download the pinned
      `Image-ExifTool-<ver>.tar.gz` from exiftool.org, verify sha256,
      extract into `packaging/vendor/exiftool/` (gitignored).
- [x] `packaging/build-macos.sh`: pyinstaller, then `hdiutil create` DMG.
- [x] `.github/workflows/build.yml`: on tag `v*`, macOS arm64 job, upload
      the DMG as a release asset. Optional: only useful on a GitHub mirror
      or a self-hosted Forgejo runner on a Mac (same syntax under
      `.forgejo/workflows/`). Windows job deferred.
- [x] README: desktop install section, Gatekeeper "Open Anyway" and the
      `xattr -dr com.apple.quarantine` alternative, `uv tool install` path.
- [x] AGENTS.md: desktop entry point, data dir rules, settings table, job
      runner, loopback hardening.

**Phase 1 status (2026-10-26): complete.** Verified on the developer Mac:
`uv run filmex` and the frozen `dist/FilmEx.app` both pass smoke mode (page
initialised through the token cookie, bridge present, geometry saved, clean
shutdown, no leftover process); the frozen app found Homebrew's ExifTool
with a Finder-like empty PATH; settings, theme and recent folders survived
reloads in a browser; a preview build ran as a job and was cancelled from
the UI; 175 tests pass. Not yet exercised by a human: real drag-and-drop
onto the window and the native folder dialog (covered by unit tests only).
ExifTool is not bundled until `packaging/exiftool.lock` gets a checksum.

Acceptance for phase 1: window opens from `uv run filmex` with no Terminal;
Activity Monitor shows one FilmEx process that ends on quit; settings and
recent folders survive relaunch; ExifTool found when launched from Finder
with a Homebrew install; an apply continues after switching views; all
tests pass; `./filmex-run` browser mode still works.

---

## Phase 2: Library view

Goal: point at the library root and see every roll folder with its tagging
status, then tag the remainder of a partially tagged roll.

### P2.1 Library service
- [x] `services/library.py`: `scan_library(root)` shallow walk, depth <= 2,
      skip the junk list from section 1. Names come from `os.scandir`
      unmodified; a separate `display_name` is NFC.
- [x] Per folder: `image_count` by kind, `sidecar_count`, manifest summary
      (roll_id, film display, camera display, last real apply timestamp),
      `applied_set` = union of `applied_files` over history entries with
      `dry_run == false`, `untagged_files` = images minus applied_set,
      `status` in `tagged` | `partial` | `untagged` | `empty`,
      `is_flat_year` when the folder is a direct child of root and holds
      images itself.
- [x] Migration v6: cache table `library_folder(path PK, parent, name,
      depth, status, image_count, untagged_count, film_display,
      camera_display, last_applied_at, scanned_at)`.
- [x] Routes: `GET /api/library` (cached rows + `stale` flag + root
      reachability), `POST /api/library/rescan` (job), `GET
      /api/library/folder?path=` (details including untagged names).
- [x] Tests: tmp tree with tagged, partial, untagged, flat-year, NFD-named
      and junk folders; dry-run entries ignored.

### P2.2 Subset apply
- [x] `DryRunRequest.files: Optional[list[str]]`; when set, the pipeline
      restricts `ctx.files` to those names after validating they exist in
      the scan. Manifest history appends as usual.
- [x] Tests: subset apply touches only listed files; manifest union grows.

### P2.3 UI
- [x] New `library-view` as the start screen when a library root is set.
      Header nav: Library | Tag | Inbox | Settings.
- [x] Rows grouped by year: display name, status chip, counts, film,
      camera. Flat year folders labelled "unsorted" with counts.
- [x] Actions: Open (Tag view with folder set, scan, manifest prefill),
      Tag remaining (same, with the file subset preselected and the
      preview limited to it), Reveal in Finder.
- [x] Rescan button with job progress; stale banner when the NAS is
      unmounted.
- [x] AGENTS.md: library semantics, status derivation rule.

---

**Phase 2 status (2026-10-26): complete.** Verified against the user's NAS
copy in a browser: 36 folders indexed in about a second, 18 tagged, 2
partial, 13 untagged, 3 empty; "Tag remaining" opened a roll with manifest
prefill and a one-file subset, and the dry run returned exactly that file.
Header nav is Library | Tag (Inbox arrives with phase 4). 193 tests pass.

## Phase 3: Naming system

Goal: one folder naming convention, enforced on new folders and offered as a
reviewable batch rename for existing ones.

### P3.1 Short names and template
- [x] Migration v7: `short_name` (nullable) on `camera` and `film_stock`.
      Settings UI fields. Fallback when null: slugified display name.
- [x] `services/naming.py`: tokens `{date}` (YYYY-MM-DD), `{year}`,
      `{month}`, `{camera}`, `{film}`, `{lens}`, `{place}`, `{roll}` (per
      year sequence derived from existing names matching the template,
      zero-padded per setting). Slug rule: NFC, keep Unicode letters and
      digits, strip `/\:*?"<>|`, whitespace to `-`, collapse repeats.
      `render(template, context)` and `parse(template, name)` so existing
      names can be recognized.
- [x] Place/title lives on the roll: new optional `place` field in the
      manifest and in `DryRunRequest`; shown in the Tag view's advanced
      section.
- [x] Tests: render, slug edge cases incl. Latvian letters, roll sequence.
      (`parse` dropped: conformity is checked by rendering and comparing.)

### P3.2 Managed rename
- [x] `services/renames.py` (fileops for phase 4 will reuse its journal):
      propose / apply (same-parent `os.rename` only) / undo; refuses an
      existing target; every batch goes to the operation log.
- [x] Migration v8: `operation_log(id, kind, started_at, finished_at,
      status, plan_json, result_json, undone_at)`.
- [x] Rename updates the manifest `roll_id` and `folder_relative_path` and
      appends a `renamed_from` note to history.
- [x] Re-apply of the `Roll|` keyword after rename: done through the normal
      intent reconciliation (a new `Roll|…` path replaces the Roll family
      and keeps everything else), no new builder flags needed; runs inside
      the exclusive rename job.
      This replaces the "rename leaves the manifest stale by design" rule
      in AGENTS.md.
- [x] Routes: `POST /api/naming/propose` (root; list of old/new/reason/
      conflicts; folders without enough manifest data are listed as
      skipped), `POST /api/naming/apply` (selected proposals, job), `POST
      /api/operations/{id}/undo`.
- [x] UI: "Normalize names" in the Library view showing the full old/new
      list with checkboxes and conflicts, confirm, progress, undo of the
      last batch. Warning text: folders already in a Lightroom catalog
      will need the parent folder relinked once in Lightroom.
- [x] Tests: propose on the fixture tree, apply, undo, conflict refusal,
      keyword re-apply idempotent.

---

**Phase 3 status (2026-10-26): complete.** Verified in a browser on a
synthetic scratch library (not the NAS): proposals classified rename /
conforms / conflict / skipped with reasons, three folders renamed with
manifests updated and the `Roll|` keyword rewritten by real ExifTool, then
undone back to the original names and keyword. 197 tests pass. Short names
are not yet set on the user's real gear, so proposals against the NAS will
use slugged display names until they are.

## UI review pass (2026-10-26)

Done after phase 3, from a walk through every view in a browser:

- Film picker tiles and the Tag view film tile show an SVG 35 mm cassette
  glyph coloured from the brand palette (replaces the flat strip). This is
  the lightweight answer to "3D canisters": no WebGL, no dependency, works
  offline and in both themes. three.js stays an option only if a real 3D
  scene is ever wanted.
- Library: quick filter box; summary chips use the shared readout styling.
- After an apply: subset cleared, library rescanned in the background,
  "Back to Library" in the result.
- Settings ▸ General: live example of the folder name template; the server
  rejects invalid templates.
- Favicon (inline SVG) so the console stops logging a 404.

Ideas noted, not done: Dock progress and system notifications for long
applies (needs the desktop shell to expose them), a Lightroom-style frame
count hint on the contact sheet (phase 4), per-row "Rename" in the Library
(batch panel covers it for now).

## Phase 4: Inbox import and roll split

Goal: point at a temp folder of fresh scans (or an untagged library folder),
cut it into rolls on a contact sheet, tag each roll, and import into
`<root>/<year>/<generated name>` with copy-and-verify.

### P4.1 Contact sheet
- [x] Reusable grid of thumbnails for a folder (previews built via the
      existing job), sorted by name, with click, shift-click range, and
      "split here" markers that turn the sequence into segments.
      Done as `renderContactSheet()` in app.js: click, shift-click range,
      ⌘-click add; "Cut here" at the selected frame, "New roll from
      selection", "Leave out", per-roll "Add selection" and "Merge into
      roll N". Roll colours as a stripe and stamp on every frame.
- [x] Each segment is a roll draft: name preview from the template, gear
      and film tiles, advanced fields. Segments can be merged or removed.
      Name rendered server-side (`POST /api/naming/render`) from the
      roll's date, camera, film and place; editable, with "use template"
      to go back to automatic.
- [x] Same-basename raw+JPEG pairs move as a unit; unmatched JPEGs are
      listed with include/exclude toggles. `services/frames.py` groups
      images, both sidecar forms and `.thm` by basename; a JPEG-only
      frame in a folder that has raw frames is flagged and the toolbar
      offers "Leave out / Include N JPEG-only frames".
- [x] The Tag view shows the same sheet read-only under the scan summary
      (click opens the file) with "Split into rolls…" leading to the Inbox.

### P4.2 Split inside the library
- [x] `fileops.plan_split(folder, groups)`: target parent is the folder
      itself when it is a flat year folder, otherwise the folder's parent
      (so `2026/EUR-04` becomes two siblings under `2026`). Each file moves
      with its sidecar (both `.xmp` forms) and `.thm`. Same-volume rename
      only; cross-volume is refused with a clear message. Never overwrite.
      Remove the source folder only when it holds no images afterwards.
      A folder outside the library is cut into subfolders. An emptied
      source keeps its old manifest in the journal (restored by undo) and
      is removed only when nothing but desktop litter is left.
- [x] `POST /api/folder/split` returns the plan; `POST
      /api/folder/split/apply` runs it as an exclusive job and logs it for
      undo.
- [x] After the split, each new folder opens in the Tag view in turn, or
      the roll drafts' tagging is applied directly as part of the job.
      Chosen: tagging runs inside the job per new folder (manifest,
      keywords, `Roll|<new name>`), so the Library shows the rolls as
      tagged at once. Without ExifTool the files still move and the
      result says they were not tagged.
- [x] Tests: plan and apply on tmp tree with sidecars and pairs, EXDEV
      refusal (monkeypatch `os.rename`), undo, empty-source cleanup.

### P4.3 Inbox import
- [x] `services/importer.py`: per roll draft: resolve year from the roll
      date (fallback: earliest file date; else ask), create
      `<root>/<year>/<name>` (refuse if it exists), copy files, sidecars
      and `.thm` with `shutil.copy2` in chunks with progress, verify size
      and BLAKE2b hash per file, run the apply pipeline on the destination,
      write the manifest with `import_source {path, imported_at,
      verified}`, then if mode is `move` delete sources only after every
      file in the roll verified. Log the operation.
      The copy hashes the source bytes on the way out and then reads the
      destination back for the comparison; a mismatch removes the bad copy
      and stops that roll. Free space is checked in the plan.
- [x] Routes: `POST /api/import/plan`, `POST /api/import/apply` (job).
- [x] UI: Inbox view: source picker, contact sheet, segments, per-segment
      tagging, "Import N rolls", progress with per-file verification,
      results with Open and Reveal actions, undo.
      One Inbox view serves both flows: the Destination control picks
      "Library · copy", "Library · move" or "Split in place". The Library's
      "Split…" row action and the Tag view's link open it preset to split.
- [x] Tests: copy+verify detects a corrupted destination, collision stops
      before any copy, move deletes only after verification, manifest
      carries `import_source`.

### P4.4 Wrap-up
- [x] AGENTS.md: inbox and split semantics, file operation rules, undo log.
- [x] README: library, naming and import sections. Screenshots not added:
      the only material at hand was synthetic scratch data; take them from
      the real library.

---

**Phase 4 status (2026-10-26): complete.** The apply pipeline moved from
the workflow routes into `services/tagging.py` so jobs can tag a folder.
Verified in a browser against a scratch inbox (19 frames: 4 DNG+JPEG pairs,
one JPEG without raw, 14 lab JPEGs) and the synthetic scratch library,
never the NAS: cut into two rolls, imported by copy with real ExifTool
tagging (sidecars written for the DNGs), undone cleanly; split in place
into two subfolders, undone cleanly. First undo attempt left a folder
behind because the tagging step had written new sidecars; both undo paths
now carry those sidecars (back with the raw on a move or split, discarded
with the copy). 219 tests pass.

Ideas noted, not done: reconnect to a running import after a page reload
(the job log supports it, the Inbox view does not use it yet); a frame
count hint ("36 of 36") per roll card; drag-and-drop between roll cards.

## UI restructure (2026-10-27)

From a conceptual review of the four users (one roll in place; many rolls
into a new library; a seasoned library user importing a card; a pro
splitting big scan sessions):

- [x] Folder-first start. Home (no library yet): drop zone, Open folder,
      recent folders, "choose its folder". Library is home once a library
      exists, with Open folder in the header.
- [x] One Folder screen replaces the Tag and Inbox views: contact sheet,
      one roll card per roll (the folder starts as one), gear tiles, date
      and place, a More expander for providers and notes, a Changes
      expander with the dry run and per-file overrides.
- [x] Destination control at the end: Keep here / Copy to library / Move
      to library, preselected by where the folder is, with one red action
      whose label says what will happen. "Split in place" is Keep here
      with two or more rolls.
- [x] Add gear from inside the picker.
- [x] Settings: Preferences first, then Gear; import mode moved to the
      Folder screen; library actions behind a menu.
- [ ] Cut suggestions for the pro user: time gaps as suggested cuts with a
      frames-per-roll rhythm, the seam frames shown side by side, filename
      counter resets and aspect changes as near-certain cuts, keyboard.

Server additions: `as_roll` on the dry run (preview the Roll keyword under
the folder name a roll is about to get), per-file overrides on roll
drafts. Verified in a browser on scratch folders: first run through to a
write, copy to library and undo, split in place and undo, add a camera
from the picker, tag-remaining subset, settings groups, paper theme.

## Cross-cutting rules

File operations:
- Copy, verify, then tag at the destination. The source is untouched until
  verification passes.
- Moves only within one volume (atomic rename). Cross-volume is a copy plus
  verify, never a move.
- Never overwrite. A collision stops the whole batch before anything runs.
- Every batch shows its full operation list first and runs only after
  confirmation. Every executed batch writes an undo record.
- Only one file-mutating job at a time.
- Skip hidden files, AppleDouble files and the junk list. Never copy them.

Unicode and SMB:
- Use names from listings as-is for paths. Display and keywords in NFC. New
  folder names in NFC.
- Tolerate `smbdelete*` and `@eaDir` entries without failing a scan.

Code conventions:
- Vanilla JS, no framework, no build step. New views are `<main>` siblings
  in `index.html` toggled by the header nav.
- Server never imports pywebview. Desktop-only behaviour lives in
  `desktop.py` and behind `window.pywebview` checks in app.js.
- Every phase keeps `uv run pytest` and `uv run ruff check .` green and
  keeps browser mode working.

## Open items

- Drop-event field name verified as `pywebviewFullPath` (pywebview 6.2.1); the
  shell forwards the folder (or a dropped file's parent) into the page.
- Pin the ExifTool tarball checksum in `packaging/exiftool.lock` so the
  bundle can carry its own ExifTool.
- Windows build: only after macOS is stable, and only if WebView2 plus
  the official ExifTool Windows build work unattended on a free runner.
- Mixed raw+JPEG overlap in the Jāņos folders: check once the NFD path
  handling exists.
