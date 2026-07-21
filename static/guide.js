"use strict";
/* Matchup guide view: pick any champion (full roster, not just ones you've
   played) and see/edit a guide — general notes, and per-matchup Markdown
   notes, patch version, and one or more full rune pages (primary tree +
   keystone + 3 minors, secondary tree + 2 minors, 3 stat shards) — for
   every matchup that champion has faced, or add one for a matchup not yet
   played. Each matchup also shows its recent games on the right, including
   the runes actually played in each one (decoded server-side from match-v5
   perks data — see server/rune_data.py / crawler.py) when available.
   Export/import a champion's whole guide as JSON, optionally
   password-encrypted; also exportable as a printable PDF (with rune icons,
   fetched server-side at export time — see server/pdf_export.py).
   Uses globals from app.js: $, getJSON, escapeHtml, displayName, champIcon,
   wrCell, renderNotes, accountParams, setMainView, fmtDate, fmtDuration.
   Uses roster/loadChampionRoster/champDisplay from blocks.js for
   champion-name resolution and the shared #champ-list datalist. Called
   from matchups.js via openGuide(). */

const guideState = {
  wired: false,
  myChampion: "",
  matchups: [],   // stats rows for myChampion: {opp_champion, games, winrate, ...}
  guide: {},      // opp_champion -> {notes, runes: [rune page, ...], patch_version}
  games: new Map(), // opp_champion -> recent games list (each may carry a "runes" field)
  expanded: new Set(), // opp_champions currently expanded (collapsed by default)
  openRunePages: new Set(), // "opp:index" rune pages expanded in read-only view
  editing: null,  // opp_champion whose editor is open
  draft: null,    // working copy of the guide being edited: {notes, patch_version, runes}
  openRuneIndex: null, // index into draft.runes currently expanded in the picker
  pendingFocus: null,  // opp_champion to focus once the next load completes (deep link)
  generalNotes: "",     // myChampion's general (non-matchup) Markdown notes
  editingGeneral: false,
  itemBuild: { core: [], situational: [] }, // myChampion's item build
  editingItemBuild: false,
  itemBuildDraft: null,  // working copy while editing: { core: [...], situational: [{label, items}] }
  itemPickerTarget: null, // { kind: "core" } or { kind: "situational", index } — which list an open picker adds to
  itemPickerQuery: "",
};

const MAX_CORE_ITEMS = 6;
const MAX_SITUATIONAL_SECTIONS = 12;
const MAX_ITEMS_PER_SECTION = 5;

const RUNE_TREES = [];
const SHARD_ROWS = [];
const ITEMS = []; // [{name, icon}] — purchasable Summoner's Rift items, current patch

async function loadRuneTrees() {
  if (RUNE_TREES.length) return;
  const data = await getJSON("/runes.json");
  RUNE_TREES.push(...data.trees);
  SHARD_ROWS.push(...data.shardRows);
}

// DDragon's item.json lists several item ids under the same display name
// (base items alongside masterwork/variant entries), so collapse by name —
// keep the first after sorting. We match/store builds by name, so one entry
// per name is what the picker wants.
function dedupeItemsByName(items) {
  const seen = new Set();
  return items.filter((i) => (seen.has(i.name) ? false : seen.add(i.name)));
}

async function loadItemData() {
  if (ITEMS.length || !state.ddragonVersion) return;
  // v2: earlier caches stored a list with duplicate names — the bump forces a refresh
  const cacheKey = `item-data-v2-${state.ddragonVersion}`;
  try {
    const cached = localStorage.getItem(cacheKey);
    if (cached) {
      ITEMS.push(...dedupeItemsByName(JSON.parse(cached)));
      return;
    }
    const data = await getJSON(
      `https://ddragon.leagueoflegends.com/cdn/${state.ddragonVersion}/data/en_US/item.json`);
    const items = dedupeItemsByName(Object.values(data.data || {})
      .filter((item) => item.gold && item.gold.purchasable && item.maps && item.maps["11"])
      .map((item) => ({ name: item.name, icon: item.image.full }))
      .sort((a, b) => a.name.localeCompare(b.name)));
    localStorage.setItem(cacheKey, JSON.stringify(items));
    ITEMS.push(...items);
  } catch {
    // offline / fetch failed — the picker just has no options; existing
    // builds still display (icon lookup by name silently finds nothing)
  }
}

function itemByName(name) { return ITEMS.find((i) => i.name === name); }
function itemIconUrl(icon) { return `https://ddragon.leagueoflegends.com/cdn/${state.ddragonVersion}/img/item/${icon}`; }

function runeIconUrl(icon) { return `https://ddragon.leagueoflegends.com/cdn/img/${icon}`; }
function shardIconUrl(icon) {
  return `https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/perk-images/statmods/${icon}`;
}

function treeByName(name) { return RUNE_TREES.find((t) => t.name === name); }

// row index (1-3) of a rune within a tree's non-keystone rows, or null
function rowIndexOf(tree, runeName) {
  if (!tree) return null;
  const row = tree.rows.find((r) => r.slot !== "keystone" && r.runes.some((x) => x.name === runeName));
  return row ? row.slot : null;
}

function runeIcon(name, tree) {
  if (!tree) return "";
  const rune = tree.rows.flatMap((r) => r.runes).find((x) => x.name === name);
  return rune ? rune.icon : "";
}

function emptyGuide() { return { notes: "", runes: [], patch_version: "", skill_order: [] }; }
function emptyRunePage() {
  return { label: "", primary_tree: "", keystone: "", primary_runes: ["", "", ""],
           secondary_tree: "", secondary_runes: [], shards: ["", "", ""] };
}
function pageHasContent(p) {
  return Boolean(p.primary_tree || p.keystone || p.secondary_tree
    || p.primary_runes.some(Boolean) || p.secondary_runes.length || p.shards.some(Boolean));
}

function guideFor(champ) {
  const g = guideState.guide[champ];
  return g ? { ...emptyGuide(), ...g } : emptyGuide();
}

// ---------- init / load ----------

async function initGuide() {
  if (!guideState.wired) {
    guideState.wired = true;
    $("#guide-champion").addEventListener("change", (e) => {
      if (guideState.editing
          && !confirm("You have an unsaved guide edit — discard it?")) {
        e.target.value = guideState.myChampion; // stay put
        return;
      }
      guideState.myChampion = e.target.value;
      guideState.editing = null;
      guideState.editingGeneral = false;
      guideState.editingItemBuild = false;
      updateGuideChampionIcon();
      loadGuide();
    });
    $("#guide-add-form").addEventListener("submit", (e) => {
      e.preventDefault();
      addGuideMatchup();
    });
    $("#guide-live-btn").addEventListener("click", liveLookup);
    wireExportImport();
  }
  await loadChampionRoster(); // loadGuideChampionOptions needs the full roster
  await Promise.all([loadGuideChampionOptions(), loadRuneTrees(), loadItemData()]);
  await loadGuide();
  if (guideState.pendingFocus) {
    const champ = guideState.pendingFocus;
    guideState.pendingFocus = null;
    addOrFocusMatchup(champ);
    renderGuide();
    await ensureGuideMatchupGames(champ);
    renderGuide();
  }
}

async function loadGuideChampionOptions() {
  // full roster (pool grouped on top), so you can prep a guide for a champion
  // you haven't played — the initial selection is the pool's first pick
  // (your main), else one you have played
  if (!guideState.myChampion) {
    const [pool, played] = await Promise.all([
      poolChampionOrder(), getJSON(`/api/filters?${accountParams()}`)]);
    guideState.myChampion = pool[0] || played.champions[0]
      || [...roster.nameById.keys()][0] || "";
  }
  const options = await championOptions(guideState.myChampion);
  $("#guide-champion").innerHTML = options || `<option value="">No champions found</option>`;
  updateGuideChampionIcon();
}

function updateGuideChampionIcon() {
  $("#guide-champion-icon").innerHTML = champIcon(guideState.myChampion);
}

