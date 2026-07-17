"use strict";
/* Cooldown comparison popup (opened from the Matchups and Champ guide
   views): your champion's ability cooldowns on the left, the opponent's on
   the right. Each side has a level slider and an 18×4 skill-order grid
   (level × Q/W/E/R — click a cell to put that level's point into that
   ability; validated against the in-game rules) that decide every spell's
   current rank, plus a list of haste sources (ability haste / ultimate
   ability haste — items, runes, buffs, whatever); reduced cooldowns render
   next to the base values. Spell data comes from DDragon's
   champion/<id>.json at open time (cached per session). Your side's build
   can be saved to the champ guide for the open matchup
   (matchup_notes.skill_order); the grid otherwise persists per champion in
   localStorage. Uses globals from app.js: state, $, getJSON, escapeHtml,
   champIcon, displayName, championOptions, ICON_NAME_FIXES. */

const SPELL_KEYS = ["Q", "W", "E", "R"];
const R_LEVELS = [6, 11, 16];
const GRID_LEVELS = 18;

const cdState = {
  sides: { me: null, opp: null }, // {champ, level, grid, haste[], detail, error}
  options: { me: "", opp: "" },   // champion <select> options per side
  // "levels": level slider + per-spell table. "matrix": the skill grid
  // doubles as the visualization — each rank-up cell shows the cooldown
  // (haste-reduced) the ability has from that level on.
  view: localStorage.getItem("cp-cd-view") === "matrix" ? "matrix" : "levels",
};

const champDetailCache = new Map();

async function loadChampionDetail(champ) {
  const id = ICON_NAME_FIXES[champ] || champ;
  if (champDetailCache.has(id)) return champDetailCache.get(id);
  const data = await getJSON(
    `https://ddragon.leagueoflegends.com/cdn/${state.ddragonVersion}/data/en_US/champion/${id}.json`);
  const detail = data.data[id] || Object.values(data.data)[0];
  champDetailCache.set(id, detail);
  return detail;
}

// ---------- skill-order grid (18 levels × Q/W/E/R) ----------

// null when valid, else a short human-readable reason. Mirrors the server's
// _validate_skill_order: basics max 5 points with point k needing level
// 2k-1; R max 3 points at levels 6/11/16.
function validateSkillGrid(cells) {
  const points = { Q: [], W: [], E: [], R: [] };
  cells.forEach((cell, i) => { if (cell) points[cell].push(i + 1); });
  for (const key of SPELL_KEYS) {
    const levels = points[key];
    const maxPoints = key === "R" ? 3 : 5;
    if (levels.length > maxPoints) return `${key} can have at most ${maxPoints} points`;
    for (let i = 0; i < levels.length; i++) {
      const needed = key === "R" ? R_LEVELS[i] : 2 * (i + 1) - 1;
      if (levels[i] < needed) return `${key} point ${i + 1} needs level ${needed}`;
    }
  }
  return null;
}

// standard default: one point in each basic over levels 1-3 (priority
// order), R at 6/11/16, the rest greedily maxing by priority
function defaultSkillGrid(priority = ["Q", "W", "E"]) {
  const cells = Array(GRID_LEVELS).fill("");
  const count = { Q: 0, W: 0, E: 0, R: 0 };
  const maxAt = (lvl) => Math.min(5, Math.floor((lvl + 1) / 2));
  for (let lvl = 1; lvl <= GRID_LEVELS; lvl++) {
    if (R_LEVELS.includes(lvl)) { cells[lvl - 1] = "R"; count.R++; continue; }
    if (lvl <= 3) { const k = priority[lvl - 1]; cells[lvl - 1] = k; count[k]++; continue; }
    for (const k of priority) {
      if (count[k] < maxAt(lvl)) { cells[lvl - 1] = k; count[k]++; break; }
    }
  }
  return cells;
}

function champGrid(champ) {
  try {
    const raw = JSON.parse(localStorage.getItem(`cp-skill-grid-${champ}`) || "null");
    if (Array.isArray(raw) && raw.length === GRID_LEVELS
        && raw.every((c) => c === "" || SPELL_KEYS.includes(c))
        && !validateSkillGrid(raw)) return raw;
  } catch { /* corrupted — fall through */ }
  try {
    // pre-v1.32 stored a Q/W/E priority list instead of a grid
    const order = JSON.parse(localStorage.getItem(`cp-skill-order-${champ}`) || "null");
    if (Array.isArray(order) && order.length === 3) return defaultSkillGrid(order);
  } catch { /* ditto */ }
  return defaultSkillGrid();
}

