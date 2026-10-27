// FilmEx — vanilla JS controller.
//
// Three screens: Home (until a library folder is set), Library, and the
// Folder screen, which is where any folder of scans is cut into rolls,
// tagged, and kept where it is or copied or moved into the library. State
// lives on `state`; rendering is imperative. The browser side is a thin
// veneer over the FastAPI endpoints, which own the real logic.

const DEFAULT_PAGE_TITLE = document.title;

const state = {
  settings: null,          // server-side settings cache (`/api/settings`)
  view: "home",
  viewInitialised: false,
  viewBeforeSettings: null,
  library: null,           // last `/api/library` payload
  libraryFilter: "",
  naming: null,            // last `/api/naming/propose` payload
  namingLastOperation: null,
  libraryRescanning: false,
  theme: "dark",
  previewInvert: false,
  // Last used gear and processing fields: the defaults for a new roll.
  camera: null,
  lens: null,
  film: null,
  advanced: {
    development_provider_id: "",
    development_method_id: "",
    scan_provider_id: "",
    scan_method_id: "",
  },
  folder: {                // the open Folder screen
    path: "",
    listing: null,         // `/api/folder/frames`
    frames: [],
    indexByKey: null,
    scanFiles: new Map(),  // name → `/api/folder/scan` file (thumbnail flags)
    thumbs: new Set(),     // image files with a thumbnail for the current invert mode
    thumbJob: false,
    manifest: null,        // existing roll manifest, if any
    inLibrary: false,
    rolls: [],
    selection: new Set(),
    anchor: null,
    cuts: { report: null, loading: false, ignored: new Set() },  // suggested cuts
    destination: null,     // "keep" | "copy" | "move"
    running: false,
    lastOperation: null,
    result: null,
  },
  // Caches of entity lists
  cameras: [],
  lenses: [],
  films: [],
  brands: [],
  providers: [],
  devMethods: [],
  scanMethods: [],
};

// ---------- Persistence ----------
//
// Sticky state lives on the server (`/api/settings`) so it survives browser
// changes, reinstalls, and the move to the desktop shell. localStorage keeps
// only a theme hint for the first paint. Writes are coalesced into one PATCH.

const THEME_HINT_KEY = "filmex.themeHint.v1";
const LEGACY_KEYS = {
  state: "filmex.state.v1",
  recent: "filmex.recentFolders.v1",
  theme: "filmex.theme.v1",
};
const RECENT_FOLDERS_LIMIT = 20;

let pendingSettingsPatch = null;
let settingsFlushTimer = null;

function queueSettingsPatch(partial) {
  pendingSettingsPatch = { ...(pendingSettingsPatch || {}), ...partial };
  if (state.settings) Object.assign(state.settings, partial);  // optimistic
  clearTimeout(settingsFlushTimer);
  settingsFlushTimer = setTimeout(flushSettingsPatch, 250);
}

async function flushSettingsPatch() {
  const patch = pendingSettingsPatch;
  pendingSettingsPatch = null;
  if (!patch) return;
  try {
    state.settings = await api("/api/settings", { method: "PATCH", body: JSON.stringify(patch) });
  } catch (e) {
    console.warn("Settings save failed", e);
  }
}


async function loadSettings() {
  state.settings = await api("/api/settings");
  await migrateLegacyLocalStorage();
  const s = state.settings;
  const sel = s.last_selection || {};
  Object.assign(state.advanced, sel.advanced || {});
  state.camera = sel.camera || null;
  state.lens = sel.lens || null;
  state.film = sel.film || null;
  state.previewInvert = !!s.preview_invert;
  applyTheme(s.theme, { persist: false });
}

async function migrateLegacyLocalStorage() {
  let patch = {};
  try {
    const raw = localStorage.getItem(LEGACY_KEYS.state);
    const hasSelection = Object.keys(state.settings.last_selection || {}).length > 0;
    if (raw && !hasSelection) {
      const saved = JSON.parse(raw);
      patch.last_selection = {
        camera: saved.camera || null,
        lens: saved.lens || null,
        film: saved.film || null,
        advanced: saved.advanced || {},
      };
      if (saved.folder) patch.last_folder = saved.folder;
      patch.preview_invert = !!saved.previewInvert;
    }
    const recent = JSON.parse(localStorage.getItem(LEGACY_KEYS.recent) || "[]");
    if (Array.isArray(recent) && recent.length && !state.settings.recent_folders.length) {
      patch.recent_folders = recent;
    }
    const theme = localStorage.getItem(LEGACY_KEYS.theme);
    if (theme === "paper" && state.settings.theme === "dark") patch.theme = theme;
  } catch (e) {
    console.warn("Legacy state migration skipped", e);
    patch = {};
  }
  if (Object.keys(patch).length) {
    try {
      state.settings = await api("/api/settings", { method: "PATCH", body: JSON.stringify(patch) });
    } catch (e) {
      console.warn("Legacy state migration failed", e);
      return;
    }
  }
  try {
    for (const key of Object.values(LEGACY_KEYS)) localStorage.removeItem(key);
  } catch {
    // storage may be unavailable — nothing to clean up then
  }
}


// The last used gear and processing fields become the defaults of the next
// roll, wherever it is opened.
function saveStickyState() {
  queueSettingsPatch({
    last_folder: state.folder.path || "",
    preview_invert: !!state.previewInvert,
    last_selection: {
      camera: state.camera,
      lens: state.lens,
      film: state.film,
      advanced: state.advanced,
    },
  });
}

function rememberGear(tagging) {
  state.camera = tagging.camera || null;
  state.lens = tagging.lens || null;
  state.film = tagging.film || null;
  for (const key of Object.keys(state.advanced)) state.advanced[key] = tagging[key] || "";
  saveStickyState();
}

function recentFolders() {
  return (state.settings && state.settings.recent_folders) || [];
}

function pushRecentFolder(path) {
  const list = [path, ...recentFolders().filter(p => p !== path)].slice(0, RECENT_FOLDERS_LIMIT);
  queueSettingsPatch({ recent_folders: list, last_folder: path });
}

function loadTheme() {
  // First paint only; the server value applied by loadSettings() wins.
  try {
    const hint = localStorage.getItem(THEME_HINT_KEY);
    if (hint === "paper" || hint === "dark") state.theme = hint;
  } catch {
    // storage may be unavailable
  }
  applyTheme(state.theme, { persist: false });
}

function applyTheme(theme, { persist = true } = {}) {
  const nextTheme = theme === "paper" ? "paper" : "dark";
  state.theme = nextTheme;
  document.body.dataset.theme = nextTheme;
  renderThemeSwitch();
  try {
    localStorage.setItem(THEME_HINT_KEY, nextTheme);
  } catch {
    // ignore — the server copy is the real one
  }
  if (persist) queueSettingsPatch({ theme: nextTheme });
}

function renderThemeSwitch() {
  const darkBtn = document.getElementById("theme-dark");
  const paperBtn = document.getElementById("theme-paper");
  if (!darkBtn || !paperBtn) return;
  const isDark = state.theme !== "paper";
  darkBtn.classList.toggle("active", isDark);
  darkBtn.setAttribute("aria-pressed", String(isDark));
  paperBtn.classList.toggle("active", !isDark);
  paperBtn.setAttribute("aria-pressed", String(!isDark));
}


// ---------- In-page dialogs ----------
//
// One dialog element serves alert, confirm and prompt. With `input`, the
// promise resolves with the typed text, or null when dismissed.

function showDialog({ title = "FilmEx", message = "", okLabel = "OK", cancelLabel = null, input = null } = {}) {
  return new Promise((resolve) => {
    const dialog = document.getElementById("dialog");
    const backdrop = document.getElementById("dialog-backdrop");
    const okBtn = document.getElementById("dialog-ok");
    const cancelBtn = document.getElementById("dialog-cancel");
    const field = document.getElementById("dialog-input");
    document.getElementById("dialog-title").textContent = title;
    document.getElementById("dialog-message").textContent = message;
    okBtn.textContent = okLabel;
    cancelBtn.textContent = cancelLabel || "Cancel";
    cancelBtn.classList.toggle("hidden", !cancelLabel);
    field.classList.toggle("hidden", !input);
    if (input) {
      field.value = input.value || "";
      field.placeholder = input.placeholder || "";
    }
    const previouslyFocused = document.activeElement;

    const result = (ok) => (input ? (ok ? field.value.trim() : null) : ok);
    const finish = (ok) => { cleanup(); resolve(result(ok)); };
    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); finish(false); }
      else if (e.key === "Enter") { e.preventDefault(); finish(true); }
    };
    function cleanup() {
      dialog.classList.add("hidden");
      backdrop.classList.add("hidden");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      backdrop.removeEventListener("click", onCancel);
      document.removeEventListener("keydown", onKey);
      if (previouslyFocused && typeof previouslyFocused.focus === "function") previouslyFocused.focus();
    }

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    backdrop.addEventListener("click", onCancel);
    document.addEventListener("keydown", onKey);
    dialog.classList.remove("hidden");
    backdrop.classList.remove("hidden");
    (input ? field : okBtn).focus();
  });
}

function notify(message, title = "FilmEx") {
  return showDialog({ title, message });
}

function ask(message, { title = "Confirm", okLabel = "Continue", cancelLabel = "Cancel" } = {}) {
  return showDialog({ title, message, okLabel, cancelLabel });
}


function askText(message, { title = "FilmEx", okLabel = "Open", placeholder = "", value = "" } = {}) {
  return showDialog({ title, message, okLabel, cancelLabel: "Cancel", input: { placeholder, value } });
}

// ---------- Desktop shell bridge ----------
//
// Inside the pywebview window the shell owns the native folder dialog and
// receives real filesystem paths for drops; it calls into this object.

function isDesktopShell() {
  return !!window.pywebview;
}

window.filmexDesktop = {
  // Absolute paths of the items dropped on the window, folder or files.
  onDrop(paths) {
    const path = (paths || [])[0];
    if (path) void openFolder(path);
  },
};



// ---------- API ----------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) throw await responseError(res);
  return res.status === 204 ? null : res.json();
}

async function responseError(res) {
  const detail = await res.text().catch(() => res.statusText);
  return new Error(`${res.status} ${res.statusText}: ${detail}`);
}


// ---------- Jobs ----------
//
// Every long operation (apply, thumbnails, rescan, rename, split, import,
// undo) is a server job streamed as NDJSON, with the protocol defined in
// services/jobs.py: one `start`, any number of job events, then exactly one
// `done` or `error`. runJob() is the only reader of that protocol: it sends
// the request, hands `start` and the job events to the caller as they
// arrive, and resolves with the `done` event or rejects with the job's
// error. Callers never see heartbeats, terminal events or NDJSON.
async function runJob(url, { body, method = "POST", signal, onStart, onEvent } = {}) {
  const init = { method, signal };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  if (!res.ok) throw await responseError(res);
  let done = null;
  await readNdjson(res, (ev) => {
    if (ev.type === "start") onStart?.(ev);
    else if (ev.type === "done") done = ev;
    else if (ev.type === "error") throw new Error(ev.error || "the job failed");
    else if (ev.type !== "heartbeat") onEvent?.(ev);
  });
  if (!done) throw new Error("the connection closed before the job finished");
  return done;
}

// Read an NDJSON response line by line, calling `onEvent` for each object as
// it arrives so progress can render live. Exceptions thrown by `onEvent`
// abort the read and propagate to the caller.

async function readNdjson(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) !== -1) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      onEvent(JSON.parse(line));
    }
  }
  const tail = buf.trim();
  if (tail) onEvent(JSON.parse(tail));
}

async function cancelJob(jobId) {
  if (!jobId) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
  } catch (e) {
    console.warn("Cancel request failed", e);
  }
}


// ---------- Initial load ----------

async function init() {
  document.body.dataset.ready = "";
  try {
    await initInner();
    document.body.dataset.ready = "ok";
  } catch (e) {
    document.body.dataset.ready = "error";
    throw e;
  }
}

// Marked on <body data-ready> so the desktop shell's smoke check can tell
// that settings and entity lists loaded through the authenticated API.


// Marked on <body data-ready> so the desktop shell's smoke check can tell
// that settings and entity lists loaded through the authenticated API.
async function initInner() {
  await loadSettings();
  await refreshExifToolStatus();
  await loadEntityCaches();
  renderHeader();
  chooseStartView();
}

async function loadEntityCaches() {
  const [cams, lenses, films, brands, providers, devM, scanM] = await Promise.all([
    api("/api/cameras"),
    api("/api/lenses"),
    api("/api/film-stocks"),
    api("/api/film-brands"),
    api("/api/providers"),
    api("/api/development-methods"),
    api("/api/scan-methods"),
  ]);
  state.cameras = cams;
  state.lenses = lenses;
  state.films = films;
  state.brands = brands;
  state.providers = providers;
  state.devMethods = devM;
  state.scanMethods = scanM;
  // Re-resolve sticky entity snapshots against the fresh DB (IDs might have
  // changed if seeds were edited); drop stale ones.
  if (state.camera && !cams.find(c => c.id === state.camera.id)) state.camera = null;
  if (state.lens && !lenses.find(l => l.id === state.lens.id)) state.lens = null;
  if (state.film && !films.find(f => f.id === state.film.id)) state.film = null;
}

async function refreshExifToolStatus() {
  // Only surface this pill when something is wrong. A green "EXIFTOOL OK"
  // badge is backend plumbing the user has no reason to watch every time.
  const el = document.getElementById("exiftool-status");
  try {
    const h = await api("/api/health");
    if (h.exiftool_available) {
      el.className = "pill ok hidden";
      el.textContent = "";
    } else {
      el.textContent = "exiftool missing";
      el.className = "pill bad";
    }
  } catch {
    el.textContent = "backend unreachable";
    el.className = "pill bad";
  }
}


// ---------- Views and header ----------

const VIEW_IDS = { home: "home-view", library: "library-view", folder: "folder-view", settings: "settings-view" };

function showView(name) {
  const next = VIEW_IDS[name] ? name : "home";
  for (const [key, id] of Object.entries(VIEW_IDS)) {
    document.getElementById(id).classList.toggle("hidden", key !== next);
  }
  state.view = next;
  renderHeader();
}

function hasLibrary() {
  return !!(state.settings && state.settings.library_root);
}

// Tabs sit next to the brand and only appear for places that exist:
// Library once a root is set, the folder once one is open. A tab that would
// stand alone pointing at the screen already shown is left out, since the
// screen's own heading says where you are. "Open folder" is always there.
function renderHeader() {
  const libraryTab = document.querySelector('.view-tab[data-view="library"]');
  const folderTab = document.querySelector('.view-tab[data-view="folder"]');
  const open = !!state.folder.path;
  const tabs = [
    { el: libraryTab, exists: hasLibrary(), active: state.view === "library" },
    { el: folderTab, exists: open, active: state.view === "folder" },
  ];
  const visible = tabs.filter(t => t.exists);
  const lone = visible.length === 1 && visible[0].active;
  for (const tab of tabs) {
    tab.el.classList.toggle("hidden", !tab.exists || lone);
    tab.el.classList.toggle("active", tab.active);
  }
  if (open) {
    folderTab.textContent = folderLabel(state.folder.path);
    folderTab.title = state.folder.path;
  }
}

function chooseStartView() {
  if (state.viewInitialised) return;
  state.viewInitialised = true;
  if (hasLibrary()) {
    showView("library");
    void loadLibrary({ rescan: true });
  } else {
    showHome();
  }
}

function baseName(path) {
  return (path || "").replace(/\/+$/, "").split("/").pop() || path;
}

// How a folder is called in the header and on its screen: inside the
// library, its path below the year (`order-77/roll-a`), else its name.
function folderLabel(path) {
  const root = (state.settings?.library_root || "").replace(/\/+$/, "");
  const clean = (path || "").replace(/\/+$/, "");
  if (root && clean.startsWith(root + "/")) {
    const parts = clean.slice(root.length + 1).split("/");
    if (parts.length >= 2) return parts.slice(1).join("/");
  }
  return baseName(clean);
}

function setPageTitleProgress(label, current, total) {
  document.title = `${DEFAULT_PAGE_TITLE} (${label}: ${current}/${total})`;
}

function resetPageTitle() {
  document.title = DEFAULT_PAGE_TITLE;
}


// ---------- Home ----------

function showHome() {
  showView("home");
  renderHome();
}

function renderHome() {
  const recent = recentFolders();
  const section = document.getElementById("home-recent");
  const list = document.getElementById("home-recent-list");
  section.classList.toggle("hidden", !recent.length);
  list.innerHTML = "";
  for (const path of recent.slice(0, 8)) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "home-recent-row";
    row.innerHTML = `<code>${escapeHtml(baseName(path))}</code><span class="lib-sub">${escapeHtml(path)}</span>`;
    row.addEventListener("click", () => void openFolder(path));
    list.appendChild(row);
  }
  document.getElementById("home-path").value = "";
}

// Native folder dialog. Inside the desktop shell pywebview exposes a JS API
// that opens a real NSOpenPanel; in browser mode the server runs an
// out-of-process picker. Returns null when the user cancels.
async function pickFolder() {
  const bridge = window.pywebview && window.pywebview.api;
  if (bridge && typeof bridge.pick_folder === "function") {
    const folder = await bridge.pick_folder();
    return folder || null;
  }
  const res = await api("/api/folder/pick", { method: "POST" });
  if (res.cancelled || !res.folder) return null;
  return res.folder;
}


async function onOpenFolder() {
  try {
    const folder = await pickFolder();
    if (folder) await openFolder(folder);
  } catch (err) {
    await notify(`Could not open folder picker: ${err.message || err}`);
  }
}

async function chooseLibraryFolder() {
  try {
    const folder = await pickFolder();
    if (!folder) return;
    state.settings = await api("/api/settings", { method: "PATCH", body: JSON.stringify({ library_root: folder }) });
    renderHeader();
    await openLibrary();
  } catch (e) {
    await notify(`Could not set the library folder: ${e.message}`);
  }
}

// ---------- Gear labels ----------

const CAMERA_TYPE_LABELS = {
  slr: "SLR",
  rangefinder: "Rangefinder",
  "point-and-shoot": "Point-and-shoot",
  tlr: "TLR",
  folder: "Folder",
  other: "Other",
};
const cameraTypeLabel = (t) => CAMERA_TYPE_LABELS[t] || t || "";

// Same idea for film_type. Stored as lowercase enum; displayed with proper
// capitalization and the ampersand spelling for B&W.
const FILM_TYPE_LABELS = {
  color_negative: "Color negative",
  bw_negative: "B&W negative",
  slide: "Slide",
  other: "Other",
};
const filmTypeLabel = (t) => FILM_TYPE_LABELS[t] || (t || "").replace(/_/g, " ");
// ISO pill color encodes the film kind at a glance: black for B&W, green for
// color negative, purple for slide. "other" shares the green pill — anything
// nonstandard reads as "color-ish, look closer."
const ISO_BADGE_KIND = {
  bw_negative: "bw",
  color_negative: "color",
  slide: "slide",
  other: "color",
};
const isoBadgeKind = (t) => ISO_BADGE_KIND[t] || "color";


// Gear rules shared by every roll card. `gear` is { camera, lens, film };
// both return a new object.
//
// Picking a camera: a fixed-lens camera brings its own lens, and a lens on
// another mount is dropped (the lens picker's "Show all" can still force one).
function withGear(gear, kind, item) {
  const next = { ...gear, [kind]: item };
  if (kind === "camera") {
    const builtIn = item.fixed_lens_id && state.lenses.find(l => l.id === item.fixed_lens_id);
    if (builtIn) next.lens = builtIn;
    if (next.lens && !lensMountMatchesCamera(next.lens, item)) next.lens = null;
  }
  return next;
}

// Clearing a slot: clearing a fixed-lens camera takes its built-in lens too.


// Clearing a slot: clearing a fixed-lens camera takes its built-in lens too.
function withoutGear(gear, kind) {
  const next = { ...gear, [kind]: null };
  if (kind === "camera" && gear.lens && state.cameras.some(c => c.fixed_lens_id === gear.lens.id)) next.lens = null;
  return next;
}


// ---------- Picker ----------
//
// The picker writes into a target: a roll card's gear. `camera()` scopes the
// lens list; `set(kind, item)` takes the choice.

