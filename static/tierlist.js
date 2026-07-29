"use strict";
/* Champion tier boards. Editing only ever happens HERE, in the Tier list tab
   (makeTierBoard() = the drag-and-drop board); a finished list is copied into a
   champion's Matchup guide with "Save to guide", and the guide renders that
   copy read-only via tierBoardStaticHtml(). Uses globals from app.js/blocks.js:
   $, escapeHtml, champIcon, displayName, champDisplay, roster,
   loadChampionRoster, getJSON. */

const TIER_COLORS = ["#ff6b6b", "#ffa94d", "#ffd43b", "#94d82d", "#4dabf7", "#b197fc"];
function defaultTiers() {
  return ["S", "A", "B", "C", "D"].map((label, i) =>
    ({ label, color: TIER_COLORS[i] || "#868e96", champions: [] }));
}

// champion -> [team_position, ...] inferred from stored games; loaded once
const TIER_ROLE_FILTERS = [["", "All"], ["TOP", "Top"], ["JUNGLE", "Jungle"],
                          ["MIDDLE", "Mid"], ["BOTTOM", "Bot"], ["UTILITY", "Support"]];
let CHAMP_ROLES = null;
async function loadChampionRoles() {
  if (CHAMP_ROLES) return CHAMP_ROLES;
  CHAMP_ROLES = await getJSON("/api/champion-roles").catch(() => ({}));
  return CHAMP_ROLES;
}

// rune name -> icon URL (trees + keystones/minors + stat shards), for row icons.
// Also builds a combined champion+rune datalist and a lowercase resolver.
let RUNE_ICON = null, RUNE_BY_LOWER = null;
async function loadRuneIcons() {
  if (RUNE_ICON) return RUNE_ICON;
  RUNE_ICON = {}; RUNE_BY_LOWER = {};
  try {
    const d = await (await fetch("/runes.json")).json();
    const dd = (i) => `https://ddragon.leagueoflegends.com/cdn/img/${i}`;
    const shard = (i) => "https://raw.communitydragon.org/latest/plugins/"
      + `rcp-be-lol-game-data/global/default/v1/perk-images/statmods/${i}`;
    const add = (name, url) => { RUNE_ICON[name] = url; RUNE_BY_LOWER[name.toLowerCase()] = name; };
    (d.trees || []).forEach((t) => {
      add(t.name, dd(t.icon));
      (t.rows || []).forEach((row) => (row.runes || []).forEach((r) => add(r.name, dd(r.icon))));
    });
    (d.shardRows || []).forEach((row) => (row.shards || []).forEach((s) => add(s.name, shard(s.icon))));
  } catch { /* row rune icons just won't be available */ }
  // combined champion + rune datalist for the row-icon inputs (built once)
  if (!document.getElementById("tier-icon-list")) {
    const dl = document.createElement("datalist");
    dl.id = "tier-icon-list";
    const champs = [...roster.nameById.values()].sort();
    dl.innerHTML = [...champs, ...Object.keys(RUNE_ICON).sort()]
      .map((n) => `<option value="${escapeHtml(n)}">`).join("");
    document.body.appendChild(dl);
  }
  return RUNE_ICON;
}
// resolve typed text -> {image, kind} (champion first, then rune); "" clears
function resolveRowIcon(text) {
  const v = (text || "").trim();
  if (!v) return { image: "", kind: "champion" };
  const cid = roster.byLookup.get(v.toLowerCase());
  if (cid) return { image: cid, kind: "champion" };
  const rn = RUNE_BY_LOWER && RUNE_BY_LOWER[v.toLowerCase()];
  if (rn) return { image: rn, kind: "rune" };
  return null; // unknown
}