// a saved-to-guide build takes priority over the per-champion default
async function savedMatchupBuild(me, opp) {
  if (!me || !opp) return null;
  try {
    const guide = await getJSON(`/api/matchups/notes?my_champion=${encodeURIComponent(me)}`);
    const cells = guide[opp] && guide[opp].skill_order;
    return Array.isArray(cells) && cells.some(Boolean)
      ? [...cells, ...Array(GRID_LEVELS).fill("")].slice(0, GRID_LEVELS) : null;
  } catch {
    return null;
  }
}

function newCdSide(champ) {
  return { champ: champ || "", level: 9, grid: champ ? champGrid(champ) : defaultSkillGrid(),
           haste: [], detail: null, error: null };
}

async function hydrateCdSide(side) {
  side.detail = null;
  side.error = null;
  if (!side.champ) return;
  if (!state.ddragonVersion) {
    side.error = "Champion data unavailable (offline).";
    return;
  }
  try {
    side.detail = await loadChampionDetail(side.champ);
  } catch {
    side.error = "Couldn't fetch champion data.";
  }
}

function ranksAtLevel(level, grid) {
  const ranks = { Q: 0, W: 0, E: 0, R: 0 };
  for (let i = 0; i < level; i++) if (grid[i]) ranks[grid[i]]++;
  return ranks;
}

function hasteTotals(side) {
  let ah = 0, ultAh = 0;
  for (const h of side.haste) {
    ah += +h.ah || 0;
    ultAh += +h.ultAh || 0;
  }
  return { ah, ultAh };
}

function fmtCd(x) { return String(Math.round(x * 10) / 10); }
function reducedCd(cd, haste) { return cd / (1 + haste / 100); }

function cdHasteSummary(side) {
  const { ah, ultAh } = hasteTotals(side);
  return `${ah} AH${ultAh ? ` · +${ultAh} ult AH` : ""}`;
}

// ---------- rendering ----------

function skillGridHtml(sideKey) {
  const side = cdState.sides[sideKey];
  const matrix = cdState.view === "matrix" && side.detail;
  const { ah, ultAh } = hasteTotals(side);
  // cooldown the ability has once the point at `lvl` is spent (its rank
  // there), haste-reduced; null when the cell holds no point
  const cellCd = (key, lvl) => {
    const cds = side.detail.spells[SPELL_KEYS.indexOf(key)].cooldown || [];
    let rank = 0;
    for (let i = 0; i < lvl; i++) if (side.grid[i] === key) rank++;
    if (!rank || !cds.length) return null;
    const base = cds[Math.min(rank, cds.length) - 1];
    const haste = key === "R" ? ah + ultAh : ah;
    return { base, reduced: reducedCd(base, haste), rank };
  };
  const head = `<div class="sg-row sg-head"><span class="sg-label"></span>${
    Array.from({ length: GRID_LEVELS }, (_, i) =>
      `<span class="sg-lvl ${!matrix && i + 1 === side.level ? "sg-now" : ""}">${i + 1}</span>`).join("")}</div>`;
  const rows = SPELL_KEYS.map((key, ki) => {
    const spellName = side.detail ? side.detail.spells[ki].name : "";
    const cells = Array.from({ length: GRID_LEVELS }, (_, i) => {
      const active = side.grid[i] === key;
      let label = "";
      let title = `${key} at level ${i + 1}`;
      if (matrix && active) {
        const cd = cellCd(key, i + 1);
        if (cd) {
          label = fmtCd(cd.reduced);
          title = `${key} rank ${cd.rank} from level ${i + 1}: ${fmtCd(cd.base)}s`
            + (cd.reduced !== cd.base ? ` → ${fmtCd(cd.reduced)}s` : "");
        }
      }
      return `<button type="button" class="sg-cell ${active ? "active" : ""}"
        data-side="${sideKey}" data-key="${key}" data-lvl="${i + 1}"
        title="${escapeHtml(title)}" aria-label="${escapeHtml(title)}"
        aria-pressed="${active}">${label}</button>`;
    }).join("");
    return `<div class="sg-row">
      <span class="sg-label" title="${escapeHtml(spellName)}">${key}</span>${cells}
    </div>`;
  }).join("");
  return `<div class="skill-grid ${matrix ? "skill-grid-values" : ""}">${head}${rows}
    <span class="cd-grid-status" data-side="${sideKey}"></span></div>`;
}

