"use strict";
/* Block Learnings view: champion pool + auto-advancing 3-game blocks.
   Uses globals from app.js: state, $, getJSON, escapeHtml, champIcon,
   displayName, fmtDate, fmtDuration, renderNotes, unionFilterOptions. */

const blockState = {
  wired: false, blocks: [], blockSize: 3, editingLearnings: null, editingNotes: null,
  pool: { main_blind: [], core: [], counter: [] },
  collapsed: new Set(JSON.parse(localStorage.getItem("cp-collapsed-blocks") || "[]")),
  expandedGameStats: new Set(),
  gameMetricsCache: new Map(),
};

function persistCollapsed() {
  localStorage.setItem("cp-collapsed-blocks", JSON.stringify([...blockState.collapsed]));
}

const BLOCK_COLS = [
  { key: "date", label: "Date" },
  { key: "account", label: "Account" },
  { key: "me", label: "Me" },
  { key: "opponent", label: "Opponent" },
  { key: "result", label: "Result" },
  { key: "kda", label: "K/D/A" },
  { key: "cs", label: "CS/min" },
  { key: "lane7", label: "Lane (7m)", off: true },
  { key: "lane14", label: "Lane (14m)" },
  { key: "notes", label: "Notes" },
  { key: "rank", label: "Rank (start → end)" },
];
const GAME_COL_KEYS = ["date", "account", "me", "opponent", "result", "kda",
                       "cs", "lane7", "lane14", "notes"];
// v2 storage key: new columns get their intended defaults for existing installs
const blockCols = colPrefs("cp-cols-blocks-v2", BLOCK_COLS.map((c) => c.key),
  BLOCK_COLS.filter((c) => !c.off).map((c) => c.key));

const POOL_ROLES = {
  main_blind: { cls: "chip-main", glyph: "★", label: "Main blind" },
  core: { cls: "chip-core", glyph: "", label: "Core pool" },
  counter: { cls: "chip-counter", glyph: "", label: "Counter pick" },
};

async function initBlocks() {
  if (!blockState.wired) {
    blockState.wired = true;
    $("#pool-save").addEventListener("click", savePool);
    $("#copy-discord").addEventListener("click", () => {
      copyDiscordMarkdown(blockState.blocks);
      closeMenus();
    });
    document.addEventListener("click", (e) => {
      // download links inside export menus should also collapse the menu
      if (e.target.matches(".col-menu a")) closeMenus();
    });
    renderColPicker($("#blocks-cols"), "cp-cols-blocks", BLOCK_COLS, blockCols,
      () => renderBlocks());
    wireChipBoxes();
    await loadChampionRoster();
  }
  await Promise.all([loadPool(), loadBlocks()]);
}

// Discord renders bold/lists/inline-code but not tables, so this uses
// plain lines with ✅/❌ result marks.
function discordMarkdown(blocks) {
  const lines = [];
  for (const block of blocks) {
    const wins = block.games.filter((g) => g.win).length;
    const title = block.title ? ` — ${block.title}` : "";
    lines.push(`**Block #${block.id}${title}** (${wins}–${block.games.length - wins})`);
    if (block.pool) {
      lines.push(`Pool: ★ ${champDisplay(block.pool.main_blind) || "–"}` +
        ` · Core: ${block.pool.core.map(champDisplay).join(", ") || "–"}` +
        ` · Counters: ${block.pool.counter.map(champDisplay).join(", ") || "–"}`);
    }
    for (const g of block.games) {
      const opp = g.opp_champion ? ` vs ${champDisplay(g.opp_champion)}` : "";
      const notes = g.notes ? ` — ${g.notes.replace(/\n+/g, " / ")}` : "";
      lines.push(`${g.win ? "✅" : "❌"} ${fmtDate(g.game_creation_ms)} · ` +
        `${champDisplay(g.my_champion)}${opp} · ${g.kills}/${g.deaths}/${g.assists}${notes}`);
    }
    if (block.learnings) {
      lines.push("**Learnings**", block.learnings.trim());
    }
    lines.push("");
  }
  return lines.join("\n").trim();
}

async function copyDiscordMarkdown(blocks) {
  const status = $("#blocks-export-status");
  try {
    await navigator.clipboard.writeText(discordMarkdown(blocks));
    status.textContent = "copied ✓";
  } catch {
    status.textContent = "copy failed — clipboard unavailable";
  }
  setTimeout(() => { status.textContent = ""; }, 2500);
}

function closeMenus() {
  document.querySelectorAll("details.col-picker[open]").forEach((d) =>
    d.removeAttribute("open"));
}