// A self-contained tier board bound to a rows element + a pool element.
// opts: { rowsEl, poolEl, searchEl?, onChange?(tiers) }. onChange fires after
// any mutation so the owner can persist.
function makeTierBoard(opts) {
  const st = { tiers: [], search: "", role: "", flagged: new Set(), undo: [] };
  const placed = () => new Set(st.tiers.flatMap((t) => t.champions));
  // a tier row's icon: a champion (default) or a rune, rendered from its name
  const rowIconImg = (t) => {
    if (!t.image) return "";
    if (t.image_kind === "rune") {
      const url = RUNE_ICON && RUNE_ICON[t.image];
      return url ? `<img src="${url}" draggable="false" alt="" title="${escapeHtml(t.image)}">` : "";
    }
    return champIcon(t.image).replace("<img ", "<img draggable=\"false\" ");
  };
  const rowIconLabel = (t) => (t.image ? (t.image_kind === "rune" ? t.image : displayName(t.image)) : "");
  // undo stack (Ctrl+Z): snapshot before each mutation
  const snap = () => ({ tiers: JSON.parse(JSON.stringify(st.tiers)), flagged: [...st.flagged] });
  function pushUndo() { st.undo.push(snap()); if (st.undo.length > 60) st.undo.shift(); }
  function undo() {
    const prev = st.undo.pop();
    if (!prev) return;
    st.tiers = prev.tiers;
    st.flagged = new Set(prev.flagged);
    render();
    if (opts.onChange) opts.onChange();
  }
  // draggable="false" on the <img> so the chip span (not the image) is the drag
  // source — otherwise the browser's native image-drag hijacks it
  const chip = (id) => `<span class="tier-chip${st.flagged.has(id) ? " flagged" : ""}" draggable="true"
    data-champ="${escapeHtml(id)}" title="${escapeHtml(displayName(id))} — click to mark ?, drag to move"
    >${champIcon(id).replace("<img ", "<img draggable=\"false\" ")}${st.flagged.has(id) ? '<span class="tier-flag">?</span>' : ""}</span>`;

  function toggleFlag(id) {
    pushUndo();
    if (st.flagged.has(id)) st.flagged.delete(id); else st.flagged.add(id);
    changed();
  }
  function wireChips(container) {
    container.querySelectorAll(".tier-chip").forEach((c) => {
      let dragged = false;
      c.addEventListener("dragstart", (e) => {
        dragged = true;
        e.dataTransfer.setData("text/champ", c.dataset.champ);
        e.dataTransfer.effectAllowed = "move";
        c.classList.add("dragging");
      });
      c.addEventListener("dragend", () => c.classList.remove("dragging"));
      // a plain click (no drag) toggles the "?" flag
      c.addEventListener("click", () => { if (!dragged) toggleFlag(c.dataset.champ); dragged = false; });
    });
  }
  function wireDrop(el, target) {
    el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("drag-over"); });
    el.addEventListener("dragleave", () => el.classList.remove("drag-over"));
    el.addEventListener("drop", (e) => {
      e.preventDefault();
      el.classList.remove("drag-over");
      const id = e.dataTransfer.getData("text/champ");
      if (id) moveChamp(id, target);
    });
  }
  function moveChamp(id, target /* tier index or null for pool */) {
    pushUndo();
    for (const t of st.tiers) {
      const k = t.champions.indexOf(id);
      if (k !== -1) t.champions.splice(k, 1);
    }
    if (target != null && st.tiers[target]) st.tiers[target].champions.push(id);
    changed();
  }
  function renderRows() {
    opts.rowsEl.innerHTML = st.tiers.map((t, i) => `
      <div class="tier-row" data-tier="${i}">
        <div class="tier-label" style="background:${escapeHtml(t.color)}">
          <input class="tier-label-input" data-tier="${i}" value="${escapeHtml(t.label)}"
            maxlength="24" aria-label="Tier label" spellcheck="false">
          <div class="tier-label-img">
            ${rowIconImg(t)}
            <input class="tier-img-input" data-tier="${i}" list="tier-icon-list"
              value="${escapeHtml(rowIconLabel(t))}"
              placeholder="+ icon" title="A champion or rune icon for this row" aria-label="Row icon">
          </div>
          <div class="tier-label-actions">
            <input type="color" class="tier-color" data-tier="${i}" value="${escapeHtml(t.color)}"
              title="Tier colour" aria-label="Tier colour">
            <button class="tier-del icon-btn-sm" data-tier="${i}" title="Remove tier"
              aria-label="Remove tier">🗑</button>
          </div>
        </div>
        <div class="tier-drop" data-tier="${i}">${t.champions.map(chip).join("")}</div>
      </div>`).join("");
    opts.rowsEl.querySelectorAll(".tier-drop").forEach((z) => { wireChips(z); wireDrop(z, +z.dataset.tier); });
    opts.rowsEl.querySelectorAll(".tier-label-input").forEach((inp) =>
      inp.addEventListener("change", () => { pushUndo(); st.tiers[+inp.dataset.tier].label = inp.value.slice(0, 24); fire(); }));
    opts.rowsEl.querySelectorAll(".tier-color").forEach((inp) =>
      inp.addEventListener("change", () => {
        pushUndo();
        const i = +inp.dataset.tier;
        st.tiers[i].color = inp.value;
        inp.closest(".tier-row").querySelector(".tier-label").style.background = inp.value;
        fire();
      }));
    opts.rowsEl.querySelectorAll(".tier-del").forEach((btn) =>
      btn.addEventListener("click", () => { pushUndo(); st.tiers.splice(+btn.dataset.tier, 1); changed(); }));
    opts.rowsEl.querySelectorAll(".tier-img-input").forEach((inp) =>
      inp.addEventListener("change", () => {
        const r = resolveRowIcon(inp.value);
        if (!r) return;  // unknown name — leave the field for the user to fix/clear
        const tt = st.tiers[+inp.dataset.tier];
        pushUndo();
        tt.image = r.image; tt.image_kind = r.kind;
        changed();       // re-render (shows the icon) + save
      }));
  }
  function renderRoleFilter() {
    if (!opts.roleFilterEl) return;
    opts.roleFilterEl.innerHTML = TIER_ROLE_FILTERS.map(([v, l]) =>
      `<button type="button" class="preset tier-role-btn${v === st.role ? " active" : ""}" data-role="${v}">${l}</button>`).join("");
    opts.roleFilterEl.querySelectorAll(".tier-role-btn").forEach((b) =>
      b.addEventListener("click", () => { st.role = b.dataset.role; renderRoleFilter(); renderPool(); }));
  }
  function renderPool() {
    const p = placed();
    const roles = CHAMP_ROLES || {};
    const shown = [...roster.nameById.keys()]
      .filter((id) => !p.has(id)
        && (!st.search || displayName(id).toLowerCase().includes(st.search))
        && (!st.role || (roles[id] || []).includes(st.role)))
      .sort((a, b) => displayName(a).localeCompare(displayName(b)));
    opts.poolEl.innerHTML = shown.map(chip).join("")
      || `<span class="muted">${st.search || st.role ? "No champions match." : "Every champion is ranked."}</span>`;
    if (opts.countEl) opts.countEl.textContent = `(${shown.length})`;
    wireChips(opts.poolEl);
    wireDrop(opts.poolEl, null);
  }
  function render() { renderRoleFilter(); renderRows(); renderPool(); }
  function fire() { if (opts.onChange) opts.onChange(st.tiers); }
  function changed() { render(); fire(); }

  if (opts.searchEl) {
    opts.searchEl.addEventListener("input", (e) => { st.search = e.target.value.trim().toLowerCase(); renderPool(); });
  }
  // Ctrl/Cmd+Z undoes the last change while THIS board is the visible one (and
  // focus isn't in a text field, where the browser's own undo should win)
  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.key.toLowerCase() !== "z") return;
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || e.target.isContentEditable) return;
    if (opts.rowsEl.offsetParent !== null) { e.preventDefault(); undo(); }
  });
  return {
    setTiers(tiers) {
      st.tiers = (tiers && tiers.length)
        ? tiers.map((t) => ({ label: t.label || "", color: t.color || "#868e96",
                              image: t.image || "", image_kind: t.image_kind || "champion",
                              champions: [...(t.champions || [])] }))
        : defaultTiers();
      st.undo = [];  // don't undo across list loads
      render();
    },
    getTiers() { return st.tiers; },
    setFlagged(ids) { st.flagged = new Set(ids || []); render(); },
    getFlagged() { return [...st.flagged]; },
    addTier() {
      pushUndo();
      st.tiers.push({ label: "", color: TIER_COLORS[st.tiers.length % TIER_COLORS.length],
                      image: "", image_kind: "champion", champions: [] });
      changed();
    },
    render,
  };
}