// compact read-only variant, shared with the champ guide's build display
function skillGridMini(cells) {
  const grid = [...cells, ...Array(GRID_LEVELS).fill("")].slice(0, GRID_LEVELS);
  return `<div class="skill-grid skill-grid-mini">${SPELL_KEYS.map((key) => `<div class="sg-row">
    <span class="sg-label">${key}</span>
    ${grid.map((c, i) => `<span class="sg-cell ${c === key ? "active" : ""}"
      title="${c === key ? `${key} at level ${i + 1}` : ""}"></span>`).join("")}
  </div>`).join("")}</div>`;
}

function cdSpellsTable(side) {
  if (!side.champ) return `<p class="muted">Pick a champion.</p>`;
  if (side.error) return `<p class="muted">${escapeHtml(side.error)}</p>`;
  if (!side.detail) return `<p class="muted">Loading…</p>`;
  const { ah, ultAh } = hasteTotals(side);
  const ranks = ranksAtLevel(side.level, side.grid);
  const rows = side.detail.spells.map((spell, i) => {
    const key = SPELL_KEYS[i];
    const cds = spell.cooldown || [];
    const rank = Math.min(ranks[key] ?? 0, cds.length);
    const haste = key === "R" ? ah + ultAh : ah;
    const perRank = cds.map((c, ri) =>
      `<span class="${ri === rank - 1 ? "cd-current" : ""}">${fmtCd(c)}</span>`).join(" / ");
    const base = rank > 0 ? cds[rank - 1] : null;
    const icon = `<img src="https://ddragon.leagueoflegends.com/cdn/${state.ddragonVersion}/img/spell/${spell.image.full}"
      width="28" height="28" alt="" loading="lazy" onerror="this.style.display='none'">`;
    const value = base == null
      ? `<span class="muted" title="No point at this level">–</span>`
      : haste > 0
        ? `<span class="muted cd-base">${fmtCd(base)}s</span> <strong>${fmtCd(reducedCd(base, haste))}s</strong>`
        : `<strong>${fmtCd(base)}s</strong>`;
    return `<div class="cd-spell">
      ${icon}
      <div class="cd-spell-main">
        <div class="cd-spell-name"><strong>${key}</strong> ${escapeHtml(spell.name)}
          <span class="muted">· rank ${rank}/${cds.length}</span></div>
        <div class="cd-spell-ranks muted">${perRank}s</div>
      </div>
      <div class="cd-spell-value">${value}</div>
    </div>`;
  }).join("");
  return `<div class="cd-spells">${rows}</div>`;
}

function cdSidePanel(sideKey, title) {
  const side = cdState.sides[sideKey];
  const hasteRows = side.haste.map((h, i) => `
    <div class="cd-haste-row">
      <input type="text" class="cd-haste-label" data-side="${sideKey}" data-i="${i}"
        placeholder="item / rune / buff…" value="${escapeHtml(h.label)}" aria-label="Haste source">
      <input type="number" class="cd-haste-ah" data-side="${sideKey}" data-i="${i}"
        min="0" max="500" step="5" value="${h.ah}" title="Ability haste" aria-label="Ability haste">
      <input type="number" class="cd-haste-ult" data-side="${sideKey}" data-i="${i}"
        min="0" max="500" step="5" value="${h.ultAh}" title="Ultimate ability haste" aria-label="Ultimate ability haste">
      <button type="button" class="preset icon-btn cd-haste-remove" data-side="${sideKey}" data-i="${i}"
        title="Remove" aria-label="Remove haste source">✕</button>
    </div>`).join("");
  const saveBtn = sideKey === "me" && side.champ && cdState.sides.opp.champ
    ? `<div class="cd-control-row">
        <button type="button" class="preset cd-save-build">Save build to champ guide
          (vs ${escapeHtml(displayName(cdState.sides.opp.champ))})</button>
        <span class="muted cd-save-status"></span>
      </div>` : "";
  return `<div class="cd-side">
    <div class="filter-label">${title}</div>
    <div class="cd-side-head">
      ${champIcon(side.champ)}
      <select class="cd-champ-select" data-side="${sideKey}" aria-label="${title}">
        ${cdState.options[sideKey]}</select>
    </div>
    ${cdState.view === "levels" ? `<div class="cd-control-row">
      <label>Level <strong class="cd-level-value" data-side="${sideKey}">${side.level}</strong></label>
      <input type="range" class="cd-level" data-side="${sideKey}" min="1" max="18" value="${side.level}">
    </div>` : ""}
    <div class="cd-control-row">
      <span>Skill order</span>
      <span class="muted">${cdState.view === "matrix"
        ? "bubbles = cooldown from that level on; click cells to edit the build"
        : "click a cell to spend that level's point"}</span>
    </div>
    <div class="cd-skillgrid" data-side="${sideKey}">${skillGridHtml(sideKey)}</div>
    ${saveBtn}
    <div class="cd-haste">
      <div class="cd-control-row">
        <span>Haste sources</span>
        <span class="muted cd-haste-totals" data-side="${sideKey}">${cdHasteSummary(side)}</span>
      </div>
      ${side.haste.length ? `<div class="cd-haste-head muted">
        <span>Source</span><span>AH</span><span>Ult AH</span><span></span></div>` : ""}
      ${hasteRows}
      <button type="button" class="preset cd-haste-add" data-side="${sideKey}">+ Add haste source</button>
    </div>
    ${cdState.view === "levels"
      ? `<div class="cd-table" data-side="${sideKey}">${cdSpellsTable(side)}</div>`
      : `<div class="cd-table" data-side="${sideKey}">${
          side.error ? `<p class="muted">${escapeHtml(side.error)}</p>`
          : !side.champ ? `<p class="muted">Pick a champion.</p>` : ""}</div>`}
  </div>`;
}