async function loadGuide() {
  guideState.games = new Map();
  guideState.expanded = new Set();
  guideState.openRunePages = new Set();
  guideState.editingItemBuild = false;
  if (!guideState.myChampion) {
    guideState.matchups = [];
    guideState.guide = {};
    guideState.generalNotes = "";
    guideState.itemBuild = { core: [], situational: [] };
    renderGuide();
    renderGuideGeneral();
    renderGuideItemBuild();
    return;
  }
  const [matchups, guide, general, itemBuild] = await Promise.all([
    getJSON(`/api/stats/matchups?${accountParams()}&champion=${encodeURIComponent(guideState.myChampion)}&min_games=1`),
    getJSON(`/api/matchups/notes?my_champion=${encodeURIComponent(guideState.myChampion)}`),
    getJSON(`/api/champions/notes/${encodeURIComponent(guideState.myChampion)}`),
    getJSON(`/api/champions/item-build/${encodeURIComponent(guideState.myChampion)}`),
  ]);
  // a guide can exist for an opponent this champion has never faced (added
  // by hand, imported, or migrated from pre-champ-guide notes) — give it a
  // zero-games row so it always shows up
  for (const opp of Object.keys(guide)) {
    if (!matchups.some((m) => m.opp_champion === opp)) {
      matchups.push({ opp_champion: opp, games: 0, winrate: null });
    }
  }
  guideState.matchups = matchups;
  guideState.guide = guide;
  guideState.generalNotes = general.notes;
  guideState.itemBuild = itemBuild;
  renderGuide();
  renderGuideGeneral();
  renderGuideItemBuild();
  updateGuideAddOptions();
}

// "Add a matchup" dropdown: full roster minus opponents that already have a
// guide for the selected champion (those are edited via their own row)
function updateGuideAddOptions() {
  const select = $("#guide-add-select");
  const previous = select.value;
  const options = [...roster.nameById.keys()]
    .filter((c) => !guideState.guide[c])
    .sort((a, b) => champDisplay(a).localeCompare(champDisplay(b)));
  select.innerHTML = `<option value="">– pick an opponent –</option>` + options.map((c) =>
    `<option value="${c}">${escapeHtml(champDisplay(c))}</option>`).join("");
  if (options.includes(previous)) select.value = previous;
}

// fetch a matchup's recent games on first expand only (collapsed rows never
// pay for this — see .guide-toggle in wireGuideHandlers)
async function ensureGuideMatchupGames(champ) {
  if (guideState.games.has(champ)) return;
  const m = guideState.matchups.find((x) => x.opp_champion === champ);
  if (!m || m.games <= 0) return;
  const games = await getJSON(`/api/stats/games?${accountParams()}` +
    `&champion=${encodeURIComponent(guideState.myChampion)}&opp_champion=${encodeURIComponent(champ)}`);
  guideState.games.set(champ, games);
}

async function addGuideMatchup() {
  const champ = $("#guide-add-select").value;
  if (!champ) {
    $("#guide-add-status").textContent = "pick an opponent first";
    return;
  }
  $("#guide-add-select").value = "";
  $("#guide-add-status").textContent = "";
  addOrFocusMatchup(champ);
  renderGuide();
  await ensureGuideMatchupGames(champ);
  renderGuide();
}

function addOrFocusMatchup(champ) {
  if (!guideState.matchups.some((m) => m.opp_champion === champ)) {
    guideState.matchups.push({ opp_champion: champ, games: 0, winrate: null, added: true });
  }
  guideState.expanded.add(champ);
  startEditing(champ);
}

function startEditing(champ) {
  guideState.expanded.add(champ);
  guideState.editing = champ;
  guideState.draft = guideFor(champ);
  guideState.draft.runes = guideState.draft.runes.map((p) => ({ ...emptyRunePage(), ...p }));
  if (!guideState.draft.patch_version) guideState.draft.patch_version = currentPatch();
  guideState.openRuneIndex = null;
}

// live game patches from DDragon's versions.json ("16.14.1" → "16.14"),
// newest first — cached in app.js's loadDdragonVersion
function currentPatch() {
  return (state.ddragonVersion || "").split(".").slice(0, 2).join(".");
}

function knownPatches() {
  const seen = new Set();
  for (const v of state.ddragonVersions || []) {
    seen.add(v.split(".").slice(0, 2).join("."));
  }
  return [...seen];
}

function patchPicker(selected) {
  const patches = knownPatches();
  if (!patches.length) {
    // offline / DDragon unreachable — plain validated text input instead
    return `<input type="text" id="guide-patch" placeholder="e.g. ${currentPatch() || "16.14"}"
      value="${escapeHtml(selected)}" style="max-width:10em">`;
  }
  if (selected && !patches.includes(selected)) patches.unshift(selected);
  return `<select id="guide-patch" style="max-width:10em">
    <option value="">– none –</option>
    ${patches.map((p) => `<option value="${escapeHtml(p)}" ${p === selected ? "selected" : ""}>${escapeHtml(p)}</option>`).join("")}
  </select>`;
}

// ---------- rune page picker ----------

function treePicker(role, selected, excluded) {
  // role: "primary" | "secondary" — which field this click sets
  return `<div class="rune-tree-picker">${RUNE_TREES.map((t) => {
    if (excluded && t.name === excluded) return "";
    return `<button type="button" class="rune-tree-btn ${t.name === selected ? "active" : ""}"
        data-role="${role}-tree" data-tree="${escapeHtml(t.name)}" title="${escapeHtml(t.name)}">
      <img src="${runeIconUrl(t.icon)}" alt="${escapeHtml(t.name)}" width="32" height="32">
      <span>${escapeHtml(t.name)}</span>
    </button>`;
  }).join("")}</div>`;
}

function runeButton(role, row, rune, active) {
  return `<button type="button" class="rune-btn ${active ? "active" : ""}"
      data-role="${role}" data-row="${row}" data-rune="${escapeHtml(rune.name)}" title="${escapeHtml(rune.name)}">
    <img src="${runeIconUrl(rune.icon)}" alt="${escapeHtml(rune.name)}" width="30" height="30">
  </button>`;
}

function primaryRunesPicker(page) {
  const tree = treeByName(page.primary_tree);
  if (!tree) return "";
  const keystoneRow = tree.rows.find((r) => r.slot === "keystone");
  const regularRows = tree.rows.filter((r) => r.slot !== "keystone");
  return `<div class="rune-row rune-row-keystone">${
      keystoneRow.runes.map((r) => runeButton("keystone", "keystone", r, r.name === page.keystone)).join("")}</div>
    ${regularRows.map((row) => `<div class="rune-row">${
      row.runes.map((r) => runeButton("primary-rune", row.slot, r, page.primary_runes[row.slot - 1] === r.name)).join("")}</div>`).join("")}`;
}

function secondaryRunesPicker(page) {
  const tree = treeByName(page.secondary_tree);
  if (!tree) return "";
  const regularRows = tree.rows.filter((r) => r.slot !== "keystone");
  return `<p class="muted rune-hint">Pick 2, from different rows</p>
    ${regularRows.map((row) => `<div class="rune-row">${
      row.runes.map((r) => runeButton("secondary-rune", row.slot, r, page.secondary_runes.includes(r.name))).join("")}</div>`).join("")}`;
}

function shardsPicker(page) {
  return SHARD_ROWS.map((row, i) => `<div class="rune-row">${
    row.shards.map((s) => `<button type="button" class="rune-btn shard-btn ${page.shards[i] === s.name ? "active" : ""}"
        data-role="shard" data-row="${i}" data-shard="${escapeHtml(s.name)}" title="${escapeHtml(s.name)}">
      <img src="${shardIconUrl(s.icon)}" alt="${escapeHtml(s.name)}" width="24" height="24">
    </button>`).join("")}</div>`).join("");
}