// ---------- read-only rendering (a copy saved into a Matchup guide) ----------

// a tier row's icon outside the editor: champion (default) or rune, by name
function tierRowIconHtml(t) {
  if (!t.image) return "";
  if (t.image_kind === "rune") {
    const url = RUNE_ICON && RUNE_ICON[t.image];
    return url ? `<img src="${url}" alt="" title="${escapeHtml(t.image)}">` : "";
  }
  return champIcon(t.image);
}

// Renders {tiers, flagged} exactly like the editor's board but with no drag,
// no inputs and no pool — what the Matchup guide shows. Call loadRuneIcons()
// first if the list may carry rune row icons.
function tierBoardStaticHtml(data) {
  const tiers = (data && data.tiers) || [];
  const flagged = new Set((data && data.flagged) || []);
  if (!tiers.length) return `<p class="muted">This tier list is empty.</p>`;
  return `<div class="tier-static">${tiers.map((t) => `
    <div class="tier-row">
      <div class="tier-label" style="background:${escapeHtml(t.color || "#868e96")}">
        <span class="tier-label-text">${escapeHtml(t.label || "")}</span>
        ${t.image ? `<div class="tier-label-img">${tierRowIconHtml(t)}</div>` : ""}
      </div>
      <div class="tier-drop">${(t.champions || []).map((id) => `
        <span class="tier-chip${flagged.has(id) ? " flagged" : ""}"
          title="${escapeHtml(displayName(id))}">${champIcon(id)}${
          flagged.has(id) ? '<span class="tier-flag">?</span>' : ""}</span>`).join("")}</div>
    </div>`).join("")}</div>`;
}