// full champion roster from the static data file (see CLAUDE.md to re-fetch)
const roster = { byLookup: new Map(), nameById: new Map() };

async function loadChampionRoster() {
  const data = await getJSON("/champions.json");
  for (const c of data.champions) {
    roster.byLookup.set(c.id.toLowerCase(), c.id);
    roster.byLookup.set(c.name.toLowerCase(), c.id);
    roster.nameById.set(c.id, c.name);
  }
  $("#champ-list").innerHTML = data.champions
    .map((c) => `<option value="${escapeHtml(c.name)}">`).join("");
}

function champDisplay(id) {
  return roster.nameById.get(id) || displayName(id);
}

// ---------- champion pool (chip editor) ----------

function poolChip(role, champ, removable) {
  const def = POOL_ROLES[role];
  return `<span class="chip ${def.cls}" title="${def.label}">
    ${def.glyph ? def.glyph + " " : ""}${escapeHtml(champDisplay(champ))}${removable
      ? `<button class="chip-x" data-role="${role}" data-champ="${escapeHtml(champ)}"
           title="Remove" aria-label="Remove ${escapeHtml(champDisplay(champ))}">×</button>` : ""}
  </span>`;
}

function renderPoolEditor() {
  document.querySelectorAll("#pool-card .chip-box").forEach((box) => {
    const role = box.dataset.role;
    const input = box.querySelector(".chip-input");
    box.querySelectorAll(".chip").forEach((chip) => chip.remove());
    input.insertAdjacentHTML("beforebegin",
      blockState.pool[role].map((c) => poolChip(role, c, true)).join(""));
    box.querySelectorAll(".chip-x").forEach((btn) =>
      btn.addEventListener("click", () => {
        blockState.pool[role] = blockState.pool[role].filter((c) => c !== btn.dataset.champ);
        renderPoolEditor();
      }));
  });
}

function addPoolChip(role, value) {
  const typed = value.trim();
  if (!typed) return true;
  const champ = roster.byLookup.get(typed.toLowerCase());
  if (!champ) {
    $("#pool-status").textContent = `"${typed}" is not a champion`;
    setTimeout(() => { $("#pool-status").textContent = ""; }, 2500);
    return false; // keep the input so the user can correct it
  }
  if (role === "main_blind") {
    blockState.pool.main_blind = [champ];  // single pick — replace
  } else if (!blockState.pool[role].includes(champ)) {
    blockState.pool[role].push(champ);
  }
  renderPoolEditor();
  return true;
}

function wireChipBoxes() {
  document.querySelectorAll("#pool-card .chip-box").forEach((box) => {
    const role = box.dataset.role;
    const input = box.querySelector(".chip-input");
    box.addEventListener("click", () => input.focus());
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        if (addPoolChip(role, input.value.replace(",", ""))) input.value = "";
      } else if (e.key === "Backspace" && !input.value) {
        blockState.pool[role].pop();
        renderPoolEditor();
      }
    });
    // datalist picks fire 'change' without a keydown
    input.addEventListener("change", () => {
      if (addPoolChip(role, input.value)) input.value = "";
    });
  });
}

async function loadPool() {
  const pool = await getJSON("/api/pool");
  blockState.pool = {
    main_blind: pool.main_blind ? [pool.main_blind] : [],
    core: pool.core,
    counter: pool.counter,
  };
  renderPoolEditor();
}

async function savePool() {
  const response = await fetch("/api/pool", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      main_blind: blockState.pool.main_blind[0] || "",
      core: blockState.pool.core,
      counter: blockState.pool.counter,
    }),
  });
  $("#pool-status").textContent = response.ok ? "saved" : "save failed";
  setTimeout(() => { $("#pool-status").textContent = ""; }, 2000);
  loadBlocks(); // a just-completed block may have been stamped with this pool
}

// ---------- blocks ----------

async function loadBlocks() {
  const data = await getJSON("/api/blocks");
  blockState.blocks = data.blocks;
  blockState.blockSize = data.block_size;
  renderBlocks();
  await renderBlockPicker();
}

function gameMetricsPanel(entryId) {
  const data = blockState.gameMetricsCache.get(entryId);
  if (data === null) return `<div class="muted">No detailed metrics recorded for this game.</div>`;
  if (!data) return `<div class="muted">Loading…</div>`;
  const meta = data.meta;
  const groups = [...new Set(meta.map((m) => m.group))];
  return `<div class="metric-groups">` + groups.map((g) => {
    const rows = meta.filter((m) => m.group === g).map((m) => `
      <div class="metric-row">
        <span class="metric-label">${m.label}</span>
        <span class="metric-value">${fmtMetric(data.metrics[m.key], m)}</span>
      </div>`).join("");
    return `<div class="metric-group"><h4>${g}</h4>${rows}</div>`;
  }).join("") + `</div>`;
}