function runePageEditor(index) {
  const page = guideState.draft.runes[index];
  return `<div class="rune-page-editor" data-index="${index}">
    <input type="text" class="rune-page-label" data-index="${index}" placeholder="Label (optional) — e.g. “vs poke”"
      value="${escapeHtml(page.label)}">
    <div class="rune-page-cols">
      <div class="rune-tree-col">
        <h5>Primary tree</h5>
        ${treePicker("primary", page.primary_tree, page.secondary_tree)}
        ${primaryRunesPicker(page)}
      </div>
      <div class="rune-tree-col">
        <h5>Secondary tree</h5>
        ${treePicker("secondary", page.secondary_tree, page.primary_tree)}
        ${secondaryRunesPicker(page)}
      </div>
    </div>
    <h5>Stat shards</h5>
    ${shardsPicker(page)}
    <div class="session-actions">
      <button type="button" class="preset rune-page-done" data-index="${index}">Done</button>
      <button type="button" class="preset rune-page-remove" data-index="${index}">Remove page</button>
    </div>
  </div>`;
}

function runePageSummary(index) {
  const page = guideState.draft.runes[index];
  const primaryTree = treeByName(page.primary_tree);
  const secondaryTree = treeByName(page.secondary_tree);
  const keystoneIcon = page.keystone ? runeIcon(page.keystone, primaryTree) : "";
  const label = page.label || `Page ${index + 1}`;
  const empty = !pageHasContent(page);
  return `<div class="rune-page-chip">
    <div class="rune-page-chip-icons">
      ${keystoneIcon ? `<img src="${runeIconUrl(keystoneIcon)}" alt="${escapeHtml(page.keystone)}" width="28" height="28" title="${escapeHtml(page.keystone)}">` : ""}
      ${secondaryTree ? `<img src="${runeIconUrl(secondaryTree.icon)}" alt="${escapeHtml(secondaryTree.name)}" width="20" height="20" title="${escapeHtml(secondaryTree.name)}" class="rune-page-chip-secondary">` : ""}
    </div>
    <div class="rune-page-chip-text">
      <strong>${escapeHtml(label)}</strong>
      ${empty ? `<span class="muted">empty — click edit to build a page</span>` : `<span class="muted">${
        [page.keystone, page.secondary_tree].filter(Boolean).join(" · ")}</span>`}
    </div>
    <button type="button" class="preset icon-btn rune-page-edit" data-index="${index}"
      title="Edit rune page" aria-label="Edit rune page">✎</button>
    <button type="button" class="preset icon-btn rune-page-remove" data-index="${index}"
      title="Remove rune page" aria-label="Remove rune page">✕</button>
  </div>`;
}

function runePagesBuilder() {
  const pages = guideState.draft.runes;
  const chips = pages.map((_, i) =>
    guideState.openRuneIndex === i ? runePageEditor(i) : runePageSummary(i)).join("");
  return `<div class="rune-pages-builder">
    <h4>Rune pages</h4>
    ${chips || `<p class="muted">No rune pages yet.</p>`}
    <button type="button" class="preset rune-page-add">+ Add rune page</button>
  </div>`;
}

// ---------- read-only display ----------

// shared icon-strip builder for a rune page — used both for saved guide
// pages (runePageCard) and the runes actually played in a past game
// (recentGamesColumn's compact variant)
function runeIconImg(name, tree, size, cls) {
  if (!name) return "";
  const icon = runeIcon(name, tree);
  if (!icon) return ""; // unknown rune/tree (e.g. migrated legacy page) — no broken <img>
  return `<img class="${cls || ""}" src="${runeIconUrl(icon)}"
      alt="${escapeHtml(name)}" title="${escapeHtml(name)}" width="${size}" height="${size}">`;
}

function shardIconImg(name, row, size) {
  if (!name) return "";
  const shard = (SHARD_ROWS[row] || { shards: [] }).shards.find((s) => s.name === name);
  return shard
    ? `<img src="${shardIconUrl(shard.icon)}" alt="${escapeHtml(name)}" title="${escapeHtml(name)}" width="${size}" height="${size}">`
    : "";
}

function runePageIcons(page, { keystoneSize, minorSize, treeSize, shardSize }) {
  const primaryTree = treeByName(page.primary_tree);
  const secondaryTree = treeByName(page.secondary_tree);
  const primaryMinorIcons = page.primary_runes
    .map((name) => runeIconImg(name, primaryTree, minorSize)).join("");
  const secondaryMinorIcons = page.secondary_runes
    .map((name) => runeIconImg(name, secondaryTree, minorSize)).join("");
  const shardIcons = page.shards
    .map((name, r) => shardIconImg(name, r, shardSize)).join("");
  return `${runeIconImg(page.keystone, primaryTree, keystoneSize, "rune-keystone-icon")}
    <div class="rune-page-minors">${primaryMinorIcons}</div>
    ${secondaryTree ? `<img class="rune-tree-icon" src="${runeIconUrl(secondaryTree.icon)}"
      alt="${escapeHtml(secondaryTree.name)}" title="${escapeHtml(secondaryTree.name)}"
      width="${treeSize}" height="${treeSize}">` : ""}
    <div class="rune-page-minors">${secondaryMinorIcons}</div>
    <div class="rune-page-shards">${shardIcons}</div>`;
}

// expanded read-only detail: every pick with its name, grouped by tree
function runePageDetail(page) {
  const primaryTree = treeByName(page.primary_tree);
  const secondaryTree = treeByName(page.secondary_tree);
  const runeItem = (name, tree, size = 18) => name
    ? `<div class="rune-detail-item">${runeIconImg(name, tree, size)}<span>${escapeHtml(name)}</span></div>` : "";
  const treeHead = (tree) => tree
    ? `<h6><img src="${runeIconUrl(tree.icon)}" width="14" height="14" alt="">${escapeHtml(tree.name)}</h6>`
    : `<h6>–</h6>`;
  const shardItem = (name, row) => name
    ? `<div class="rune-detail-item">${shardIconImg(name, row, 14)}<span>${escapeHtml(name)}</span></div>` : "";
  return `<div class="rune-page-detail">
    <div class="rune-detail-col">
      ${treeHead(primaryTree)}
      ${runeItem(page.keystone, primaryTree, 22)}
      ${(page.primary_runes || []).map((n) => runeItem(n, primaryTree)).join("")}
    </div>
    <div class="rune-detail-col">
      ${treeHead(secondaryTree)}
      ${(page.secondary_runes || []).map((n) => runeItem(n, secondaryTree)).join("")}
    </div>
    <div class="rune-detail-col">
      <h6>Shards</h6>
      ${(page.shards || []).map((n, r) => shardItem(n, r)).join("")}
    </div>
  </div>`;
}

function runePageCard(page, i, opp) {
  const key = `${opp}:${i}`;
  const open = guideState.openRunePages.has(key);
  return `<div class="rune-page-card">
    <div class="rune-page-card-head">
      <span class="rune-page-title${page.label ? "" : " muted"}">${escapeHtml(page.label || `Page ${i + 1}`)}</span>
      <button type="button" class="preset seg-toggle rune-page-view-toggle" data-key="${escapeHtml(key)}"
        aria-expanded="${open}" title="${open ? "Hide" : "Show"} rune details">${open ? "▾" : "▸"}</button>
    </div>
    <div class="rune-page-display">${
      runePageIcons(page, { keystoneSize: 17, minorSize: 10, treeSize: 12, shardSize: 8 })}
    </div>
    ${open ? runePageDetail(page) : ""}
  </div>`;
}

function runePagesDisplay(runes, opp) {
  if (!runes || !runes.length) return "";
  return `<div class="rune-pages-display">${
    runes.map((page, i) => runePageCard(page, i, opp)).join("")}</div>`;
}

// ---------- matchup rows ----------

// per-row indicator column: 📝 notes, 🔮 rune pages. Always rendered (even
// empty) so the collapsed list's grid columns line up like a table.
function guideFlags(champ) {
  const g = guideState.guide[champ];
  const notesFlag = g && g.notes
    ? `<span class="note-flag" title="Has matchup notes">📝</span>` : "";
  const runesFlag = g && g.runes && g.runes.length
    ? `<span class="note-flag" title="Has rune pages">🔮</span>` : "";
  return `<span class="guide-flags">${notesFlag}${runesFlag}</span>`;
}