function updateCdSide(sideKey) {
  const side = cdState.sides[sideKey];
  const table = $(`.cd-table[data-side="${sideKey}"]`);
  if (table && cdState.view === "levels") table.innerHTML = cdSpellsTable(side);
  const grid = $(`.cd-skillgrid[data-side="${sideKey}"]`);
  if (grid && cdState.view === "matrix") grid.innerHTML = skillGridHtml(sideKey);
  const totals = $(`.cd-haste-totals[data-side="${sideKey}"]`);
  if (totals) totals.textContent = cdHasteSummary(side);
}

function renderCooldowns() {
  const box = $("#modal-box");
  const viewBtn = (view, label) => `<button type="button" data-view="${view}"
    class="${cdState.view === view ? "active" : ""}">${label}</button>`;
  box.innerHTML = `
    <div class="modal-head">
      <h3>Cooldown comparison</h3>
      <div class="view-toggle cd-view-toggle" role="tablist">
        ${viewBtn("levels", "At level")}${viewBtn("matrix", "Level matrix")}
      </div>
      <button type="button" class="preset icon-btn" id="modal-close" title="Close" aria-label="Close">✕</button>
    </div>
    <div class="cd-grid ${cdState.view === "matrix" ? "cd-grid-stacked" : ""}">${
      cdSidePanel("me", "You")}${cdSidePanel("opp", "Opponent")}</div>`;
  wireCooldowns(box);
}

function flashGridStatus(sideKey, message) {
  const status = $(`.cd-grid-status[data-side="${sideKey}"]`);
  if (!status) return;
  status.textContent = message;
  setTimeout(() => { if (status.textContent === message) status.textContent = ""; }, 2500);
}