async function toggleGameStats(entryId, matchId, puuid) {
  if (blockState.expandedGameStats.has(entryId)) {
    blockState.expandedGameStats.delete(entryId);
  } else {
    blockState.expandedGameStats.add(entryId);
    if (!blockState.gameMetricsCache.has(entryId)) {
      const response = await fetch(
        `/api/stats/games/metrics?match_id=${encodeURIComponent(matchId)}&puuid=${encodeURIComponent(puuid)}`);
      blockState.gameMetricsCache.set(entryId, response.ok ? await response.json() : null);
    }
  }
  renderBlocks();
}

function laneCell(value) {
  if (value == null) return `<td class="muted">–</td>`;
  return value >= 1
    ? `<td><span class="lane-yes" title="Ahead in lane">✓</span></td>`
    : `<td><span class="lane-no" title="Behind in lane">✗</span></td>`;
}

function blockGameRow(g) {
  const statsOpen = blockState.expandedGameStats.has(g.entry_id);
  const cells = {
    date: `<td>${fmtDate(g.game_creation_ms)}</td>`,
    account: `<td>${escapeHtml(g.account)}</td>`,
    me: `<td><span class="champ-cell">${champIcon(g.my_champion)}${displayName(g.my_champion)}</span></td>`,
    opponent: `<td><span class="champ-cell">${g.opp_champion ? champIcon(g.opp_champion) + "vs " + displayName(g.opp_champion) : "–"}</span></td>`,
    result: `<td><span class="result-pill ${g.win ? "win" : "loss"}">${g.win ? "W" : "L"}</span></td>`,
    kda: `<td>${g.kills}/${g.deaths}/${g.assists}</td>`,
    cs: `<td>${(g.cs * 60 / g.game_duration_s).toFixed(1)}</td>`,
    lane7: laneCell(g.lane_adv_early),
    lane14: laneCell(g.lane_adv_late),
    notes: `<td class="notes-cell">${blockState.editingNotes === g.entry_id
      ? `<textarea class="game-notes" data-entry="${g.entry_id}" rows="1"
           placeholder="notes… (Markdown, Enter saves, Shift+Enter new line)">${escapeHtml(g.notes)}</textarea>`
      : `<div class="notes-display" data-entry="${g.entry_id}" title="Click to edit">${
          g.notes ? renderNotes(g.notes) : `<span class="muted">notes…</span>`}</div>`}</td>`,
  };
  const visible = GAME_COL_KEYS.filter((k) => blockCols.has(k));
  let html = `<tr>
    <td><button class="preset seg-toggle game-stats-toggle" data-entry="${g.entry_id}"
      data-match="${g.match_id}" data-puuid="${g.puuid}" aria-expanded="${statsOpen}"
      title="Per-game stats">${statsOpen ? "▾" : "▸"}</button></td>` +
    visible.map((k) => cells[k]).join("") +
    `<td><button class="preset game-remove" data-entry="${g.entry_id}" title="Remove from block">×</button></td>
  </tr>`;
  if (statsOpen) {
    html += `<tr class="games-row"><td colspan="${visible.length + 2}">${gameMetricsPanel(g.entry_id)}</td></tr>`;
  }
  return html;
}

function blockRankLine(block) {
  if (!blockCols.has("rank") || (!block.start_ranks && !block.end_ranks)) return "";
  const ends = new Map((block.end_ranks || []).map((r) => [r.account, r]));
  const parts = (block.start_ranks || []).map((r) => {
    const end = ends.get(r.account);
    return `${escapeHtml(r.account.split("#")[0])} ${fmtRank(r)}${end ? " → " + fmtRank(end) : ""}`;
  });
  return parts.length ? `<div class="block-pool">Rank: ${parts.join(" · ")}</div>` : "";
}

function blockPoolChips(pool) {
  if (!pool || (!pool.main_blind && !pool.core.length && !pool.counter.length)) return "";
  const chips = [
    ...(pool.main_blind ? [poolChip("main_blind", pool.main_blind, false)] : []),
    ...pool.core.map((c) => poolChip("core", c, false)),
    ...pool.counter.map((c) => poolChip("counter", c, false)),
  ].join("");
  return `<div class="block-pool"><span class="muted">Pool at completion:</span> ${chips}</div>`;
}