function guideRow(m) {
  const champ = m.opp_champion;
  const expanded = guideState.expanded.has(champ);
  const editing = guideState.editing === champ;
  const statLine = m.games
    ? `<span class="muted guide-stat">${m.games} games · ${wrCell(m.winrate)}</span>`
    : `<span class="muted guide-stat">Not played yet</span>`;
  const toggleBtn = `<button class="preset seg-toggle guide-toggle" data-opp="${escapeHtml(champ)}"
      aria-expanded="${expanded}" title="${expanded ? "Collapse" : "Expand"} matchup">${expanded ? "▾" : "▸"}</button>`;
  if (!expanded) {
    return `<div class="mu-notes mu-guide guide-row guide-row-collapsed" data-opp="${escapeHtml(champ)}">
      <div class="mu-notes-head">
        ${toggleBtn}
        <h4>${champIcon(champ)}${displayName(champ)}</h4>
        ${guideFlags(champ)}
        ${statLine}
      </div>
    </div>`;
  }
  const { notes, runes, patch_version, skill_order } = guideFor(champ);
  const hasBuild = skill_order && skill_order.some(Boolean);
  const hasAny = notes || (runes && runes.length) || patch_version || hasBuild;
  let body;
  if (editing) {
    const draft = guideState.draft;
    // the skill order is edited (and saved) through the cooldown popup —
    // here it just shows with its own nested edit/remove controls
    body = `${runePagesBuilder()}
      <div class="guide-build">
        <div class="guide-build-head">
          <h5>Skill order</h5>
          <button type="button" class="preset icon-btn-sm guide-cd-link" data-opp="${escapeHtml(champ)}"
            title="Edit skill order in the cooldown comparison">✎ Edit</button>
          ${hasBuild ? `<button type="button" class="preset icon-btn-sm guide-build-clear" data-opp="${escapeHtml(champ)}"
            title="Remove saved skill order" aria-label="Remove saved skill order">🗑</button>` : ""}
        </div>
        ${hasBuild ? skillGridMini(skill_order)
          : `<p class="muted">No saved skill order yet — ✎ Edit opens the cooldown view to build one.</p>`}
      </div>
      <label class="filter-label" for="guide-patch">Patch</label>
      ${patchPicker(draft.patch_version)}
      <label class="filter-label" for="guide-notes">How to play this matchup (Markdown)</label>
      <textarea id="guide-notes" rows="8"
        placeholder="Game plan, power spikes, bans…">${escapeHtml(draft.notes)}</textarea>
      <div class="session-actions">
        <button class="preset guide-save" data-opp="${escapeHtml(champ)}">Save</button>
        <button class="preset guide-cancel">Cancel</button>
        <span class="muted guide-status"></span>
      </div>`;
  } else if (hasAny) {
    const buildBlock = hasBuild
      ? `<div class="guide-build"><h5>Skill order</h5>${skillGridMini(skill_order)}</div>` : "";
    body = `${runePagesDisplay(runes, champ)}${buildBlock}${
      notes ? `<div class="md-body">${renderNotes(notes)}</div>` : ""}`;
  } else {
    body = `<p class="muted">No guide yet —
      <button type="button" class="link-btn guide-edit" data-opp="${escapeHtml(champ)}">click here to create one</button>.</p>`;
  }
  const patchBadge = !editing && patch_version
    ? `<span class="guide-patch-badge" title="Written for this patch">Patch ${escapeHtml(patch_version)}</span>` : "";
  const gamesSide = m.games > 0
    ? `<div class="guide-row-side">${recentGamesColumn(champ)}</div>` : "";
  return `<div class="mu-notes mu-guide guide-row" data-opp="${escapeHtml(champ)}">
    <div class="mu-notes-head">
      ${toggleBtn}
      <h4>${champIcon(champ)}${displayName(champ)}</h4>
      ${patchBadge}
      ${statLine}
      <button class="preset icon-btn guide-op-link" data-opp="${escapeHtml(champ)}"
        title="One Pager — full-screen quick reference" aria-label="One Pager">📄</button>
      <button class="preset icon-btn guide-cd-link" data-opp="${escapeHtml(champ)}"
        title="Compare ability cooldowns" aria-label="Compare ability cooldowns">⏱</button>
      ${state.enableComparison ? `<button class="preset icon-btn guide-compare-link" data-opp="${escapeHtml(champ)}"
        title="Compare vs other players — opens a side-by-side window" aria-label="Compare vs other players">⧉</button>` : ""}
      ${editing || !hasAny ? "" : `<button class="preset icon-btn guide-edit" data-opp="${escapeHtml(champ)}"
        title="Edit matchup guide" aria-label="Edit matchup guide">✎</button>`}
    </div>
    <div class="guide-row-grid">
      <div class="guide-row-main">${body}</div>
      ${gamesSide}
    </div>
  </div>`;
}

function recentGamesColumn(champ) {
  const games = guideState.games.get(champ);
  if (!games) return `<h5>Recent games</h5><div class="muted">Loading…</div>`;
  if (!games.length) return `<h5>Recent games</h5><div class="muted">No games recorded yet.</div>`;
  const rows = games.slice(0, 10).map((g) => {
    const csMin = (g.cs * 60 / g.game_duration_s).toFixed(1);
    const runesStrip = g.runes
      ? `<div class="guide-game-runes">${
          runePageIcons(g.runes, { keystoneSize: 18, minorSize: 14, treeSize: 16, shardSize: 12 })}</div>`
      : "";
    return `<div class="guide-game-entry">
      <div class="guide-game-row">
        <span class="result-pill ${g.win ? "win" : "loss"}">${g.win ? "W" : "L"}</span>
        <span class="muted">${fmtDateTime(g.game_creation_ms)}</span>
        <span>${g.kills}/${g.deaths}/${g.assists}</span>
        <span class="muted">${csMin} cs/min</span>
        <span class="muted">${fmtDuration(g.game_duration_s)}</span>
      </div>
      ${runesStrip}
    </div>`;
  }).join("");
  return `<h5>Recent games</h5>${rows}`;
}

