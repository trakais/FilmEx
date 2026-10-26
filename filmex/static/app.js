// FilmEx — minimal vanilla JS controller.
//
// State lives on the `state` object; rendering is imperative. The browser
// side does not need to be clever — it's a thin veneer over the FastAPI
// endpoints, which own the real logic.

const DEFAULT_PAGE_TITLE = document.title;

const state = {
  settings: null,  // server-side settings cache (`/api/settings`)
  view: "workflow",
  viewInitialised: false,
  library: null,          // last `/api/library` payload
  libraryRescanning: false,
  fileSubset: null,       // { folder, files } when tagging only part of a roll
  folder: "",
  theme: "dark",
  scan: null,
  camera: null,
  lens: null,
  film: null,
  previewInvert: false,
  lastDryRunResult: null,
  previewStale: false,
  previewBuildController: null,
  previewBuildJobId: null,
  fileKeywords: {},
  fileLensOverrides: {},
  fileKeywordEditorOpen: {},
  advanced: {
    development_provider_id: "",
    development_method_id: "",
    scan_provider_id: "",
    scan_method_id: "",
    roll_notes: "",
    roll_date: "",
    replace_managed_metadata: false,
    roll_date_source: "",  // "suggested" | "user" — controls the "from file creation" hint
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
  state.folder = s.last_folder || "";
  state.previewInvert = !!s.preview_invert;
  applyTheme(s.theme, { persist: false });
}

// One-time import of state written by builds that used localStorage, then
// clean those keys up so this never runs twice.
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

function saveStickyState() {
  queueSettingsPatch({
    last_folder: state.folder || "",
    preview_invert: !!state.previewInvert,
    last_selection: {
      camera: state.camera,
      lens: state.lens,
      film: state.film,
      advanced: state.advanced,
    },
  });
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
// Replaces window.alert/confirm. Browsers offer to suppress repeated native
// dialogs (after which `confirm` silently returns false), and the desktop
// shell should never pop a WebKit sheet over the app.

function showDialog({ title = "FilmEx", message = "", okLabel = "OK", cancelLabel = null } = {}) {
  return new Promise((resolve) => {
    const dialog = document.getElementById("dialog");
    const backdrop = document.getElementById("dialog-backdrop");
    const okBtn = document.getElementById("dialog-ok");
    const cancelBtn = document.getElementById("dialog-cancel");
    document.getElementById("dialog-title").textContent = title;
    document.getElementById("dialog-message").textContent = message;
    okBtn.textContent = okLabel;
    cancelBtn.textContent = cancelLabel || "Cancel";
    cancelBtn.classList.toggle("hidden", !cancelLabel);
    const previouslyFocused = document.activeElement;

    const finish = (value) => { cleanup(); resolve(value); };
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
    okBtn.focus();
  });
}

function notify(message, title = "FilmEx") {
  return showDialog({ title, message });
}

function ask(message, { title = "Confirm", okLabel = "Continue", cancelLabel = "Cancel" } = {}) {
  return showDialog({ title, message, okLabel, cancelLabel });
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
    if (path) acceptDroppedFolder(path);
  },
};

function acceptDroppedFolder(path) {
  const input = document.getElementById("folder-input");
  input.value = path;
  state.folder = path;
  saveStickyState();
  onScan();
}

// ---------- API ----------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} ${res.statusText}: ${detail}`);
  }
  return res.status === 204 ? null : res.json();
}

