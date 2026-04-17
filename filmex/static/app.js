// FilmEx — minimal vanilla JS controller.
//
// State lives on the `state` object; rendering is imperative. The browser
// side does not need to be clever — it's a thin veneer over the FastAPI
// endpoints, which own the real logic.

const STORAGE_KEY = "filmex.state.v1";
const RECENT_FOLDERS_KEY = "filmex.recentFolders.v1";

const state = {
  folder: "",
  scan: null,
  camera: null,
  lens: null,
  film: null,
  fileKeywords: {},
  fileKeywordEditorOpen: {},
  advanced: {
    development_provider_id: "",
    development_method_id: "",
    scan_provider_id: "",
    scan_method_id: "",
    extra_keywords: "",
    roll_date: "",
    roll_date_source: "",  // "suggested" | "user" — controls the "from file creation" hint
  },
  // Caches of entity lists
  cameras: [],
  lenses: [],
  films: [],
  providers: [],
  devMethods: [],
  scanMethods: [],
};

// ---------- Persistence ----------

function loadStickyState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    Object.assign(state.advanced, saved.advanced || {});
    state.camera = saved.camera || null;
    state.lens = saved.lens || null;
    state.film = saved.film || null;
    state.folder = saved.folder || "";
  } catch (e) {
    console.warn("Sticky state load failed", e);
  }
}

function saveStickyState() {
  const payload = {
    folder: state.folder,
    camera: state.camera,
    lens: state.lens,
    film: state.film,
    advanced: state.advanced,
  };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
}

function recentFolders() {
  try { return JSON.parse(localStorage.getItem(RECENT_FOLDERS_KEY) || "[]"); }
  catch { return []; }
}