function renderGuide() {
  const target = $("#guide-list");
  if (!guideState.myChampion) {
    target.innerHTML = `<div class="empty">Pick a champion above to see or build its matchup guide.</div>`;
    return;
  }
  const rows = [...guideState.matchups].sort((a, b) => {
    // guided/freshly-added matchups float to the top, then most-played
    const aHas = Boolean(guideState.guide[a.opp_champion]) || a.added;
    const bHas = Boolean(guideState.guide[b.opp_champion]) || b.added;
    if (aHas !== bHas) return aHas ? -1 : 1;
    return (b.games || 0) - (a.games || 0);
  });
  target.innerHTML = rows.length
    ? rows.map(guideRow).join("")
    : `<div class="empty">No matchups for ${displayName(guideState.myChampion)} yet — add one below.</div>`;
  wireGuideHandlers(target);
  if (guideState.editing) {
    const row = target.querySelector(`.guide-row[data-opp="${guideState.editing}"]`);
    if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

// ---------- event wiring ----------

function wireGuideHandlers(target) {
  target.querySelectorAll(".guide-toggle").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const champ = btn.dataset.opp;
      if (guideState.expanded.has(champ)) {
        if (guideState.editing === champ
            && !confirm("You have an unsaved guide edit — discard it?")) return;
        guideState.expanded.delete(champ);
        if (guideState.editing === champ) guideState.editing = null;
        renderGuide();
        return;
      }
      guideState.expanded.add(champ);
      renderGuide(); // show "Loading…" immediately
      await ensureGuideMatchupGames(champ);
      renderGuide();
    }));
  target.querySelectorAll(".guide-edit").forEach((btn) =>
    btn.addEventListener("click", async () => {
      startEditing(btn.dataset.opp);
      renderGuide();
      await ensureGuideMatchupGames(btn.dataset.opp);
      renderGuide();
    }));
  target.querySelectorAll(".guide-cancel").forEach((btn) =>
    btn.addEventListener("click", () => {
      guideState.editing = null;
      guideState.draft = null;
      guideState.openRuneIndex = null;
      renderGuide();
    }));
  target.querySelectorAll(".guide-save").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const opp = btn.dataset.opp;
      const payload = {
        notes: $("#guide-notes").value,
        patch_version: $("#guide-patch").value,
        runes: guideState.draft.runes.filter(pageHasContent),
      };
      const response = await fetch(
        `/api/matchups/notes/${encodeURIComponent(guideState.myChampion)}/${encodeURIComponent(opp)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const status = btn.parentElement.querySelector(".guide-status");
        status.classList.add("status-error");
        status.textContent = `Save failed — ${body.detail || `error ${response.status}`}`;
        return;
      }
      // the editor doesn't touch skill_order (saved from the cooldown popup)
      // — the server keeps it (partial update), so keep it client-side too
      const keptBuild = guideFor(opp).skill_order;
      const hasAny = payload.notes.trim() || payload.runes.length
        || payload.patch_version.trim() || keptBuild.some(Boolean);
      if (hasAny) guideState.guide[opp] = { ...payload, skill_order: keptBuild };
      else delete guideState.guide[opp];
      guideState.editing = null;
      guideState.draft = null;
      guideState.openRuneIndex = null;
      renderGuide();
      updateGuideAddOptions();
    }));
  target.querySelectorAll(".guide-cd-link").forEach((btn) =>
    btn.addEventListener("click", () =>
      openCooldowns(guideState.myChampion, btn.dataset.opp))); // cooldowns.js
  target.querySelectorAll(".guide-op-link").forEach((btn) =>
    btn.addEventListener("click", () => openOnePager(btn.dataset.opp)));
  target.querySelectorAll(".guide-compare-link").forEach((btn) =>
    btn.addEventListener("click", () => openComparisonWindow(btn.dataset.opp)));
  target.querySelectorAll(".guide-build-clear").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const opp = btn.dataset.opp;
      if (!confirm("Remove the saved skill order for this matchup?")) return;
      const response = await fetch(
        `/api/matchups/notes/${encodeURIComponent(guideState.myChampion)}/${encodeURIComponent(opp)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skill_order: [] }),
        });
      if (!response.ok) return;
      const entry = guideState.guide[opp];
      if (entry) {
        entry.skill_order = [];
        if (!entry.notes && !(entry.runes || []).length && !entry.patch_version) {
          delete guideState.guide[opp]; // server deleted the all-blank row too
        }
      }
      renderGuide();
      updateGuideAddOptions();
    }));

  // rune page builder — only present while editing
  // read-only rune-page detail toggle (no edit mode needed)
  target.querySelectorAll(".rune-page-view-toggle").forEach((btn) =>
    btn.addEventListener("click", () => {
      const key = btn.dataset.key;
      guideState.openRunePages.has(key)
        ? guideState.openRunePages.delete(key) : guideState.openRunePages.add(key);
      renderGuide();
    }));
  target.querySelectorAll(".rune-page-add").forEach((btn) =>
    btn.addEventListener("click", () => {
      guideState.draft.runes.push(emptyRunePage());
      guideState.openRuneIndex = guideState.draft.runes.length - 1;
      renderGuide();
    }));
  target.querySelectorAll(".rune-page-edit").forEach((btn) =>
    btn.addEventListener("click", () => {
      guideState.openRuneIndex = +btn.dataset.index;
      renderGuide();
    }));
  target.querySelectorAll(".rune-page-done").forEach((btn) =>
    btn.addEventListener("click", () => { guideState.openRuneIndex = null; renderGuide(); }));
  target.querySelectorAll(".rune-page-remove").forEach((btn) =>
    btn.addEventListener("click", () => {
      const i = +btn.dataset.index;
      guideState.draft.runes.splice(i, 1);
      if (guideState.openRuneIndex === i) guideState.openRuneIndex = null;
      else if (guideState.openRuneIndex > i) guideState.openRuneIndex -= 1;
      renderGuide();
    }));
  target.querySelectorAll(".rune-page-label").forEach((input) =>
    input.addEventListener("input", () => {
      guideState.draft.runes[+input.dataset.index].label = input.value;
      // no re-render needed — avoids losing focus mid-type
    }));
  // keep the draft in sync so rune-click re-renders don't wipe typed text
  const notesInput = target.querySelector("#guide-notes");
  if (notesInput) notesInput.addEventListener("input", () => {
    guideState.draft.notes = notesInput.value;
  });
  const patchInput = target.querySelector("#guide-patch");
  if (patchInput) patchInput.addEventListener("change", () => {
    guideState.draft.patch_version = patchInput.value;
  });
  target.querySelectorAll("[data-role='primary-tree']").forEach((btn) =>
    btn.addEventListener("click", () => {
      const page = guideState.draft.runes[guideState.openRuneIndex];
      if (page.primary_tree === btn.dataset.tree) return;
      page.primary_tree = btn.dataset.tree;
      page.keystone = "";
      page.primary_runes = ["", "", ""];
      renderGuide();
    }));
  target.querySelectorAll("[data-role='secondary-tree']").forEach((btn) =>
    btn.addEventListener("click", () => {
      const page = guideState.draft.runes[guideState.openRuneIndex];
      if (page.secondary_tree === btn.dataset.tree) return;
      page.secondary_tree = btn.dataset.tree;
      page.secondary_runes = [];
      renderGuide();
    }));
  target.querySelectorAll("[data-role='keystone']").forEach((btn) =>
    btn.addEventListener("click", () => {
      const page = guideState.draft.runes[guideState.openRuneIndex];
      page.keystone = page.keystone === btn.dataset.rune ? "" : btn.dataset.rune;
      renderGuide();
    }));
  target.querySelectorAll("[data-role='primary-rune']").forEach((btn) =>
    btn.addEventListener("click", () => {
      const page = guideState.draft.runes[guideState.openRuneIndex];
      const row = +btn.dataset.row - 1;
      page.primary_runes[row] = page.primary_runes[row] === btn.dataset.rune ? "" : btn.dataset.rune;
      renderGuide();
    }));
  target.querySelectorAll("[data-role='secondary-rune']").forEach((btn) =>
    btn.addEventListener("click", () => {
      const page = guideState.draft.runes[guideState.openRuneIndex];
      const tree = treeByName(page.secondary_tree);
      const name = btn.dataset.rune;
      const rowIdx = rowIndexOf(tree, name);
      if (page.secondary_runes.includes(name)) {
        page.secondary_runes = page.secondary_runes.filter((n) => n !== name);
      } else {
        const withoutThisRow = page.secondary_runes.filter((n) => rowIndexOf(tree, n) !== rowIdx);
        if (withoutThisRow.length < 2) page.secondary_runes = [...withoutThisRow, name];
      }
      renderGuide();
    }));
  target.querySelectorAll("[data-role='shard']").forEach((btn) =>
    btn.addEventListener("click", () => {
      const page = guideState.draft.runes[guideState.openRuneIndex];
      const row = +btn.dataset.row;
      page.shards[row] = page.shards[row] === btn.dataset.shard ? "" : btn.dataset.shard;
      renderGuide();
    }));
}

// deep link from the Matchups table's 📖 button — sets pending state and
// lets setMainView's normal initGuide() call apply it once loaded, so we
// don't race two concurrent initGuide() calls against each other
// ---------- comparison ("research") players: opens beside the guide so you can
// theorycraft against them. Real pop-out window in a browser; falls back to an
// in-app overlay when window.open is blocked — the desktop app runs in a
// pywebview window that blocks pop-ups, so the overlay is what shows there.
// Styling lives in style.css (.cmp-*), shared by both paths. ----------