// ---------- compare window (up to 4 lists, 2 on top / 2 below) ----------
// Read-only: it draws the same static boards side by side so two (or four)
// lists can be scanned against each other — editing stays in the tab.

const TIER_COMPARE_MAX = 4;
const tierCompare = { lists: [], selected: [] };

async function openTierCompare() {
  await loadChampionRoster();
  await loadRuneIcons();
  // every list, including the copies saved into champions' Matchup guides
  tierCompare.lists = await getJSON("/api/tier-lists?scope=all").catch(() => []);
  const alive = new Set(tierCompare.lists.map((l) => l.id));
  tierCompare.selected = tierCompare.selected.filter((id) => alive.has(id));
  if (!tierCompare.selected.length && alive.has(tierState.activeId)) {
    tierCompare.selected = [tierState.activeId];  // start from the list you're on
  }
  $("#tier-compare-overlay").classList.remove("hidden");
  renderTierCompare();
}

function closeTierCompare() { $("#tier-compare-overlay").classList.add("hidden"); }

function tierCompareLabel(l) {
  return l.champion ? `${l.title} · ${champDisplay(l.champion)}` : l.title;
}

function toggleTierCompare(id) {
  const i = tierCompare.selected.indexOf(id);
  if (i !== -1) tierCompare.selected.splice(i, 1);
  else if (tierCompare.selected.length < TIER_COMPARE_MAX) tierCompare.selected.push(id);
  renderTierCompare();
}

