"use strict";
/* Matchups view: matchup table with per-matchup notes and a tabbed expansion
   (overview: win/loss timeline + notes; games: per-game list).
   Uses globals from app.js: state, $, getJSON, QUEUE_NAMES, escapeHtml,
   displayName, champIcon, fmt, pct, wrCell, fmtDate, fmtDuration, titleCase,
   renderNotes, metricGroupsPanel, wirePromoteButtons. */

const muState = {
  wired: false,
  range: "all",
  champion: "",
  queue: "",
  rankTier: "",
  minGames: 1,
  view: "flat", // flat | rank
  rows: [],
  notes: {},            // opp_champion -> markdown
  editingNotes: null,   // matchup key currently in note-edit mode
  expanded: new Set(),
  tab: new Map(),       // matchup key -> "overview" | "games"
  games: new Map(),     // matchup key -> games list
  statsOpen: new Set(),
  statsCache: new Map(),
};

function muKey(row) {
  return muState.view === "rank" ? `${row.rank_tier}:${row.opp_champion}` : row.opp_champion;
}

function muQuery() {
  const params = new URLSearchParams({ puuid: state.puuid });
  if (muState.range !== "all") params.set("range", muState.range);
  if (muState.champion) params.set("champion", muState.champion);
  if (muState.queue) params.set("queue", muState.queue);
  if (muState.rankTier) params.set("rank_tier", muState.rankTier);
  if (muState.minGames > 1) params.set("min_games", muState.minGames);
  return params;
}

async function initMatchups() {
  if (!muState.wired) {
    muState.wired = true;
    document.querySelectorAll("#mu-range-presets .preset").forEach((btn) =>
      btn.addEventListener("click", () => {
        document.querySelectorAll("#mu-range-presets .preset").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        muState.range = btn.dataset.range;
        loadMatchups();
      }));
    $("#mu-champion").addEventListener("change", (e) => { muState.champion = e.target.value; loadMatchups(); });
    $("#mu-queue").addEventListener("change", (e) => { muState.queue = e.target.value; loadMatchups(); });
    $("#mu-rank").addEventListener("change", (e) => { muState.rankTier = e.target.value; loadMatchups(); });
    $("#mu-min-games").addEventListener("change", (e) => {
      muState.minGames = Math.max(1, +e.target.value || 1); loadMatchups();
    });
    $("#mu-view-flat").addEventListener("click", () => setMatchupView("flat"));
    $("#mu-view-rank").addEventListener("click", () => setMatchupView("rank"));
  }
  await loadMatchupFilterOptions();
  await loadMatchups();
}

function setMatchupView(view) {
  muState.view = view;
  $("#mu-view-flat").classList.toggle("active", view === "flat");
  $("#mu-view-rank").classList.toggle("active", view === "rank");
  loadMatchups();
}

async function loadMatchupFilterOptions() {
  const opts = await getJSON(`/api/filters?puuid=${encodeURIComponent(state.puuid)}`);
  $("#mu-champion").innerHTML = `<option value="">All</option>` +
    opts.champions.map((c) => `<option value="${c}" ${c === muState.champion ? "selected" : ""}>${displayName(c)}</option>`).join("");
  $("#mu-queue").innerHTML = `<option value="">All</option>` +
    opts.queues.map((q) => `<option value="${q}" ${String(q) === muState.queue ? "selected" : ""}>${QUEUE_NAMES[q] ?? q}</option>`).join("");
  $("#mu-rank").innerHTML = `<option value="">All</option>` +
    opts.rank_tiers.map((t) => `<option value="${t}" ${t === muState.rankTier ? "selected" : ""}>${titleCase(t)}</option>`).join("");
}

async function loadMatchups() {
  // filters, account or data changed — cached game lists are stale
  muState.games.clear();
  muState.statsOpen.clear();
  muState.statsCache.clear();
  muState.editingNotes = null;
  const url = muState.view === "rank"
    ? `/api/stats/matchups_by_rank?${muQuery()}` : `/api/stats/matchups?${muQuery()}`;
  const [rows, notes] = await Promise.all([getJSON(url), getJSON("/api/matchups/notes")]);
  muState.notes = notes;
  renderMU(rows);
  // re-hydrate games for anything the user had expanded
  const open = muState.rows.filter((r) => muState.expanded.has(muKey(r)));
  if (open.length) {
    await Promise.all(open.map((r) => ensureMatchupGames(r)));
    renderMU(muState.rows);
  }
}