function comparisonBodyHtml(my, opp, data) {
  const smallRunes = { keystoneSize: 20, minorSize: 12, treeSize: 14, shardSize: 10 };
  const pct = (v) => (v == null ? "—" : `${Math.round(v * 100)}%`);
  const kda = (s) => (s.games ? `${(s.kills || 0).toFixed(1)}/${(s.deaths || 0).toFixed(1)}`
    + `/${(s.assists || 0).toFixed(1)}` : "—");
  const csmin = (s) => (s.games && s.cs_min ? ` · ${s.cs_min.toFixed(1)} CS/min` : "");
  const statLine = (label, s) => `<div class="cmp-stat"><span class="cmp-stat-l">${label}</span>
    <span>${s.games || 0} games · ${pct(s.winrate)}${s.games ? ` · ${kda(s)}${csmin(s)}` : ""}</span></div>`;
  const recentRunes = (recent) => (recent && recent.length
    ? recent.slice(0, 8).map((g) => `<div class="cmp-game">
        <span class="cmp-res ${g.win ? "w" : "l"}">${g.win ? "W" : "L"}</span>
        <span class="cmp-runes">${g.runes ? runePageIcons(g.runes, smallRunes)
          : "<span class='muted'>no runes recorded</span>"}</span></div>`).join("")
    : `<p class="muted">No recent games recorded — add or "Fetch more" in Settings.</p>`);

  const cards = (data.players || []).map((p) => `<div class="cmp-card"><h2>${escapeHtml(p.game_name)}
    <span class="muted">#${escapeHtml(p.tag_line)}</span></h2>
    ${statLine(`This matchup vs ${displayName(opp)}`, p.matchup)}
    ${statLine(`Overall on ${displayName(my)}`, p.overall)}
    <h3>Recent runes played</h3>${recentRunes(p.recent)}</div>`).join("")
    || `<div class="cmp-card"><p class="muted">No enabled comparison players — add them in
        Settings → Research &amp; comparison.</p></div>`;

  return `<h1 class="cmp-title">${displayName(opp)} matchup — other players</h1>
    <div class="cmp-cards">${cards}</div>`;
}

function showComparisonOverlay(inner) {
  let overlay = document.getElementById("compare-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "compare-overlay";
    overlay.className = "cmp-overlay";
    document.body.appendChild(overlay);
  }
  overlay.innerHTML = `<div class="cmp-overlay-inner">
    <button class="preset icon-btn cmp-close" title="Close (Esc)" aria-label="Close">✕</button>
    ${inner}</div>`;
  overlay.classList.remove("hidden");
  function close() { overlay.classList.add("hidden"); document.removeEventListener("keydown", onKey); }
  const onKey = (e) => { if (e.key === "Escape") close(); };
  overlay.querySelector(".cmp-close").addEventListener("click", close);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  document.addEventListener("keydown", onKey);
}

async function openComparisonWindow(opp) {
  const my = guideState.myChampion;
  const url = `compare.html?my=${encodeURIComponent(my)}&opp=${encodeURIComponent(opp)}`;
  const pw = (typeof window.pywebview !== "undefined") ? window.pywebview : null;
  // Desktop app: ask Python (pywebview) to open a real, independent second
  // native window — the main window stays fully interactive so you can keep
  // typing notes while it's open. (window.open here would trip the WebView2
  // "open in the Store?" prompt, so we never call it in the desktop app.)
  if (pw && pw.api && typeof pw.api.open_compare === "function") {
    try { await pw.api.open_compare(my, opp); return; }
    catch (e) { /* fall through to the overlay */ }
  }
  // Browser: a normal, independent pop-out window pointed at the standalone page.
  if (!pw) {
    let win = null;
    try {
      win = window.open(url, `cp-compare-${my}-${opp}`,
        "width=820,height=960,scrollbars=yes,resizable=yes");
    } catch (e) { win = null; }
    if (win) return;
  }
  // Last resort (older desktop build without the api, or a blocked browser
  // pop-up): an in-app overlay — not free-floating, but keeps it usable.
  let data;
  try {
    data = await getJSON(`/api/matchups/comparison?my_champion=${encodeURIComponent(my)}`
      + `&opp_champion=${encodeURIComponent(opp)}`);
  } catch (e) { alert("Failed to load the comparison."); return; }
  showComparisonOverlay(comparisonBodyHtml(my, opp, data));
}

function openGuide(myChampion, oppChampion) {
  guideState.myChampion = myChampion;
  guideState.pendingFocus = oppChampion;
  setMainView("guide");
}

// ---------- Live Lookup ----------
// Find the account currently in a game, switch the guide to that champion,
// and open the one-pager for the likely lane opponent. Role isn't in the
// spectator API, so the opponent is guessed from matchup history (all crawled
// games are top, so the enemy you've faced most as this champion is a strong
// bet) and can be corrected via the one-pager's enemy switcher.
async function guessLaneOpponent(myChampion, enemies) {
  if (!enemies.length) return null;
  try {
    const rows = await getJSON(`/api/stats/matchups?${accountParams()}`
      + `&champion=${encodeURIComponent(myChampion)}&min_games=1`);
    const faced = new Map(rows.map((r) => [r.opp_champion, r.games]));
    return [...enemies].sort((a, b) => (faced.get(b) || 0) - (faced.get(a) || 0))[0];
  } catch {
    return enemies[0];
  }
}

async function liveLookup() {
  const status = $("#guide-live-status");
  status.classList.remove("status-error");
  status.textContent = "Looking up your live game…";
  let data;
  try {
    data = await getJSON("/api/live-game");
  } catch {
    status.classList.add("status-error");
    status.textContent = "Live lookup failed — check your API key.";
    return;
  }
  if (!data.found) {
    status.textContent = "No live game found on your accounts.";
    return;
  }
  const keyMap = await loadChampionKeyMap(); // app.js
  const myChampion = keyMap[data.my_champion_id];
  const enemies = (data.enemy_champion_ids || []).map((id) => keyMap[id]).filter(Boolean);
  if (!myChampion) {
    status.classList.add("status-error");
    status.textContent = "Couldn't identify your champion (champion data unavailable).";
    return;
  }
  status.textContent = "";
  const opponent = await guessLaneOpponent(myChampion, enemies);
  guideState.myChampion = myChampion;
  guideState.editing = null;
  guideState.editingGeneral = false;
  await loadGuideChampionOptions();
  updateGuideChampionIcon();
  await loadGuide();
  openOnePager(opponent || enemies[0] || "", enemies);  // enemies enable the switcher
}

// ---------- general (non-matchup) notes ----------

function generalNotesBlock() {
  const champ = guideState.myChampion;
  const notes = guideState.generalNotes;
  if (guideState.editingGeneral) {
    return `<div class="mu-notes">
      <div class="mu-notes-head"><h4>General ${displayName(champ)} notes</h4></div>
      <textarea id="guide-general-input" rows="6"
        placeholder="Markdown supported — build order, itemization, general tips…">${escapeHtml(notes)}</textarea>
      <div class="session-actions">
        <button class="preset guide-general-save">Save</button>
        <button class="preset guide-general-cancel">Cancel</button>
        <span class="muted guide-general-status"></span>
      </div>
    </div>`;
  }
  const body = notes
    ? `<div class="md-body">${renderNotes(notes)}</div>`
    : `<p class="muted">No general notes for ${displayName(champ)} yet.</p>`;
  return `<div class="mu-notes">
    <div class="mu-notes-head"><h4>General ${displayName(champ)} notes</h4>
      <button class="preset icon-btn guide-general-edit" title="Edit general notes" aria-label="Edit general notes">✎</button>
    </div>${body}</div>`;
}