let activePickerKind = null;
let pickerTarget = null;

const PICKER_NEW_LABEL = { camera: "+ New camera…", lens: "+ New lens…", film: "+ New film…" };
const PICKER_FORM_KIND = { camera: "camera", lens: "lens", film: "film" };

function openPicker(kind, target) {
  activePickerKind = kind;
  pickerTarget = target;
  const panel = document.getElementById("picker-panel");
  const backdrop = document.getElementById("picker-backdrop");
  const title = document.getElementById("picker-title");
  const grid = document.getElementById("picker-grid");
  const showAll = document.getElementById("picker-show-all");
  const search = document.getElementById("picker-search");

  title.textContent = { camera: "Pick a camera", lens: "Pick a lens", film: "Pick a film" }[kind];
  search.value = "";
  delete grid.dataset.showAll;
  showAll.classList.toggle("hidden", kind !== "lens");
  document.getElementById("picker-new").textContent = PICKER_NEW_LABEL[kind];

  grid.className = "picker-grid" + (kind === "film" ? " film-grid" : "");

  renderPickerGrid();
  panel.classList.remove("hidden");
  backdrop.classList.remove("hidden");
  search.focus();
}

function closePicker() {
  document.getElementById("picker-panel").classList.add("hidden");
  document.getElementById("picker-backdrop").classList.add("hidden");
  activePickerKind = null;
}

async function renderPickerGrid() {
  const grid = document.getElementById("picker-grid");
  const search = document.getElementById("picker-search").value.trim().toLowerCase();

  let items = [];
  if (activePickerKind === "camera") {
    items = state.cameras;
  } else if (activePickerKind === "lens") {
    // Re-fetch on camera context if a camera is selected.
    const camera = pickerTarget.camera();
    if (camera && !grid.dataset.showAll) {
      items = await api(`/api/lenses?camera_id=${encodeURIComponent(camera.id)}`);
    } else if (camera && grid.dataset.showAll === "true") {
      items = await api(`/api/lenses?camera_id=${encodeURIComponent(camera.id)}&show_all=true`);
    } else {
      items = state.lenses;
    }
  } else if (activePickerKind === "film") {
    items = state.films;
  }

  const filtered = search
    ? items.filter(i => {
        const haystack = [i.display_name || "", ...(i.aliases || [])]
          .join("\n")
          .toLowerCase();
        return haystack.includes(search);
      })
    : items;

  grid.innerHTML = "";
  if (activePickerKind === "film") {
    for (const group of groupPickerFilms(filtered)) {
      const label = document.createElement("div");
      label.className = "picker-group-label" + (group.modifier ? ` picker-group-label--${group.modifier}` : "");
      label.textContent = group.label;
      grid.appendChild(label);
      for (const item of group.items) {
        grid.appendChild(renderPickerTile(item, activePickerKind));
      }
    }
  } else {
    for (const item of filtered) {
      grid.appendChild(renderPickerTile(item, activePickerKind));
    }
  }
  if (filtered.length === 0) {
    const empty = document.createElement("div");
    empty.className = "picker-tile-sub";
    empty.textContent = "No matches.";
    grid.appendChild(empty);
  }
}

// Per-brand two-color palette rendered as a stacked strip on the left of
// each film tile — a bookmark-style visual grouping. Colors now come from
// the `film_brand` DB rows (editable under Settings → Film brands). Brands
// without a palette, or stocks whose brand row is missing, fall back to a
// neutral pair so the strip still appears but doesn't misrepresent.


// Add gear without a trip to Settings: the same form opens over the page
// and the saved row becomes the picker's choice.
function addFromPicker() {
  const kind = activePickerKind;
  const target = pickerTarget;
  closePicker();
  openSettingsForm(PICKER_FORM_KIND[kind], null, {
    onSaved: (row) => target && target.set(kind, row),
  });
}

const FILM_BRAND_FALLBACK = ["#3a4050", "#2a3040"];

function filmBrandColors(brand) {
  const needle = (brand || "").trim().toLowerCase();
  const row = state.brands.find(b => (b.name || "").trim().toLowerCase() === needle);
  const c1 = row?.color_primary;
  const c2 = row?.color_secondary;
  if (c1 && c2) return [c1, c2];
  if (c1) return [c1, c1];
  if (c2) return [c2, c2];
  return FILM_BRAND_FALLBACK;
}

// A 35 mm cassette, front view: metal cap, dark body, the brand's label
// band and accent stripe (from --brand-c1/--brand-c2 on the parent), and a
// shared shading gradient that rounds the body. Pure SVG, no dependencies.

function canisterSvg(extraClass = "canister") {
  return (
    `<svg class="${extraClass}" viewBox="0 0 24 36" aria-hidden="true" focusable="false">` +
      `<rect x="9.5" y="0" width="5" height="2.6" rx="0.5" fill="#8d8d8d"/>` +
      `<rect x="3" y="2" width="18" height="4.2" rx="1" fill="url(#can-metal)"/>` +
      `<rect x="4" y="6" width="16" height="29" rx="1.6" fill="#1a1a1a"/>` +
      `<rect x="4" y="9" width="16" height="19" fill="var(--brand-c1, #3a4050)"/>` +
      `<rect x="4" y="24" width="16" height="4" fill="var(--brand-c2, #2a3040)"/>` +
      `<rect x="4" y="6" width="16" height="29" rx="1.6" fill="url(#can-shade)"/>` +
      `<rect x="19" y="13" width="2.4" height="9" rx="0.5" fill="#070707" opacity="0.85"/>` +
    `</svg>`
  );
}

function groupPickerFilms(items) {
  // Returns [{ label, modifier, items }] — a "Favorites" group (if any
  // favorites exist) followed by one group per brand. Favorites are NOT
  // duplicated in their brand groups; pulling them out of the brand list
  // is how the top group stays meaningful.
  const byName = (a, b) => (a.display_name || "").localeCompare(b.display_name || "");
  const favorites = items.filter(i => i.favorite).sort(byName);
  const rest = items.filter(i => !i.favorite).sort((a, b) => {
    const brandCmp = (a.brand || "").localeCompare(b.brand || "");
    if (brandCmp !== 0) return brandCmp;
    return byName(a, b);
  });

  const groups = [];
  if (favorites.length > 0) {
    groups.push({ label: "Favorites", modifier: "favorites", items: favorites });
  }
  const brandGroups = new Map();
  for (const item of rest) {
    const brand = item.brand || "Other";
    if (!brandGroups.has(brand)) brandGroups.set(brand, []);
    brandGroups.get(brand).push(item);
  }
  for (const [brand, groupItems] of brandGroups) {
    groups.push({ label: brand, modifier: null, items: groupItems });
  }
  return groups;
}

function renderPickerTile(item, kind) {
  const tile = document.createElement("div");
  // Film tiles would collide with the brand-color strip if we reused the
  // default .favorite ::before dot — the CSS resolves that by attaching the
  // film-grid favorite marker to ::after instead, so the class is safe here.
  tile.className = "picker-tile" + (item.favorite ? " favorite" : "");

  if (kind === "film") {
    const [c1, c2] = filmBrandColors(item.brand);
    tile.style.setProperty("--brand-c1", c1);
    tile.style.setProperty("--brand-c2", c2);
    tile.insertAdjacentHTML("afterbegin", canisterSvg());
  }

  const title = document.createElement("div");
  title.className = "picker-tile-title";
  title.textContent = item.display_name;
  tile.appendChild(title);

  if (kind === "camera") {
    const sub = document.createElement("div");
    sub.className = "picker-tile-sub";
    sub.textContent = [cameraTypeLabel(item.camera_type), item.mount].filter(Boolean).join(" · ");
    tile.appendChild(sub);
  } else if (kind === "lens") {
    const sub = document.createElement("div");
    sub.className = "picker-tile-sub";
    sub.textContent = [
      item.focal_length_mm && `${item.focal_length_mm}mm`,
      item.max_aperture && `f/${item.max_aperture}`,
      item.mount,
    ].filter(Boolean).join(" · ");
    tile.appendChild(sub);
  } else if (kind === "film") {
    // Brand identity is already carried by the colored strip on the left, so
    // the subtitle just adds the type — no need to repeat the brand name.
    const sub = document.createElement("div");
    sub.className = "picker-tile-sub";
    sub.textContent = filmTypeLabel(item.film_type);
    tile.appendChild(sub);
    const badge = document.createElement("div");
    badge.className = `iso-badge iso-badge--${isoBadgeKind(item.film_type)}`;
    badge.textContent = item.box_speed_iso;
    tile.appendChild(badge);
  }

  tile.addEventListener("click", () => selectFromPicker(item));
  return tile;
}

// Case-insensitive mount comparison. AGENTS.md says lens.mount must exactly
// match a camera's mount, but we normalize anyway so stray whitespace or
// mixed-case DB edits don't wrongly clear a valid pairing.

function lensMountMatchesCamera(lens, camera) {
  if (!lens || !camera) return false;
  const lm = (lens.mount || "").trim().toLowerCase();
  const cm = (camera.mount || "").trim().toLowerCase();
  if (!lm || !cm) return false;
  return lm === cm;
}


function selectFromPicker(item) {
  if (pickerTarget) pickerTarget.set(activePickerKind, item);
  closePicker();
}

// ---------- Manifest values ----------
//
// A folder that was tagged before carries a manifest; its snapshots become
// the first roll's values, matched back to the current gear lists.

function taggingFromManifest(manifest, imageNames) {
  const unresolved = [];
  const tagging = defaultTagging();
  tagging.camera = resolveCameraFromManifest(manifest.analog_camera_snapshot, unresolved);
  tagging.lens = resolveLensFromManifest(manifest.analog_lens_snapshot, unresolved);
  tagging.film = resolveFilmFromManifest(manifest.film_stock_snapshot, unresolved);
  tagging.development_provider_id = resolveNamedEntityId(state.providers, manifest.development_provider_snapshot, unresolved, "development provider");
  tagging.development_method_id = resolveNamedEntityId(state.devMethods, manifest.development_method_snapshot, unresolved, "development method");
  tagging.scan_provider_id = resolveNamedEntityId(state.providers, manifest.scan_provider_snapshot, unresolved, "scan provider");
  tagging.scan_method_id = resolveNamedEntityId(state.scanMethods, manifest.scan_method_snapshot, unresolved, "scan method");
  const files = imageNames.map(name => ({ name }));
  tagging.file_keywords = fileKeywordsFromManifest(manifest.file_overrides, files);
  tagging.file_lens_ids = fileLensOverridesFromManifest(manifest.file_overrides, files, unresolved);
  const date = (manifest.capture_date_value || "").trim();
  if (date) tagging.roll_date = date;
  tagging.roll_notes = (manifest.notes || "").trim();
  tagging.place = (manifest.place || "").trim();
  return { tagging, unresolved };
}

function resolveCameraFromManifest(snapshot, unresolved) {
  return resolveEntityFromManifest(
    state.cameras,
    snapshot,
    unresolved,
    "camera",
    item => item.display_name,
    ["make", "model"]
  );
}

function resolveLensFromManifest(snapshot, unresolved) {
  return resolveEntityFromManifest(
    state.lenses,
    snapshot,
    unresolved,
    "lens",
    item => item.display_name,
    ["make", "model"]
  );
}

function resolveFilmFromManifest(snapshot, unresolved) {
  return resolveEntityFromManifest(
    state.films,
    snapshot,
    unresolved,
    "film stock",
    item => item.display_name,
    ["brand", "display_name"]
  );
}

function resolveNamedEntityId(items, snapshot, unresolved, label) {
  const row = resolveEntityFromManifest(
    items,
    snapshot,
    unresolved,
    label,
    item => item.name,
    ["name"]
  );
  return row ? row.id : "";
}

function resolveEntityFromManifest(items, snapshot, unresolved, label, labelFn, keyFields) {
  if (!snapshot || typeof snapshot !== "object") return null;
  const meaningful = keyFields.some(key => (snapshot[key] || "").toString().trim()) || !!snapshot.id;
  if (!meaningful) return null;

  if (snapshot.id) {
    const byId = items.find(item => item.id === snapshot.id);
    if (byId) return byId;
  }

  const manifestLabel = (labelFn(snapshot) || "").toString().trim().toLowerCase();
  if (manifestLabel) {
    const byLabel = items.find(item => ((labelFn(item) || "").toString().trim().toLowerCase() === manifestLabel));
    if (byLabel) return byLabel;
  }

  const byKey = items.find(item =>
    keyFields.every(key => compareCaseInsensitive(item[key], snapshot[key]))
  );
  if (byKey) return byKey;

  unresolved.push(buildManifestLabel(label, snapshot, labelFn));
  return null;
}

function buildManifestLabel(label, snapshot, labelFn) {
  const named = (labelFn(snapshot) || "").toString().trim();
  if (named) return `${label} "${named}"`;
  const parts = [snapshot.make, snapshot.model, snapshot.brand].filter(Boolean).map(String);
  return parts.length ? `${label} "${parts.join(" ")}"` : label;
}

function compareCaseInsensitive(a, b) {
  return ((a || "").toString().trim().toLowerCase() === (b || "").toString().trim().toLowerCase());
}


// ---------- Helpers ----------

function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function formatValue(v) {
  if (v == null) return "<em style='color:var(--muted)'>unset</em>";
  if (Array.isArray(v)) return escapeHtml(v.join(", "));
  return escapeHtml(v);
}

// Developed positives in the open folder (DSC0001-positive.jpg beside
// DSC0001.ARW): already pictures, so their thumbnails are never inverted.
function positiveFiles() {
  const folder = state.folder;
  if (folder._positivesFor !== folder.listing) {
    folder._positivesFor = folder.listing;
    folder.positives = new Set((folder.listing?.frames || []).flatMap(f => f.positives || []));
  }
  return folder.positives;
}

function isPositiveFile(folder, name) {
  return folder === state.folder.path && positiveFiles().has(name);
}

function buildThumbnailUrl(folder, name, invert = state.previewInvert) {
  if (invert && isPositiveFile(folder, name)) invert = false;
  const params = new URLSearchParams({ folder, name, invert: invert ? "true" : "false" });
  return `/api/files/thumbnail?${params.toString()}`;
}

function fileKeywordsFromManifest(fileOverrides, files) {
  const valid = new Set((files || []).map(file => file.name));
  const out = {};
  for (const [name, payload] of Object.entries(fileOverrides || {})) {
    if (!valid.has(name)) continue;
    const keywords = Array.isArray(payload?.extra_keywords)
      ? payload.extra_keywords.map(v => String(v).trim()).filter(Boolean)
      : [];
    if (keywords.length) out[name] = keywords.join(", ");
  }
  return out;
}

function fileLensOverridesFromManifest(fileOverrides, files, unresolved) {
  const valid = new Set((files || []).map(file => file.name));
  const out = {};
  for (const [name, payload] of Object.entries(fileOverrides || {})) {
    if (!valid.has(name)) continue;
    const row = resolveLensFromManifest(payload?.lens_snapshot, unresolved);
    if (row) out[name] = row.id;
  }
  return out;
}


function keywordPayload(byFile) {
  const out = {};
  for (const [name, raw] of Object.entries(byFile || {})) {
    const keywords = String(raw).split(",").map(s => s.trim()).filter(Boolean);
    if (keywords.length) out[name] = keywords;
  }
  return out;
}

function compatibleLensOptions(camera) {
  if (camera?.fixed_lens_id) return state.lenses.filter(l => l.id === camera.fixed_lens_id);
  if (camera?.mount) return state.lenses.filter(l => !l.mount || l.mount === camera.mount);
  return state.lenses;
}

function lensOverrideOptions(camera, selectedId) {
  const options = ['<option value="">(main lens)</option>'];
  for (const lens of compatibleLensOptions(camera)) {
    options.push(`<option value="${escapeHtml(lens.id)}" ${lens.id === selectedId ? "selected" : ""}>${escapeHtml(lens.display_name)}</option>`);
  }
  return options.join("");
}

// Small inline icons: a chevron for expanders, a magnifier for "open this
// image", so a control looks like one instead of a word.
const CHEVRON_ICON = `<svg class="chev" viewBox="0 0 10 10" width="9" height="9" aria-hidden="true" focusable="false"><path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const MAGNIFIER_ICON = `<svg viewBox="0 0 14 14" width="12" height="12" aria-hidden="true" focusable="false"><circle cx="5.75" cy="5.75" r="4" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8.75 8.75 12.5 12.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`;

async function openSourceFile(name) {
  try {
    const params = new URLSearchParams({ folder: state.folder.path, name });
    await api(`/api/files/open?${params.toString()}`, { method: "POST" });
  } catch (e) {
    await notify(`Could not open file:\n${e.message}`);
  }
}

async function revealFolder(path) {
  const bridge = window.pywebview && window.pywebview.api;
  try {
    if (bridge && typeof bridge.reveal_path === "function") {
      await bridge.reveal_path(path);
    } else {
      await api("/api/folder/reveal", { method: "POST", body: JSON.stringify({ folder: path }) });
    }
  } catch (e) {
    renderLibraryBanner(`Could not reveal the folder: ${e.message}`, "bad");
  }
}

// Hand a library folder to the Tag view, optionally restricted to a subset
// of its files (the untagged remainder of a partially tagged roll).


function formatStamp(iso, { dateOnly = false } = {}) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return dateOnly ? date : `${date} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}