// Read an NDJSON response line by line, calling `onEvent` for each object as
// it arrives so progress can render live. Exceptions thrown by `onEvent`
// abort the read and propagate to the caller.
async function consumeNdjson(res, onEvent) {
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

// Follow a server job: replays the events already emitted, then streams live
// ones until the job reaches a terminal status. Use after a reload to pick a
// long apply or import back up.
async function followJob(jobId, onEvent, { signal, fromSeq = 0 } = {}) {
  const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/events?from_seq=${fromSeq}`, { signal });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `HTTP ${res.status}`);
  }
  await consumeNdjson(res, onEvent);
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
async function initInner() {
  await loadSettings();

  await refreshExifToolStatus();

  // Load all entity caches in parallel
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

  populateAdvancedSelects();
  renderPreviewInvertToggle();
  renderTiles();
  renderFolderInput();
  renderRecentFolders();
  updateActionButtons();

  // Re-resolve sticky entity snapshots against the fresh DB (IDs might have
  // changed if seeds were edited); drop stale ones.
  if (state.camera && !cams.find(c => c.id === state.camera.id)) state.camera = null;
  if (state.lens && !lenses.find(l => l.id === state.lens.id)) state.lens = null;
  if (state.film && !films.find(f => f.id === state.film.id)) state.film = null;
  renderTiles();
  chooseStartView();
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

// ---------- Folder ----------

function renderFolderInput() {
  const input = document.getElementById("folder-input");
  input.value = state.folder || "";
}

function renderRecentFolders() {
  const dl = document.getElementById("folder-history");
  dl.innerHTML = "";
  for (const path of recentFolders()) {
    const opt = document.createElement("option");
    opt.value = path;
    dl.appendChild(opt);
  }
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

async function onBrowse() {
  const btn = document.getElementById("browse-btn");
  btn.disabled = true;
  try {
    const folder = await pickFolder();
    if (!folder) return;
    const input = document.getElementById("folder-input");
    input.value = folder;
    state.folder = folder;
    saveStickyState();
    onScan();
  } catch (err) {
    await notify(`Could not open folder picker: ${err.message || err}`);
  } finally {
    btn.disabled = false;
  }
}

async function onScan() {
  const input = document.getElementById("folder-input");
  const folder = input.value.trim();
  if (!folder) return;
  clearScanOutput();
  clearPreviewStatus();
  clearManifestSummary();
  if (state.fileSubset && state.fileSubset.folder !== folder) state.fileSubset = null;
  state.folder = folder;
  try {
    const scan = await api("/api/folder/scan", {
      method: "POST",
      body: JSON.stringify({ folder }),
    });
    state.scan = scan;
    applySuggestedRollDate(scan.suggested_roll_date);
    resetPerFileKeywordState();
    pushRecentFolder(folder);
    renderRecentFolders();
    renderScanSummary();
    saveStickyState();
    updateActionButtons();
    void maybeLoadManifestPrefill(scan).then(() => {
      saveStickyState();
      updateActionButtons();
    });
  } catch (e) {
    state.scan = null;
    clearPreviewStatus();
    const summary = document.getElementById("scan-summary");
    summary.classList.remove("hidden");
    summary.innerHTML = `<span class="pill bad">Scan failed</span> <span>${escapeHtml(e.message)}</span>`;
    updateActionButtons();
  }
}

// Feather-ish refresh-cw: 3/4 arc + single arrow head. Sized to match the
// summary-value text height; strokes pick up currentColor so hover can
// flip it yellow. Used as the rescan affordance next to the file count.
const REFRESH_ICON_SVG =
  '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<polyline points="23 4 23 10 17 10"/>' +
    '<path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>' +
  '</svg>';

function renderScanSummary() {
  const el = document.getElementById("scan-summary");
  if (!state.scan) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const count = state.scan.files.length;
  const sourceLabel = scanSourceKindLabel(state.scan.source_kind);
  const tierLabel = scanTierLabel(state.scan.source_kind, state.scan.scan_tier);
  const filesValue = `${count} image${count === 1 ? "" : "s"}`;
  // Files item is hand-built so the count + rescan icon sit in the same
  // flex line, right where the eye lands to check "did my new files land?".
  const filesMarkup =
    `<span class="summary-item">` +
      `<span class="summary-label">Files</span>` +
      `<span class="summary-value summary-value--accent">${escapeHtml(filesValue)}</span>` +
      `<button id="scan-refresh-btn" type="button" class="summary-refresh" aria-label="Rescan folder" title="Rescan folder">${REFRESH_ICON_SVG}</button>` +
    `</span>`;
  const parts = [
    filesMarkup,
    renderScanSummaryItem("Source", sourceLabel),
  ];
  if (tierLabel) {
    parts.push(renderScanSummaryItem("Resolution", tierLabel));
  }
  parts.push(renderScanSummaryItem("Folder", state.scan.roll_id, "summary-code"));
  const subset = activeFileSubset();
  if (subset) {
    parts.push(
      `<span class="summary-item">` +
        `<span class="summary-label">Tagging</span>` +
        `<span class="summary-value summary-value--accent">${subset.length} of ${count} files</span>` +
        `<button id="subset-clear-btn" type="button" class="summary-refresh" aria-label="Tag all files instead" title="Tag all files instead">✕</button>` +
      `</span>`
    );
  }
  el.innerHTML = parts.join("");
  const refreshBtn = document.getElementById("scan-refresh-btn");
  if (refreshBtn) refreshBtn.addEventListener("click", onScan);
  const subsetBtn = document.getElementById("subset-clear-btn");
  if (subsetBtn) subsetBtn.addEventListener("click", clearFileSubset);
}

function renderScanSummaryItem(label, value, valueClass = "") {
  const classes = ["summary-value"];
  if (valueClass) classes.push(valueClass);
  return `<span class="summary-item"><span class="summary-label">${escapeHtml(label)}</span><span class="${classes.join(" ")}">${escapeHtml(value)}</span></span>`;
}

function scanSourceKindLabel(kind) {
  switch (kind) {
    case "camera_scan_raw":
      return "Camera-scanned raw files";
    case "positive_master_tiff":
      return "High-resolution TIFF masters";
    case "lab_scan_jpeg":
      return "Lab scan JPEGs";
    default:
      return kind || "Unknown";
  }
}

function scanTierLabel(sourceKind, tier) {
  if (sourceKind === "camera_scan_raw" || sourceKind === "positive_master_tiff") {
    return "";
  }
  switch (tier) {
    case "high-res":
      return "High resolution";
    case "standard":
      return "Standard resolution";
    default:
      return tier || "";
  }
}

function renderManifestSummary(message, level = "warn") {
  const el = document.getElementById("manifest-summary");
  if (!message) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = `<span class="pill ${escapeHtml(level)}">${level === "ok" ? "Manifest loaded" : "Manifest note"}</span><span>${escapeHtml(message)}</span>`;
}

function clearManifestSummary() {
  renderManifestSummary("");
}

function renderPreviewStatus(message, level = "warn") {
  const el = document.getElementById("preview-status");
  if (!message) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = `<span class="pill ${escapeHtml(level)}">${level === "ok" ? "Previews ready" : "Preview note"}</span><span>${escapeHtml(message)}</span>`;
}

function clearPreviewStatus() {
  renderPreviewStatus("");
}

function renderPreviewStaleSummary() {
  const el = document.getElementById("preview-stale-summary");
  if (!state.lastDryRunResult || !state.previewStale) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = `<span class="pill warn">Preview stale</span><span>Preview is out of date. Refresh before applying.</span>`;
}

function renderDryRunButton() {
  const btn = document.getElementById("dry-run-btn");
  btn.textContent = state.previewStale ? "Preview changes" : "Preview";
}

function markPreviewFresh() {
  state.previewStale = false;
  renderPreviewStaleSummary();
  renderDryRunButton();
}

function markPreviewStale() {
  if (!state.lastDryRunResult) return;
  state.previewStale = true;
  renderPreviewStaleSummary();
  renderDryRunButton();
  updateActionButtons();
}

function renderPreviewInvertToggle() {
  const btn = document.getElementById("preview-invert-toggle");
  btn.setAttribute("aria-pressed", state.previewInvert ? "true" : "false");
  btn.textContent = `Invert thumbnails: ${state.previewInvert ? "on" : "off"}`;
}

function setPageTitleProgress(label, current, total) {
  document.title = `${DEFAULT_PAGE_TITLE} (${label}: ${current}/${total})`;
}

function resetPageTitle() {
  document.title = DEFAULT_PAGE_TITLE;
}

function fileHasPreview(file) {
  if (!file) return false;
  return state.previewInvert ? !!file.has_inverted_preview : !!file.has_thumbnail;
}

function missingPreviewCount() {
  return (state.scan?.files || []).filter(file => !fileHasPreview(file)).length;
}

function renderCreatePreviewsButton() {
  const btn = document.getElementById("create-previews-btn");
  const missing = missingPreviewCount();
  if (state.previewBuildController) {
    btn.classList.remove("hidden");
    btn.disabled = false;
    btn.textContent = btn.dataset.progressText || "Stop";
    return;
  }
  if (!state.scan || missing === 0) {
    btn.classList.add("hidden");
    btn.disabled = true;
    btn.textContent = "Create thumbnails";
    delete btn.dataset.progressText;
    return;
  }
  btn.classList.remove("hidden");
  btn.disabled = false;
  btn.textContent = missing === (state.scan.files || []).length ? "Create thumbnails" : "Create missing thumbnails";
  delete btn.dataset.progressText;
}

// ---------- Tiles ----------

// Keep in sync with the Settings form options. Stored values stay lowercase
// kebab for URL/DB safety; these labels are only for display.
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

function renderTiles() {
  const tileCamera = document.getElementById("tile-camera");
  const tileLens = document.getElementById("tile-lens");
  const tileFilm = document.getElementById("tile-film");

  renderTile(tileCamera, state.camera, "Pick a camera (optional)", c => ({
    title: c.display_name,
    sub: cameraTypeLabel(c.camera_type) || c.mount || "",
  }));
  renderTile(tileLens, state.lens, "Pick a main lens (optional)", l => ({
    title: l.display_name,
    sub: [l.focal_length_mm && `${l.focal_length_mm}mm`, l.max_aperture && `f/${l.max_aperture}`, l.mount]
      .filter(Boolean).join(" · "),
  }));
  renderTile(tileFilm, state.film, "Pick a film (optional)", f => ({
    title: f.display_name,
    sub: `ISO ${f.box_speed_iso} · ${filmTypeLabel(f.film_type)}`,
  }));
}

function renderTile(el, entity, placeholder, toCopy) {
  const content = el.querySelector(".tile-content");
  content.className = "tile-content" + (entity ? "" : " empty");
  // Always tear down the clear button — re-added below only when selected.
  el.querySelector(".tile-clear")?.remove();
  if (!entity) {
    content.textContent = placeholder;
    el.querySelector(".tile-sub")?.remove();
    el.classList.remove("selected");
    return;
  }
  const copy = toCopy(entity);
  content.textContent = copy.title;
  let sub = el.querySelector(".tile-sub");
  if (!sub) {
    sub = document.createElement("div");
    sub.className = "tile-sub";
    el.appendChild(sub);
  }
  sub.textContent = copy.sub || "";
  el.classList.add("selected");

  // Tiny ✕ in the corner so the user can blank a slot without re-opening the
  // picker — useful when only one or two of camera/lens/film are known.
  // stopPropagation so the click doesn't fall through to the tile and
  // immediately reopen the picker.
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "tile-clear";
  clear.setAttribute("aria-label", `Clear ${el.dataset.kind}`);
  clear.title = "Clear";
  clear.textContent = "✕";
  clear.addEventListener("click", (e) => {
    e.stopPropagation();
    clearTile(el.dataset.kind);
  });
  el.appendChild(clear);
}

function clearTile(kind) {
  if (kind === "camera") {
    state.camera = null;
    state.fileLensOverrides = {};
    // Also clear lens if it was the camera's fixed lens — otherwise the user
    // is left with a lens that no longer matches anything sensible.
    if (state.lens && state.cameras.some(c => c.fixed_lens_id === state.lens.id)) {
      state.lens = null;
    }
  } else if (kind === "lens") {
    state.lens = null;
  } else if (kind === "film") {
    state.film = null;
  }
  markPreviewStale();
  saveStickyState();
  renderTiles();
  updateActionButtons();
}

// ---------- Picker ----------

let activePickerKind = null;

function openPicker(kind) {
  activePickerKind = kind;
  const panel = document.getElementById("picker-panel");
  const backdrop = document.getElementById("picker-backdrop");
  const title = document.getElementById("picker-title");
  const grid = document.getElementById("picker-grid");
  const showAll = document.getElementById("picker-show-all");
  const search = document.getElementById("picker-search");

  title.textContent = { camera: "Pick a camera", lens: "Pick a main lens", film: "Pick a film" }[kind];
  search.value = "";
  delete grid.dataset.showAll;
  showAll.classList.toggle("hidden", kind !== "lens");

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
    if (state.camera && !grid.dataset.showAll) {
      items = await api(`/api/lenses?camera_id=${encodeURIComponent(state.camera.id)}`);
    } else if (state.camera && grid.dataset.showAll === "true") {
      items = await api(`/api/lenses?camera_id=${encodeURIComponent(state.camera.id)}&show_all=true`);
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
  if (activePickerKind === "camera") {
    state.camera = item;
    state.fileLensOverrides = {};
    // Auto-populate the fixed lens first — a Fixed-Lens camera always mounts
    // its own lens, and the compat check below then confirms it.
    if (item.fixed_lens_id) {
      const lens = state.lenses.find(l => l.id === item.fixed_lens_id);
      if (lens) state.lens = lens;
    }
    // Drop the currently selected lens if its mount doesn't match the new
    // camera. Saves the user from noticing the mismatch themselves; they
    // can still bypass via "Show all (ignore mount)" in the lens picker.
    if (state.lens && !lensMountMatchesCamera(state.lens, item)) {
      state.lens = null;
    }
  } else if (activePickerKind === "lens") {
    state.lens = item;
  } else if (activePickerKind === "film") {
    state.film = item;
  }
  markPreviewStale();
  saveStickyState();
  renderTiles();
  updateActionButtons();
  closePicker();
}

// ---------- Advanced ----------

function populateAdvancedSelects() {
  const devProviders = state.providers.filter(p => p.kinds.includes("development"));
  const scanProviders = state.providers.filter(p => p.kinds.includes("scan"));

  fillSelect("adv-dev-provider", devProviders, state.advanced.development_provider_id);
  fillSelect("adv-dev-method", state.devMethods, state.advanced.development_method_id);
  fillSelect("adv-scan-provider", scanProviders, state.advanced.scan_provider_id);
  fillSelect("adv-scan-method", state.scanMethods, state.advanced.scan_method_id);
  document.getElementById("adv-roll-notes").value = state.advanced.roll_notes || "";
  document.getElementById("adv-replace-managed").checked = !!state.advanced.replace_managed_metadata;
  renderRollDate();
}

function applySuggestedRollDate(suggested) {
  // Pre-fill the date field with the scanner's creation date on every scan,
  // *unless* the user has manually typed one (user > suggestion).
  if (!suggested) return;
  if (state.advanced.roll_date && state.advanced.roll_date_source === "user") return;
  state.advanced.roll_date = suggested;
  state.advanced.roll_date_source = "suggested";
  renderRollDate();
}

async function maybeLoadManifestPrefill(scan) {
  try {
    const params = new URLSearchParams({ folder: state.folder });
    const res = await api(`/api/manifest?${params.toString()}`);
    applyManifestPrefill(res.manifest, scan);
  } catch (e) {
    if (String(e.message || "").startsWith("404 ")) return;
    renderManifestSummary(`Could not load existing roll manifest: ${e.message}`, "bad");
  }
}

function applyManifestPrefill(manifest, scan) {
  const unresolved = [];
  state.camera = resolveCameraFromManifest(manifest.analog_camera_snapshot, unresolved);
  state.lens = resolveLensFromManifest(manifest.analog_lens_snapshot, unresolved);
  state.film = resolveFilmFromManifest(manifest.film_stock_snapshot, unresolved);
  state.advanced.development_provider_id = resolveNamedEntityId(
    state.providers,
    manifest.development_provider_snapshot,
    unresolved,
    "development provider"
  );
  state.advanced.development_method_id = resolveNamedEntityId(
    state.devMethods,
    manifest.development_method_snapshot,
    unresolved,
    "development method"
  );
  state.advanced.scan_provider_id = resolveNamedEntityId(
    state.providers,
    manifest.scan_provider_snapshot,
    unresolved,
    "scan provider"
  );
  state.advanced.scan_method_id = resolveNamedEntityId(
    state.scanMethods,
    manifest.scan_method_snapshot,
    unresolved,
    "scan method"
  );
  state.fileKeywords = fileKeywordsFromManifest(manifest.file_overrides, scan.files);
  state.fileLensOverrides = fileLensOverridesFromManifest(manifest.file_overrides, scan.files, unresolved);
  state.fileKeywordEditorOpen = {};

  const manifestRollDate = (manifest.capture_date_value || "").trim();
  if (manifestRollDate) {
    state.advanced.roll_date = manifestRollDate;
    state.advanced.roll_date_source = "manifest";
  }
  state.advanced.roll_notes = (manifest.notes || "").trim();

  populateAdvancedSelects();
  renderTiles();

  const msg = unresolved.length
    ? `Loaded existing roll manifest. Could not match: ${unresolved.join(", ")}.`
    : "Loaded existing roll manifest values for this folder.";
  renderManifestSummary(msg, unresolved.length ? "warn" : "ok");
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

function renderRollDate() {
  const input = document.getElementById("adv-roll-date");
  const hint = document.getElementById("roll-date-hint");
  input.value = state.advanced.roll_date || "";
  if (state.advanced.roll_date && state.advanced.roll_date_source === "suggested") {
    hint.textContent = "from file creation";
    hint.classList.remove("hidden");
  } else {
    hint.textContent = "";
    hint.classList.add("hidden");
  }
}

function fillSelect(id, items, selectedId) {
  const el = document.getElementById(id);
  // Keep the existing "(none)" option at index 0
  while (el.options.length > 1) el.remove(1);
  for (const item of items) {
    const opt = document.createElement("option");
    opt.value = item.id;
    opt.textContent = item.display_name || item.name;
    if (item.id === selectedId) opt.selected = true;
    el.appendChild(opt);
  }
}

function readAdvanced() {
  return {
    development_provider_id: document.getElementById("adv-dev-provider").value || null,
    development_method_id: document.getElementById("adv-dev-method").value || null,
    scan_provider_id: document.getElementById("adv-scan-provider").value || null,
    scan_method_id: document.getElementById("adv-scan-method").value || null,
    roll_date: document.getElementById("adv-roll-date").value || null,
    roll_notes: document.getElementById("adv-roll-notes").value.trim() || null,
    replace_managed_metadata: !!document.getElementById("adv-replace-managed").checked,
  };
}

// ---------- Dry run / Apply ----------

function buildRequestBody() {
  const files = activeFileSubset();
  return {
    folder: state.folder,
    files: files || undefined,
    camera_id: state.camera?.id,
    lens_id: state.lens?.id || null,
    film_stock_id: state.film?.id,
    file_keywords: buildPerFileKeywordPayload(),
    file_lens_ids: { ...state.fileLensOverrides },
    ...readAdvanced(),
  };
}

function updateActionButtons() {
  // Camera/lens/film are all optional — at minimum we still write Roll|<id>
  // and any extras/dev/scan keywords, which is useful even with no gear set.
  const ready = !!(state.folder && state.scan);
  const busy = !!state.previewBuildController;
  renderCreatePreviewsButton();
  renderDryRunButton();
  renderPreviewStaleSummary();
  document.getElementById("dry-run-btn").disabled = !ready || busy;
  document.getElementById("apply-btn").disabled = !ready || busy || !state.lastDryRunResult || state.previewStale;
  document.getElementById("preview-invert-toggle").disabled = !ready || busy;
}

async function onDryRun() {
  try {
    const res = await api("/api/metadata/dry-run", {
      method: "POST",
      body: JSON.stringify(buildRequestBody()),
    });
    state.lastDryRunResult = res;
    markPreviewFresh();
    renderDiff(res);
    updateActionButtons();
  } catch (e) {
    await notify(`Dry-run failed:\n${e.message}`);
  }
}

async function onCreatePreviews() {
  if (!state.folder || !state.scan) return;
  if (state.previewBuildController) {
    state.previewBuildController.abort();
    void cancelJob(state.previewBuildJobId);
    return;
  }
  const btn = document.getElementById("create-previews-btn");
  clearPreviewStatus();
  const controller = new AbortController();
  state.previewBuildController = controller;
  btn.dataset.progressText = "Stop";
  updateActionButtons();
  let total = missingPreviewCount();
  let processed = 0;
  let created = 0;
  let failed = 0;
  try {
    const res = await fetch("/api/previews/create/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder: state.folder, invert: state.previewInvert }),
      signal: controller.signal,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `HTTP ${res.status}`);
    }
    let doneEvent = null;
    await consumeNdjson(res, (ev) => {
      if (ev.type === "start") {
        state.previewBuildJobId = ev.job_id || null;
        total = ev.total;
        btn.dataset.progressText = total ? `Stop ${processed}/${total}` : "Stop";
        renderCreatePreviewsButton();
      } else if (ev.type === "progress") {
        processed += 1;
        if (ev.status === "created" || ev.status === "reused") {
          created += 1;
          markPreviewFilesAvailable([ev.file]);
          if (state.lastDryRunResult) renderDiff(state.lastDryRunResult);
        } else if (ev.status === "failed") {
          failed += 1;
        }
        btn.dataset.progressText = total ? `Stop ${processed}/${total}` : "Stop";
        renderCreatePreviewsButton();
      } else if (ev.type === "done") {
        doneEvent = ev;
      } else if (ev.type === "error") {
        throw new Error(ev.error || "preview job failed");
      }
    });
    const cancelled = !!doneEvent?.cancelled;
    failed = doneEvent?.failed?.length ?? failed;
    created = doneEvent?.created_files?.length ?? created;
    const parts = [];
    if (created) parts.push(`${created} ready`);
    if (failed) parts.push(`${failed} failed`);
    renderPreviewStatus(
      parts.length
        ? `Previews: ${parts.join(", ")}${cancelled ? ", cancelled early" : ""}.`
        : cancelled
          ? "Preview creation cancelled."
          : "No previewable files were missing.",
      failed || cancelled ? "warn" : "ok"
    );
    if (doneEvent?.created_files?.length) markPreviewFilesAvailable(doneEvent.created_files);
    if (state.lastDryRunResult) renderDiff(state.lastDryRunResult);
  } catch (e) {
    if (e.name === "AbortError") {
      const parts = [];
      if (created) parts.push(`${created} ready`);
      if (failed) parts.push(`${failed} failed`);
      renderPreviewStatus(
        parts.length ? `Preview creation stopped: ${parts.join(", ")}.` : "Preview creation stopped.",
        "warn"
      );
    } else {
      renderPreviewStatus(`Could not create thumbnails: ${e.message}`, "bad");
    }
  } finally {
    state.previewBuildController = null;
    state.previewBuildJobId = null;
    delete btn.dataset.progressText;
    updateActionButtons();
  }
}

function markPreviewFilesAvailable(fileNames) {
  const ready = new Set(fileNames);
  for (const file of state.scan?.files || []) {
    if (ready.has(file.name)) {
      if (state.previewInvert) {
        file.has_inverted_preview = true;
      } else {
        file.has_thumbnail = true;
      }
    }
  }
}

function renderDiff(result) {
  const section = document.getElementById("diff-section");
  const body = document.getElementById("diff-body");
  section.classList.remove("hidden");
  body.innerHTML = "";
  const scanFilesByName = new Map((state.scan?.files || []).map(file => [file.name, file]));
  for (const file of result.diffs) {
    const frag = document.createElement("div");
    frag.className = "diff-file";
    const scanFile = scanFilesByName.get(file.name);
    const hasThumbnail = fileHasPreview(scanFile);
    const thumbUrl = hasThumbnail
      ? buildThumbnailUrl(state.folder, file.name, state.previewInvert)
      : "";
    const keywordValue = state.fileKeywords[file.name] || "";
    const lensOverrideValue = state.fileLensOverrides[file.name] || "";
    const editorOpen = !!state.fileKeywordEditorOpen[file.name];
    const lensOptions = lensOverrideOptions(lensOverrideValue);
    frag.innerHTML = `
      ${hasThumbnail ? `
        <button type="button" class="diff-thumb-link" aria-label="Open source file">
          <img class="diff-thumb" src="${escapeHtml(thumbUrl)}" alt="" loading="lazy" />
        </button>
      ` : ""}
      <div class="diff-file-main">
        <div class="diff-file-header">
          <div class="diff-file-name">${escapeHtml(file.name)}</div>
          <div class="diff-file-controls">
            <select class="diff-file-lens-select" aria-label="Lens override for this frame">
              ${lensOptions}
            </select>
            <button type="button" class="diff-add-keyword-link">${editorOpen ? "hide keywords" : "+ keywords"}</button>
          </div>
        </div>
        <div class="diff-file-keyword-editor${editorOpen ? "" : " hidden"}">
          <input
            type="text"
            class="diff-file-keyword-input"
            value="${escapeHtml(keywordValue)}"
            placeholder="extra keywords, comma-separated"
          />
          <div class="diff-file-keyword-hint">Merged with auto keywords, duplicates skipped.</div>
        </div>
      </div>
    `;
    const changes = document.createElement("div");
    changes.className = "diff-changes";
    for (const ch of file.changes) {
      const row = document.createElement("div");
      row.className = "diff-row";
      row.innerHTML = `
        <span class="diff-tag">${escapeHtml(ch.tag)}</span>
        <span class="diff-action ${ch.action}">${ch.action}</span>
        <span class="diff-value">${formatValue(ch.after)}</span>
      `;
      changes.appendChild(row);
    }
    frag.querySelector(".diff-file-main").appendChild(changes);
    frag.querySelector(".diff-thumb-link")?.addEventListener("click", () => openSourceFile(file.name));
    frag.querySelector(".diff-add-keyword-link").addEventListener("click", () => {
      state.fileKeywordEditorOpen[file.name] = !state.fileKeywordEditorOpen[file.name];
      renderDiff(result);
    });
    const input = frag.querySelector(".diff-file-keyword-input");
    if (input) {
      input.addEventListener("input", (e) => {
        state.fileKeywords[file.name] = e.target.value;
        markPreviewStale();
      });
    }
    const lensSelect = frag.querySelector(".diff-file-lens-select");
    if (lensSelect) {
      lensSelect.addEventListener("change", (e) => {
        if (e.target.value) {
          state.fileLensOverrides[file.name] = e.target.value;
        } else {
          delete state.fileLensOverrides[file.name];
        }
        markPreviewStale();
      });
    }
    body.appendChild(frag);
  }
}

async function onApply() {
  if (!(await ask("Write metadata to all files in this folder?", { okLabel: "Write metadata" }))) return;
  const btn = document.getElementById("apply-btn");
  const dryBtn = document.getElementById("dry-run-btn");
  btn.disabled = true;
  dryBtn.disabled = true;
  btn.textContent = "Applying…";
  try {
    const res = await fetch("/api/metadata/apply/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildRequestBody()),
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `HTTP ${res.status}`);
    }

    // NDJSON: one JSON object per line, parsed as it streams in so the
    // button can show `3/12` live — matters on a slow NAS.
    let total = 0;
    let done_event = null;
    await consumeNdjson(res, (ev) => {
      if (ev.type === "start") {
        total = ev.total;
        btn.textContent = `Applying… 0/${total}`;
        setPageTitleProgress("Applying", 0, total);
      } else if (ev.type === "progress") {
        const n = Number(btn.dataset.n || 0) + 1;
        btn.dataset.n = String(n);
        btn.textContent = `Applying… ${n}/${total}`;
        setPageTitleProgress("Applying", n, total);
      } else if (ev.type === "done") {
        done_event = ev;
      } else if (ev.type === "error") {
        throw new Error(ev.error || "apply failed");
      }
    });
    if (!done_event) throw new Error("stream ended without completion event");
    renderApplyResult(done_event);
  } catch (e) {
    await notify(`Apply failed:\n${e.message}`);
  } finally {
    delete btn.dataset.n;
    btn.textContent = "Apply";
    resetPageTitle();
    updateActionButtons();
  }
}

function renderApplyResult(res) {
  const section = document.getElementById("apply-section");
  const body = document.getElementById("apply-body");
  section.classList.remove("hidden");
  body.innerHTML = `
    <div>Wrote ${res.applied_files.length} file${res.applied_files.length === 1 ? "" : "s"}.</div>
    <div style="margin-top: 6px; font-size: 12px; color: var(--muted);">
      Manifest: <code>${escapeHtml(res.manifest_path)}</code>
    </div>
    ${res.errors.length
      ? `<div class="pill bad" style="margin-top: 10px;">${res.errors.length} error(s)</div>
         <pre style="white-space: pre-wrap; color: var(--danger); font-size: 12px;">${escapeHtml(res.errors.join("\n"))}</pre>`
      : ""
    }
  `;
}

// ---------- Helpers ----------

function clearScanOutput() {
  state.lastDryRunResult = null;
  state.previewStale = false;
  renderPreviewStaleSummary();
  renderDryRunButton();
  const diffSection = document.getElementById("diff-section");
  const diffBody = document.getElementById("diff-body");
  const applySection = document.getElementById("apply-section");
  const applyBody = document.getElementById("apply-body");
  diffSection.classList.add("hidden");
  diffBody.innerHTML = "";
  applySection.classList.add("hidden");
  applyBody.innerHTML = "";
}

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

function buildThumbnailUrl(folder, name) {
  const params = new URLSearchParams({ folder, name, invert: state.previewInvert ? "true" : "false" });
  return `/api/files/thumbnail?${params.toString()}`;
}

function buildPerFileKeywordPayload() {
  const out = {};
  for (const [name, raw] of Object.entries(state.fileKeywords || {})) {
    const keywords = raw.split(",").map(s => s.trim()).filter(Boolean);
    if (keywords.length) out[name] = keywords;
  }
  return out;
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

function resetPerFileKeywordState() {
  state.fileKeywords = {};
  state.fileLensOverrides = {};
  state.fileKeywordEditorOpen = {};
}

function lensOverrideOptions(selectedId) {
  const options = ['<option value="">(main lens)</option>'];
  for (const lens of compatibleLensOptions()) {
    options.push(
      `<option value="${escapeHtml(lens.id)}" ${lens.id === selectedId ? "selected" : ""}>${escapeHtml(lens.display_name)}</option>`
    );
  }
  return options.join("");
}

function compatibleLensOptions() {
  if (state.camera?.fixed_lens_id) {
    return state.lenses.filter(l => l.id === state.camera.fixed_lens_id);
  }
  if (state.camera?.mount) {
    return state.lenses.filter(l => !l.mount || l.mount === state.camera.mount);
  }
  return state.lenses;
}

async function openSourceFile(name) {
  try {
    const params = new URLSearchParams({ folder: state.folder, name });
    await api(`/api/files/open?${params.toString()}`, { method: "POST" });
  } catch (e) {
    await notify(`Could not open file:\n${e.message}`);
  }
}

// ---------- Settings ----------

// Single config table keyed by UI "kind" — drives list rendering, form
// rendering, and the REST endpoints. Adding a new entity type = one entry here.
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
const DEFAULT_SETTINGS_TAB = "camera";

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
  state.viewBeforeSettings = state.view === "settings" ? "workflow" : state.view;
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

// ---------- Views ----------

const VIEW_IDS = { library: "library-view", workflow: "workflow-view", settings: "settings-view" };

function showView(name) {
  const next = VIEW_IDS[name] ? name : "workflow";
  for (const [key, id] of Object.entries(VIEW_IDS)) {
    document.getElementById(id).classList.toggle("hidden", key !== next);
  }
  for (const tab of document.querySelectorAll(".view-tab")) {
    tab.classList.toggle("active", tab.dataset.view === next);
  }
  state.view = next;
}

function chooseStartView() {
  if (state.viewInitialised) return;
  state.viewInitialised = true;
  if (state.settings && state.settings.library_root) {
    showView("library");
    void loadLibrary({ rescan: true });
  } else {
    showView("workflow");
  }
}

// ---------- Library view ----------

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
  const btn = document.getElementById("library-rescan");
  btn.disabled = true;
  btn.textContent = "Rescanning…";
  try {
    const res = await fetch("/api/library/rescan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`);
    await consumeNdjson(res, (ev) => {
      if (ev.type === "progress") btn.textContent = `Rescanning… ${ev.scanned}`;
      else if (ev.type === "error") throw new Error(ev.error || "rescan failed");
    });
    state.library = await api("/api/library");
    renderLibrary();
  } catch (e) {
    renderLibraryBanner(`Rescan failed: ${e.message}`, "bad");
  } finally {
    state.libraryRescanning = false;
    btn.disabled = false;
    btn.textContent = "Rescan";
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
  const rootEl = document.getElementById("library-root-path");
  const groupsEl = document.getElementById("library-groups");
  const summaryEl = document.getElementById("library-summary");
  const rescanBtn = document.getElementById("library-rescan");
  renderLibraryBanner("");

  if (!lib || !lib.root) {
    rootEl.textContent = "no library root";
    rescanBtn.disabled = true;
    summaryEl.classList.add("hidden");
    groupsEl.innerHTML = `
      <div class="library-empty">
        <div>No library root set. Point FilmEx at the top folder of your film library, the one with the year folders inside.</div>
        <button type="button" class="primary" id="library-empty-choose">Choose library root…</button>
      </div>`;
    document.getElementById("library-empty-choose").addEventListener("click", onChooseLibraryRoot);
    return;
  }

  rootEl.textContent = lib.root;
  rootEl.title = lib.root;
  rescanBtn.disabled = !lib.reachable || state.libraryRescanning;
  if (!lib.reachable) {
    renderLibraryBanner(
      `Library root is not reachable right now (NAS unmounted?). Showing cached results` +
      (lib.scanned_at ? ` from ${formatStamp(lib.scanned_at)}.` : "."),
      "warn"
    );
  }

  const s = lib.summary || {};
  summaryEl.classList.remove("hidden");
  summaryEl.innerHTML = [
    renderScanSummaryItem("Rolls", String(lib.folders.length)),
    renderScanSummaryItem("Tagged", String(s.tagged || 0)),
    renderScanSummaryItem("Partial", String(s.partial || 0)),
    renderScanSummaryItem("Untagged", String(s.untagged || 0)),
    lib.scanned_at ? renderScanSummaryItem("Scanned", formatStamp(lib.scanned_at)) : "",
  ].join("");

  const groups = new Map();
  for (const row of lib.folders) {
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
    groupsEl.innerHTML = `<div class="library-empty">No folders with images found under this root yet.</div>`;
  }
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
  const el = document.createElement("div");
  el.className = `lib-row ${row.status}`;
  el.dataset.path = row.path;
  const meta = [row.film_display, row.camera_display].filter(Boolean).join(" · ");
  const actions = [];
  if (row.status === "partial") {
    actions.push(`<button type="button" class="primary" data-act="remaining">Tag remaining ${row.untagged_count}</button>`);
  } else if (row.status === "untagged") {
    actions.push(`<button type="button" class="primary" data-act="open">Tag</button>`);
  }
  if (row.status !== "untagged") {
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
      await openFolderInTagView(row.path, details.untagged_files);
    } catch (e) {
      renderLibraryBanner(`Could not read the folder: ${e.message}`, "bad");
    }
    return;
  }
  await openFolderInTagView(row.path, null);
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
async function openFolderInTagView(path, files) {
  state.folder = path;
  state.fileSubset = null;
  renderFolderInput();
  showView("workflow");
  await onScan();
  if (files && files.length && state.scan) {
    state.fileSubset = { folder: path, files: files.slice() };
    renderScanSummary();
  }
}