function blockCard(block, isCurrent) {
  const wins = block.games.filter((g) => g.win).length;
  const collapsed = blockState.collapsed.has(block.id);
  const editing = blockState.editingLearnings === block.id;
  let learnings;
  if (editing) {
    learnings = `<div class="session-body">
      <label class="filter-label" for="block-learnings-${block.id}">Learnings (Markdown)</label>
      <textarea id="block-learnings-${block.id}" rows="8">${escapeHtml(block.learnings)}</textarea>
      <div class="session-actions">
        <button class="preset learnings-save" data-id="${block.id}">Save</button>
        <button class="preset learnings-cancel">Cancel</button>
      </div></div>`;
  } else {
    learnings = `<div class="session-body">
      <div class="learnings-head">
        <h4>Learnings</h4>
        <button class="preset icon-btn learnings-edit" data-id="${block.id}"
          title="Edit learnings" aria-label="Edit learnings">✎</button>
      </div>
      <div class="md-body">${block.learnings
        ? renderNotes(block.learnings)
        : `<p class="muted">No learnings recorded yet.</p>`}</div>
    </div>`;
  }
  const head = `<div class="session-head">
      <button class="preset session-toggle block-collapse" data-id="${block.id}"
        aria-expanded="${!collapsed}" title="${collapsed ? "Expand" : "Collapse"} block">
        ${collapsed ? "▸" : "▾"}</button>
      <span class="session-date">Block #${block.id}</span>
      ${isCurrent ? `<span class="block-badge">current</span>` : ""}
      <span class="muted">${block.games.length}/${blockState.blockSize} games
        ${block.games.length ? `· ${wins}–${block.games.length - wins}` : ""}</span>
      <input type="text" class="block-title" data-id="${block.id}"
        value="${escapeHtml(block.title)}" placeholder="block title…">
      <span class="session-actions">
        <details class="col-picker">
          <summary class="preset icon-btn" title="Export this block"
            aria-label="Export block ${block.id}">📤</summary>
          <div class="col-menu">
            <a href="/api/blocks/export.md?block_id=${block.id}" download>Export .md</a>
            <a href="/api/blocks/export.csv?block_id=${block.id}" download>Export .csv</a>
            <button class="block-discord" data-id="${block.id}" type="button">Copy for Discord</button>
          </div>
        </details>
        <button class="preset icon-btn block-delete" data-id="${block.id}"
          title="Delete block" aria-label="Delete block">🗑</button>
      </span>
    </div>`;
  if (collapsed) {
    return `<div class="session-card block-card">${head}</div>`;
  }
  const headerCells = GAME_COL_KEYS.filter((k) => blockCols.has(k)).map((k) => {
    const label = BLOCK_COLS.find((c) => c.key === k).label;
    return `<th${k === "notes" ? ' class="notes-col"' : ""}>${label}</th>`;
  }).join("");
  return `<div class="session-card block-card">
    ${head}
    ${blockPoolChips(block.pool)}
    ${blockRankLine(block)}
    ${block.games.length ? `<div class="table-wrap block-games"><table>
      <thead><tr><th></th>${headerCells}<th></th></tr></thead>
      <tbody>${block.games.map(blockGameRow).join("")}</tbody></table></div>` : ""}
    ${learnings}
  </div>`;
}