function wireCooldowns(box) {
  box.querySelector("#modal-close").addEventListener("click", closeModal);
  box.querySelectorAll(".cd-champ-select").forEach((select) => {
    select.value = cdState.sides[select.dataset.side].champ; // innerHTML selection can be stale
    select.addEventListener("change", async () => {
      const side = cdState.sides[select.dataset.side];
      side.champ = select.value;
      side.grid = side.champ ? champGrid(side.champ) : defaultSkillGrid();
      if (select.dataset.side === "me") {
        const saved = await savedMatchupBuild(side.champ, cdState.sides.opp.champ);
        if (saved) side.grid = saved;
      }
      renderCooldowns();
      await hydrateCdSide(side);
      renderCooldowns();
    });
  });
  box.querySelectorAll(".cd-level").forEach((input) =>
    input.addEventListener("input", () => {
      const side = cdState.sides[input.dataset.side];
      side.level = +input.value;
      $(`.cd-level-value[data-side="${input.dataset.side}"]`).textContent = side.level;
      // move the current-level highlight without a full re-render
      input.closest(".cd-side").querySelectorAll(".sg-head .sg-lvl").forEach((el, idx) =>
        el.classList.toggle("sg-now", idx + 1 === side.level));
      updateCdSide(input.dataset.side);
    }));
  box.querySelectorAll(".cd-view-toggle button").forEach((btn) =>
    btn.addEventListener("click", () => {
      cdState.view = btn.dataset.view;
      localStorage.setItem("cp-cd-view", cdState.view);
      renderCooldowns();
    }));
  const saveBtn = box.querySelector(".cd-save-build");
  if (saveBtn) saveBtn.addEventListener("click", async () => {
    const me = cdState.sides.me.champ;
    const opp = cdState.sides.opp.champ;
    const status = box.querySelector(".cd-save-status");
    const response = await fetch(
      `/api/matchups/notes/${encodeURIComponent(me)}/${encodeURIComponent(opp)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill_order: cdState.sides.me.grid }),
      });
    if (response.ok) {
      status.textContent = "saved ✓";
    } else {
      const body = await response.json().catch(() => ({}));
      status.classList.add("status-error");
      status.textContent = `save failed — ${body.detail || `error ${response.status}`}`;
    }
  });
  box.querySelectorAll(".cd-haste-add").forEach((btn) =>
    btn.addEventListener("click", () => {
      cdState.sides[btn.dataset.side].haste.push({ label: "", ah: 10, ultAh: 0 });
      renderCooldowns();
    }));
  box.querySelectorAll(".cd-haste-remove").forEach((btn) =>
    btn.addEventListener("click", () => {
      cdState.sides[btn.dataset.side].haste.splice(+btn.dataset.i, 1);
      renderCooldowns();
    }));
  box.querySelectorAll(".cd-haste-label").forEach((input) =>
    input.addEventListener("input", () => {
      cdState.sides[input.dataset.side].haste[+input.dataset.i].label = input.value;
    }));
  box.querySelectorAll(".cd-haste-ah").forEach((input) =>
    input.addEventListener("input", () => {
      cdState.sides[input.dataset.side].haste[+input.dataset.i].ah = +input.value || 0;
      updateCdSide(input.dataset.side);
    }));
  box.querySelectorAll(".cd-haste-ult").forEach((input) =>
    input.addEventListener("input", () => {
      cdState.sides[input.dataset.side].haste[+input.dataset.i].ultAh = +input.value || 0;
      updateCdSide(input.dataset.side);
    }));
}

async function openCooldowns(me, opp) {
  cdState.sides.me = newCdSide(me);
  cdState.sides.opp = newCdSide(opp);
  $("#modal-overlay").classList.remove("hidden");
  $("#modal-box").innerHTML = `<p class="muted">Loading…</p>`;
  const [meOptions, oppOptions, saved] = await Promise.all([
    championOptions(cdState.sides.me.champ, "– pick a champion –"),
    championOptions(cdState.sides.opp.champ, "– pick a champion –"),
    savedMatchupBuild(me, opp),
  ]);
  cdState.options.me = meOptions;
  cdState.options.opp = oppOptions;
  if (saved) cdState.sides.me.grid = saved;
  renderCooldowns();
  await Promise.all([hydrateCdSide(cdState.sides.me), hydrateCdSide(cdState.sides.opp)]);
  renderCooldowns();
}

function closeModal() {
  $("#modal-overlay").classList.add("hidden");
}

// skill-grid cell clicks are delegated (the grid re-renders on haste edits
// in matrix mode, so per-cell listeners would go stale)
$("#modal-box").addEventListener("click", (e) => {
  const cell = e.target.closest("button.sg-cell");
  if (!cell) return;
  const sideKey = cell.dataset.side;
  const side = cdState.sides[sideKey];
  const i = +cell.dataset.lvl - 1;
  const key = cell.dataset.key;
  const next = [...side.grid];
  next[i] = next[i] === key ? "" : key; // second click clears the point
  const problem = validateSkillGrid(next);
  if (problem) {
    flashGridStatus(sideKey, problem);
    return;
  }
  side.grid = next;
  if (side.champ) {
    localStorage.setItem(`cp-skill-grid-${side.champ}`, JSON.stringify(next));
  }
  renderCooldowns();
});

// overlay click (outside the box) and Esc close the modal
$("#modal-overlay").addEventListener("click", (e) => {
  if (e.target === $("#modal-overlay")) closeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#modal-overlay").classList.contains("hidden")) closeModal();
});