function activeFileSubset() {
  const subset = state.fileSubset;
  if (!subset || subset.folder !== state.folder || !state.scan) return null;
  return subset.files;
}

function clearFileSubset() {
  state.fileSubset = null;
  state.lastDryRunResult = null;
  renderScanSummary();
  clearScanOutput();
  updateActionButtons();
}

async function onChooseLibraryRoot() {
  try {
    const folder = await pickFolder();
    if (!folder) return;
    state.settings = await api("/api/settings", { method: "PATCH", body: JSON.stringify({ library_root: folder }) });
    await loadLibrary({ rescan: true });
  } catch (e) {
    renderLibraryBanner(`Could not set the library root: ${e.message}`, "bad");
  }
}

function formatStamp(iso, { dateOnly = false } = {}) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return dateOnly ? date : `${date} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ---------- Settings: General tab ----------

const DEFAULT_NAMING_TEMPLATE = "{date}-{camera}-{film}";

async function renderGeneralSettings() {
  const form = document.getElementById("general-settings-form");
  if (!form) return;
  const s = state.settings || (state.settings = await api("/api/settings"));
  form.elements.library_root.value = s.library_root || "";
  form.elements.exiftool_path.value = s.exiftool_path || "";
  form.elements.naming_template.value = s.naming_template || DEFAULT_NAMING_TEMPLATE;
  form.elements.naming_place_suffix.checked = !!s.naming_place_suffix;
  form.elements.import_mode.value = s.import_mode || "copy";
  document.getElementById("general-settings-status").textContent = "";
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
    import_mode: form.elements.import_mode.value,
  };
  try {
    state.settings = await api("/api/settings", { method: "PATCH", body: JSON.stringify(patch) });
    status.textContent = "Saved.";
    status.classList.remove("bad");
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

function closeSettings() {
  const back = state.viewBeforeSettings || "workflow";
  showView(back);
  // Refresh main-flow entity caches in case the user edited/archived anything.
  init();
  if (back === "library") void loadLibrary({ rescan: false });
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
  if (kind === "camera") {
    state.cameras = rows;
    renderTiles();
    return;
  }
  if (kind === "lens") {
    state.lenses = rows;
    renderTiles();
    return;
  }
  if (kind === "film-brand") {
    state.brands = rows;
    renderTiles();
    return;
  }
  if (kind === "film") {
    state.films = rows;
    renderTiles();
    return;
  }
  if (kind === "provider") {
    state.providers = rows;
    populateAdvancedSelects();
    return;
  }
  if (kind === "dev-method") {
    state.devMethods = rows;
    populateAdvancedSelects();
    return;
  }
  if (kind === "scan-method") {
    state.scanMethods = rows;
    populateAdvancedSelects();
  }
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

let settingsFormCtx = null;  // { kind, row } — row is null for "add"

function openSettingsForm(kind, row) {
  const cfg = SETTINGS_KINDS[kind];
  settingsFormCtx = { kind, row };
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
  const { kind, row } = settingsFormCtx;
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

  try {
    if (row) {
      await api(`${cfg.endpoint}/${row.id}`, { method: "PATCH", body: JSON.stringify(payload) });
    } else {
      await api(cfg.endpoint, { method: "POST", body: JSON.stringify(payload) });
    }
  } catch (err) {
    errorEl.textContent = err.message || String(err);
    return;
  }

  closeSettingsForm();
  await refreshSettingsList(kind);
  if (kind === "film-brand") {
    await refreshSettingsList("film");
  }
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

// ---------- Wiring ----------

window.addEventListener("DOMContentLoaded", () => {
  loadTheme();
  init();

  document.getElementById("browse-btn").addEventListener("click", onBrowse);
  document.getElementById("folder-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); onScan(); }
  });

  for (const kind of ["camera", "lens", "film"]) {
    document.getElementById(`tile-${kind}`).addEventListener("click", () => openPicker(kind));
  }

  document.getElementById("picker-close").addEventListener("click", closePicker);
  document.getElementById("picker-backdrop").addEventListener("click", closePicker);
  document.getElementById("picker-search").addEventListener("input", renderPickerGrid);
  document.getElementById("picker-show-all").addEventListener("click", () => {
    const grid = document.getElementById("picker-grid");
    grid.dataset.showAll = "true";
    renderPickerGrid();
  });

  document.getElementById("advanced-toggle").addEventListener("click", (e) => {
    const btn = e.currentTarget;
    const body = document.getElementById("advanced-body");
    const expanded = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", String(!expanded));
    body.classList.toggle("hidden", expanded);
  });

  for (const id of ["adv-dev-provider", "adv-dev-method", "adv-scan-provider", "adv-scan-method"]) {
    document.getElementById(id).addEventListener("change", (e) => {
      state.advanced[e.target.dataset.field] = e.target.value;
      markPreviewStale();
      saveStickyState();
    });
  }
  document.getElementById("adv-roll-notes").addEventListener("change", (e) => {
    state.advanced.roll_notes = e.target.value;
    markPreviewStale();
    saveStickyState();
  });
  document.getElementById("adv-roll-date").addEventListener("change", (e) => {
    state.advanced.roll_date = e.target.value || "";
    state.advanced.roll_date_source = state.advanced.roll_date ? "user" : "";
    renderRollDate();
    markPreviewStale();
    saveStickyState();
  });
  document.getElementById("adv-replace-managed").addEventListener("change", (e) => {
    state.advanced.replace_managed_metadata = !!e.target.checked;
    markPreviewStale();
    saveStickyState();
  });

  document.getElementById("preview-invert-toggle").addEventListener("click", () => {
    state.previewInvert = !state.previewInvert;
    renderPreviewInvertToggle();
    saveStickyState();
    clearPreviewStatus();
    if (state.lastDryRunResult) renderDiff(state.lastDryRunResult);
  });
  document.getElementById("create-previews-btn").addEventListener("click", onCreatePreviews);
  document.getElementById("dry-run-btn").addEventListener("click", onDryRun);
  document.getElementById("apply-btn").addEventListener("click", onApply);

  // Settings view toggle
  document.getElementById("settings-open").addEventListener("click", openSettings);
  for (const tab of document.querySelectorAll(".view-tab")) {
    tab.addEventListener("click", () => {
      if (tab.dataset.view === "library") void openLibrary();
      else showView(tab.dataset.view);
    });
  }
  document.getElementById("library-rescan").addEventListener("click", () => void rescanLibrary());
  document.getElementById("library-choose-root").addEventListener("click", onChooseLibraryRoot);
  document.getElementById("theme-dark").addEventListener("click", () => applyTheme("dark"));
  document.getElementById("theme-paper").addEventListener("click", () => applyTheme("paper"));
  document.getElementById("settings-back").addEventListener("click", closeSettings);
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
  document.getElementById("general-library-browse").addEventListener("click", onBrowseLibraryRoot);

  setupWindowDrop();
});

// Full-window drop handling.
// Finder/Explorer/Nautilus all write the folder's absolute path into the drag
// payload as `text/uri-list` (`file:///Users/you/Photos/...`). We read that,
// fill the folder input, and auto-scan. The browser intentionally blocks
// access to absolute paths via `File` / `webkitdirectory` for security, so
// this URI-list trick is the *only* way to get a real path out of a drop.
//
// Dragenter/dragleave fire per-element as the cursor crosses children, so a
// depth counter keeps the overlay stable — it only hides when the counter
// hits zero (drag truly left the window or dropped).
function setupWindowDrop() {
  const overlay = document.getElementById("drop-overlay");
  const input = document.getElementById("folder-input");
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
      acceptDroppedFolder(path);
      return;
    }

    // Browser stripped the path (common with network volumes under
    // /Volumes/...). Fall back: focus the input, pre-fill whatever name we
    // can glean, show a hint next to it, and let the user paste the path
    // they can easily grab by dragging the same folder into Terminal.
    const hintName = guessDroppedName(e.dataTransfer);
    showPasteFallback(hintName);
  });
}

// Walk every mimetype the browser exposes and try to extract an absolute
// filesystem path. Browsers vary wildly here, especially for network mounts.
// We log the raw payload to console so users can send us evidence if it
// still fails for a weird file manager.
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

function showPasteFallback(hintName) {
  const input = document.getElementById("folder-input");
  input.focus();
  input.select();
  const msg = [
    "Your browser wouldn't share the full folder path with this page",
    "(common for network drives).",
    "",
    hintName ? `Detected: ${hintName}` : "",
    "",
    "Easiest workaround: drag the same folder into Terminal to copy the full",
    "path, then paste it into the folder input.",
  ].filter(Boolean).join("\n");
  void notify(msg, "Folder path needed");
}