function pushRecentFolder(path) {
  const list = [path, ...recentFolders().filter(p => p !== path)].slice(0, 10);
  localStorage.setItem(RECENT_FOLDERS_KEY, JSON.stringify(list));
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

// ---------- Initial load ----------

async function init() {
  loadStickyState();

  await refreshExifToolStatus();

  // Load all entity caches in parallel
  const [cams, lenses, films, providers, devM, scanM] = await Promise.all([
    api("/api/cameras"),
    api("/api/lenses"),
    api("/api/film-stocks"),
    api("/api/providers"),
    api("/api/development-methods"),
    api("/api/scan-methods"),
  ]);
  state.cameras = cams;
  state.lenses = lenses;
  state.films = films;
  state.providers = providers;
  state.devMethods = devM;
  state.scanMethods = scanM;

  populateAdvancedSelects();
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
}

async function refreshExifToolStatus() {
  const el = document.getElementById("exiftool-status");
  try {
    const h = await api("/api/health");
    if (h.exiftool_available) {
      el.textContent = "exiftool ok";
      el.className = "pill ok";
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

async function onBrowse() {
  const btn = document.getElementById("browse-btn");
  btn.disabled = true;
  try {
    const res = await api("/api/folder/pick", { method: "POST" });
    if (res.cancelled || !res.folder) return;
    const input = document.getElementById("folder-input");
    input.value = res.folder;
    state.folder = res.folder;
    saveStickyState();
    onScan();
  } catch (err) {
    alert(`Could not open folder picker: ${err.message || err}`);
  } finally {
    btn.disabled = false;
  }
}

async function onScan() {
  const input = document.getElementById("folder-input");
  const folder = input.value.trim();
  if (!folder) return;
  state.folder = folder;
  try {
    const scan = await api("/api/folder/scan", {
      method: "POST",
      body: JSON.stringify({ folder }),
    });
    state.scan = scan;
    prunePerFileKeywordState();
    applySuggestedRollDate(scan.suggested_roll_date);
    pushRecentFolder(folder);
    renderRecentFolders();
    renderScanSummary();
    saveStickyState();
    updateActionButtons();
  } catch (e) {
    state.scan = null;
    const summary = document.getElementById("scan-summary");
    summary.classList.remove("hidden");
    summary.innerHTML = `<span class="pill bad">Scan failed</span> <span>${escapeHtml(e.message)}</span>`;
    updateActionButtons();
  }
}

function renderScanSummary() {
  const el = document.getElementById("scan-summary");
  if (!state.scan) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  el.innerHTML = `
    <span class="chip">${state.scan.files.length} file${state.scan.files.length === 1 ? "" : "s"}</span>
    <span class="chip">${escapeHtml(state.scan.source_kind)}</span>
    <span class="chip">${escapeHtml(state.scan.scan_tier)}</span>
    <span>roll: <code>${escapeHtml(state.scan.roll_id)}</code></span>
  `;
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

function renderTiles() {
  const tileCamera = document.getElementById("tile-camera");
  const tileLens = document.getElementById("tile-lens");
  const tileFilm = document.getElementById("tile-film");

  renderTile(tileCamera, state.camera, "Pick a camera (optional)", c => ({
    title: c.display_name,
    sub: cameraTypeLabel(c.camera_type) || c.mount || "",
  }));
  renderTile(tileLens, state.lens, "Pick a lens (optional)", l => ({
    title: l.display_name,
    sub: [l.focal_length_mm && `${l.focal_length_mm}mm`, l.max_aperture && `f/${l.max_aperture}`, l.mount]
      .filter(Boolean).join(" · "),
  }));
  renderTile(tileFilm, state.film, "Pick a film (optional)", f => ({
    title: f.display_name,
    sub: `ISO ${f.box_speed_iso} · ${f.film_type.replace("_", " ")}`,
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

  title.textContent = { camera: "Pick a camera", lens: "Pick a lens", film: "Pick a film" }[kind];
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
  for (const item of filtered) {
    grid.appendChild(renderPickerTile(item, activePickerKind));
  }
  if (filtered.length === 0) {
    const empty = document.createElement("div");
    empty.className = "picker-tile-sub";
    empty.textContent = "No matches.";
    grid.appendChild(empty);
  }
}

// Per-brand two-color palette rendered as a stacked strip on the left of
// each film tile — a bookmark-style visual grouping. Unknown brands get a
// neutral fallback so the strip still appears but doesn't misrepresent.
const FILM_BRAND_COLORS = {
  Kodak: ["#f7ad03", "#c9489e"],
  Fomapan: ["#c9489e", "#f1d100"],
  Ilford: ["#000000", "#ffffff"],
  Kentmere: ["#2f2298", "#e50163"],
  Agfa: ["#73767c", "#cf1c30"],
  Fujifilm: ["#079952", "#292826"],
};
const FILM_BRAND_FALLBACK = ["#3a4050", "#2a3040"];

function filmBrandColors(brand) {
  return FILM_BRAND_COLORS[brand] || FILM_BRAND_FALLBACK;
}

function renderPickerTile(item, kind) {
  const tile = document.createElement("div");
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
    const sub = document.createElement("div");
    sub.className = "picker-tile-sub";
    sub.textContent = `${item.brand} · ${item.film_type.replace("_", " ")}`;
    tile.appendChild(sub);
    const badge = document.createElement("div");
    badge.className = "iso-badge";
    badge.textContent = item.box_speed_iso;
    tile.appendChild(badge);
  }

  tile.addEventListener("click", () => selectFromPicker(item));
  return tile;
}

function selectFromPicker(item) {
  if (activePickerKind === "camera") {
    state.camera = item;
    // If camera has a fixed lens, auto-populate it.
    if (item.fixed_lens_id) {
      const lens = state.lenses.find(l => l.id === item.fixed_lens_id);
      if (lens) state.lens = lens;
    }
  } else if (activePickerKind === "lens") {
    state.lens = item;
  } else if (activePickerKind === "film") {
    state.film = item;
  }
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
  document.getElementById("adv-extra-keywords").value = state.advanced.extra_keywords || "";
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
    extra_keywords: document.getElementById("adv-extra-keywords").value
      .split(",").map(s => s.trim()).filter(Boolean),
    roll_date: document.getElementById("adv-roll-date").value || null,
  };
}

// ---------- Dry run / Apply ----------

function buildRequestBody() {
  return {
    folder: state.folder,
    camera_id: state.camera?.id,
    lens_id: state.lens?.id || null,
    film_stock_id: state.film?.id,
    file_keywords: buildPerFileKeywordPayload(),
    ...readAdvanced(),
  };
}

function updateActionButtons() {
  // Camera/lens/film are all optional — at minimum we still write Roll|<id>
  // and any extras/dev/scan keywords, which is useful even with no gear set.
  const ready = !!(state.folder && state.scan);
  document.getElementById("dry-run-btn").disabled = !ready;
  document.getElementById("apply-btn").disabled = !ready;
}

async function onDryRun() {
  try {
    const res = await api("/api/metadata/dry-run", {
      method: "POST",
      body: JSON.stringify(buildRequestBody()),
    });
    renderDiff(res);
  } catch (e) {
    alert(`Dry-run failed:\n${e.message}`);
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
    const hasThumbnail = !!scanFile?.has_thumbnail;
    const thumbUrl = hasThumbnail
      ? buildThumbnailUrl(state.folder, file.name)
      : "";
    const keywordValue = state.fileKeywords[file.name] || "";
    const editorOpen = !!state.fileKeywordEditorOpen[file.name];
    frag.innerHTML = `
      ${hasThumbnail ? `
        <button type="button" class="diff-thumb-link" aria-label="Open source file">
          <img class="diff-thumb" src="${escapeHtml(thumbUrl)}" alt="" loading="lazy" />
        </button>
      ` : ""}
      <div class="diff-file-main">
        <div class="diff-file-name">${escapeHtml(file.name)}</div>
        <div class="diff-file-actions">
          <button type="button" class="diff-add-keyword-link">${editorOpen ? "hide keywords" : "add keywords"}</button>
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
        schedulePreviewRefresh();
      });
    }
    body.appendChild(frag);
  }
}

async function onApply() {
  if (!confirm("Write metadata to all files in this folder?")) return;
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

    // NDJSON: one JSON object per line. Parse as they stream in so the button
    // can show `3/12` live — matters on a slow NAS where apply takes minutes.
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let total = 0;
    let done_event = null;

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) !== -1) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        const ev = JSON.parse(line);
        if (ev.type === "start") {
          total = ev.total;
          btn.textContent = `Applying… 0/${total}`;
        } else if (ev.type === "progress") {
          const n = Number(btn.dataset.n || 0) + 1;
          btn.dataset.n = String(n);
          btn.textContent = `Applying… ${n}/${total}`;
        } else if (ev.type === "done") {
          done_event = ev;
        }
      }
    }
    if (!done_event) throw new Error("stream ended without completion event");
    renderApplyResult(done_event);
  } catch (e) {
    alert(`Apply failed:\n${e.message}`);
  } finally {
    delete btn.dataset.n;
    btn.textContent = "Apply";
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
  const params = new URLSearchParams({ folder, name });
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

function prunePerFileKeywordState() {
  const valid = new Set((state.scan?.files || []).map(file => file.name));
  for (const key of Object.keys(state.fileKeywords)) {
    if (!valid.has(key)) delete state.fileKeywords[key];
  }
  for (const key of Object.keys(state.fileKeywordEditorOpen)) {
    if (!valid.has(key)) delete state.fileKeywordEditorOpen[key];
  }
}

let previewRefreshTimer = null;

function schedulePreviewRefresh() {
  if (previewRefreshTimer) clearTimeout(previewRefreshTimer);
  previewRefreshTimer = setTimeout(() => {
    previewRefreshTimer = null;
    if (document.getElementById("diff-section").classList.contains("hidden")) return;
    onDryRun();
  }, 250);
}

async function openSourceFile(name) {
  try {
    const params = new URLSearchParams({ folder: state.folder, name });
    await api(`/api/files/open?${params.toString()}`, { method: "POST" });
  } catch (e) {
    alert(`Could not open file:\n${e.message}`);
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
      { name: "mount", label: "Mount (blank if none)", placeholder: "K, M39, Fixed-Lens…" },
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
  film: {
    label: "Film stock",
    endpoint: "/api/film-stocks",
    groupBy: "brand",
    primary: "display_name",
    sub: (row) => `${row.film_type} · ISO ${row.box_speed_iso}`,
    fields: [
      { name: "brand", label: "Brand", required: true },
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
};

async function openSettings() {
  document.getElementById("workflow-view").classList.add("hidden");
  document.getElementById("settings-view").classList.remove("hidden");
  await refreshAllSettingsLists();
}

function closeSettings() {
  document.getElementById("settings-view").classList.add("hidden");
  document.getElementById("workflow-view").classList.remove("hidden");
  // Refresh main-flow entity caches in case the user edited/archived anything.
  init();
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
  renderSettingsList(kind);
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
  // Sort: favorites first, then alpha by primary key or name.
  const sorted = [...rows].sort((a, b) => {
    if (a.favorite !== b.favorite) return a.favorite ? -1 : 1;
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
  el.innerHTML = `
    <div class="settings-row-name">
      ${escapeHtml(primary)}
      ${sub ? `<span class="sub">${escapeHtml(sub)}</span>` : ""}
    </div>
    <button class="settings-row-star ${row.favorite ? "on" : ""}" title="Favorite">★</button>
    <button class="settings-row-edit">Edit</button>
    <button class="settings-row-archive">${row.archived ? "Unarchive" : "Archive"}</button>
  `;
  el.querySelector(".settings-row-star").addEventListener("click", () => toggleFavorite(kind, row));
  el.querySelector(".settings-row-edit").addEventListener("click", () => openSettingsForm(kind, row));
  el.querySelector(".settings-row-archive").addEventListener("click", () => toggleArchive(kind, row));
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
}

// ---------- Wiring ----------

window.addEventListener("DOMContentLoaded", () => {
  init();

  document.getElementById("scan-btn").addEventListener("click", onScan);
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
      saveStickyState();
    });
  }
  document.getElementById("adv-extra-keywords").addEventListener("change", (e) => {
    state.advanced.extra_keywords = e.target.value;
    saveStickyState();
  });
  document.getElementById("adv-roll-date").addEventListener("change", (e) => {
    state.advanced.roll_date = e.target.value || "";
    state.advanced.roll_date_source = state.advanced.roll_date ? "user" : "";
    renderRollDate();
    saveStickyState();
  });

  document.getElementById("dry-run-btn").addEventListener("click", onDryRun);
  document.getElementById("apply-btn").addEventListener("click", onApply);

  // Settings view toggle
  document.getElementById("settings-open").addEventListener("click", openSettings);
  document.getElementById("settings-back").addEventListener("click", closeSettings);
  document.getElementById("settings-show-archived").addEventListener("change", (e) => {
    settingsState.showArchived = e.target.checked;
    refreshAllSettingsLists();
  });
  for (const btn of document.querySelectorAll("[data-add]")) {
    btn.addEventListener("click", () => openSettingsForm(btn.dataset.add, null));
  }
  document.getElementById("settings-form-close").addEventListener("click", closeSettingsForm);
  document.getElementById("settings-form-cancel").addEventListener("click", closeSettingsForm);
  document.getElementById("settings-form-backdrop").addEventListener("click", closeSettingsForm);
  document.getElementById("settings-form-save").addEventListener("click", submitSettingsForm);

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

    const path = extractPathFromDrop(e.dataTransfer);
    if (path) {
      input.value = path;
      state.folder = path;
      saveStickyState();
      onScan();
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
  alert(msg);
}