function renderGuideGeneral() {
  const target = $("#guide-general-notes");
  target.innerHTML = guideState.myChampion ? generalNotesBlock() : "";
  target.querySelectorAll(".guide-general-edit").forEach((btn) =>
    btn.addEventListener("click", () => { guideState.editingGeneral = true; renderGuideGeneral(); }));
  target.querySelectorAll(".guide-general-cancel").forEach((btn) =>
    btn.addEventListener("click", () => { guideState.editingGeneral = false; renderGuideGeneral(); }));
  target.querySelectorAll(".guide-general-save").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const notes = $("#guide-general-input").value;
      const response = await fetch(`/api/champions/notes/${encodeURIComponent(guideState.myChampion)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notes }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        btn.parentElement.querySelector(".guide-general-status").textContent =
          body.detail || `error ${response.status}`;
        return;
      }
      guideState.generalNotes = notes;
      guideState.editingGeneral = false;
      renderGuideGeneral();
    }));
}

// ---------- item build (core + situational sections) ----------

function itemChip(name, removable, dataAttrs) {
  const item = itemByName(name);
  const icon = item
    ? `<img src="${itemIconUrl(item.icon)}" alt="${escapeHtml(name)}" title="${escapeHtml(name)}" width="24" height="24">`
    : "";
  return `<span class="item-chip" ${dataAttrs || ""}>${icon}<span class="item-chip-name">${escapeHtml(name)}</span>${
    removable ? `<button class="preset chip-x item-chip-remove" type="button" title="Remove">×</button>` : ""}</span>`;
}

function itemBuildSectionView(label, items) {
  if (!items.length) return "";
  return `<div class="item-build-section">
    <h5>${escapeHtml(label)}</h5>
    <div class="item-build-icons">${items.map((n) => itemChip(n, false)).join("")}</div>
  </div>`;
}

function itemPickerHtml() {
  const q = guideState.itemPickerQuery.toLowerCase();
  const results = (q ? ITEMS.filter((i) => i.name.toLowerCase().includes(q)) : ITEMS).slice(0, 30);
  const rows = results.length
    ? results.map((i) => `<button class="preset item-picker-result" type="button" data-name="${escapeHtml(i.name)}">
        <img src="${itemIconUrl(i.icon)}" alt="" width="20" height="20">${escapeHtml(i.name)}</button>`).join("")
    : `<p class="muted">${ITEMS.length ? "No matching items." : "Item list unavailable (offline?)."}</p>`;
  return `<div class="item-picker">
    <input type="text" id="item-picker-search" placeholder="Search items…" value="${escapeHtml(guideState.itemPickerQuery)}">
    <div class="item-picker-results">${rows}</div>
    <button class="preset item-picker-close" type="button">Cancel</button>
  </div>`;
}

function itemBuildBlock() {
  const champ = guideState.myChampion;
  const { core, situational } = guideState.itemBuild;
  if (guideState.editingItemBuild) {
    const draft = guideState.itemBuildDraft;
    const coreChips = draft.core.map((name, i) =>
      itemChip(name, true, `data-target="core" data-index="${i}"`)).join("");
    const coreAdd = draft.core.length < MAX_CORE_ITEMS
      ? `<button class="preset item-add-btn" type="button" data-target="core">+ Add item</button>` : "";
    const situationalHtml = draft.situational.map((section, si) => {
      const chips = section.items.map((name, ii) =>
        itemChip(name, true, `data-target="situational" data-section="${si}" data-index="${ii}"`)).join("");
      const addBtn = section.items.length < MAX_ITEMS_PER_SECTION
        ? `<button class="preset item-add-btn" type="button" data-target="situational" data-section="${si}">+ Add item</button>` : "";
      return `<div class="item-build-section-editor" data-section="${si}">
        <div class="item-section-head">
          <input type="text" class="item-section-label" data-section="${si}"
            value="${escapeHtml(section.label)}" placeholder="e.g. vs heavy AP">
          <button class="preset chip-x item-section-remove" type="button" data-section="${si}" title="Remove section">×</button>
        </div>
        <div class="item-build-icons">${chips}${addBtn}</div>
      </div>`;
    }).join("");
    const addSectionBtn = draft.situational.length < MAX_SITUATIONAL_SECTIONS
      ? `<button class="preset item-section-add" type="button">+ Add situational section</button>` : "";
    const picker = guideState.itemPickerTarget ? itemPickerHtml() : "";
    return `<div class="mu-notes">
      <div class="mu-notes-head"><h4>${displayName(champ)} item build</h4></div>
      <div class="item-build-editor">
        <div class="item-build-section-editor">
          <h5>Core build <span class="muted">(first 2-3 items, in order)</span></h5>
          <div class="item-build-icons">${coreChips}${coreAdd}</div>
        </div>
        <h5>Situational <span class="muted">(1-5 items each)</span></h5>
        ${situationalHtml}
        ${addSectionBtn}
        ${picker}
        <div class="session-actions">
          <button class="preset item-build-save" type="button">Save</button>
          <button class="preset item-build-cancel" type="button">Cancel</button>
          <span class="muted item-build-status"></span>
        </div>
      </div>
    </div>`;
  }
  const sections = [
    itemBuildSectionView("Core build", core),
    ...situational.map((s) => itemBuildSectionView(s.label, s.items)),
  ].join("");
  const body = sections
    ? `<div class="item-build-display">${sections}</div>`
    : `<p class="muted">No item build for ${displayName(champ)} yet.</p>`;
  return `<div class="mu-notes">
    <div class="mu-notes-head"><h4>${displayName(champ)} item build</h4>
      <button class="preset icon-btn item-build-edit" title="Edit item build" aria-label="Edit item build">✎</button>
    </div>${body}</div>`;
}

function renderGuideItemBuild() {
  const target = $("#guide-item-build");
  target.innerHTML = guideState.myChampion ? itemBuildBlock() : "";
  target.querySelectorAll(".item-build-edit").forEach((btn) =>
    btn.addEventListener("click", () => {
      guideState.editingItemBuild = true;
      guideState.itemBuildDraft = {
        core: [...guideState.itemBuild.core],
        situational: guideState.itemBuild.situational.map((s) => ({ label: s.label, items: [...s.items] })),
      };
      guideState.itemPickerTarget = null;
      renderGuideItemBuild();
    }));
  target.querySelectorAll(".item-build-cancel").forEach((btn) =>
    btn.addEventListener("click", () => {
      guideState.editingItemBuild = false;
      guideState.itemBuildDraft = null;
      guideState.itemPickerTarget = null;
      renderGuideItemBuild();
    }));
  target.querySelectorAll(".item-chip-remove").forEach((btn) =>
    btn.addEventListener("click", () => {
      const chip = btn.closest("[data-target]");
      if (chip.dataset.target === "core") {
        guideState.itemBuildDraft.core.splice(+chip.dataset.index, 1);
      } else {
        guideState.itemBuildDraft.situational[+chip.dataset.section].items.splice(+chip.dataset.index, 1);
      }
      renderGuideItemBuild();
    }));
  target.querySelectorAll(".item-add-btn").forEach((btn) =>
    btn.addEventListener("click", () => {
      guideState.itemPickerTarget = btn.dataset.target === "core"
        ? { kind: "core" } : { kind: "situational", index: +btn.dataset.section };
      guideState.itemPickerQuery = "";
      renderGuideItemBuild();
      const search = $("#item-picker-search");
      if (search) search.focus();
    }));
  target.querySelectorAll(".item-picker-result").forEach((btn) =>
    btn.addEventListener("click", () => {
      const name = btn.dataset.name;
      const t = guideState.itemPickerTarget;
      if (!t) return;
      if (t.kind === "core") {
        if (guideState.itemBuildDraft.core.length < MAX_CORE_ITEMS) guideState.itemBuildDraft.core.push(name);
      } else {
        const section = guideState.itemBuildDraft.situational[t.index];
        if (section.items.length < MAX_ITEMS_PER_SECTION) section.items.push(name);
      }
      guideState.itemPickerTarget = null;
      renderGuideItemBuild();
    }));
  const search = target.querySelector("#item-picker-search");
  if (search) {
    search.focus();
    search.selectionStart = search.selectionEnd = search.value.length;
    search.addEventListener("input", (e) => {
      guideState.itemPickerQuery = e.target.value;
      renderGuideItemBuild();
    });
  }
  target.querySelectorAll(".item-picker-close").forEach((btn) =>
    btn.addEventListener("click", () => {
      guideState.itemPickerTarget = null;
      renderGuideItemBuild();
    }));
  target.querySelectorAll(".item-section-add").forEach((btn) =>
    btn.addEventListener("click", () => {
      if (guideState.itemBuildDraft.situational.length >= MAX_SITUATIONAL_SECTIONS) return;
      guideState.itemBuildDraft.situational.push({ label: "", items: [] });
      renderGuideItemBuild();
    }));
  target.querySelectorAll(".item-section-remove").forEach((btn) =>
    btn.addEventListener("click", () => {
      guideState.itemBuildDraft.situational.splice(+btn.dataset.section, 1);
      renderGuideItemBuild();
    }));
  target.querySelectorAll(".item-section-label").forEach((input) =>
    input.addEventListener("input", (e) => {
      guideState.itemBuildDraft.situational[+e.target.dataset.section].label = e.target.value;
    }));
  target.querySelectorAll(".item-build-save").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const draft = guideState.itemBuildDraft;
      const situational = draft.situational
        .map((s) => ({ label: s.label.trim(), items: s.items }))
        .filter((s) => s.label || s.items.length);
      const response = await fetch(`/api/champions/item-build/${encodeURIComponent(guideState.myChampion)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ core: draft.core, situational }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        target.querySelector(".item-build-status").textContent = body.detail || `error ${response.status}`;
        return;
      }
      guideState.itemBuild = { core: draft.core, situational };
      guideState.editingItemBuild = false;
      guideState.itemBuildDraft = null;
      renderGuideItemBuild();
    }));
}