async function ensureMatchupGames(row) {
  const key = muKey(row);
  if (muState.games.has(key)) return;
  const params = muQuery();
  params.delete("min_games");
  params.set("opp_champion", row.opp_champion);
  if (muState.view === "rank") params.set("rank_tier", row.rank_tier);
  muState.games.set(key, await getJSON(`/api/stats/games?${params}`));
}

// ---------- expansion panel ----------

const WL_BAR_W = 14, WL_H = 64;

function winLossStrip(games) {
  if (!games) return `<div class="muted">Loading…</div>`;
  if (!games.length) return `<div class="muted">No games.</div>`;
  const ordered = [...games].sort((a, b) => a.game_creation_ms - b.game_creation_ms);
  const width = Math.max(220, ordered.length * WL_BAR_W + 8);
  const mid = WL_H / 2;
  const bars = ordered.map((g, i) => {
    const win = Boolean(g.win);
    const x = 4 + i * WL_BAR_W;
    const tip = `${fmtDate(g.game_creation_ms)}: ${displayName(g.my_champion)} ` +
      `${win ? "won" : "lost"} vs ${displayName(g.opp_champion || "?")}`;
    return `<rect class="wl-bar ${win ? "wl-win" : "wl-loss"}" x="${x}" width="${WL_BAR_W - 4}"
        y="${win ? 6 : mid + 2}" height="${mid - 8}" rx="2"/>
      <rect class="wl-hit" x="${x - 2}" width="${WL_BAR_W}" y="0" height="${WL_H}"
        data-tip="${escapeHtml(tip)}"/>`;
  }).join("");
  const wins = ordered.filter((g) => g.win).length;
  return `<div class="wl-wrap">
    <div class="muted wl-caption">Chronological, oldest first —
      ${wins}–${ordered.length - wins} (${pct(wins / ordered.length)})</div>
    <div class="wl-scroll"><svg width="${width}" height="${WL_H}" role="img"
      aria-label="Win/loss timeline">
      <line class="wl-mid" x1="0" x2="${width}" y1="${mid}" y2="${mid}"/>${bars}
    </svg></div></div>`;
}

function matchupNotesBlock(row) {
  const key = muKey(row);
  const champ = row.opp_champion;
  const notes = muState.notes[champ] || "";
  if (muState.editingNotes === key) {
    return `<div class="mu-notes">
      <div class="mu-notes-head"><h4>Notes vs ${displayName(champ)}</h4></div>
      <textarea id="mu-notes-input" rows="8"
        placeholder="Markdown supported — game plan, power spikes, bans…">${escapeHtml(notes)}</textarea>
      <div class="session-actions">
        <button class="preset mu-notes-save" data-key="${escapeHtml(key)}">Save</button>
        <button class="preset mu-notes-cancel">Cancel</button>
        <span class="muted mu-notes-status"></span>
      </div>
    </div>`;
  }
  const body = notes
    ? `<div class="md-body">${renderNotes(notes)}</div>`
    : `<p class="muted">No notes for this matchup yet.</p>`;
  return `<div class="mu-notes">
    <div class="mu-notes-head"><h4>Notes vs ${displayName(champ)}</h4>
      <button class="preset icon-btn mu-notes-edit" data-key="${escapeHtml(key)}"
        title="Edit matchup notes" aria-label="Edit matchup notes">✎</button>
    </div>${body}</div>`;
}