function renderBlocks() {
  const target = $("#blocks-list");
  if (!blockState.blocks.length) {
    target.innerHTML = `<div class="muted">No blocks yet — add a game below to start Block #1.</div>`;
    return;
  }
  const currentId = Math.max(...blockState.blocks.map((b) => b.id));
  target.innerHTML = blockState.blocks
    .map((b) => blockCard(b, b.id === currentId)).join("");

  target.querySelectorAll(".block-collapse").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = +btn.dataset.id;
      blockState.collapsed.has(id) ? blockState.collapsed.delete(id) : blockState.collapsed.add(id);
      persistCollapsed();
      renderBlocks();
    }));
  target.querySelectorAll(".game-stats-toggle").forEach((btn) =>
    btn.addEventListener("click", () =>
      toggleGameStats(+btn.dataset.entry, btn.dataset.match, btn.dataset.puuid)));
  target.querySelectorAll(".block-title").forEach((input) =>
    input.addEventListener("change", () =>
      fetch(`/api/blocks/${input.dataset.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: input.value }),
      })));
  target.querySelectorAll(".learnings-edit").forEach((btn) =>
    btn.addEventListener("click", () => {
      blockState.editingLearnings = +btn.dataset.id;
      renderBlocks();
    }));
  target.querySelectorAll(".learnings-cancel").forEach((btn) =>
    btn.addEventListener("click", () => {
      blockState.editingLearnings = null;
      renderBlocks();
    }));
  target.querySelectorAll(".learnings-save").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const id = +btn.dataset.id;
      await fetch(`/api/blocks/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ learnings: $(`#block-learnings-${id}`).value }),
      });
      blockState.editingLearnings = null;
      loadBlocks();
    }));
  target.querySelectorAll(".notes-display").forEach((el) =>
    el.addEventListener("click", () => {
      blockState.editingNotes = +el.dataset.entry;
      renderBlocks();
      const input = target.querySelector(`.game-notes[data-entry="${el.dataset.entry}"]`);
      if (input) {
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      }
    }));
  const autoGrow = (el) => { el.style.height = "auto"; el.style.height = el.scrollHeight + "px"; };
  target.querySelectorAll(".game-notes").forEach((input) => {
    autoGrow(input);
    let cancelled = false;
    input.addEventListener("input", () => autoGrow(input));
    input.addEventListener("keydown", (e) => {
      // Enter saves (blur); Shift+Enter inserts a new line; Esc cancels
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        input.blur();
      } else if (e.key === "Escape") {
        cancelled = true;
        input.blur();
      }
    });
    input.addEventListener("blur", async () => {
      const entryId = +input.dataset.entry;
      if (!cancelled) {
        await fetch(`/api/blocks/games/${entryId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ notes: input.value }),
        });
        for (const block of blockState.blocks) {
          const game = block.games.find((g) => g.entry_id === entryId);
          if (game) game.notes = input.value;
        }
      }
      cancelled = false;
      blockState.editingNotes = null;
      renderBlocks();
    });
  });
  target.querySelectorAll(".game-remove").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Remove this game from the block?")) return;
      await fetch(`/api/blocks/games/${btn.dataset.entry}`, { method: "DELETE" });
      loadBlocks();
    }));
  target.querySelectorAll(".block-discord").forEach((btn) =>
    btn.addEventListener("click", () => {
      const block = blockState.blocks.find((b) => b.id === +btn.dataset.id);
      if (block) copyDiscordMarkdown([block]);
      closeMenus();
    }));
  target.querySelectorAll(".block-delete").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this block and its game entries? (The games themselves stay in the database.)")) return;
      await fetch(`/api/blocks/${btn.dataset.id}`, { method: "DELETE" });
      loadBlocks();
    }));
}

// ---------- picker ----------

async function renderBlockPicker() {
  const target = $("#block-picker");
  const games = await getJSON("/api/stats/games");
  const taken = new Set(blockState.blocks.flatMap(
    (b) => b.games.map((g) => `${g.match_id}:${g.puuid}`)));
  const candidates = games.filter((g) => !taken.has(`${g.match_id}:${g.my_puuid}`)).slice(0, 10);
  if (!candidates.length) {
    target.innerHTML = `<div class="muted">No unassigned games found.</div>`;
    return;
  }
  target.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Date</th><th>Account</th><th>Me</th><th>Opponent</th>
    <th>Result</th><th>K/D/A</th><th></th></tr></thead>
    <tbody>${candidates.map((g) => `<tr>
      <td>${fmtDate(g.game_creation_ms)}</td>
      <td>${escapeHtml(g.account)}</td>
      <td><span class="champ-cell">${champIcon(g.my_champion)}${displayName(g.my_champion)}</span></td>
      <td><span class="champ-cell">${g.opp_champion ? champIcon(g.opp_champion) + "vs " + displayName(g.opp_champion) : "–"}</span></td>
      <td><span class="result-pill ${g.win ? "win" : "loss"}">${g.win ? "W" : "L"}</span></td>
      <td>${g.kills}/${g.deaths}/${g.assists}</td>
      <td><button class="preset picker-add" data-match="${g.match_id}" data-puuid="${g.my_puuid}">Add</button></td>
    </tr>`).join("")}</tbody></table></div>`;
  target.querySelectorAll(".picker-add").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await promoteGame(btn.dataset.match, btn.dataset.puuid, btn);
      loadBlocks();
    }));
}

// shared with match-list promote buttons in app.js
async function promoteGame(matchId, puuid, btn) {
  const response = await fetch("/api/blocks/games", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ match_id: matchId, puuid }),
  });
  const body = await response.json().catch(() => ({}));
  if (response.ok) {
    btn.textContent = `✓ Block #${body.block_id}`;
    btn.disabled = true;
  } else {
    alert(body.detail || `error ${response.status}`);
  }
  return response.ok;
}