function humanBytes(n) {
  if (!n && n !== 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(n);
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return i === 0 ? `${value} B` : `${value.toFixed(value >= 100 ? 0 : 1)} ${units[i]}`;
}

// ----- Tag view: read-only sheet -----


// ---------- Settings ----------

const SETTINGS_KINDS = {
  camera: {
    label: "Camera",
    endpoint: "/api/cameras",
    groupBy: "make",
    primary: "display_name",
    sub: (row) => cameraTypeLabel(row.camera_type) || row.mount || "",
    fields: [
      { name: "make", label: "Make", required: true },
      { name: "model", label: "Model", required: true },
      { name: "display_name", label: "Display name", required: true },
      { name: "short_name", label: "Short name (folder names)", placeholder: "F3, Fed2, Canonet…", help: "Used by the roll folder name template as {camera}. Blank uses the display name." },
      { name: "mount", label: "Mount (blank if none)", type: "camera-mount-entry", placeholder: "K, M39, Fixed-Lens…", help: "Choose an existing mount when possible. You can still type a new one for the first camera on a new mount." },
      { name: "camera_type", label: "Type", type: "select", options: [
          { value: "", label: "(none)" },
          { value: "slr", label: "SLR" },
          { value: "rangefinder", label: "Rangefinder" },
          { value: "point-and-shoot", label: "Point-and-shoot" },
          { value: "tlr", label: "TLR" },
          { value: "folder", label: "Folder" },
          { value: "other", label: "Other" },
      ] },
      { name: "fixed_lens_id", label: "Fixed lens (for built-in lenses)", type: "lens-ref", editOnly: true },
      { name: "notes", label: "Notes" },
    ],
  },
  lens: {
    label: "Lens",
    endpoint: "/api/lenses",
    groupBy: "make",
    primary: "display_name",
    sub: (row) => [row.mount, row.focal_length_mm && `${row.focal_length_mm}mm`, row.max_aperture && `f/${row.max_aperture}`].filter(Boolean).join(" · "),
    fields: [
      { name: "make", label: "Make", required: true },
      { name: "model", label: "Model", required: true },
      { name: "display_name", label: "Display name", required: true },
      { name: "lens_model_write_value", label: "Written to EXIF LensModel", required: true, help: "Usually matches display name" },
      { name: "focal_length_mm", label: "Focal length (mm, blank for zoom)", type: "number" },
      { name: "max_aperture", label: "Max aperture", type: "number", step: "0.1", min: "0.1", placeholder: "1.4, 2.8…" },
      { name: "mount", label: "Mount", type: "camera-mount", required: true, help: "Mounts come from existing cameras. Add the camera first, then the lens." },
      { name: "notes", label: "Notes" },
    ],
  },
  "film-brand": {
    label: "Film brand",
    endpoint: "/api/film-brands",
    groupBy: null,
    primary: "name",
    sub: (row) => [row.color_primary, row.color_secondary].filter(Boolean).join(" / ") || "no palette",
    fields: [
      { name: "name", label: "Brand name", required: true, help: "Kodak, Ilford, Fujifilm… exactly as you want it printed on tiles." },
      { name: "color_primary", label: "Primary color (hex)", type: "color-hex", placeholder: "#bb9d70", help: "Dominant film-box color. Blank for neutral fallback." },
      { name: "color_secondary", label: "Secondary color (hex)", type: "color-hex", placeholder: "#292825", help: "Accent color, shown as second stripe on the tile." },
    ],
  },
  film: {
    label: "Film stock",
    endpoint: "/api/film-stocks",
    groupBy: "brand",
    primary: "display_name",
    sub: (row) => `${row.film_type} · ISO ${row.box_speed_iso}`,
    fields: [
      { name: "brand", label: "Brand", type: "brand-ref", required: true, help: "Pick from existing brands. Create a new brand under Film brands first." },
      { name: "name", label: "Name", required: true },
      { name: "display_name", label: "Display name", required: true, help: "Brand Line [Variant] ISO — e.g. Kodak Portra 400" },
      { name: "short_name", label: "Short name (folder names)", placeholder: "Gold, HP5, Portra400…", help: "Used by the roll folder name template as {film}. Blank uses the display name." },
      { name: "box_speed_iso", label: "Box speed ISO", type: "number", required: true },
      { name: "film_type", label: "Film type", type: "select", required: true, options: ["color_negative", "bw_negative", "slide", "other"] },
      { name: "process_type", label: "Process", type: "select", required: true, options: ["c41", "bw", "e6", "ecn2", "other"] },
      { name: "notes", label: "Notes" },
    ],
  },
  provider: {
    label: "Provider",
    endpoint: "/api/providers",
    groupBy: null,
    primary: "name",
    sub: (row) => (row.kinds || []).join(" + "),
    fields: [
      { name: "name", label: "Name", required: true },
      { name: "kinds", label: "Services offered", type: "kinds", required: true },
    ],
  },
  "dev-method": {
    label: "Development method",
    endpoint: "/api/development-methods",
    groupBy: null,
    primary: "name",
    sub: () => "",
    fields: [{ name: "name", label: "Name", required: true }],
  },
  "scan-method": {
    label: "Scan method",
    endpoint: "/api/scan-methods",
    groupBy: null,
    primary: "name",
    sub: () => "",
    fields: [{ name: "name", label: "Name", required: true }],
  },
};

const settingsState = {
  showArchived: false,
  rowsByKind: {},  // kind -> [rows] fetched from the API
  cameraMounts: [],
  activeTab: null,
};


const SETTINGS_TAB_STORAGE_KEY = "filmex.settingsTab";
const DEFAULT_SETTINGS_TAB = "general";

function setActiveSettingsTab(tab) {
  const tabs = Array.from(document.querySelectorAll(".settings-tab"));
  const valid = tabs.some((b) => b.dataset.tab === tab);
  const next = valid ? tab : DEFAULT_SETTINGS_TAB;
  settingsState.activeTab = next;
  for (const button of tabs) {
    const on = button.dataset.tab === next;
    button.classList.toggle("active", on);
    button.setAttribute("aria-selected", on ? "true" : "false");
  }
  for (const section of document.querySelectorAll(".settings-section")) {
    section.hidden = section.dataset.kind !== next;
  }
  try {
    sessionStorage.setItem(SETTINGS_TAB_STORAGE_KEY, next);
  } catch {
    // sessionStorage can throw in private mode / disabled storage — fine.
  }
}


async function openSettings() {
  state.viewBeforeSettings = state.view === "settings" ? null : state.view;
  showView("settings");
  let remembered = null;
  try {
    remembered = sessionStorage.getItem(SETTINGS_TAB_STORAGE_KEY);
  } catch {
    // ignore — fall through to default
  }
  setActiveSettingsTab(remembered || DEFAULT_SETTINGS_TAB);
  await Promise.all([refreshAllSettingsLists(), renderGeneralSettings()]);
}

async function closeSettings() {
  await loadEntityCaches();
  const back = state.viewBeforeSettings || (hasLibrary() ? "library" : "home");
  if (back === "home") showHome();
  else if (back === "library") await openLibrary();
  else {
    showView(back);
    if (back === "folder") renderRollCards();
  }
}

const DEFAULT_NAMING_TEMPLATE = "{date}-{camera}-{film}";

const TEMPLATE_TOKENS = ["date", "year", "month", "camera", "film", "lens", "place", "roll"];
const TEMPLATE_SAMPLE = { date: "2026-05-14", year: "2026", month: "05", camera: "Fed2", film: "Gold", lens: "Industar-50", place: "Jāņos", roll: "01" };

// Client-side preview of the folder name template; the server's
// services/naming.py is the real renderer, this only mirrors it for feedback.
function renderTemplateExample() {
  const form = document.getElementById("general-settings-form");
  const out = document.getElementById("naming-template-example");
  if (!form || !out) return;
  const template = form.elements.naming_template.value.trim() || DEFAULT_NAMING_TEMPLATE;
  const tokens = Array.from(template.matchAll(/\{([a-z_]+)\}/g)).map((m) => m[1]);
  const unknown = tokens.filter((t) => !TEMPLATE_TOKENS.includes(t));
  if (!tokens.length || unknown.length || /[/\\:*?"<>|]/.test(template)) {
    out.textContent = unknown.length ? `unknown token {${unknown[0]}}` : "template needs at least one token and no / \\ : * ? \" < > |";
    out.classList.add("bad");
    return;
  }
  let example = template;
  if (form.elements.naming_place_suffix.checked && !template.includes("{place}")) example += "-{place}";
  example = example.replace(/\{([a-z_]+)\}/g, (_, t) => TEMPLATE_SAMPLE[t] || "");
  out.textContent = example.replace(/-{2,}/g, "-").replace(/^[-.]+|[-.]+$/g, "");
  out.classList.remove("bad");
}


async function renderGeneralSettings() {
  const form = document.getElementById("general-settings-form");
  if (!form) return;
  const s = state.settings || (state.settings = await api("/api/settings"));
  form.elements.library_root.value = s.library_root || "";
  form.elements.exiftool_path.value = s.exiftool_path || "";
  form.elements.naming_template.value = s.naming_template || DEFAULT_NAMING_TEMPLATE;
  form.elements.naming_place_suffix.checked = !!s.naming_place_suffix;
  document.getElementById("general-settings-status").textContent = "";
  renderTemplateExample();
  await renderExifToolDetection();
}

async function renderExifToolDetection() {
  const el = document.getElementById("general-exiftool-status");
  if (!el) return;
  try {
    const h = await api("/api/health");
    if (h.exiftool_available) {
      const version = h.exiftool_version ? ` v${h.exiftool_version}` : "";
      el.textContent = `Using ${h.exiftool_path}${version} (${h.exiftool_source}).`;
      el.classList.remove("bad");
    } else {
      el.textContent = "Not found. Install ExifTool or enter the path to its executable.";
      el.classList.add("bad");
    }
  } catch {
    el.textContent = "Could not check ExifTool.";
    el.classList.add("bad");
  }
}


async function submitGeneralSettings(e) {
  e.preventDefault();
  const form = e.currentTarget;
  const status = document.getElementById("general-settings-status");
  const patch = {
    library_root: form.elements.library_root.value.trim() || null,
    exiftool_path: form.elements.exiftool_path.value.trim() || null,
    naming_template: form.elements.naming_template.value.trim() || DEFAULT_NAMING_TEMPLATE,
    naming_place_suffix: form.elements.naming_place_suffix.checked,
  };
  try {
    state.settings = await api("/api/settings", { method: "PATCH", body: JSON.stringify(patch) });
    status.textContent = "Saved.";
    status.classList.remove("bad");
    renderHeader();
    await Promise.all([renderExifToolDetection(), refreshExifToolStatus()]);
  } catch (err) {
    status.textContent = err.message;
    status.classList.add("bad");
  }
}

async function onBrowseLibraryRoot() {
  const btn = document.getElementById("general-library-browse");
  btn.disabled = true;
  try {
    const folder = await pickFolder();
    if (folder) document.getElementById("general-settings-form").elements.library_root.value = folder;
  } catch (err) {
    document.getElementById("general-settings-status").textContent = `Could not open folder picker: ${err.message || err}`;
  } finally {
    btn.disabled = false;
  }
}

async function refreshAllSettingsLists() {
  await Promise.all([
    ...Object.keys(SETTINGS_KINDS).map(refreshSettingsList),
    refreshCameraMounts(),
  ]);
}

async function refreshCameraMounts() {
  settingsState.cameraMounts = await api("/api/camera-mounts");
}

async function refreshSettingsList(kind) {
  const cfg = SETTINGS_KINDS[kind];
  const url = `${cfg.endpoint}?include_archived=${settingsState.showArchived}`;
  const rows = await api(url);
  settingsState.rowsByKind[kind] = rows;
  syncMainEntityCache(kind, rows);
  renderSettingsList(kind);
}


function syncMainEntityCache(kind, rows) {
  const target = { camera: "cameras", lens: "lenses", "film-brand": "brands", film: "films",
                   provider: "providers", "dev-method": "devMethods", "scan-method": "scanMethods" }[kind];
  if (target) state[target] = rows;
  if (state.folder.listing) renderRollCards();
}

function renderSettingsList(kind) {
  const cfg = SETTINGS_KINDS[kind];
  const container = document.querySelector(`.settings-section[data-kind="${kind}"] .settings-list`);
  const rows = settingsState.rowsByKind[kind] || [];
  container.innerHTML = "";

  if (!rows.length) {
    container.innerHTML = `<div class="settings-group-label" style="color: var(--muted)">No entries yet. Click "Add" to create one.</div>`;
    return;
  }

  const groups = groupRows(rows, cfg.groupBy);
  for (const [groupName, groupRows] of groups) {
    if (groupName) {
      const label = document.createElement("div");
      label.className = "settings-group-label";
      label.textContent = groupName;
      container.appendChild(label);
    }
    for (const row of groupRows) {
      container.appendChild(renderSettingsRow(kind, row, cfg));
    }
  }
}

function groupRows(rows, groupBy) {
  // Sort alpha by primary key or name. Favorites no longer jump to the top
  // so toggling a star in Settings does not make the row leap around.
  const sorted = [...rows].sort((a, b) => {
    const aKey = a.display_name || a.name || "";
    const bKey = b.display_name || b.name || "";
    return aKey.localeCompare(bKey);
  });
  if (!groupBy) return [["", sorted]];
  const map = new Map();
  for (const row of sorted) {
    const key = row[groupBy] || "—";
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(row);
  }
  return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

function renderSettingsRow(kind, row, cfg) {
  const el = document.createElement("div");
  el.className = "settings-row" + (row.archived ? " archived" : "");
  const primary = row[cfg.primary] || row.name || "";
  const sub = cfg.sub(row);
  const hasFavorite = "favorite" in row;
  const hasArchive = "archived" in row;
  el.innerHTML = `
    <div class="settings-row-name">
      ${escapeHtml(primary)}
      ${sub ? `<span class="sub">${escapeHtml(sub)}</span>` : ""}
    </div>
    ${hasFavorite ? `<button class="settings-row-star ${row.favorite ? "on" : ""}" title="Favorite">★</button>` : ""}
    <button class="settings-row-edit">Edit</button>
    ${hasArchive ? `<button class="settings-row-archive">${row.archived ? "Unarchive" : "Archive"}</button>` : ""}
  `;
  el.querySelector(".settings-row-star")?.addEventListener("click", () => toggleFavorite(kind, row));
  el.querySelector(".settings-row-edit").addEventListener("click", () => openSettingsForm(kind, row));
  el.querySelector(".settings-row-archive")?.addEventListener("click", () => toggleArchive(kind, row));
  return el;
}

async function toggleFavorite(kind, row) {
  const cfg = SETTINGS_KINDS[kind];
  await api(`${cfg.endpoint}/${row.id}`, {
    method: "PATCH",
    body: JSON.stringify({ favorite: !row.favorite }),
  });
  await refreshSettingsList(kind);
}

async function toggleArchive(kind, row) {
  const cfg = SETTINGS_KINDS[kind];
  const action = row.archived ? "unarchive" : "archive";
  await api(`${cfg.endpoint}/${row.id}/${action}`, { method: "POST" });
  await refreshSettingsList(kind);
}

// ---- Settings form panel (add + edit) ----


let settingsFormCtx = null;  // { kind, row, onSaved } — row is null for "add"

function openSettingsForm(kind, row, { onSaved = null } = {}) {
  const cfg = SETTINGS_KINDS[kind];
  settingsFormCtx = { kind, row, onSaved };
  document.getElementById("settings-form-title").textContent =
    row ? `Edit ${cfg.label.toLowerCase()}` : `Add ${cfg.label.toLowerCase()}`;

  const form = document.getElementById("settings-form");
  form.innerHTML = cfg.fields
    .filter(f => row || !f.editOnly)
    .map(f => renderFormField(f, row))
    .join("") +
    `<div class="settings-form-error" id="settings-form-error"></div>`;

  document.getElementById("settings-form-panel").classList.remove("hidden");
  document.getElementById("settings-form-backdrop").classList.remove("hidden");

  // Two-way sync for color-hex dual inputs. The text input is the form field
  // (name=...) so submit reads from it; the picker just mirrors.
  form.querySelectorAll(".color-hex-row").forEach(row => {
    const picker = row.querySelector(".color-hex-picker");
    const text = row.querySelector(".color-hex-text");
    if (!picker || !text) return;
    picker.addEventListener("input", () => { text.value = picker.value; });
    text.addEventListener("input", () => {
      if (/^#([0-9a-fA-F]{6})$/.test(text.value)) picker.value = text.value;
    });
  });
  form.querySelector("input, select")?.focus();
}

function closeSettingsForm() {
  document.getElementById("settings-form-panel").classList.add("hidden");
  document.getElementById("settings-form-backdrop").classList.add("hidden");
  settingsFormCtx = null;
}

function renderFormField(f, row) {
  const value = row ? (row[f.name] ?? "") : "";
  const help = f.help ? `<span style="color: var(--muted); font-size: 11px;">${escapeHtml(f.help)}</span>` : "";

  if (f.type === "select") {
    const opts = f.options.map(opt => {
      const [v, label] = typeof opt === "string" ? [opt, opt || "(none)"] : [opt.value, opt.label];
      return `<option value="${escapeHtml(v)}" ${value === v ? "selected" : ""}>${escapeHtml(label)}</option>`;
    }).join("");
    return `<label><span>${escapeHtml(f.label)}${f.required ? " *" : ""}</span><select name="${f.name}">${opts}</select>${help}</label>`;
  }

  if (f.type === "kinds") {
    const kinds = Array.isArray(value) ? value : [];
    return `
      <label><span>${escapeHtml(f.label)}${f.required ? " *" : ""}</span>
        <div class="kinds-group">
          <label><input type="checkbox" name="kinds[]" value="development" ${kinds.includes("development") ? "checked" : ""}/>Development</label>
          <label><input type="checkbox" name="kinds[]" value="scan" ${kinds.includes("scan") ? "checked" : ""}/>Scan</label>
        </div>
      </label>`;
  }

  if (f.type === "lens-ref") {
    const lenses = settingsState.rowsByKind.lens || [];
    const opts = ['<option value="">(none)</option>'].concat(
      lenses.map(l =>
        `<option value="${escapeHtml(l.id)}" ${value === l.id ? "selected" : ""}>${escapeHtml(l.display_name)}</option>`
      )
    ).join("");
    return `<label><span>${escapeHtml(f.label)}</span><select name="${f.name}">${opts}</select>${help}</label>`;
  }

  if (f.type === "brand-ref") {
    const brands = settingsState.rowsByKind["film-brand"] || [];
    const current = typeof value === "string" ? value.trim() : "";
    // Match by case-insensitive name so a stock loaded with brand "kodak"
    // still highlights the "Kodak" row in the picker.
    const matched = brands.find(b => (b.name || "").toLowerCase() === current.toLowerCase());
    const baseOption = brands.length
      ? '<option value="">(select brand)</option>'
      : '<option value="">(add a film brand first)</option>';
    const opts = [baseOption].concat(
      brands.map(b =>
        `<option value="${escapeHtml(b.name)}" ${matched && b.id === matched.id ? "selected" : ""}>${escapeHtml(b.name)}</option>`
      )
    ).join("");
    return `<label><span>${escapeHtml(f.label)}${f.required ? " *" : ""}</span><select name="${f.name}">${opts}</select>${help}</label>`;
  }

  if (f.type === "color-hex") {
    // Dual input: a native color picker and a text input, kept in sync via
    // oninput handlers. Text is the source of truth so the user can paste a
    // literal `#bb9d70` from a design doc; the picker just scrubs. Blank
    // text == no color (falls back to neutral tile).
    const hex = (value || "").toString();
    const pickerVal = /^#([0-9a-f]{6})$/i.test(hex) ? hex : "#888888";
    const placeholder = f.placeholder ? `placeholder="${escapeHtml(f.placeholder)}"` : "";
    return `<label><span>${escapeHtml(f.label)}</span>
      <div class="color-hex-row">
        <input type="color" class="color-hex-picker" value="${escapeHtml(pickerVal)}" aria-label="${escapeHtml(f.label)} picker" />
        <input type="text" name="${f.name}" class="color-hex-text" value="${escapeHtml(hex)}" ${placeholder} />
      </div>${help}</label>`;
  }

  if (f.type === "camera-mount") {
    const mounts = [...settingsState.cameraMounts];
    const current = typeof value === "string" ? value.trim() : "";
    if (current && !mounts.includes(current)) mounts.unshift(current);
    const baseOption = mounts.length
      ? '<option value="">(select mount)</option>'
      : '<option value="">(add a camera with a mount first)</option>';
    const opts = [baseOption].concat(
      mounts.map(m =>
        `<option value="${escapeHtml(m)}" ${current === m ? "selected" : ""}>${escapeHtml(m)}</option>`
      )
    ).join("");
    return `<label><span>${escapeHtml(f.label)}${f.required ? " *" : ""}</span><select name="${f.name}">${opts}</select>${help}</label>`;
  }

  if (f.type === "camera-mount-entry") {
    const mounts = [...settingsState.cameraMounts].sort((a, b) => a.localeCompare(b));
    const current = typeof value === "string" ? value.trim() : "";
    const listId = `mount-suggestions-${settingsFormCtx?.kind || "camera"}`;
    const options = mounts.map(m => `<option value="${escapeHtml(m)}"></option>`).join("");
    const placeholder = f.placeholder ? `placeholder="${escapeHtml(f.placeholder)}"` : "";
    return `<label><span>${escapeHtml(f.label)}</span>
      <input type="text" name="${f.name}" value="${escapeHtml(current)}" list="${listId}" ${placeholder} autocapitalize="off" autocomplete="off" spellcheck="false" />
      <datalist id="${listId}">${options}</datalist>${help}</label>`;
  }

  const type = f.type === "number" ? "number" : "text";
  const step = f.step ? `step="${f.step}"` : "";
  const min = f.min != null ? `min="${f.min}"` : "";
  const placeholder = f.placeholder ? `placeholder="${escapeHtml(f.placeholder)}"` : "";
  return `<label><span>${escapeHtml(f.label)}${f.required ? " *" : ""}</span>
    <input type="${type}" name="${f.name}" value="${escapeHtml(value)}" ${step} ${min} ${placeholder} />${help}</label>`;
}


async function submitSettingsForm() {
  if (!settingsFormCtx) return;
  const { kind, row, onSaved } = settingsFormCtx;
  const cfg = SETTINGS_KINDS[kind];
  const errorEl = document.getElementById("settings-form-error");
  errorEl.textContent = "";

  const form = document.getElementById("settings-form");
  const payload = {};
  const activeFields = cfg.fields.filter(f => row || !f.editOnly);
  for (const f of activeFields) {
    if (f.type === "kinds") {
      payload[f.name] = [...form.querySelectorAll('input[name="kinds[]"]:checked')].map(el => el.value);
    } else {
      const raw = form.querySelector(`[name="${f.name}"]`).value;
      if (raw === "") {
        payload[f.name] = null;  // Pydantic Optional handles this
      } else if (f.type === "number") {
        payload[f.name] = Number(raw);
      } else {
        payload[f.name] = raw;
      }
    }
    if (f.required && (payload[f.name] === null || payload[f.name] === "" ||
        (Array.isArray(payload[f.name]) && payload[f.name].length === 0))) {
      errorEl.textContent = `${f.label} is required`;
      return;
    }
  }

  if (kind === "camera") {
    const originalMount = payload.mount;
    const normalizedMount = normalizeMountText(originalMount);
    if (normalizedMount !== originalMount) payload.mount = normalizedMount;
    const exactMount = canonicalMountMatch(payload.mount);
    if (exactMount) {
      payload.mount = exactMount;
    } else {
      const suggestion = similarMountSuggestion(payload.mount);
      if (suggestion && suggestion !== payload.mount) {
        const ok = await ask(`Mount "${payload.mount}" is very close to existing mount "${suggestion}". Save as a new mount anyway?`, { okLabel: "Save as new mount" });
        if (!ok) {
          errorEl.textContent = `Use existing mount "${suggestion}" or type a different new mount`;
          return;
        }
      }
    }
  }

  let saved;
  try {
    saved = row
      ? await api(`${cfg.endpoint}/${row.id}`, { method: "PATCH", body: JSON.stringify(payload) })
      : await api(cfg.endpoint, { method: "POST", body: JSON.stringify(payload) });
  } catch (err) {
    errorEl.textContent = err.message || String(err);
    return;
  }

  closeSettingsForm();
  await refreshSettingsList(kind);
  if (kind === "film-brand") await refreshSettingsList("film");
  if (kind === "camera") await refreshCameraMounts();
  if (onSaved && saved) onSaved(saved);
}

function normalizeMountText(value) {
  if (value == null) return null;
  const cleaned = String(value).trim().replace(/\s+/g, " ");
  return cleaned || null;
}

function mountKey(value) {
  return normalizeMountText(value)?.toLowerCase() || "";
}

function mountLooseKey(value) {
  return mountKey(value).replace(/[\s-]+/g, "");
}

function canonicalMountMatch(value) {
  const needle = mountKey(value);
  if (!needle) return null;
  return settingsState.cameraMounts.find(m => mountKey(m) === needle) || null;
}

function similarMountSuggestion(value) {
  const needle = mountLooseKey(value);
  if (!needle) return null;
  return settingsState.cameraMounts.find(m => mountLooseKey(m) === needle) || null;
}


// ---------- Library ----------

async function openLibrary() {
  showView("library");
  await loadLibrary({ rescan: true });
}

async function loadLibrary({ rescan = false } = {}) {
  try {
    state.library = await api("/api/library");
  } catch (e) {
    renderLibraryBanner(`Could not load the library index: ${e.message}`, "bad");
    return;
  }
  renderLibrary();
  if (rescan && state.library.root && state.library.reachable && !state.libraryRescanning) {
    await rescanLibrary();
  }
}

async function rescanLibrary() {
  if (state.libraryRescanning) return;
  state.libraryRescanning = true;
  const name = document.getElementById("library-root-name");
  const label = name.textContent;
  try {
    await runJob("/api/library/rescan", {
      body: {},
      onEvent: (ev) => { name.textContent = `${label} · scanning ${ev.scanned}…`; },
    });
    state.library = await api("/api/library");
    renderLibrary();
  } catch (e) {
    name.textContent = label;
    renderLibraryBanner(`Rescan failed: ${e.message}`, "bad");
  } finally {
    state.libraryRescanning = false;
  }
}

function renderLibraryBanner(message, level = "warn") {
  const el = document.getElementById("library-banner");
  if (!message) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  el.classList.remove("hidden");
  el.innerHTML = `<span class="pill ${level}">${level === "bad" ? "Problem" : "Note"}</span> <span>${escapeHtml(message)}</span>`;
}


function renderLibrary() {
  const lib = state.library;
  const rootEl = document.getElementById("library-root-name");
  const groupsEl = document.getElementById("library-groups");
  const summaryEl = document.getElementById("library-summary");
  renderLibraryBanner("");

  if (!lib || !lib.root) {
    rootEl.textContent = "";
    summaryEl.classList.add("hidden");
    groupsEl.innerHTML = `
      <div class="library-empty">
        <div>Point FilmEx at the top folder of your film library, the one with the year folders inside.</div>
        <button type="button" class="primary" id="library-empty-choose">Choose library folder…</button>
      </div>`;
    document.getElementById("library-empty-choose").addEventListener("click", () => void chooseLibraryFolder());
    return;
  }

  rootEl.textContent = baseName(lib.root);
  rootEl.title = lib.root;
  if (!lib.reachable) {
    renderLibraryBanner(
      `Library folder is not reachable right now (NAS unmounted?). Showing cached results` +
      (lib.scanned_at ? ` from ${formatStamp(lib.scanned_at)}.` : "."),
      "warn"
    );
  }

  const s = lib.summary || {};
  summaryEl.classList.remove("hidden");
  summaryEl.innerHTML = [
    summaryItem("Rolls", String(lib.folders.length)),
    summaryItem("Tagged", String(s.tagged || 0)),
    summaryItem("Partial", String(s.partial || 0)),
    summaryItem("Untagged", String(s.untagged || 0)),
    lib.scanned_at ? summaryItem("Scanned", formatStamp(lib.scanned_at)) : "",
  ].join("");

  const needle = (state.libraryFilter || "").trim().toLowerCase();
  const matches = (row) => !needle || [row.display_name, row.film_display, row.camera_display, row.status, row.parent]
    .some((v) => (v || "").toLowerCase().includes(needle));
  const groups = new Map();
  for (const row of lib.folders.filter(matches)) {
    const key = row.parent || row.display_name;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  const keys = Array.from(groups.keys()).sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));
  groupsEl.innerHTML = "";
  for (const key of keys) {
    const rows = groups.get(key).slice().sort((a, b) => {
      if (a.is_flat_year !== b.is_flat_year) return a.is_flat_year ? -1 : 1;
      return a.display_name.localeCompare(b.display_name, undefined, { numeric: true });
    });
    const section = document.createElement("section");
    section.className = "lib-group";
    section.innerHTML = `<h3 class="lib-group-title">${escapeHtml(key)} <span class="lib-group-count">${rows.length} folder${rows.length === 1 ? "" : "s"}</span></h3>`;
    const list = document.createElement("div");
    list.className = "lib-list";
    for (const row of rows) list.appendChild(renderLibraryRow(row));
    section.appendChild(list);
    groupsEl.appendChild(section);
  }
  if (!keys.length) {
    groupsEl.innerHTML = needle
      ? `<div class="library-empty">No rolls match “${escapeHtml(needle)}”.</div>`
      : `<div class="library-empty"><div>No folders with images under this library yet.</div><button type="button" class="primary" id="library-empty-open">Open folder…</button></div>`;
    document.getElementById("library-empty-open")?.addEventListener("click", () => void onOpenFolder());
  }
}

function summaryItem(label, value, valueClass = "") {
  const classes = ["summary-value"];
  if (valueClass) classes.push(valueClass);
  return `<span class="summary-item"><span class="summary-label">${escapeHtml(label)}</span><span class="${classes.join(" ")}">${escapeHtml(value)}</span></span>`;
}

const KIND_SHORT = { camera_scan_raw: "raw", lab_scan_jpeg: "jpg", positive_master_tiff: "tif", other: "other" };

function libraryCountsText(row) {
  const parts = Object.entries(row.kinds || {})
    .sort((a, b) => b[1] - a[1])
    .map(([kind, n]) => `${n} ${KIND_SHORT[kind] || kind}`);
  if (!parts.length) parts.push("no images");
  if (row.sidecar_count) parts.push(`${row.sidecar_count} xmp`);
  return parts.join(" · ");
}

function libraryStatusPill(row) {
  if (row.is_flat_year) return `<span class="pill warn lib-status" title="Loose files directly in a year folder">Unsorted</span>`;
  const cls = { tagged: "ok", partial: "warn", untagged: "", empty: "" }[row.status] || "";
  return `<span class="pill ${cls} lib-status">${escapeHtml(row.status)}</span>`;
}


function renderLibraryRow(row) {
  if (row.renditions?.length) return renderLibraryRoll(row);
  return renderLibraryFolderRow(row);
}

// A roll kept as RAW and JPG renditions: the roll's row carries the combined
// status and gear; one slimmer row per rendition carries the actions, since
// each rendition is tagged on its own (same gear, its own scan source).
function renderLibraryRoll(row) {
  const frag = document.createDocumentFragment();
  const head = document.createElement("div");
  head.className = `lib-row lib-roll ${row.status}`;
  head.dataset.path = row.path;
  const meta = [row.film_display, row.camera_display].filter(Boolean).join(" · ");
  head.innerHTML = `
    ${libraryStatusPill(row)}
    <span class="lib-name"><code title="${escapeHtml(row.path)}">${escapeHtml(row.display_name)}</code><span class="lib-sub">${escapeHtml(libraryCountsText(row))} · ${row.renditions.length} renditions</span></span>
    <span class="lib-meta" title="${escapeHtml(meta)}">${escapeHtml(meta || "")}</span>
    <span class="lib-date">${row.last_applied_at ? escapeHtml(formatStamp(row.last_applied_at, { dateOnly: true })) : ""}</span>
    <span class="lib-actions"><button type="button" class="secondary" data-act="gather" title="Move the renditions' files into the roll folder">Gather</button><button type="button" class="secondary" data-act="reveal" title="Show in Finder">Reveal</button></span>
  `;
  head.querySelector("[data-act=reveal]").addEventListener("click", () => onLibraryAction("reveal", row));
  head.querySelector("[data-act=gather]").addEventListener("click", () => void gatherRoll(row.path));
  frag.appendChild(head);
  for (const part of row.renditions) {
    const el = renderLibraryFolderRow({ ...part, display_name: part.rendition });
    el.classList.add("lib-rendition");
    frag.appendChild(el);
  }
  return frag;
}

function renderLibraryFolderRow(row) {
  const el = document.createElement("div");
  el.className = `lib-row ${row.status}`;
  el.dataset.path = row.path;
  const meta = [row.film_display, row.camera_display].filter(Boolean).join(" · ");
  const actions = [];
  if (row.status === "partial") {
    actions.push(`<button type="button" class="primary" data-act="remaining">Tag remaining ${row.untagged_count}</button>`);
  } else if (row.status === "untagged") {
    actions.push(`<button type="button" class="primary" data-act="open">Tag</button>`);
  } else {
    actions.push(`<button type="button" class="secondary" data-act="open">Open</button>`);
  }
  actions.push(`<button type="button" class="secondary" data-act="reveal" title="Show in Finder">Reveal</button>`);
  el.innerHTML = `
    ${libraryStatusPill(row)}
    <span class="lib-name"><code title="${escapeHtml(row.path)}">${escapeHtml(row.display_name)}</code><span class="lib-sub">${escapeHtml(libraryCountsText(row))}</span></span>
    <span class="lib-meta" title="${escapeHtml(meta)}">${escapeHtml(meta || "")}</span>
    <span class="lib-date">${row.last_applied_at ? escapeHtml(formatStamp(row.last_applied_at, { dateOnly: true })) : ""}</span>
    <span class="lib-actions">${actions.join("")}</span>
  `;
  el.querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", () => onLibraryAction(btn.dataset.act, row));
  });
  return el;
}

async function onLibraryAction(action, row) {
  if (action === "reveal") {
    await revealFolder(row.path);
    return;
  }
  if (action === "remaining") {
    try {
      const details = await api(`/api/library/folder?${new URLSearchParams({ path: row.path })}`);
      await openFolder(row.path, { onlyFiles: details.untagged_files });
    } catch (e) {
      renderLibraryBanner(`Could not read the folder: ${e.message}`, "bad");
    }
    return;
  }
  await openFolder(row.path);
}

function toggleLibraryMenu(open) {
  const menu = document.getElementById("library-menu");
  const btn = document.getElementById("library-menu-btn");
  const show = open === undefined ? menu.classList.contains("hidden") : open;
  menu.classList.toggle("hidden", !show);
  btn.setAttribute("aria-expanded", String(show));
}

async function onLibraryMenu(item) {
  toggleLibraryMenu(false);
  if (item === "rescan") await rescanLibrary();
  else if (item === "normalize") await openNamingPanel();
  else if (item === "reveal" && state.library?.root) await revealFolder(state.library.root);
  else if (item === "choose-root") await chooseLibraryFolder();
}

// ---------- Library: normalize folder names ----------

async function openNamingPanel() {
  const panel = document.getElementById("naming-panel");
  const status = document.getElementById("naming-status");
  panel.classList.remove("hidden");
  status.textContent = "Proposing names…";
  document.getElementById("naming-list").innerHTML = "";
  document.getElementById("naming-apply").disabled = true;
  try {
    state.naming = await api("/api/naming/propose", { method: "POST", body: JSON.stringify({}) });
    renderNamingPanel();
  } catch (e) {
    status.textContent = `Could not propose names: ${e.message}`;
  }
}

function closeNamingPanel() {
  document.getElementById("naming-panel").classList.add("hidden");
}

const NAMING_STATUS_LABEL = { rename: "Rename", conforms: "OK", skipped: "Skip", conflict: "Conflict" };
const NAMING_STATUS_CLASS = { rename: "warn", conforms: "ok", skipped: "", conflict: "bad" };

function renderNamingPanel() {
  const data = state.naming;
  const list = document.getElementById("naming-list");
  const counts = data.counts || {};
  document.getElementById("naming-template").textContent = data.template;
  document.getElementById("naming-counts").textContent =
    `${counts.rename || 0} to rename · ${counts.conforms || 0} already fine · ${counts.conflict || 0} conflicts · ${counts.skipped || 0} skipped`;
  const order = { rename: 0, conflict: 1, conforms: 2, skipped: 3 };
  const rows = data.proposals.slice().sort((a, b) => (order[a.status] - order[b.status]) || a.current.localeCompare(b.current, undefined, { numeric: true }));
  list.innerHTML = "";
  for (const p of rows) {
    const el = document.createElement("label");
    el.className = `naming-row ${p.status}`;
    const checkable = p.status === "rename";
    el.innerHTML = `
      <input type="checkbox" ${checkable ? "checked" : "disabled"} data-path="${escapeHtml(p.path)}" data-proposed="${escapeHtml(p.proposed || "")}" />
      <span class="pill ${NAMING_STATUS_CLASS[p.status] || ""}">${NAMING_STATUS_LABEL[p.status] || p.status}</span>
      <span class="naming-old" title="${escapeHtml(p.path)}">${escapeHtml((p.parent ? p.parent + "/" : "") + p.current)}</span>
      ${p.proposed && p.status !== "conforms"
        ? `<span class="naming-arrow">→</span><span class="naming-new" title="${escapeHtml(p.proposed)}">${escapeHtml(p.proposed)}</span>`
        : `<span></span><span class="naming-new"></span>`}
      ${p.reason ? `<span class="naming-reason">${escapeHtml(p.reason)}</span>` : ""}
    `;
    el.querySelector("input").addEventListener("change", updateNamingApplyButton);
    list.appendChild(el);
  }
  if (!rows.length) list.innerHTML = `<div class="naming-row skipped"><span></span><span></span><span class="naming-old">No roll folders found.</span></div>`;
  document.getElementById("naming-status").textContent = "";
  document.getElementById("naming-undo").classList.toggle("hidden", !state.namingLastOperation);
  updateNamingApplyButton();
}

function selectedRenames() {
  return Array.from(document.querySelectorAll("#naming-list input[type=checkbox]:checked"))
    .map((cb) => ({ path: cb.dataset.path, proposed: cb.dataset.proposed }))
    .filter((item) => item.proposed);
}

function updateNamingApplyButton() {
  const btn = document.getElementById("naming-apply");
  const n = selectedRenames().length;
  btn.disabled = n === 0;
  btn.textContent = n ? `Rename ${n} folder${n === 1 ? "" : "s"}` : "Rename";
}

async function applySelectedRenames() {
  const items = selectedRenames();
  if (!items.length) return;
  const ok = await ask(
    `Rename ${items.length} folder${items.length === 1 ? "" : "s"} and rewrite the Roll keyword on their tagged files?`,
    { okLabel: "Rename" }
  );
  if (!ok) return;
  const btn = document.getElementById("naming-apply");
  const status = document.getElementById("naming-status");
  btn.disabled = true;
  let renamed = 0;
  let keywordFiles = 0;
  try {
    const done = await runJob("/api/naming/apply", {
      body: { items },
      onEvent: (ev) => {
        if (ev.step === "rename" && ev.ok) {
          renamed += 1;
          status.textContent = `Renamed ${renamed}/${items.length}…`;
        } else if (ev.step === "keywords") {
          keywordFiles += ev.ok ? 1 : 0;
          status.textContent = `Renamed ${renamed}/${items.length} · keywords ${keywordFiles}…`;
        }
      },
    });
    state.namingLastOperation = done.operation_id;
    const problems = done.errors && done.errors.length ? ` · ${done.errors.length} problem${done.errors.length === 1 ? "" : "s"}` : "";
    const summary = `Renamed ${done.renamed.length} folder${done.renamed.length === 1 ? "" : "s"}, rewrote keywords on ${done.keyword_files} file${done.keyword_files === 1 ? "" : "s"}${problems}.`;
    status.textContent = summary;
    if (done.errors && done.errors.length) await notify(done.errors.join("\n"), "Some renames did not go through");
    await loadLibrary({ rescan: true });
    state.naming = await api("/api/naming/propose", { method: "POST", body: JSON.stringify({}) });
    renderNamingPanel();
    document.getElementById("naming-status").textContent = summary;
  } catch (e) {
    status.textContent = `Rename failed: ${e.message}`;
    btn.disabled = false;
  }
}

async function undoLastRename() {
  const opId = state.namingLastOperation;
  if (!opId) return;
  const ok = await ask("Put the renamed folders back under their previous names and restore their Roll keywords?", { okLabel: "Undo renames" });
  if (!ok) return;
  const status = document.getElementById("naming-status");
  status.textContent = "Undoing…";
  try {
    const done = await runJob(`/api/operations/${encodeURIComponent(opId)}/undo`);
    state.namingLastOperation = null;
    await loadLibrary({ rescan: true });
    state.naming = await api("/api/naming/propose", { method: "POST", body: JSON.stringify({}) });
    renderNamingPanel();
    const problems = done.errors && done.errors.length ? ` · ${done.errors.length} problem${done.errors.length === 1 ? "" : "s"}` : "";
    document.getElementById("naming-status").textContent = `Restored ${done.restored.length} folder${done.restored.length === 1 ? "" : "s"}${problems}.`;
  } catch (e) {
    status.textContent = `Undo failed: ${e.message}`;
  }
}


// ---------- Folder screen ----------
//
// A frame is every file sharing one basename (raw + JPEG + sidecar + .thm);
// the server lists them in sheet order. The folder starts as one roll; the
// user cuts the sequence into more, each with its own gear, film, date and
// folder name. The destination decides what the one action does: keep the
// rolls here (tag in place, or split into new folders when there are
// several), or copy or move them into the library.

const ROLL_HUE_COUNT = 8;
const NAME_DEBOUNCE_MS = 250;

function folderFrames() { return state.folder.frames || []; }

function sheetIndexOf(key) {
  if (!state.folder.indexByKey) {
    state.folder.indexByKey = new Map(folderFrames().map((f, i) => [f.key, i]));
  }
  const i = state.folder.indexByKey.get(key);
  return i === undefined ? -1 : i;
}

function frameByKey(key) {
  const i = sheetIndexOf(key);
  return i < 0 ? null : folderFrames()[i];
}

function sortedKeys(keys) {
  return Array.from(new Set(keys)).filter(k => sheetIndexOf(k) >= 0).sort((a, b) => sheetIndexOf(a) - sheetIndexOf(b));
}

function rollOf(key) {
  return state.folder.rolls.find(r => r.frames.includes(key)) || null;
}

function rollImages(roll) {
  return roll.frames.flatMap(k => frameByKey(k)?.images || []);
}

let rollSeq = 0;

// A new roll starts from the last used gear and processing fields, so the
// common case (same camera, next roll) needs no clicks.
function defaultTagging() {
  return {
    camera: state.camera || null,
    lens: state.lens || null,
    film: state.film || null,
    roll_date: state.folder.listing?.suggested_roll_date || "",
    place: "",
    development_provider_id: state.advanced.development_provider_id || "",
    development_method_id: state.advanced.development_method_id || "",
    scan_provider_id: state.advanced.scan_provider_id || "",
    scan_method_id: state.advanced.scan_method_id || "",
    roll_notes: "",
    replace_managed_metadata: false,
    file_keywords: {},   // name → "a, b"
    file_lens_ids: {},   // name → lens id
  };
}

function newRoll(frames, tagging = null) {
  rollSeq += 1;
  return {
    id: `roll${rollSeq}`,
    name: "",
    nameAuto: true,
    nameError: null,
    frames: sortedKeys(frames),
    tagging: tagging ? { ...tagging, file_keywords: { ...tagging.file_keywords }, file_lens_ids: { ...tagging.file_lens_ids } } : defaultTagging(),
    moreOpen: false,
    changes: null,       // { diffs } from the dry run, or null
    changesOpen: false,
    changesStale: false,
  };
}

function normalizeRolls() {
  const rolls = state.folder.rolls.filter(s => s.frames.length);
  for (const s of rolls) s.frames = sortedKeys(s.frames);
  rolls.sort((a, b) => sheetIndexOf(a.frames[0]) - sheetIndexOf(b.frames[0]));
  state.folder.rolls = rolls;
}


function unassignedKeys() {
  const assigned = new Set(state.folder.rolls.flatMap(r => r.frames));
  return folderFrames().map(f => f.key).filter(k => !assigned.has(k));
}

// ----- opening ------

// Open a folder on the Folder screen. `onlyFiles` limits the first roll to
// those image files (the untagged remainder of a partly tagged roll).
async function openFolder(path, { onlyFiles = null } = {}) {
  path = (path || "").trim().replace(/\/+$/, "");
  if (!path) return;
  const folder = state.folder;
  Object.assign(folder, {
    path, listing: null, frames: [], indexByKey: null, scanFiles: new Map(), thumbs: new Set(),
    manifest: null, inLibrary: false, rolls: [], selection: new Set(), anchor: null,
    cuts: { report: null, loading: false, ignored: new Set() }, destination: null, result: null,
  });
  showView("folder");
  renderFolderBanner("");
  renderFolderProgress(null);
  renderFolderResult(null);
  try {
    const [listing, scan, manifest] = await Promise.all([
      api(`/api/folder/frames?${new URLSearchParams({ path })}`),
      api("/api/folder/scan", { method: "POST", body: JSON.stringify({ folder: path }) }),
      api(`/api/manifest?${new URLSearchParams({ folder: path })}`).then(r => r.manifest).catch(() => null),
    ]);
    if (folder.path !== path) return;
    folder.listing = listing;
    folder.frames = listing.frames;
    folder.indexByKey = null;
    folder.scanFiles = new Map((scan.files || []).map(f => [f.name, f]));
    folder.manifest = manifest;
    folder.inLibrary = folderIsInLibrary(path);
    const seed = !manifest && listing.rendition ? await siblingSeed(listing.sibling_renditions || []) : null;
    if (folder.path !== path) return;
    // Inside the library, tagging part of a roll, or without a library: keep.
    // A new folder outside the library goes in the way the last one did.
    const keep = folder.inLibrary || !hasLibrary() || (onlyFiles && onlyFiles.length);
    folder.destination = keep ? "keep" : (state.settings.import_mode === "move" ? "move" : "copy");
    refreshThumbSet();
    folder.rolls = firstRolls(listing, manifest, onlyFiles, seed);
    pushRecentFolder(path);
    saveStickyState();
    renderFolder();
    for (const roll of folder.rolls) scheduleRollName(roll);
    void buildThumbnails();
    void loadCuts();
  } catch (e) {
    renderFolderBanner(`Could not read the folder: ${e.message}`, "bad");
    renderFolder();
  }
}

function folderIsInLibrary(path) {
  const root = (state.settings?.library_root || "").replace(/\/+$/, "");
  return !!root && (path === root || path.startsWith(root + "/"));
}

// The folder opens as one roll: every frame, or only the requested files.
// A manifest from an earlier tagging fills the roll's values.
function firstRolls(listing, manifest, onlyFiles, seed = null) {
  if (!listing.frames.length) return [];
  let keys = listing.frames.map(f => f.key);
  if (onlyFiles && onlyFiles.length) {
    const wanted = new Set(onlyFiles);
    keys = listing.frames.filter(f => f.images.some(n => wanted.has(n))).map(f => f.key);
  }
  if (!manifest && seed) {
    // Another rendition of this roll is tagged: same camera, lens, film,
    // date and place; the scan source is this rendition's own to pick.
    const { tagging } = taggingFromManifest(seed.manifest, []);
    Object.assign(tagging, { scan_provider_id: "", scan_method_id: "", file_keywords: {}, file_lens_ids: {} });
    renderFolderBanner(`Gear, film and date filled from the ${seed.from} rendition of this roll. Pick the scan provider and method for these files.`, "ok");
    return [newRoll(keys, tagging)];
  }
  if (!manifest) return [newRoll(keys)];
  const { tagging, unresolved } = taggingFromManifest(manifest, listing.frames.flatMap(f => f.images));
  if (unresolved.length) renderFolderBanner(`Loaded the roll's manifest. Could not match: ${unresolved.join(", ")}.`, "warn");
  return [newRoll(keys, tagging)];
}

// The first tagged sibling rendition's manifest, to seed an untagged one.
async function siblingSeed(paths) {
  for (const sibling of paths) {
    try {
      const res = await api(`/api/manifest?${new URLSearchParams({ folder: sibling })}`);
      if (res.manifest) return { manifest: res.manifest, from: baseName(sibling) };
    } catch {
      // no manifest there; try the next rendition
    }
  }
  return null;
}

// ----- thumbnails -----

function refreshThumbSet() {
  const folder = state.folder;
  folder.thumbs = new Set();
  for (const [name, file] of folder.scanFiles) {
    const inverted = state.previewInvert && !isPositiveFile(folder.path, name);
    if (inverted ? file.has_inverted_preview : file.has_thumbnail) folder.thumbs.add(name);
  }
}

async function buildThumbnails() {
  const folder = state.folder;
  const path = folder.path;
  const invert = state.previewInvert;
  if (!path || folder.thumbJob) return;
  const needed = folderFrames().filter(f => f.preview && !folder.thumbs.has(f.preview));
  if (!needed.length) return;
  folder.thumbJob = true;
  try {
    await runJob("/api/previews/create/stream", {
      body: { folder: path, invert },
      onEvent: (ev) => {
        if (folder.path !== path || state.previewInvert !== invert) return;
        if (ev.status === "created" || ev.status === "reused") {
          const file = folder.scanFiles.get(ev.file);
          if (file) file[invert ? "has_inverted_preview" : "has_thumbnail"] = true;
          folder.thumbs.add(ev.file);
          updateSheetThumb(ev.file);
        }
      },
    });
  } catch (e) {
    renderFolderBanner(`Thumbnails: ${e.message}`, "warn");
  } finally {
    folder.thumbJob = false;
    if (folder.path === path && state.previewInvert !== invert) void buildThumbnails();
  }
}

function toggleInvert() {
  state.previewInvert = !state.previewInvert;
  saveStickyState();
  refreshThumbSet();
  renderSheet();
  renderRollCards();
  void buildThumbnails();
}

function updateSheetThumb(file) {
  const container = document.getElementById("contact-sheet");
  const frame = folderFrames().find(f => f.preview === file);
  if (!frame) return;
  const cell = container.querySelector(`.sheet-cell[data-key="${cssEscape(frame.key)}"] .sheet-thumb`);
  if (!cell) return;
  const stamp = cell.querySelector(".sheet-roll-tag")?.outerHTML || "";
  cell.innerHTML = `<img src="${escapeHtml(buildThumbnailUrl(state.folder.path, file, state.previewInvert))}" alt="" loading="lazy" />` + stamp;
}

function cssEscape(value) {
  return window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/["\\]/g, "\\$&");
}

// ----- rendering -----


// ----- rendering -----

function renderFolderBanner(message, level = "warn") {
  const el = document.getElementById("folder-banner");
  if (!message) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  el.classList.remove("hidden");
  el.innerHTML = `<span class="pill ${level}">${level === "bad" ? "Problem" : "Note"}</span> <span>${escapeHtml(message)}</span>`;
}

function renderFolder() {
  const folder = state.folder;
  const has = !!folder.listing;
  document.getElementById("folder-name").textContent = folderLabel(folder.path);
  document.getElementById("folder-name").title = folder.path;
  renderFolderStatus();
  document.getElementById("sheet-section").classList.toggle("hidden", !has);
  document.getElementById("rolls-section").classList.toggle("hidden", !has);
  document.getElementById("folder-actions").classList.toggle("hidden", !has);
  renderFolderSummary();
  renderFolderRenditions();
  renderHeader();
  if (!has) return;
  renderSheet();
  renderCutsPanel();
  renderRollCards();
  renderDestination();
  updateSheetToolbar();
  updateRunButton();
}

function renderFolderStatus() {
  const el = document.getElementById("folder-status");
  const folder = state.folder;
  const parts = [];
  if (folder.inLibrary) parts.push("in library");
  const history = (folder.manifest?.apply_history || []).filter(h => !h.dry_run);
  if (history.length) parts.push(`tagged ${formatStamp(history[history.length - 1].timestamp, { dateOnly: true })}`);
  else if (folder.listing) parts.push("untagged");
  el.textContent = parts.join(" · ");
}

// A rendition folder (RAW, JPG…) names its roll and the other renditions,
// with the way to make them one folder.
function renderFolderRenditions() {
  const el = document.getElementById("folder-renditions");
  const listing = state.folder.listing;
  if (!listing || !listing.rendition) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  const others = (listing.sibling_renditions || []).map(baseName);
  const text = others.length
    ? `${listing.rendition} rendition of ${listing.roll_id}; ${others.join(", ")} beside it.`
    : `${listing.rendition} rendition of ${listing.roll_id}.`;
  el.classList.remove("hidden");
  el.innerHTML = `<span>${escapeHtml(text)}</span> <button type="button" class="action-link" id="folder-gather">Gather into one folder</button>`;
  document.getElementById("folder-gather").addEventListener("click", () => void gatherRoll(state.folder.path));
}

// Gather the roll that `path` belongs to: its renditions' files move up into
// the roll folder. Lightroom tracks files by path, so the confirmation says
// what that means before anything moves.
async function gatherRoll(path) {
  if (state.folder.running) return;
  let plan;
  try {
    plan = await api("/api/folder/gather", { method: "POST", body: JSON.stringify({ folder: path }) });
  } catch (e) {
    await notify(planErrorsText(e), "Nothing to gather");
    return;
  }
  if (!plan.ok) {
    await notify(plan.problems.join("\n"), "The roll cannot be gathered as it is");
    return;
  }
  const names = plan.items.map(i => i.rendition).join(" and ");
  const ok = await ask(
    `Move ${plan.file_count} file${plan.file_count === 1 ? "" : "s"} from ${names} into ${plan.roll_name}, merge their manifests, and remove the empty subfolders?\n\n` +
    `Lightroom knows these files by their current paths: a catalog that has them will show them as missing until you relink the folder (Library ▸ Find Missing Folder). ` +
    `That is harmless for developed JPEGs; for raws you have edited in Lightroom, move the folder in Lightroom instead.\n\nThe batch can be undone.`,
    { title: "Gather into one folder", okLabel: "Gather" }
  );
  if (!ok) return;
  if (state.folder.path !== plan.roll) await openFolder(plan.roll);
  const folder = state.folder;
  folder.running = true;
  updateRunButton();
  renderFolderProgress("Gathering…", 0);
  try {
    let total = 0;
    const done = await runJob("/api/folder/gather/apply", {
      body: { folder: path },
      onStart: (ev) => { total = ev.total || 0; setPageTitleProgress("Gathering", 0, total); },
      onEvent: (ev) => {
        if (ev.step === "move" && ev.file) { renderFolderProgress(`Moving ${ev.file}`, total ? ev.done / total : null); setPageTitleProgress("Gathering", ev.done, total); }
        else if (ev.step === "keywords" && ev.file) renderFolderProgress(`Roll keyword on ${ev.file}`, null);
      },
    });
    renderFolderProgress(null);
    const problems = done.errors?.length ? `\n\n${done.errors.join("\n")}` : "";
    await notify(`${done.moves.length} file${done.moves.length === 1 ? "" : "s"} moved into ${baseName(done.roll)}, ${done.removed_folders.length} subfolder${done.removed_folders.length === 1 ? "" : "s"} removed, Roll keyword updated on ${done.keyword_files.length}.${problems}`, "Roll gathered");
    if (hasLibrary()) void loadLibrary({ rescan: true });
    await openFolder(plan.roll);
    state.folder.lastOperation = done.operation_id || null;
    updateRunButton();
  } catch (e) {
    renderFolderProgress(null);
    await notify(planErrorsText(e), "Gathering failed");
  } finally {
    state.folder.running = false;
    resetPageTitle();
    updateRunButton();
  }
}

function renderFolderSummary() {
  const el = document.getElementById("folder-summary");
  const listing = state.folder.listing;
  if (!listing) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const frames = folderFrames();
  const images = frames.reduce((n, f) => n + f.images.length, 0);
  const bytes = frames.reduce((n, f) => n + f.bytes, 0);
  const unmatched = frames.filter(f => f.unmatched_jpeg).length;
  const parts = [
    summaryItem("Frames", `${frames.length}`),
    summaryItem("Files", `${images} image${images === 1 ? "" : "s"} · ${humanBytes(bytes)}`),
  ];
  const positives = frames.filter(f => f.positives?.length).length;
  if (positives) parts.push(summaryItem("Positives", `${positives} frame${positives === 1 ? "" : "s"} with a developed copy`));
  if (listing.has_raw) parts.push(summaryItem("Pairs", unmatched ? `${unmatched} JPEG${unmatched === 1 ? "" : "s"} without raw` : "raw + JPEG"));
  if (listing.other_files.length) parts.push(summaryItem("Stays behind", `${listing.other_files.length} other file${listing.other_files.length === 1 ? "" : "s"}`));
  el.innerHTML = parts.join("");
}

function renderSheet() {
  const folder = state.folder;
  const rollIndexByKey = new Map();
  folder.rolls.forEach((roll, i) => roll.frames.forEach(k => rollIndexByKey.set(k, i)));
  renderContactSheet(document.getElementById("contact-sheet"), {
    folder: folder.path,
    frames: folderFrames(),
    hasThumb: (file) => folder.thumbs.has(file),
    invert: state.previewInvert,
    rollIndex: (key) => (rollIndexByKey.has(key) ? rollIndexByKey.get(key) : null),
    selection: folder.selection,
    onClick: onSheetCellClick,
    cutAt: suggestedCutAt,
    onCut: cutBefore,
  });
}

// Generic grid. `rollIndex(key)` → number | null (null = left out, no
// stamp when every frame is in one roll). `selection` is a Set or null.
function renderContactSheet(container, { folder, frames, hasThumb, invert, rollIndex, selection, onClick, cutAt = null, onCut = null, compact = false }) {
  container.innerHTML = "";
  container.classList.toggle("compact", compact);
  const multi = new Set(frames.map(f => rollIndex ? rollIndex(f.key) : 0).filter(v => v !== null)).size > 1;
  const frag = document.createDocumentFragment();
  for (const frame of frames) {
    const index = rollIndex ? rollIndex(frame.key) : 0;
    const cell = document.createElement("div");
    cell.className = "sheet-cell";
    cell.dataset.key = frame.key;
    if (index === null) cell.classList.add("unassigned");
    else if (multi) cell.classList.add(`roll-hue-${index % ROLL_HUE_COUNT}`);
    if (selection && selection.has(frame.key)) cell.classList.add("selected");
    const thumb = frame.preview && hasThumb(frame.preview)
      ? `<img src="${escapeHtml(buildThumbnailUrl(folder, frame.preview, invert))}" alt="" loading="lazy" />`
      : `<span class="sheet-thumb-placeholder">${escapeHtml(frame.display_key)}</span>`;
    const stamp = index !== null && multi ? `<span class="sheet-roll-tag">R${index + 1}</span>` : "";
    const badge = frame.positives?.length ? `<span class="sheet-badge ok" title="${escapeHtml(frame.positives.join("\n"))}">positive</span>`
      : frame.unmatched_jpeg ? `<span class="sheet-badge warn">jpg only</span>`
      : frame.has_raw && frame.images.length > 1 ? `<span class="sheet-badge">pair</span>` : "";
    const cut = cutAt ? cutAt(frame.key) : null;
    if (cut) cell.classList.add(`cut-${cut.strength}`);
    const scissors = cut ? `<button type="button" class="sheet-cut-tag" title="${escapeHtml(cutReasonText(cut))}" aria-label="Cut here">✂</button>` : "";
    const openName = frame.preview || frame.images[0];
    const magnifier = openName ? `<button type="button" class="sheet-open" title="Open ${escapeHtml(openName)}" aria-label="Open ${escapeHtml(openName)}">${MAGNIFIER_ICON}</button>` : "";
    cell.innerHTML = `
      <div class="sheet-thumb">${thumb}${stamp}${scissors}${magnifier}</div>
      <div class="sheet-cell-name"><span>${escapeHtml(frame.display_key)}</span>${badge}</div>
    `;
    cell.title = frame.files.join("\n");
    if (onClick) cell.addEventListener("click", (ev) => onClick(frame.key, ev));
    if (cut && onCut) cell.querySelector(".sheet-cut-tag").addEventListener("click", (ev) => { ev.stopPropagation(); onCut(frame.key); });
    if (openName) cell.querySelector(".sheet-open").addEventListener("click", (ev) => { ev.stopPropagation(); void openSourceFile(openName); });
    frag.appendChild(cell);
  }
  container.appendChild(frag);
}


function onSheetCellClick(key, ev) {
  const folder = state.folder;
  if (ev.shiftKey && folder.anchor && sheetIndexOf(folder.anchor) >= 0) {
    const [a, b] = [sheetIndexOf(folder.anchor), sheetIndexOf(key)].sort((x, y) => x - y);
    for (let i = a; i <= b; i++) folder.selection.add(folderFrames()[i].key);
  } else if (ev.metaKey || ev.ctrlKey) {
    if (folder.selection.has(key)) folder.selection.delete(key); else folder.selection.add(key);
    folder.anchor = key;
  } else {
    if (folder.selection.size === 1 && folder.selection.has(key)) folder.selection.clear();
    else { folder.selection = new Set([key]); }
    folder.anchor = key;
  }
  renderSheet();
  updateSheetToolbar();
}

function updateSheetToolbar() {
  const folder = state.folder;
  const n = folder.selection.size;
  const label = document.getElementById("sheet-selection");
  label.textContent = n
    ? `${n} frame${n === 1 ? "" : "s"} selected`
    : "Click a frame, shift-click for a range, ⌘-click to add. Arrow keys move, C cuts, X leaves out.";
  document.getElementById("cuts-rhythm").value = String(state.settings?.frames_per_roll ?? 36);
  document.getElementById("sheet-cut").disabled = n !== 1 || !canCutBefore(firstSelectedKey());
  document.getElementById("sheet-new-roll").disabled = n === 0;
  document.getElementById("sheet-leave-out").disabled = n === 0 || !Array.from(folder.selection).some(k => rollOf(k));
  document.getElementById("sheet-clear").classList.toggle("hidden", n === 0);
  const invert = document.getElementById("sheet-invert");
  invert.setAttribute("aria-pressed", String(!!state.previewInvert));
  invert.textContent = state.previewInvert ? "Inverted" : "Invert";
  const unmatched = folderFrames().filter(f => f.unmatched_jpeg);
  const toggle = document.getElementById("sheet-unmatched");
  toggle.classList.toggle("hidden", !unmatched.length);
  if (unmatched.length) {
    const allOut = unmatched.every(f => !rollOf(f.key));
    toggle.textContent = allOut ? `Include ${unmatched.length} JPEG-only frame${unmatched.length === 1 ? "" : "s"}` : `Leave out ${unmatched.length} JPEG-only frame${unmatched.length === 1 ? "" : "s"}`;
    toggle.dataset.mode = allOut ? "include" : "exclude";
  }
}

function firstSelectedKey() {
  const keys = sortedKeys(Array.from(state.folder.selection));
  return keys[0] || null;
}

function canCutBefore(key) {
  if (!key) return false;
  const roll = rollOf(key);
  return !!roll && roll.frames[0] !== key;
}

// ----- roll operations -----


// ----- roll operations -----

function cutBeforeSelected() {
  cutBefore(firstSelectedKey());
}

// Start a new roll at `key`: the frames from it to the end of its roll
// become the next roll, with the same gear.
function cutBefore(key) {
  const roll = rollOf(key);
  if (!roll || !canCutBefore(key)) return false;
  const at = roll.frames.indexOf(key);
  const tail = roll.frames.slice(at);
  roll.frames = roll.frames.slice(0, at);
  const next = newRoll(tail, roll.tagging);
  const pos = state.folder.rolls.indexOf(roll);
  state.folder.rolls.splice(pos + 1, 0, next);
  state.folder.selection.clear();
  afterRollsChanged([roll, next]);
  return true;
}

function newRollFromSelection() {
  const keys = sortedKeys(Array.from(state.folder.selection));
  if (!keys.length) return;
  const touched = new Set();
  for (const roll of state.folder.rolls) {
    const before = roll.frames.length;
    roll.frames = roll.frames.filter(k => !keys.includes(k));
    if (roll.frames.length !== before) touched.add(roll);
  }
  const template = rollOf(keys[0]) || state.folder.rolls[0];
  const next = newRoll(keys, template ? template.tagging : null);
  state.folder.rolls.push(next);
  state.folder.selection.clear();
  afterRollsChanged([...touched, next]);
}

function leaveOutSelection() {
  const keys = new Set(state.folder.selection);
  const touched = [];
  for (const roll of state.folder.rolls) {
    const before = roll.frames.length;
    roll.frames = roll.frames.filter(k => !keys.has(k));
    if (roll.frames.length !== before) touched.push(roll);
  }
  state.folder.selection.clear();
  afterRollsChanged(touched);
}

function addSelectionToRoll(id) {
  const target = state.folder.rolls.find(r => r.id === id);
  const keys = Array.from(state.folder.selection);
  if (!target || !keys.length) return;
  const touched = new Set([target]);
  for (const roll of state.folder.rolls) {
    if (roll === target) continue;
    const before = roll.frames.length;
    roll.frames = roll.frames.filter(k => !keys.includes(k));
    if (roll.frames.length !== before) touched.add(roll);
  }
  target.frames = sortedKeys([...target.frames, ...keys]);
  state.folder.selection.clear();
  afterRollsChanged([...touched]);
}

function mergeRollIntoPrevious(id) {
  const rolls = state.folder.rolls;
  const pos = rolls.findIndex(r => r.id === id);
  if (pos <= 0) return;
  rolls[pos - 1].frames = sortedKeys([...rolls[pos - 1].frames, ...rolls[pos].frames]);
  rolls.splice(pos, 1);
  afterRollsChanged([rolls[pos - 1]]);
}

function removeRoll(id) {
  state.folder.rolls = state.folder.rolls.filter(r => r.id !== id);
  afterRollsChanged([]);
}

function toggleUnmatchedFrames(mode) {
  const unmatched = folderFrames().filter(f => f.unmatched_jpeg).map(f => f.key);
  if (mode === "exclude") {
    state.folder.selection = new Set(unmatched);
    leaveOutSelection();
    return;
  }
  // Include: each JPEG-only frame joins the roll of the nearest earlier
  // assigned frame, else the first roll.
  const touched = new Set();
  for (const key of unmatched) {
    if (rollOf(key)) continue;
    let target = null;
    for (let i = sheetIndexOf(key) - 1; i >= 0 && !target; i--) target = rollOf(folderFrames()[i].key);
    target = target || state.folder.rolls[0];
    if (!target) { state.folder.rolls.push(newRoll([key])); target = state.folder.rolls[0]; }
    else target.frames = sortedKeys([...target.frames, key]);
    touched.add(target);
  }
  afterRollsChanged([...touched]);
}

function clearSheetSelection() {
  state.folder.selection.clear();
  renderSheet();
  updateSheetToolbar();
}

function afterRollsChanged(touched) {
  normalizeRolls();
  for (const roll of state.folder.rolls) markChangesStale(roll);
  renderSheet();
  renderCutsPanel();
  renderRollCards();
  updateSheetToolbar();
  renderDestination();
  updateRunButton();
  for (const roll of touched) if (state.folder.rolls.includes(roll)) scheduleRollName(roll);
}

// ----- suggested cuts -----
//
// The server scores where a new roll probably starts (capture-time gaps
// weighed by the frames-per-roll rhythm, file-name counter resets, frame
// shape changes). Each suggestion is a marker on the sheet and a seam card
// below it; the user cuts there or ignores it. A suggestion disappears once
// its frame already starts a roll.

async function loadCuts() {
  const folder = state.folder;
  const path = folder.path;
  if (!path || !folder.listing || folder.listing.frames.length < 2) return;
  folder.cuts.loading = true;
  renderCutsPanel();
  try {
    const rhythm = state.settings?.frames_per_roll ?? 36;
    const report = await api(`/api/folder/cuts?${new URLSearchParams({ path, frames_per_roll: String(rhythm) })}`);
    if (folder.path !== path) return;
    folder.cuts.report = report;
  } catch (e) {
    if (folder.path !== path) return;
    folder.cuts.report = null;
    renderFolderBanner(`Could not look for cuts: ${e.message}`, "warn");
  } finally {
    if (folder.path === path) {
      folder.cuts.loading = false;
      renderSheet();
      renderCutsPanel();
    }
  }
}

// Suggestions still worth showing: not ignored, and not already a cut.
function activeCuts() {
  const { report, ignored } = state.folder.cuts;
  if (!report) return [];
  return report.cuts.filter(c => !ignored.has(c.key) && !startsRoll(c.key));
}

function startsRoll(key) {
  const roll = rollOf(key);
  if (!roll) return true;
  return roll.frames[0] === key;
}

function suggestedCutAt(key) {
  return activeCuts().find(c => c.key === key) || null;
}

function cutReasonText(cut) {
  const parts = cut.reasons.map((r) => {
    if (r.kind === "gap") return `${humanDuration(r.seconds)} pause`;
    if (r.kind === "counter_reset") return `numbering restarts at ${r.to}`;
    if (r.kind === "aspect") return `frame shape changes, ${r.from} to ${r.to}`;
    return r.kind;
  });
  return `${parts.join(", ")} · ${cut.frames_before} frame${cut.frames_before === 1 ? "" : "s"} before`;
}

const CUT_STRENGTH_LABEL = { sure: "New roll", likely: "Probably a new roll", maybe: "A pause, maybe a break" };

function renderCutsPanel() {
  const panel = document.getElementById("cuts-panel");
  const summary = document.getElementById("cuts-summary");
  const list = document.getElementById("cuts-list");
  const applyAll = document.getElementById("cuts-apply-all");
  const folder = state.folder;
  const cuts = activeCuts();
  const show = folder.cuts.loading || cuts.length > 0;
  panel.classList.toggle("hidden", !show);
  if (!show) { list.innerHTML = ""; return; }
  if (folder.cuts.loading) {
    summary.textContent = "Looking for cuts…";
    applyAll.classList.add("hidden");
    list.innerHTML = "";
    return;
  }
  const report = folder.cuts.report;
  const source = report.time_source === "exif" ? "capture times" : report.time_source === "file" ? "file times" : "times";
  const pace = report.typical_gap_seconds ? ` (${humanDuration(report.typical_gap_seconds)} between frames)` : "";
  const firm = cuts.filter(c => c.strength !== "maybe");
  summary.textContent = `${cuts.length} suggested cut${cuts.length === 1 ? "" : "s"} from ${source}${pace}, counters and frame shapes.`;
  applyAll.classList.toggle("hidden", firm.length < 2);
  applyAll.textContent = `Cut all ${firm.length}`;
  list.innerHTML = "";
  for (const cut of cuts) list.appendChild(renderSeam(cut));
}

function renderSeam(cut) {
  const folder = state.folder;
  const el = document.createElement("div");
  el.className = `seam seam-${cut.strength}`;
  const figure = (key) => {
    const frame = frameByKey(key);
    const thumb = frame?.preview && folder.thumbs.has(frame.preview)
      ? `<img src="${escapeHtml(buildThumbnailUrl(folder.path, frame.preview, state.previewInvert))}" alt="" loading="lazy" />`
      : `<span class="sheet-thumb-placeholder">${escapeHtml(frame ? frame.display_key : key)}</span>`;
    return `<figure><div class="seam-thumb">${thumb}</div><figcaption>${escapeHtml(frame ? frame.display_key : key)}</figcaption></figure>`;
  };
  el.innerHTML = `
    <div class="seam-pair">${figure(cut.previous)}<span class="seam-mark">✂</span>${figure(cut.key)}</div>
    <div class="seam-text">
      <div class="seam-label">${CUT_STRENGTH_LABEL[cut.strength] || cut.strength}</div>
      <div class="lib-sub">${escapeHtml(cutReasonText(cut))}</div>
    </div>
    <div class="seam-actions">
      <button type="button" class="primary" data-act="cut">Cut here</button>
      <button type="button" class="action-link" data-act="ignore">Ignore</button>
    </div>`;
  el.querySelector("[data-act=cut]").addEventListener("click", () => cutBefore(cut.key));
  el.querySelector("[data-act=ignore]").addEventListener("click", () => {
    folder.cuts.ignored.add(cut.key);
    renderSheet();
    renderCutsPanel();
  });
  for (const fig of el.querySelectorAll("figure")) {
    fig.addEventListener("click", () => {
      folder.selection = new Set([cut.key]);
      folder.anchor = cut.key;
      renderSheet();
      updateSheetToolbar();
      scrollCellIntoView(cut.key);
    });
  }
  return el;
}

function applyAllCuts() {
  const keys = activeCuts().filter(c => c.strength !== "maybe").map(c => c.key);
  for (const key of keys) cutBefore(key);
}

function setRhythm(value) {
  const rhythm = Number(value);
  if (state.settings) state.settings.frames_per_roll = rhythm;
  queueSettingsPatch({ frames_per_roll: rhythm });
  void loadCuts();
}

function humanDuration(seconds) {
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const rest = m - h * 60;
  return rest ? `${h} h ${rest} min` : `${h} h`;
}

// ----- keyboard -----
//
// On the sheet: arrow keys move the cursor (shift extends the selection),
// C cuts before the cursor, X leaves the selection out, Enter takes a
// suggested cut at the cursor, Escape clears. Off while typing in a field
// or while a dialog is open.

function onSheetKey(e) {
  if (state.view !== "folder" || !state.folder.listing || state.folder.running) return;
  if (!document.getElementById("dialog").classList.contains("hidden")) return;
  if (!document.getElementById("picker-panel").classList.contains("hidden")) return;
  if (!document.getElementById("settings-form-panel").classList.contains("hidden")) return;
  const tag = (document.activeElement?.tagName || "").toLowerCase();
  if (["input", "select", "textarea"].includes(tag) || document.activeElement?.isContentEditable) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const folder = state.folder;
  const frames = folderFrames();
  if (!frames.length) return;
  const cursor = folder.anchor && sheetIndexOf(folder.anchor) >= 0 ? sheetIndexOf(folder.anchor) : -1;
  const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -sheetColumns(), ArrowDown: sheetColumns() }[e.key];
  if (step !== undefined) {
    e.preventDefault();
    const next = Math.max(0, Math.min(frames.length - 1, cursor < 0 ? 0 : cursor + step));
    moveCursor(frames[next].key, e.shiftKey);
    return;
  }
  const key = e.key.toLowerCase();
  if (key === "c") {
    e.preventDefault();
    if (folder.anchor) cutBefore(folder.anchor);
  } else if (key === "x") {
    e.preventDefault();
    if (folder.selection.size) leaveOutSelection();
  } else if (e.key === "Enter") {
    if (folder.anchor && suggestedCutAt(folder.anchor)) { e.preventDefault(); cutBefore(folder.anchor); }
  } else if (e.key === "Escape") {
    clearSheetSelection();
  }
}

function moveCursor(key, extend) {
  const folder = state.folder;
  if (extend && folder.anchor && sheetIndexOf(folder.anchor) >= 0) {
    const [a, b] = [sheetIndexOf(folder.anchor), sheetIndexOf(key)].sort((x, y) => x - y);
    for (let i = a; i <= b; i++) folder.selection.add(folderFrames()[i].key);
  } else {
    folder.selection = new Set([key]);
    folder.anchor = key;
  }
  renderSheet();
  updateSheetToolbar();
  scrollCellIntoView(key);
}

function sheetColumns() {
  const cells = document.querySelectorAll("#contact-sheet .sheet-cell");
  if (!cells.length) return 1;
  const top = cells[0].offsetTop;
  let n = 0;
  for (const cell of cells) { if (cell.offsetTop !== top) break; n += 1; }
  return Math.max(1, n);
}

function scrollCellIntoView(key) {
  document.querySelector(`#contact-sheet .sheet-cell[data-key="${cssEscape(key)}"]`)?.scrollIntoView({ block: "nearest" });
}

// ----- roll cards -----

function rollRangeText(roll) {
  const first = frameByKey(roll.frames[0]);
  const last = frameByKey(roll.frames[roll.frames.length - 1]);
  const n = roll.frames.length;
  const files = roll.frames.reduce((acc, k) => acc + (frameByKey(k)?.files.length || 0), 0);
  const bytes = roll.frames.reduce((acc, k) => acc + (frameByKey(k)?.bytes || 0), 0);
  const range = first && last ? (first === last ? first.display_key : `${first.display_key} – ${last.display_key}`) : "";
  return `${n} frame${n === 1 ? "" : "s"} · ${files} file${files === 1 ? "" : "s"} · ${humanBytes(bytes)}${range ? ` · ${range}` : ""}`;
}

// With one roll kept in place the folder is the roll: no cutting colours,
// no new folder name.
function singleRollInPlace() {
  return state.folder.destination === "keep" && state.folder.rolls.length === 1;
}

function renderRollCards() {
  const host = document.getElementById("roll-cards");
  host.innerHTML = "";
  const rolls = state.folder.rolls;
  document.getElementById("rolls-heading").textContent = rolls.length === 1 ? "Roll" : "Rolls";
  if (!rolls.length) {
    host.innerHTML = `<div class="library-empty">No rolls yet. Select frames on the sheet and press “New roll from selection”.</div>`;
    return;
  }
  rolls.forEach((roll, i) => host.appendChild(renderRollCard(roll, i)));
  const left = unassignedKeys().length;
  if (left) {
    const note = document.createElement("div");
    note.className = "lib-sub roll-left-note";
    note.textContent = `${left} frame${left === 1 ? "" : "s"} left out: they stay as they are.`;
    host.appendChild(note);
  }
}

// Line drawings for the gear tiles: faint when empty, in the accent colour
// once something is picked. A picked film shows its brand's cassette.
const TILE_GLYPHS = {
  camera: `<svg class="tile-glyph" viewBox="0 0 44 32" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true" focusable="false">
    <rect x="2" y="9" width="40" height="21" rx="3"/>
    <path d="M10 9V6.5a1.5 1.5 0 0 1 1.5-1.5h7a1.5 1.5 0 0 1 1.5 1.5V9"/>
    <rect x="30" y="5" width="6" height="4" rx="1"/>
    <circle cx="22" cy="19.5" r="7"/><circle cx="22" cy="19.5" r="3.5"/>
    <circle cx="36.5" cy="14" r="1.5"/><path d="M6 14h4"/>
  </svg>`,
  lens: `<svg class="tile-glyph" viewBox="0 0 44 32" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true" focusable="false">
    <rect x="6" y="7" width="28" height="18" rx="2"/>
    <path d="M34 10h4a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-4"/>
    <path d="M6 11H3.5a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1H6"/>
    <path d="M13 7v18M17 7v18M25 7v18M28 7v18"/>
    <path d="M39 13.5c1.5 0 2.5 1.1 2.5 2.5s-1 2.5-2.5 2.5"/>
  </svg>`,
  film: `<svg class="tile-glyph tile-glyph-film" viewBox="0 0 24 36" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true" focusable="false">
    <rect x="9.5" y="1" width="5" height="2.4" rx="0.5"/>
    <rect x="3" y="3.4" width="18" height="3.8" rx="1"/>
    <rect x="4" y="7.2" width="16" height="27.8" rx="1.6"/>
    <path d="M4 11h16M4 26h16"/>
    <rect x="19" y="13" width="2.4" height="9" rx="0.5"/>
  </svg>`,
};

function compactTile(kind, entity, roll) {
  const label = { camera: "Camera", lens: "Lens", film: "Film" }[kind];
  const copy = !entity ? null
    : kind === "camera" ? { title: entity.display_name, sub: cameraTypeLabel(entity.camera_type) || entity.mount || "" }
    : kind === "lens" ? { title: entity.display_name, sub: [entity.focal_length_mm && `${entity.focal_length_mm}mm`, entity.max_aperture && `f/${entity.max_aperture}`].filter(Boolean).join(" · ") }
    : { title: entity.display_name, sub: `ISO ${entity.box_speed_iso} · ${filmTypeLabel(entity.film_type)}` };
  const colors = kind === "film" && entity ? filmBrandColors(entity.brand) : null;
  const style = colors ? ` style="--brand-c1:${escapeHtml(colors[0])};--brand-c2:${escapeHtml(colors[1])}"` : "";
  return `
    <div class="tile compact ${kind}-tile${entity ? " selected" : ""}" data-kind="${kind}" data-roll="${roll.id}"${style}>
      <div class="tile-label">${label}</div>
      <div class="tile-content${entity ? "" : " empty"}">${entity ? escapeHtml(copy.title) : `Pick a ${kind}`}</div>
      ${entity ? `<div class="tile-sub">${escapeHtml(copy.sub)}</div>` : ""}
      ${entity ? `<button type="button" class="tile-clear" aria-label="Clear ${kind}" title="Clear">✕</button>` : ""}
      ${colors ? canisterSvg("tile-canister") : TILE_GLYPHS[kind]}
    </div>`;
}

function optionsHtml(items, selectedId, labelOf = (i) => i.display_name || i.name) {
  return ['<option value="">(none)</option>', ...items.map(i =>
    `<option value="${escapeHtml(i.id)}"${i.id === selectedId ? " selected" : ""}>${escapeHtml(labelOf(i))}</option>`)].join("");
}


function renderRollCard(roll, index) {
  const el = document.createElement("div");
  const single = singleRollInPlace();
  el.className = `roll-card${single ? " single" : ` roll-hue-${index % ROLL_HUE_COUNT}`}`;
  el.dataset.id = roll.id;
  el.innerHTML = rollCardHead(roll, index) + (single ? "" : rollCardName(roll)) + rollCardGear(roll) + rollCardFields(roll) + rollCardMore(roll) + rollCardChanges(roll);
  wireRollActions(el, roll);
  if (!single) wireRollName(el, roll);
  wireRollGear(el, roll);
  wireRollFields(el, roll);
  wireRollExpanders(el, roll);
  return el;
}

function rollCardHead(roll, index) {
  const selected = state.folder.selection.size;
  const single = singleRollInPlace();
  const title = single ? folderLabel(state.folder.path) : `Roll ${index + 1}`;
  return `
    <div class="roll-card-head">
      <span class="roll-card-index">${escapeHtml(title)}</span>
      <span class="roll-card-range">${escapeHtml(rollRangeText(roll))}</span>
      <span class="roll-card-actions">
        <button type="button" class="action-link" data-act="add" ${selected ? "" : "disabled"}>Add ${selected ? `${selected} selected` : "selection"}</button>
        ${index > 0 ? `<button type="button" class="action-link" data-act="merge">Merge into roll ${index}</button>` : ""}
        ${single ? "" : `<button type="button" class="action-link" data-act="remove">Leave out</button>`}
      </span>
    </div>`;
}

function rollCardName(roll) {
  return `
    <div class="roll-card-name">
      <span class="tile-label">Folder name</span>
      <input type="text" class="roll-name-input" value="${escapeHtml(roll.name)}" placeholder="from the template…" spellcheck="false" />
      <button type="button" class="action-link roll-name-auto${roll.nameAuto ? " hidden" : ""}">use template</button>
      <span class="roll-name-error${roll.nameError ? "" : " hidden"}">${escapeHtml(roll.nameError || "")}</span>
    </div>`;
}

function rollCardGear(roll) {
  const t = roll.tagging;
  return `
    <div class="tile-row compact">
      ${compactTile("camera", t.camera, roll)}
      ${compactTile("lens", t.lens, roll)}
      ${compactTile("film", t.film, roll)}
    </div>`;
}

function rollCardFields(roll) {
  const t = roll.tagging;
  return `
    <div class="field-grid compact">
      <label><span>Roll date</span><input type="date" data-field="roll_date" value="${escapeHtml(t.roll_date || "")}" /></label>
      <label><span>Place / title</span><input type="text" data-field="place" value="${escapeHtml(t.place || "")}" placeholder="Jāņos, Mexico…" /></label>
    </div>
    <div class="roll-card-links">
      <button type="button" class="action-link expander${roll.moreOpen ? " open" : ""}" data-expand="more" aria-expanded="${roll.moreOpen}">${CHEVRON_ICON}<span>${roll.moreOpen ? "Less" : "More"}</span></button>
      <button type="button" class="action-link expander${roll.changesOpen ? " open" : ""}" data-expand="changes" aria-expanded="${roll.changesOpen}">${CHEVRON_ICON}<span>${roll.changesOpen ? "Hide changes" : "Changes"}</span></button>
    </div>`;
}

function rollCardMore(roll) {
  const t = roll.tagging;
  const devProviders = state.providers.filter(p => p.kinds.includes("development"));
  const scanProviders = state.providers.filter(p => p.kinds.includes("scan"));
  return `
    <div class="field-grid compact roll-more${roll.moreOpen ? "" : " hidden"}">
      <label><span>Development provider</span><select data-field="development_provider_id">${optionsHtml(devProviders, t.development_provider_id)}</select></label>
      <label><span>Development method</span><select data-field="development_method_id">${optionsHtml(state.devMethods, t.development_method_id)}</select></label>
      <label><span>Scan provider</span><select data-field="scan_provider_id">${optionsHtml(scanProviders, t.scan_provider_id)}</select></label>
      <label><span>Scan method</span><select data-field="scan_method_id">${optionsHtml(state.scanMethods, t.scan_method_id)}</select></label>
      <label class="full"><span>Roll notes</span><input type="text" data-field="roll_notes" value="${escapeHtml(t.roll_notes || "")}" /></label>
      <label class="checkbox-field full">
        <span>Existing FilmEx metadata</span>
        <span class="checkbox-row">
          <input type="checkbox" data-field="replace_managed_metadata" ${t.replace_managed_metadata ? "checked" : ""} />
          <span>Replace instead of merge</span>
        </span>
      </label>
    </div>`;
}

// The dry run for this roll: per file, what will be written, with the
// per-file lens override and extra keywords.
function rollCardChanges(roll) {
  if (!roll.changesOpen) return `<div class="roll-changes hidden"></div>`;
  if (!roll.changes) return `<div class="roll-changes"><span class="lib-sub">Checking…</span></div>`;
  const stale = roll.changesStale ? `<div class="roll-changes-stale"><span class="pill warn">Changed since</span> <button type="button" class="action-link" data-act="refresh-changes">Check again</button></div>` : "";
  return `<div class="roll-changes">${stale}${roll.changes.diffs.map(file => diffFileHtml(roll, file)).join("")}</div>`;
}

function diffFileHtml(roll, file) {
  const t = roll.tagging;
  const thumb = state.folder.thumbs.has(file.name)
    ? `<button type="button" class="diff-thumb-link" data-open="${escapeHtml(file.name)}" title="Open ${escapeHtml(file.name)}" aria-label="Open ${escapeHtml(file.name)}"><img class="diff-thumb" src="${escapeHtml(buildThumbnailUrl(state.folder.path, file.name, state.previewInvert))}" alt="" loading="lazy" /><span class="thumb-open">${MAGNIFIER_ICON}</span></button>`
    : "";
  const rows = file.changes.map(ch => `
      <div class="diff-row">
        <span class="diff-tag">${escapeHtml(ch.tag)}</span>
        <span class="diff-action ${ch.action}">${ch.action}</span>
        <span class="diff-value">${formatValue(ch.after)}</span>
      </div>`).join("");
  return `
    <div class="diff-file" data-file="${escapeHtml(file.name)}">
      ${thumb}
      <div class="diff-file-main">
        <div class="diff-file-header">
          <div class="diff-file-name">${escapeHtml(file.name)}</div>
          <div class="diff-file-controls">
            <select class="diff-file-lens-select" aria-label="Lens for this frame">${lensOverrideOptions(t.camera, t.file_lens_ids[file.name] || "")}</select>
            <input type="text" class="diff-file-keyword-input" value="${escapeHtml(t.file_keywords[file.name] || "")}" placeholder="extra keywords, comma-separated" />
          </div>
        </div>
        <div class="diff-changes">${rows}</div>
      </div>
    </div>`;
}

function wireRollActions(el, roll) {
  el.querySelector("[data-act=add]").addEventListener("click", () => addSelectionToRoll(roll.id));
  el.querySelector("[data-act=merge]")?.addEventListener("click", () => mergeRollIntoPrevious(roll.id));
  el.querySelector("[data-act=remove]")?.addEventListener("click", () => removeRoll(roll.id));
}

// Typing a name takes it out of the template's hands until "use template".
function wireRollName(el, roll) {
  el.querySelector(".roll-name-input").addEventListener("input", (e) => {
    roll.name = e.target.value;
    roll.nameAuto = false;
    roll.nameError = null;
    el.querySelector(".roll-name-auto").classList.remove("hidden");
    el.querySelector(".roll-name-error").classList.add("hidden");
    markChangesStale(roll);
    updateRunButton();
  });
  el.querySelector(".roll-name-auto").addEventListener("click", () => {
    roll.nameAuto = true;
    void refreshRollName(roll);
  });
}

function wireRollGear(el, roll) {
  for (const tile of el.querySelectorAll(".tile")) {
    tile.addEventListener("click", () => openPicker(tile.dataset.kind, rollPickerTarget(roll)));
    tile.querySelector(".tile-clear")?.addEventListener("click", (e) => {
      e.stopPropagation();
      setRollGear(roll, withoutGear(rollGear(roll), tile.dataset.kind));
    });
  }
}

function wireRollFields(el, roll) {
  for (const field of el.querySelectorAll("[data-field]")) {
    field.addEventListener("change", (e) => {
      const name = e.target.dataset.field;
      roll.tagging[name] = e.target.type === "checkbox" ? e.target.checked : e.target.value;
      markChangesStale(roll);
      if (["roll_date", "place"].includes(name)) scheduleRollName(roll);
      else rememberGear(roll.tagging);
      updateRunButton();
    });
  }
}

function wireRollExpanders(el, roll) {
  el.querySelector("[data-expand=more]").addEventListener("click", () => {
    roll.moreOpen = !roll.moreOpen;
    renderRollCards();
  });
  el.querySelector("[data-expand=changes]").addEventListener("click", () => {
    roll.changesOpen = !roll.changesOpen;
    renderRollCards();
    if (roll.changesOpen && (!roll.changes || roll.changesStale)) void loadRollChanges(roll);
  });
  el.querySelector("[data-act=refresh-changes]")?.addEventListener("click", () => void loadRollChanges(roll));
  for (const btn of el.querySelectorAll("[data-open]")) btn.addEventListener("click", () => void openSourceFile(btn.dataset.open));
  for (const file of el.querySelectorAll(".diff-file")) {
    const name = file.dataset.file;
    file.querySelector(".diff-file-lens-select").addEventListener("change", (e) => {
      if (e.target.value) roll.tagging.file_lens_ids[name] = e.target.value;
      else delete roll.tagging.file_lens_ids[name];
      markChangesStale(roll, { render: true });
    });
    file.querySelector(".diff-file-keyword-input").addEventListener("input", (e) => {
      if (e.target.value.trim()) roll.tagging.file_keywords[name] = e.target.value;
      else delete roll.tagging.file_keywords[name];
      markChangesStale(roll, { render: true });
    });
  }
}

function markChangesStale(roll, { render = false } = {}) {
  if (!roll.changes || roll.changesStale) return;
  roll.changesStale = true;
  if (render) {
    const card = document.querySelector(`.roll-card[data-id="${cssEscape(roll.id)}"] .roll-changes`);
    if (card && !card.querySelector(".roll-changes-stale")) {
      card.insertAdjacentHTML("afterbegin", `<div class="roll-changes-stale"><span class="pill warn">Changed since</span> <button type="button" class="action-link" data-act="refresh-changes">Check again</button></div>`);
      card.querySelector("[data-act=refresh-changes]").addEventListener("click", () => void loadRollChanges(roll));
    }
  }
}

async function loadRollChanges(roll) {
  const folder = state.folder;
  const single = singleRollInPlace();
  try {
    const body = {
      folder: folder.path,
      files: rollImages(roll),
      as_roll: single ? undefined : (roll.name.trim() || undefined),
      ...rollTagging(roll),
    };
    const res = await api("/api/metadata/dry-run", { method: "POST", body: JSON.stringify(body) });
    if (!folder.rolls.includes(roll)) return;
    roll.changes = { diffs: res.diffs };
    roll.changesStale = false;
  } catch (e) {
    roll.changes = { diffs: [] };
    roll.changesStale = false;
    renderFolderBanner(`Could not check the changes: ${planErrorsText(e)}`, "bad");
  }
  renderRollCards();
}

function rollGear(roll) {
  const t = roll.tagging;
  return { camera: t.camera, lens: t.lens, film: t.film };
}

function setRollGear(roll, gear) {
  Object.assign(roll.tagging, gear);
  if (gear.camera !== undefined) roll.tagging.file_lens_ids = {};
  rememberGear(roll.tagging);
  markChangesStale(roll);
  renderRollCards();
  scheduleRollName(roll);
}

function rollPickerTarget(roll) {
  return {
    camera: () => roll.tagging.camera,
    set: (kind, item) => setRollGear(roll, withGear(rollGear(roll), kind, item)),
  };
}

const rollNameTimers = new Map();

function scheduleRollName(roll) {
  if (!roll.nameAuto) return;
  clearTimeout(rollNameTimers.get(roll.id));
  rollNameTimers.set(roll.id, setTimeout(() => void refreshRollName(roll), NAME_DEBOUNCE_MS));
}

async function refreshRollName(roll) {
  if (!roll.nameAuto) return;
  const t = roll.tagging;
  try {
    const res = await api("/api/naming/render", {
      method: "POST",
      body: JSON.stringify({
        roll_date: t.roll_date || null,
        camera_id: t.camera?.id || null,
        film_stock_id: t.film?.id || null,
        lens_id: t.lens?.id || null,
        place: t.place || null,
      }),
    });
    if (!roll.nameAuto) return;
    roll.name = res.name || "";
    roll.nameError = res.error ? `Template: ${res.error}` : null;
  } catch (e) {
    roll.nameError = `Could not render the name: ${e.message}`;
  }
  const card = document.querySelector(`.roll-card[data-id="${cssEscape(roll.id)}"]`);
  const input = card?.querySelector(".roll-name-input");
  if (input) {
    input.value = roll.name;
    const err = card.querySelector(".roll-name-error");
    err.textContent = roll.nameError || "";
    err.classList.toggle("hidden", !roll.nameError);
    card.querySelector(".roll-name-auto").classList.toggle("hidden", roll.nameAuto);
  }
  updateRunButton();
}

// ----- destination and the one action -----

// Where the open folder sits, which decides where its rolls can go:
// "outside" the library (or no library), a proper "roll" at <year>/<roll>,
// or "nested" anywhere else inside the library (a lab order's subfolder, a
// flat year folder).
function folderPlacement() {
  const root = (state.settings?.library_root || "").replace(/\/+$/, "");
  const path = state.folder.path;
  if (!root || !path.startsWith(root + "/")) return "outside";
  if (state.folder.listing?.rendition) return "roll";
  return path.slice(root.length + 1).split("/").length === 2 ? "roll" : "nested";
}

// A proper roll has nowhere else to go. A nested roll can move into its
// place at <year>/<name>. Copying inside the library only makes duplicates.
function destinationChoices() {
  return { outside: ["keep", "copy", "move"], nested: ["keep", "move"], roll: ["keep"] }[folderPlacement()];
}

function destination() {
  const choices = destinationChoices();
  const chosen = state.folder.destination || "keep";
  return choices.includes(chosen) ? chosen : "keep";
}

function renderDestination() {
  const current = destination();
  const choices = destinationChoices();
  const placement = folderPlacement();
  document.getElementById("destination").classList.toggle("hidden", choices.length < 2);
  for (const btn of document.querySelectorAll("#destination .segment")) {
    const dest = btn.dataset.dest;
    btn.classList.toggle("hidden", !choices.includes(dest));
    btn.classList.toggle("active", dest === current);
    btn.setAttribute("aria-checked", String(dest === current));
    if (dest === "move") btn.textContent = placement === "nested" ? "Move into place" : "Move to library";
  }
  const hint = document.getElementById("destination-hint");
  const n = state.folder.rolls.length;
  const root = state.settings?.library_root || "";
  if (!hasLibrary()) {
    hint.innerHTML = `Tagged where it is. <button type="button" class="action-link" id="destination-choose-library">Choose a library folder</button> to copy or move rolls into one.`;
    document.getElementById("destination-choose-library").addEventListener("click", () => void chooseLibraryFolder());
  } else if (current === "keep") {
    hint.textContent = n > 1
      ? `Each roll becomes its own folder ${placement === "roll" ? "next to this one" : "inside this one"}, tagged.`
      : "Tagged where it is.";
  } else if (placement === "nested") {
    hint.textContent = "Moved into place at <year>/<name>, every file verified, then tagged there.";
  } else {
    hint.textContent = `${current === "move" ? "Moved" : "Copied"} to ${baseName(root)}/<year>/<name>, every file verified, then tagged there.`;
  }
}

function setDestination(value) {
  state.folder.destination = value;
  if (value !== "keep" && folderPlacement() === "outside") {
    if (state.settings) state.settings.import_mode = value;
    queueSettingsPatch({ import_mode: value });
  }
  for (const roll of state.folder.rolls) markChangesStale(roll);
  renderRollCards();
  renderDestination();
  updateRunButton();
}

function runLabel() {
  const n = state.folder.rolls.length;
  const dest = destination();
  if (dest === "keep") {
    if (n <= 1) {
      const files = n ? rollImages(state.folder.rolls[0]).length : 0;
      return `Write ${files} file${files === 1 ? "" : "s"}`;
    }
    return `Split into ${n} rolls`;
  }
  if (folderPlacement() === "nested") return `Move ${n} roll${n === 1 ? "" : "s"} into place`;
  return `${dest === "move" ? "Move" : "Copy"} ${n} roll${n === 1 ? "" : "s"} to library`;
}

function runBlockers() {
  const rolls = state.folder.rolls;
  const problems = [];
  if (!rolls.length) problems.push("no rolls");
  if (singleRollInPlace()) return problems;
  const names = new Set();
  for (const roll of rolls) {
    const name = (roll.name || "").trim();
    if (!name) problems.push("a roll has no folder name");
    else if (names.has(name.toLowerCase())) problems.push(`two rolls are named ${name}`);
    names.add(name.toLowerCase());
  }
  return problems;
}

function updateRunButton() {
  const btn = document.getElementById("folder-run");
  btn.textContent = runLabel();
  const blockers = runBlockers();
  btn.disabled = !!state.folder.running || blockers.length > 0;
  btn.title = blockers.join("; ");
  document.getElementById("folder-undo").classList.toggle("hidden", !state.folder.lastOperation);
}

function rollTagging(roll) {
  const t = roll.tagging;
  return {
    camera_id: t.camera?.id || null,
    lens_id: t.lens?.id || null,
    film_stock_id: t.film?.id || null,
    development_provider_id: t.development_provider_id || null,
    development_method_id: t.development_method_id || null,
    scan_provider_id: t.scan_provider_id || null,
    scan_method_id: t.scan_method_id || null,
    roll_date: t.roll_date || null,
    roll_notes: (t.roll_notes || "").trim() || null,
    place: (t.place || "").trim() || null,
    replace_managed_metadata: !!t.replace_managed_metadata,
    file_keywords: keywordPayload(t.file_keywords),
    file_lens_ids: { ...t.file_lens_ids },
  };
}

function batchRequestBody() {
  const dest = destination();
  const body = {
    folder: state.folder.path,
    rolls: state.folder.rolls.map(roll => ({ name: roll.name.trim(), frames: roll.frames, tagging: rollTagging(roll) })),
  };
  if (dest !== "keep") body.mode = dest;
  return body;
}

function planErrorsText(e) {
  // The server answers 400 with {"detail": {"errors": [...]}} for plan problems.
  const m = /^\d{3} [^:]*: (.*)$/s.exec(e.message || "");
  try {
    const parsed = JSON.parse(m ? m[1] : e.message);
    const errors = parsed?.detail?.errors || parsed?.errors;
    if (Array.isArray(errors)) return errors.join("\n");
    if (typeof parsed?.detail === "string") return parsed.detail;
  } catch { /* not JSON */ }
  return e.message;
}


// What the one action does: write in place, split, or import.
function plannedAction() {
  const dest = destination();
  if (dest === "keep" && state.folder.rolls.length <= 1) return "write";
  return dest === "keep" ? "split" : "import";
}

function batchEndpoints(action) {
  return action === "split"
    ? { plan: "/api/folder/split", apply: "/api/folder/split/apply" }
    : { plan: "/api/import/plan", apply: "/api/import/apply" };
}

function describePlan(plan, action, dest) {
  const lines = [];
  if (action === "split") {
    lines.push(plan.layout === "siblings" ? `New roll folders next to this one, under ${folderLabel(plan.target_parent)}:` : `New roll folders inside ${folderLabel(plan.target_parent)}:`);
  } else {
    lines.push(`${dest === "move" ? "Move" : "Copy"} ${humanBytes(plan.total_bytes)} into ${plan.root}:`);
  }
  for (const item of plan.items) {
    const where = action === "split" ? item.name : `${item.year}/${item.name}`;
    lines.push(`  ${where}  —  ${item.frames.length} frames, ${item.files.length} files, ${humanBytes(item.bytes)}`);
  }
  if (plan.unassigned.length) lines.push(`${plan.unassigned.length} frame${plan.unassigned.length === 1 ? "" : "s"} stay in this folder.`);
  if (plan.other_files.length) lines.push(`${plan.other_files.length} other file${plan.other_files.length === 1 ? "" : "s"} (${plan.other_files.slice(0, 3).join(", ")}${plan.other_files.length > 3 ? ", …" : ""}) stay behind.`);
  if (action === "split" && plan.remove_source) lines.push("This folder will be empty afterwards and is removed.");
  if (action === "import") {
    lines.push(dest === "move"
      ? "Sources are deleted only after every file of a roll has been read back and verified."
      : "Every copy is read back and verified; this folder is left as it is.");
  }
  lines.push("Each new folder is tagged with its roll's gear and film. The batch can be undone.");
  return lines.join("\n");
}

function gearSummary(roll) {
  const t = roll.tagging;
  return [t.camera?.display_name, t.lens?.display_name, t.film?.display_name, t.roll_date].filter(Boolean).join(" · ") || "no gear or film set";
}

async function runFolderAction() {
  if (runBlockers().length || state.folder.running) return;
  const action = plannedAction();
  if (action === "write") await writeInPlace();
  else await runBatch(action);
}

// Keep here, one roll: write metadata to the files where they are.
async function writeInPlace() {
  const folder = state.folder;
  const roll = folder.rolls[0];
  const images = rollImages(roll);
  const all = images.length === folderFrames().reduce((n, f) => n + f.images.length, 0);
  const body = { folder: folder.path, files: all ? undefined : images, ...rollTagging(roll) };
  const ok = await ask(writeConfirmText(folder.path, images, roll), { title: "Write metadata", okLabel: "Write" });
  if (!ok) return;
  startRun();
  try {
    let total = 0;
    let written = 0;
    const done = await runJob("/api/metadata/apply/stream", {
      body,
      onStart: (ev) => { total = ev.total; renderFolderProgress(`Writing 0/${total}`, 0); setPageTitleProgress("Writing", 0, total); },
      onEvent: () => { written += 1; renderFolderProgress(`Writing ${written}/${total}`, total ? written / total : null); setPageTitleProgress("Writing", written, total); },
    });
    folder.result = { action: "write", done };
    await finishRun();
  } catch (e) {
    renderFolderProgress(null);
    await notify(planErrorsText(e), "Writing failed");
  } finally {
    endRun();
  }
}

// Names the files when there are few of them and the folder by its label
// under the library year, never by its bare basename: a roll folder called
// "RAW" must not read as "a RAW file".
function writeConfirmText(path, images, roll) {
  const where = folderLabel(path);
  const n = images.length;
  const head = n === 1
    ? `Write metadata to ${images[0]} in ${where}?`
    : `Write metadata to ${n} files in ${where}?`;
  const names = n > 1 && n <= 4 ? images.join(", ") : null;
  return [head, names, gearSummary(roll)].filter(Boolean).join("\n");
}

async function runBatch(action) {
  const dest = destination();
  const body = batchRequestBody();
  if (!(await confirmPlan(action, dest, body))) return;
  startRun();
  try {
    const done = await runJob(batchEndpoints(action).apply, { body, ...batchProgressHandlers(dest) });
    state.folder.lastOperation = done.operation_id;
    state.folder.result = { action, dest, done };
    await finishRun();
  } catch (e) {
    renderFolderProgress(null);
    await notify(planErrorsText(e), action === "split" ? "Split failed" : "Import failed");
  } finally {
    endRun();
  }
}

function startRun() {
  state.folder.running = true;
  state.folder.result = null;
  renderFolderResult(null);
  updateRunButton();
  renderFolderProgress("Starting…", 0);
}

function endRun() {
  state.folder.running = false;
  resetPageTitle();
  updateRunButton();
}

async function finishRun() {
  const folder = state.folder;
  const keepResult = folder.result;
  const keepOp = folder.lastOperation;
  renderFolderProgress(null);
  if (hasLibrary()) void loadLibrary({ rescan: true });
  await reloadAfterRun();
  folder.result = keepResult;
  folder.lastOperation = keepOp;
  renderFolderResult(keepResult);
  updateRunButton();
}

// Ask the server for the batch's plan, show it, and resolve true once the
// user confirms. A plan that cannot run is explained instead.
async function confirmPlan(action, dest, body) {
  let plan;
  try {
    plan = await api(batchEndpoints(action).plan, { method: "POST", body: JSON.stringify(body) });
  } catch (e) {
    await notify(planErrorsText(e), "The batch cannot run as planned");
    return false;
  }
  if (!plan.ok) {
    await notify(plan.problems.join("\n"), "The batch cannot run as planned");
    return false;
  }
  const label = action === "split" ? "Split" : dest === "move" ? "Move" : "Copy";
  const n = plan.items.length;
  return ask(describePlan(plan, action, dest), { title: `${label} ${n} roll${n === 1 ? "" : "s"}?`, okLabel: label });
}

function batchProgressHandlers(dest) {
  let total = 0;
  return {
    onStart: (ev) => {
      total = ev.total || 0;
      setPageTitleProgress(dest === "keep" ? "Split" : dest === "move" ? "Move" : "Copy", 0, total);
    },
    onEvent: (ev) => {
      const line = batchProgressLine(ev, dest, total);
      if (!line) return;
      renderFolderProgress(line.text, line.fraction);
      if (line.title) setPageTitleProgress(...line.title);
    },
  };
}

// What one job event means on screen: { text, fraction, title? }, or null
// for events the progress bar does not show.
function batchProgressLine(ev, dest, total) {
  const byBytes = ev.bytes_total ? ev.bytes_done / ev.bytes_total : null;
  switch (ev.step) {
    case "move":
      return { text: `Moving ${ev.done}/${ev.total} · ${ev.roll}`, fraction: total ? ev.done / total : null, title: ["Moving", ev.done, ev.total] };
    case "copy":
      return {
        text: `${dest === "move" ? "Moving" : "Copying"} ${ev.roll} · file ${ev.files_done + 1}/${ev.files_total} · ${humanBytes(ev.bytes_done)} of ${humanBytes(ev.bytes_total)}`,
        fraction: byBytes,
        title: ["Copying", ev.files_done, ev.files_total],
      };
    case "verify":
      return ev.ok ? { text: `Verified ${ev.files_done}/${ev.files_total} · ${ev.roll}`, fraction: byBytes } : null;
    case "tag":
      return { text: `Tagging ${ev.roll} · ${ev.file || ""}`, fraction: null };
    case "remove":
      return { text: `Removing sources · ${ev.roll} · ${ev.file}`, fraction: null };
    default:
      return null;
  }
}

// The folder changed on disk: read it again. A source emptied by a split is
// gone, so the screen keeps the result and says so.
async function reloadAfterRun() {
  const path = state.folder.path;
  try {
    await api(`/api/folder/frames?${new URLSearchParams({ path })}`);
  } catch {
    const folder = state.folder;
    Object.assign(folder, { listing: null, frames: [], indexByKey: null, rolls: [], selection: new Set() });
    renderFolder();
    renderFolderBanner("This folder is empty now and was removed.", "ok");
    return;
  }
  await openFolder(path);
}

function renderFolderProgress(text, fraction) {
  const el = document.getElementById("folder-progress");
  if (!text) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  el.classList.remove("hidden");
  const pct = fraction === null || fraction === undefined ? null : Math.max(0, Math.min(100, Math.round(fraction * 100)));
  el.innerHTML = `
    <div class="progress-row">
      <span class="progress-text">${escapeHtml(text)}</span>
      <span class="progress-pct">${pct === null ? "" : `${pct}%`}</span>
    </div>
    <div class="progress-bar${pct === null ? " indeterminate" : ""}"><div class="progress-fill" style="width:${pct === null ? 30 : pct}%"></div></div>
  `;
}

function renderFolderResult(result) {
  const section = document.getElementById("folder-result");
  const body = document.getElementById("folder-result-body");
  if (!result) { section.classList.add("hidden"); body.innerHTML = ""; return; }
  section.classList.remove("hidden");
  const { action, dest, done } = result;
  if (action === "write") {
    const n = done.applied_files.length;
    body.innerHTML = `
      <div>Wrote ${n} file${n === 1 ? "" : "s"}.</div>
      ${done.errors.length ? `<pre class="result-errors">${escapeHtml(done.errors.join("\n"))}</pre>` : ""}
      ${done.cancelled ? `<div class="pill warn" style="margin-top:10px">Stopped early</div>` : ""}
      <div class="apply-actions">
        <button type="button" class="secondary" data-reveal="${escapeHtml(state.folder.path)}">Reveal</button>
        ${hasLibrary() ? `<button type="button" class="secondary" id="result-back-library">Back to Library</button>` : ""}
      </div>`;
    document.getElementById("result-back-library")?.addEventListener("click", () => void openLibrary());
  } else {
    const rows = (done.created || []).map((c) => {
      const files = c.files.length;
      const problems = c.errors.length ? `<span class="pill bad">${c.errors.length} problem${c.errors.length === 1 ? "" : "s"}</span>` : "";
      const skipped = c.tag_skipped ? `<span class="pill warn" title="${escapeHtml(c.tag_skipped)}">not tagged</span>` : "";
      const verified = action === "import" ? (c.verified ? `<span class="pill ok">verified</span>` : `<span class="pill bad">incomplete</span>`) : "";
      return `
        <div class="result-row">
          <code title="${escapeHtml(c.path)}">${escapeHtml(c.name)}</code>
          <span class="lib-sub">${files} file${files === 1 ? "" : "s"} · ${c.applied.length} tagged</span>
          <span class="result-pills">${verified}${skipped}${problems}</span>
          <span class="lib-actions">
            <button type="button" class="secondary" data-open="${escapeHtml(c.path)}">Open</button>
            <button type="button" class="secondary" data-reveal="${escapeHtml(c.path)}">Reveal</button>
          </span>
        </div>`;
    }).join("");
    const errors = (done.errors || []);
    body.innerHTML = `
      <div class="result-list">${rows || `<div class="lib-sub">Nothing was created.</div>`}</div>
      ${done.cancelled ? `<div class="pill warn" style="margin-top:10px">Stopped early</div>` : ""}
      ${action === "split" && done.removed_source ? `<div class="lib-sub" style="margin-top:10px">The emptied folder was removed; its old manifest is kept in the undo journal.</div>` : ""}
      ${action === "import" && done.removed_sources?.length ? `<div class="lib-sub" style="margin-top:10px">${done.removed_sources.length} source file${done.removed_sources.length === 1 ? "" : "s"} removed after verification.</div>` : ""}
      ${errors.length ? `<pre class="result-errors">${escapeHtml(errors.join("\n"))}</pre>` : ""}
      ${dest !== "keep" && hasLibrary() ? `<div class="apply-actions"><button type="button" class="secondary" id="result-back-library">Back to Library</button></div>` : ""}
    `;
    document.getElementById("result-back-library")?.addEventListener("click", () => void openLibrary());
  }
  for (const btn of body.querySelectorAll("[data-open]")) btn.addEventListener("click", () => void openFolder(btn.dataset.open));
  for (const btn of body.querySelectorAll("[data-reveal]")) btn.addEventListener("click", () => void revealFolder(btn.dataset.reveal));
}

async function undoLastBatch() {
  const opId = state.folder.lastOperation;
  if (!opId || state.folder.running) return;
  const ok = await ask(
    "Put every file back where it came from and remove the folders this batch created?\nMetadata written by the batch stays on the files until the folder is tagged again.",
    { okLabel: "Undo batch" }
  );
  if (!ok) return;
  state.folder.running = true;
  updateRunButton();
  renderFolderProgress("Undoing…", null);
  try {
    const done = await runJob(`/api/operations/${encodeURIComponent(opId)}/undo`, {
      onEvent: (ev) => { if (ev.file) renderFolderProgress(`Restoring ${ev.file}`, null); },
    });
    state.folder.lastOperation = null;
    state.folder.result = null;
    renderFolderProgress(null);
    renderFolderResult(null);
    // A moved file is both restored at the source and removed from the
    // library; a copy is only removed. Count each file once.
    const n = (done.restored || []).length || (done.removed || []).length;
    const problems = done.errors?.length ? `\n\n${done.errors.join("\n")}` : "";
    await notify(`${n} file${n === 1 ? "" : "s"} put back, ${(done.removed_folders || []).length} folder${(done.removed_folders || []).length === 1 ? "" : "s"} removed.${problems}`, "Batch undone");
    if (hasLibrary()) void loadLibrary({ rescan: true });
    if (state.folder.path) await openFolder(state.folder.path);
  } catch (e) {
    renderFolderProgress(null);
    await notify(e.message, "Undo failed");
  } finally {
    state.folder.running = false;
    updateRunButton();
  }
}

// ---------- Wiring ----------

window.addEventListener("DOMContentLoaded", () => {
  loadTheme();
  init();

  // Header
  document.getElementById("open-folder").addEventListener("click", () => void onOpenFolder());
  document.getElementById("folder-reveal").addEventListener("click", () => { if (state.folder.path) void revealFolder(state.folder.path); });
  for (const tab of document.querySelectorAll(".view-tab")) {
    tab.addEventListener("click", () => {
      if (tab.dataset.view === "library") void openLibrary();
      else showView(tab.dataset.view);
    });
  }
  document.getElementById("theme-dark").addEventListener("click", () => applyTheme("dark"));
  document.getElementById("theme-paper").addEventListener("click", () => applyTheme("paper"));
  document.getElementById("settings-open").addEventListener("click", () => void openSettings());

  // Home
  document.getElementById("home-open").addEventListener("click", () => void onOpenFolder());
  document.getElementById("home-path").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); void openFolder(e.target.value); }
  });
  document.getElementById("home-choose-library").addEventListener("click", () => void chooseLibraryFolder());

  // Picker
  document.getElementById("picker-close").addEventListener("click", closePicker);
  document.getElementById("picker-backdrop").addEventListener("click", closePicker);
  document.getElementById("picker-search").addEventListener("input", renderPickerGrid);
  document.getElementById("picker-new").addEventListener("click", addFromPicker);
  document.getElementById("picker-show-all").addEventListener("click", () => {
    const grid = document.getElementById("picker-grid");
    grid.dataset.showAll = "true";
    renderPickerGrid();
  });

  // Library
  document.getElementById("library-filter").addEventListener("input", (e) => {
    state.libraryFilter = e.target.value;
    if (state.library) renderLibrary();
  });
  document.getElementById("library-menu-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleLibraryMenu(); });
  for (const item of document.querySelectorAll("#library-menu .menu-item")) {
    item.addEventListener("click", () => void onLibraryMenu(item.dataset.menu));
  }
  document.addEventListener("click", () => toggleLibraryMenu(false));
  document.getElementById("naming-close").addEventListener("click", closeNamingPanel);
  document.getElementById("naming-apply").addEventListener("click", () => void applySelectedRenames());
  document.getElementById("naming-undo").addEventListener("click", () => void undoLastRename());

  // Folder screen
  document.getElementById("sheet-cut").addEventListener("click", cutBeforeSelected);
  document.getElementById("sheet-new-roll").addEventListener("click", newRollFromSelection);
  document.getElementById("sheet-leave-out").addEventListener("click", leaveOutSelection);
  document.getElementById("sheet-clear").addEventListener("click", clearSheetSelection);
  document.getElementById("sheet-invert").addEventListener("click", toggleInvert);
  document.getElementById("sheet-unmatched").addEventListener("click", (e) => toggleUnmatchedFrames(e.currentTarget.dataset.mode));
  document.getElementById("cuts-rhythm").addEventListener("change", (e) => setRhythm(e.target.value));
  document.getElementById("cuts-apply-all").addEventListener("click", applyAllCuts);
  document.addEventListener("keydown", onSheetKey);
  for (const btn of document.querySelectorAll("#destination .segment")) {
    btn.addEventListener("click", () => setDestination(btn.dataset.dest));
  }
  document.getElementById("folder-run").addEventListener("click", () => void runFolderAction());
  document.getElementById("folder-undo").addEventListener("click", () => void undoLastBatch());

  // Settings
  document.getElementById("settings-back").addEventListener("click", () => void closeSettings());
  document.getElementById("settings-show-archived").addEventListener("change", (e) => {
    settingsState.showArchived = e.target.checked;
    refreshAllSettingsLists();
  });
  for (const btn of document.querySelectorAll("[data-add]")) {
    btn.addEventListener("click", () => openSettingsForm(btn.dataset.add, null));
  }
  for (const btn of document.querySelectorAll(".settings-tab")) {
    btn.addEventListener("click", () => setActiveSettingsTab(btn.dataset.tab));
  }
  document.getElementById("settings-form-close").addEventListener("click", closeSettingsForm);
  document.getElementById("settings-form-cancel").addEventListener("click", closeSettingsForm);
  document.getElementById("settings-form-backdrop").addEventListener("click", closeSettingsForm);
  document.getElementById("settings-form-save").addEventListener("click", submitSettingsForm);
  document.getElementById("general-settings-form").addEventListener("submit", submitGeneralSettings);
  for (const name of ["naming_template", "naming_place_suffix"]) {
    document.getElementById("general-settings-form").elements[name].addEventListener("input", renderTemplateExample);
  }
  document.getElementById("general-library-browse").addEventListener("click", onBrowseLibraryRoot);

  setupWindowDrop();
});