function renderTierCompare() {
  const sel = tierCompare.selected;
  const full = sel.length >= TIER_COMPARE_MAX;
  $("#tier-compare-picker").innerHTML = tierCompare.lists.map((l) => {
    const on = sel.includes(l.id);
    return `<label class="tier-compare-pick${on ? " active" : ""}${!on && full ? " disabled" : ""}">
      <input type="checkbox" data-id="${l.id}"${on ? " checked" : ""}${!on && full ? " disabled" : ""}>
      ${escapeHtml(tierCompareLabel(l))}</label>`;
  }).join("") || `<span class="muted">No tier lists yet.</span>`;
  $("#tier-compare-picker").querySelectorAll("input[data-id]").forEach((cb) =>
    cb.addEventListener("change", () => toggleTierCompare(+cb.dataset.id)));

  const chosen = sel.map((id) => tierCompare.lists.find((l) => l.id === id)).filter(Boolean);
  $("#tier-compare-grid").innerHTML = chosen.length
    ? chosen.map((l) => `
      <div class="tier-compare-cell">
        <div class="tier-compare-cell-head">
          ${l.champion ? champIcon(l.champion) : ""}
          <strong>${escapeHtml(l.title)}</strong>
          ${l.champion ? `<span class="muted">${escapeHtml(champDisplay(l.champion))}'s guide</span>` : ""}
        </div>
        ${tierBoardStaticHtml(l.data)}
      </div>`).join("")
    : `<p class="muted">Pick up to ${TIER_COMPARE_MAX} tier lists above.</p>`;
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#tier-compare-overlay").classList.contains("hidden")) {
    closeTierCompare();
  }
});

// ---------- the standalone Tier list tab ----------

const tierState = { wired: false, lists: [], activeId: null, board: null, saveTimer: null };

async function initTiers() {
  if (!tierState.wired) {
    tierState.wired = true;
    await loadChampionRoster();
    await loadChampionRoles();
    await loadRuneIcons();
    tierState.board = makeTierBoard({
      rowsEl: $("#tier-rows"), poolEl: $("#tier-pool"), searchEl: $("#tier-search"),
      countEl: $("#tier-pool-count"), roleFilterEl: $("#tier-role-filter"),
      onChange: () => saveActiveTierList(),
    });
    $("#tier-new").addEventListener("click", createTierList);
    $("#tier-new-name").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); createTierList(); }
    });
    $("#tier-rename").addEventListener("click", renameTierList);
    $("#tier-delete").addEventListener("click", deleteTierList);
    $("#tier-add-row").addEventListener("click", () => tierState.board.addTier());
    $("#tier-list-select").addEventListener("change", (e) => selectList(+e.target.value));
    $("#tier-save-to-guide").addEventListener("click", saveTierListToGuide);
    $("#tier-compare").addEventListener("click", openTierCompare);
    $("#tier-compare-close").addEventListener("click", closeTierCompare);
    renderTierGuideChampions();
  }
  await loadTierLists();
}

// "Save to guide" target: the full roster, same as the guide's own picker
function renderTierGuideChampions() {
  const ids = [...roster.nameById.keys()].sort((a, b) => champDisplay(a).localeCompare(champDisplay(b)));
  $("#tier-guide-champion").innerHTML = `<option value="">champion…</option>`
    + ids.map((id) => `<option value="${escapeHtml(id)}">${escapeHtml(champDisplay(id))}</option>`).join("");
}

// Each list remembers the champion it was last saved to, so re-saving after an
// edit is one click (the copy in the guide is a snapshot — it does NOT follow
// later edits until saved again).
function guideTargetKey(id) { return `cp-tier-guide-${id}`; }

function syncTierGuideTarget() {
  $("#tier-guide-champion").value = localStorage.getItem(guideTargetKey(tierState.activeId)) || "";
}

async function saveTierListToGuide() {
  const champ = $("#tier-guide-champion").value;
  const list = tierState.lists.find((l) => l.id === tierState.activeId);
  if (!list) return;
  if (!champ) { tierStatus("pick a champion to save to"); setTimeout(() => tierStatus(""), 2000); return; }
  tierStatus("saving to guide…");
  const res = await fetch(`/api/champions/${encodeURIComponent(champ)}/tier-lists`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: list.title,
      data: { tiers: tierState.board.getTiers(), flagged: tierState.board.getFlagged() },
    }),
  }).catch(() => null);
  if (!(res && res.ok)) { tierStatus("save to guide failed"); setTimeout(() => tierStatus(""), 2500); return; }
  const saved = await res.json();
  localStorage.setItem(guideTargetKey(list.id), champ);
  tierStatus(`${saved.replaced ? "updated in" : "saved to"} ${champDisplay(champ)}'s guide ✓`);
  setTimeout(() => tierStatus(""), 2500);
}

async function loadTierLists() {
  tierState.lists = await getJSON("/api/tier-lists").catch(() => []);
  if (!tierState.lists.length) {
    const created = await postTierList({ title: "My tier list", data: { tiers: defaultTiers() } });
    if (created) tierState.lists = [created];
  }
  if (!tierState.lists.some((l) => l.id === tierState.activeId)) {
    tierState.activeId = tierState.lists.length ? tierState.lists[0].id : null;
  }
  renderListSelect();
  loadActiveIntoBoard();
}

function loadActiveIntoBoard() {
  const list = tierState.lists.find((l) => l.id === tierState.activeId);
  const data = (list && list.data) || {};
  tierState.board.setTiers(data.tiers || []);
  tierState.board.setFlagged(data.flagged || []);
  syncTierGuideTarget();
}

function renderListSelect() {
  $("#tier-list-select").innerHTML = tierState.lists
    .map((l) => `<option value="${l.id}"${l.id === tierState.activeId ? " selected" : ""}>${escapeHtml(l.title)}</option>`)
    .join("");
  $("#tier-delete").disabled = tierState.lists.length <= 1; // keep at least one
}

function selectList(id) { tierState.activeId = id; loadActiveIntoBoard(); }

function tierStatus(msg) { $("#tier-status").textContent = msg; }

async function postTierList(body) {
  const res = await fetch("/api/tier-lists", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  return res.ok ? res.json() : null;
}

function saveActiveTierList() {
  const data = { tiers: tierState.board.getTiers(), flagged: tierState.board.getFlagged() };
  const list = tierState.lists.find((l) => l.id === tierState.activeId);
  if (list) list.data = data;
  clearTimeout(tierState.saveTimer);
  tierStatus("saving…");
  tierState.saveTimer = setTimeout(async () => {
    const res = await fetch(`/api/tier-lists/${tierState.activeId}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data }),
    }).catch(() => null);
    tierStatus(res && res.ok ? "saved ✓" : "save failed");
    setTimeout(() => tierStatus(""), 1500);
  }, 350);
}

async function createTierList() {
  const nameInput = $("#tier-new-name");
  const title = nameInput.value.trim() || "New tier list";
  const created = await postTierList({ title, data: { tiers: defaultTiers() } });
  if (!created) { tierStatus("create failed"); return; }
  nameInput.value = "";
  tierState.lists.push(created);
  tierState.activeId = created.id;
  renderListSelect();
  loadActiveIntoBoard();
}

async function renameTierList() {
  const list = tierState.lists.find((l) => l.id === tierState.activeId);
  if (!list) return;
  const title = (prompt("Rename tier list:", list.title) || "").trim();
  if (!title || title === list.title) return;
  const res = await fetch(`/api/tier-lists/${list.id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }),
  }).catch(() => null);
  if (res && res.ok) { list.title = title; renderListSelect(); }
}

async function deleteTierList() {
  if (tierState.lists.length <= 1) return;
  const list = tierState.lists.find((l) => l.id === tierState.activeId);
  if (!list || !confirm(`Delete tier list "${list.title}"?`)) return;
  const res = await fetch(`/api/tier-lists/${list.id}`, { method: "DELETE" }).catch(() => null);
  if (!(res && res.ok)) { tierStatus("delete failed"); return; }
  localStorage.removeItem(guideTargetKey(list.id));  // copies already in a guide stay
  tierState.lists = tierState.lists.filter((l) => l.id !== list.id);
  tierState.activeId = tierState.lists[0].id;
  renderListSelect();
  loadActiveIntoBoard();
}