// ---------- export / import ----------

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function wireExportImport() {
  $("#guide-export-password-toggle").addEventListener("change", (e) => {
    $("#guide-export-password").classList.toggle("hidden", !e.target.checked);
  });

  $("#guide-export-btn").addEventListener("click", async () => {
    if (!guideState.myChampion) return;
    const usePassword = $("#guide-export-password-toggle").checked;
    const password = usePassword ? $("#guide-export-password").value : null;
    if (usePassword && !password) {
      alert('Enter a password, or untick "Password protect".');
      return;
    }
    const response = await fetch("/api/matchups/notes/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ my_champion: guideState.myChampion, password }),
    });
    if (!response.ok) {
      alert("Export failed.");
      return;
    }
    downloadBlob(await response.blob(), `champ-guide-${guideState.myChampion.toLowerCase()}.json`);
    $("#guide-export-password").value = "";
  });

  $("#guide-export-pdf-btn").addEventListener("click", async () => {
    if (!guideState.myChampion) return;
    const status = $("#guide-export-pdf-status");
    status.textContent = "generating… (fetches rune icons, may take a moment)";
    const response = await fetch(
      `/api/matchups/notes/export.pdf?my_champion=${encodeURIComponent(guideState.myChampion)}`);
    if (!response.ok) {
      status.textContent = "export failed";
      return;
    }
    downloadBlob(await response.blob(), `champ-guide-${guideState.myChampion.toLowerCase()}.pdf`);
    status.textContent = "";
  });

  $("#guide-import-btn").addEventListener("click", async () => {
    const status = $("#guide-import-status");
    status.textContent = "";
    const file = $("#guide-import-file").files[0];
    if (!file) { status.textContent = "choose a file first"; return; }
    let data;
    try {
      data = JSON.parse(await file.text());
    } catch {
      status.textContent = "not valid JSON";
      return;
    }
    const password = $("#guide-import-password").value || undefined;
    const preview = await fetch("/api/matchups/notes/import/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data, password }),
    });
    if (!preview.ok) {
      const body = await preview.json().catch(() => ({}));
      status.textContent = preview.status === 401
        ? "password required or incorrect" : (body.detail || `error ${preview.status}`);
      return;
    }
    const info = await preview.json();
    const overwriteNote = info.will_overwrite.length
      ? ` ${info.will_overwrite.length} of these will overwrite existing guides: ${info.will_overwrite.map(displayName).join(", ")}.`
      : "";
    const extras = [info.has_general_notes && "general notes", info.has_item_build && "item build"]
      .filter(Boolean).join(" and ");
    const confirmMsg = `Import ${info.opponents.length} matchup guide(s) for ` +
      `${displayName(info.my_champion)}${extras ? ` plus its ${extras}` : ""}?${overwriteNote}`;
    if (!confirm(confirmMsg)) { status.textContent = "cancelled"; return; }
    const result = await fetch("/api/matchups/notes/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data, password }),
    });
    if (!result.ok) {
      const body = await result.json().catch(() => ({}));
      status.textContent = body.detail || `error ${result.status}`;
      return;
    }
    status.textContent = "imported!";
    $("#guide-import-file").value = "";
    $("#guide-import-password").value = "";
    guideState.editing = null;
    guideState.draft = null;
    guideState.openRuneIndex = null;
    guideState.editingGeneral = false;
    if (info.my_champion === guideState.myChampion) {
      await loadGuide();
    } else {
      guideState.myChampion = info.my_champion;
      await loadGuideChampionOptions();
      await loadGuide();
    }
  });
}

// ---------- One Pager (second-screen quick reference) ----------

// Full-screen, distraction-free summary of ONE matchup: runes, skill order,
// item build, matchup notes, general champion notes. Deliberately no
// history/stats — it's meant to sit on a second screen during a game.
function openOnePager(opp, enemies) {
  const my = guideState.myChampion;
  const { notes, runes, patch_version, skill_order } = guideFor(opp);
  const build = guideState.itemBuild || { core: [], situational: [] };
  const sections = [];
  if (runes && runes.length) {
    sections.push(`<section><h3>Runes</h3><div class="op-runes">${
      runes.map((page, i) => `<div class="op-rune-page">
        <div class="rune-page-title${page.label ? "" : " muted"}">${escapeHtml(page.label || `Page ${i + 1}`)}</div>
        <div class="rune-page-display">${
          runePageIcons(page, { keystoneSize: 44, minorSize: 28, treeSize: 32, shardSize: 20 })}</div>
      </div>`).join("")}</div></section>`);
  }
  if (skill_order && skill_order.some(Boolean)) {
    sections.push(`<section><h3>Skill order</h3>
      <div class="op-skill">${skillGridMini(skill_order)}</div></section>`);
  }
  const buildSections = [
    itemBuildSectionView("Core build", build.core || []),
    ...(build.situational || []).map((s) => itemBuildSectionView(s.label || "Situational", s.items || [])),
  ].join("");
  if (buildSections) sections.push(`<section><h3>Item build</h3>${buildSections}</section>`);
  if (notes) {
    sections.push(`<section><h3>How to play vs ${escapeHtml(displayName(opp))}</h3>
      <div class="md-body">${renderNotes(notes)}</div></section>`);
  }
  if (guideState.generalNotes) {
    sections.push(`<section><h3>General ${escapeHtml(displayName(my))} notes</h3>
      <div class="md-body">${renderNotes(guideState.generalNotes)}</div></section>`);
  }
  // from Live Lookup: since role isn't in the spectator API, offer the 5 enemy
  // champions to switch the matchup if the auto-guess picked the wrong one
  const live = enemies || [];
  const switcher = live.length > 1
    ? `<div class="op-live-switch"><span class="muted">Laning vs:</span>${
        live.map((c) => `<button type="button" class="op-enemy-btn${c === opp ? " active" : ""}"
          data-opp="${escapeHtml(c)}" title="${escapeHtml(displayName(c))}">${champIcon(c)}</button>`).join("")}</div>`
    : "";
  $("#onepager-body").innerHTML = `
    <div class="op-head">
      <div class="op-title">
        ${champIcon(my)}<strong>${escapeHtml(displayName(my))}</strong>
        <span class="muted">vs</span>
        ${champIcon(opp)}<strong>${escapeHtml(displayName(opp))}</strong>
        ${patch_version ? `<span class="guide-patch-badge">Patch ${escapeHtml(patch_version)}</span>` : ""}
      </div>
      <button type="button" class="preset icon-btn" id="onepager-close"
        title="Close (Esc)" aria-label="Close">✕</button>
    </div>
    ${switcher}
    ${sections.join("") || `<p class="muted">Nothing recorded for this matchup yet —
      add runes, a build, or notes in the Matchup guide first.</p>`}`;
  $("#onepager-overlay").classList.remove("hidden");
  $("#onepager-close").addEventListener("click", closeOnePager);
  $("#onepager-body").querySelectorAll(".op-enemy-btn").forEach((btn) =>
    btn.addEventListener("click", () => openOnePager(btn.dataset.opp, live)));
}

function closeOnePager() {
  $("#onepager-overlay").classList.add("hidden");
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#onepager-overlay").classList.contains("hidden")) closeOnePager();
});