// Full-window drop handling.
// Finder/Explorer/Nautilus all write the folder's absolute path into the drag
// payload as `text/uri-list` (`file:///Users/you/Photos/...`). We read that
// and open the folder. The browser intentionally blocks access to absolute
// paths via `File` / `webkitdirectory` for security, so this URI-list trick
// is the *only* way to get a real path out of a drop.
//
// Dragenter/dragleave fire per-element as the cursor crosses children, so a
// depth counter keeps the overlay stable — it only hides when the counter
// hits zero (drag truly left the window or dropped).
function setupWindowDrop() {
  const overlay = document.getElementById("drop-overlay");
  let depth = 0;

  const show = () => { overlay.classList.remove("hidden"); };
  const hide = () => { depth = 0; overlay.classList.add("hidden"); };

  window.addEventListener("dragenter", (e) => {
    // Only react to file/URL drags — ignore text selections dragged around
    // the page.
    if (!e.dataTransfer || !Array.from(e.dataTransfer.types).some(
          t => t === "Files" || t === "text/uri-list")) return;
    e.preventDefault();
    depth += 1;
    show();
  });

  window.addEventListener("dragover", (e) => {
    if (overlay.classList.contains("hidden")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });

  window.addEventListener("dragleave", (e) => {
    if (overlay.classList.contains("hidden")) return;
    depth -= 1;
    if (depth <= 0) hide();
  });

  window.addEventListener("drop", async (e) => {
    if (overlay.classList.contains("hidden")) return;
    e.preventDefault();
    hide();

    // Inside the desktop shell the real path arrives through
    // window.filmexDesktop.onDrop; the browser payload has no path.
    if (isDesktopShell()) return;

    const path = extractPathFromDrop(e.dataTransfer);
    if (path) {
      void openFolder(path);
      return;
    }

    // Browser stripped the path (common with network volumes under
    // /Volumes/...). Ask for it: dragging the same folder into Terminal
    // gives the full path to paste.
    const hintName = guessDroppedName(e.dataTransfer);
    const typed = await askText(
      `Your browser did not share the folder's full path${hintName ? ` (${hintName})` : ""}. Drag the same folder into Terminal to copy its path, then paste it here.`,
      { title: "Folder path needed", placeholder: "/Volumes/…" }
    );
    if (typed) void openFolder(typed);
  });
}

function extractPathFromDrop(dt) {
  if (!dt) return "";
  const dump = {};
  for (const t of dt.types || []) {
    try { dump[t] = dt.getData(t); } catch { /* some types are opaque */ }
  }
  console.debug("[filmex] drop payload:", dump);

  // Priority 1: uri-list — a newline-delimited list of URIs, first non-comment
  // line is the drop. Finder on macOS writes this for local volumes; for
  // network volumes it sometimes writes it too, sometimes not.
  const uri = (dump["text/uri-list"] || "")
    .split(/\r?\n/).find(l => l && !l.startsWith("#")) || "";

  // Priority 2: any value across all types that looks like a file:// URL.
  const fileUrl = uri || Object.values(dump).find(v => v && /^file:\/\//.test(v)) || "";
  if (fileUrl) return fileUrlToPath(fileUrl);

  // Priority 3: any value that looks like a plain absolute path. macOS
  // network mounts live under /Volumes; Linux/other unix under /; Windows
  // uses `C:\...`.
  for (const v of Object.values(dump)) {
    if (typeof v !== "string") continue;
    const trimmed = v.trim();
    if (/^\/[^\0\n]+$/.test(trimmed)) return trimmed;          // unix
    if (/^[A-Za-z]:[\\\/]/.test(trimmed)) return trimmed;      // windows
  }
  return "";
}

function fileUrlToPath(url) {
  let path = url.trim();
  if (!path.startsWith("file://")) return path;
  path = path.slice(7);
  // Strip host component if any: file://host/path → /path
  const firstSlash = path.indexOf("/");
  if (firstSlash > 0) path = path.slice(firstSlash);
  path = decodeURIComponent(path);
  // Windows: /C:/x/y → C:/x/y
  if (/^\/[A-Za-z]:/.test(path)) path = path.slice(1);
  return path;
}

function guessDroppedName(dt) {
  if (dt && dt.files && dt.files[0]) return dt.files[0].name;
  return "";
}