function matchupGamesTable(key) {
  const games = muState.games.get(key);
  if (!games) return `<div class="muted">Loading…</div>`;
  if (!games.length) return `<div class="muted">No games.</div>`;
  const rows = games.map((g) => {
    const gkey = `${g.match_id}:${g.my_puuid}`;
    const open = muState.statsOpen.has(gkey);
    let html = `<tr>
      <td><button class="preset seg-toggle mg-stats-toggle" data-gkey="${gkey}"
        data-match="${g.match_id}" data-puuid="${g.my_puuid}" aria-expanded="${open}"
        title="Per-game stats">${open ? "▾" : "▸"}</button></td>
      <td>${fmtDate(g.game_creation_ms)}</td>
      <td>${QUEUE_NAMES[g.queue_id] ?? g.queue_id}</td>
      <td><span class="champ-cell">${champIcon(g.my_champion)}${displayName(g.my_champion)}</span></td>
      <td>${g.opp_champion ? titleCase(g.rank_tier) : "–"}</td>
      <td><span class="result-pill ${g.win ? "win" : "loss"}">${g.win ? "W" : "L"}</span></td>
      <td>${g.kills}/${g.deaths}/${g.assists}</td>
      <td>${(g.cs * 60 / g.game_duration_s).toFixed(1)}</td>
      <td>${fmtDuration(g.game_duration_s)}</td>
      <td><button class="preset promote-btn" data-match="${g.match_id}"
        data-puuid="${g.my_puuid}" title="Add to current block">+ Block</button></td>
    </tr>`;
    if (open) {
      html += `<tr class="games-row"><td colspan="10">${metricGroupsPanel(muState.statsCache.get(gkey))}</td></tr>`;
    }
    return html;
  }).join("");
  return `<table class="games-inner">
    <thead><tr><th></th><th>Date</th><th>Queue</th><th>Me</th><th>Opp. rank</th>
    <th>Result</th><th>K/D/A</th><th>CS/min</th><th>Length</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function matchupPanel(row) {
  const key = muKey(row);
  const tab = muState.tab.get(key) || "overview";
  const games = muState.games.get(key);
  const body = tab === "games"
    ? matchupGamesTable(key)
    : `${winLossStrip(games)}${matchupNotesBlock(row)}`;
  return `<div class="mu-panel">
    <div class="view-toggle mu-tabbar" role="tablist">
      <button class="mu-tab ${tab === "overview" ? "active" : ""}" data-key="${escapeHtml(key)}"
        data-tab="overview" role="tab">Overview</button>
      <button class="mu-tab ${tab === "games" ? "active" : ""}" data-key="${escapeHtml(key)}"
        data-tab="games" role="tab">Games${games ? ` (${games.length})` : ""}</button>
    </div>
    <div class="mu-panel-body">${body}</div>
  </div>`;
}

// ---------- table ----------

const MU_HEADER = `<thead><tr>
  <th></th><th>Opponent</th><th>Notes</th><th>Games</th><th>W–L</th><th class="wr-col">Winrate</th><th>KDA</th>
  <th>CS/min</th><th>Gold/min</th><th>DMG/min</th><th>Avg length</th>
</tr></thead>`;
const MU_COLS = 11;

function matchupRow(row) {
  const key = muKey(row);
  const expanded = muState.expanded.has(key);
  const hasNotes = Boolean(muState.notes[row.opp_champion]);
  let html = `<tr>
    <td><button class="preset seg-toggle matchup-toggle" data-key="${escapeHtml(key)}"
      aria-expanded="${expanded}" title="Matchup details">${expanded ? "▾" : "▸"}</button></td>
    <td><span class="champ-cell">${champIcon(row.opp_champion)}${displayName(row.opp_champion)}</span></td>
    <td>${hasNotes ? `<span class="note-flag" title="Has notes">📝</span>` : ""}</td>
    <td>${row.games}</td>
    <td>${row.wins}–${row.games - row.wins}</td>
    <td class="wr-col">${wrCell(row.winrate)}</td>
    <td>${fmt(row.kda, 2)}</td>
    <td>${fmt(row.cs_min)}</td>
    <td>${fmt(row.gold_min, 0)}</td>
    <td>${fmt(row.dmg_min, 0)}</td>
    <td>${fmtDuration(row.avg_duration_s)}</td>
  </tr>`;
  if (expanded) {
    html += `<tr class="games-row"><td colspan="${MU_COLS}">${matchupPanel(row)}</td></tr>`;
  }
  return html;
}

function renderMU(rows) {
  muState.rows = rows;
  const target = $("#mu-table");
  if (!rows.length) {
    target.innerHTML = `<div class="table-wrap"><div class="empty">No top-lane games match the current filters.</div></div>`;
    return;
  }
  let body;
  if (muState.view === "rank") {
    const groups = new Map();
    for (const row of rows) {
      if (!groups.has(row.rank_tier)) groups.set(row.rank_tier, []);
      groups.get(row.rank_tier).push(row);
    }
    body = [...groups.entries()].map(([tier, tierRows]) => {
      const games = tierRows.reduce((a, r) => a + r.games, 0);
      const wins = tierRows.reduce((a, r) => a + r.wins, 0);
      return `<tr class="rank-header"><td colspan="${MU_COLS}">${titleCase(tier)} — ${games} games, ${pct(wins / games)} WR</td></tr>`
        + tierRows.map(matchupRow).join("");
    }).join("");
  } else {
    body = rows.map(matchupRow).join("");
  }
  target.innerHTML = `<div class="table-wrap"><table>${MU_HEADER}<tbody>${body}</tbody></table></div>`;
  wireMUHandlers(target);
}

function wireMUHandlers(target) {
  const rowFor = (key) => muState.rows.find((r) => muKey(r) === key);
  target.querySelectorAll(".matchup-toggle").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const key = btn.dataset.key;
      if (muState.expanded.has(key)) {
        muState.expanded.delete(key);
        if (muState.editingNotes === key) muState.editingNotes = null;
      } else {
        muState.expanded.add(key);
        const row = rowFor(key);
        renderMU(muState.rows); // show "Loading…" immediately
        if (row) await ensureMatchupGames(row);
      }
      renderMU(muState.rows);
    }));
  target.querySelectorAll(".mu-tab").forEach((btn) =>
    btn.addEventListener("click", () => {
      muState.tab.set(btn.dataset.key, btn.dataset.tab);
      renderMU(muState.rows);
    }));
  target.querySelectorAll(".mu-notes-edit").forEach((btn) =>
    btn.addEventListener("click", () => {
      muState.editingNotes = btn.dataset.key;
      renderMU(muState.rows);
    }));
  target.querySelectorAll(".mu-notes-cancel").forEach((btn) =>
    btn.addEventListener("click", () => {
      muState.editingNotes = null;
      renderMU(muState.rows);
    }));
  target.querySelectorAll(".mu-notes-save").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const row = rowFor(btn.dataset.key);
      if (!row) return;
      const notes = $("#mu-notes-input").value;
      const response = await fetch(
        `/api/matchups/notes/${encodeURIComponent(row.opp_champion)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ notes }),
        });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        btn.parentElement.querySelector(".mu-notes-status").textContent =
          body.detail || `error ${response.status}`;
        return;
      }
      if (notes.trim()) muState.notes[row.opp_champion] = notes;
      else delete muState.notes[row.opp_champion];
      muState.editingNotes = null;
      renderMU(muState.rows);
    }));
  target.querySelectorAll(".mg-stats-toggle").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const gkey = btn.dataset.gkey;
      if (muState.statsOpen.has(gkey)) {
        muState.statsOpen.delete(gkey);
      } else {
        muState.statsOpen.add(gkey);
        if (!muState.statsCache.has(gkey)) {
          const response = await fetch(
            `/api/stats/games/metrics?match_id=${encodeURIComponent(btn.dataset.match)}&puuid=${encodeURIComponent(btn.dataset.puuid)}`);
          muState.statsCache.set(gkey, response.ok ? await response.json() : null);
        }
      }
      renderMU(muState.rows);
    }));
  const tip = $("#chart-tip");
  target.querySelectorAll(".wl-hit").forEach((el) => {
    el.addEventListener("mouseenter", () => {
      tip.textContent = el.dataset.tip;
      tip.classList.remove("hidden");
      const r = el.getBoundingClientRect();
      tip.style.left = `${r.left + window.scrollX + 12}px`;
      tip.style.top = `${r.top + window.scrollY - 30}px`;
    });
    el.addEventListener("mouseleave", () => tip.classList.add("hidden"));
  });
  wirePromoteButtons(target);
}
