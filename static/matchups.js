"use strict";
/* Matchups view: matchup table with a tabbed expansion (overview: win/loss
   timeline + block notes; games: per-game list). When a specific "My
   champion" filter is active, each row also links out to that champion's
   entry on the standalone Matchup guide page (guide.js) for runes/patch/notes.
   Uses globals from app.js: state, $, getJSON, QUEUE_NAMES, escapeHtml,
   displayName, champIcon, fmt, pct, wrCell, fmtDate, fmtDuration, titleCase,
   renderNotes, metricGroupsPanel, wirePromoteButtons. Uses openGuide from
   guide.js and openCooldowns from cooldowns.js. */

const muState = {
  wired: false,
  range: "all",
  champion: "",
  queue: "",
  rankTier: "",
  side: "", // "" all | blue | red
  minGames: 1,
  view: "flat", // flat | rank
  rows: [],
  sort: { key: "games", dir: -1 }, // default: most-played first
  guideFlags: new Set(), // opp_champion set with a matchup guide for muState.champion
  expanded: new Set(),
  tab: new Map(),       // matchup key -> "overview" | "games"
  games: new Map(),     // matchup key -> games list
  blockNotes: new Map(),    // opp_champion -> block-game notes (all my picks)
  blockNotesAll: new Set(), // matchup keys with "all my picks" checked
  statsOpen: new Set(),
  statsCache: new Map(),
};

function muKey(row) {
  return muState.view === "rank" ? `${row.rank_tier}:${row.opp_champion}` : row.opp_champion;
}

function muQuery() {
  const params = accountParams();
  if (muState.range !== "all") params.set("range", muState.range);
  if (muState.champion) params.set("champion", muState.champion);
  if (muState.queue) params.set("queue", muState.queue);
  if (muState.side) params.set("side", muState.side);
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
    $("#mu-side").addEventListener("change", (e) => { muState.side = e.target.value; loadMatchups(); });
    $("#mu-rank").addEventListener("change", (e) => { muState.rankTier = e.target.value; loadMatchups(); });
    $("#mu-min-games").addEventListener("change", (e) => {
      muState.minGames = Math.max(1, +e.target.value || 1); loadMatchups();
    });
    $("#mu-view-flat").addEventListener("click", () => setMatchupView("flat"));
    $("#mu-view-rank").addEventListener("click", () => setMatchupView("rank"));
    // picker controls the MAIN table's columns (base stats always available,
    // metric averages incl. lane deltas off by default)
    renderColPicker($("#mu-metric-cols"), "cp-mucols",
      muAllCols().map((c) => ({ key: c.key, label: c.label })),
      colPrefs("cp-mucols", muAllCols().map((c) => c.key), MU_BASE_COLS.map((c) => c.key)),
      () => renderMU(muState.rows), MU_BASE_COLS.map((c) => c.key));
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
  const opts = await getJSON(`/api/filters?${accountParams()}`);
  if (muState.champion && !opts.champions.includes(muState.champion)) muState.champion = "";
  if (muState.queue && !opts.queues.map(String).includes(muState.queue)) muState.queue = "";
  if (muState.rankTier && !opts.rank_tiers.includes(muState.rankTier)) muState.rankTier = "";
  $("#mu-champion").innerHTML = `<option value="">All</option>` +
    opts.champions.map((c) => `<option value="${c}" ${c === muState.champion ? "selected" : ""}>${displayName(c)}</option>`).join("");
  $("#mu-queue").innerHTML = `<option value="">All</option>` +
    opts.queues.map((q) => `<option value="${q}" ${String(q) === muState.queue ? "selected" : ""}>${QUEUE_NAMES[q] ?? q}</option>`).join("");
  $("#mu-rank").innerHTML = `<option value="">All</option>` +
    opts.rank_tiers.map((t) => `<option value="${t}" ${t === muState.rankTier ? "selected" : ""}>${titleCase(t)}</option>`).join("");
}

async function loadMatchups() {
  const seq = (muState.seq = (muState.seq || 0) + 1);
  // filters, account or data changed — cached game lists are stale
  muState.games.clear();
  muState.blockNotes.clear();
  muState.statsOpen.clear();
  muState.statsCache.clear();
  const url = muState.view === "rank"
    ? `/api/stats/matchups_by_rank?${muQuery()}` : `/api/stats/matchups?${muQuery()}`;
  const [rows, blockNoted, guide] = await Promise.all([
    getJSON(url),
    getJSON("/api/blocks/noted-champions"),
    muState.champion
      ? getJSON(`/api/matchups/notes?my_champion=${encodeURIComponent(muState.champion)}`)
      : Promise.resolve({}),
  ]);
  if (seq !== muState.seq) return; // superseded by a newer load
  muState.guideFlags = new Set(Object.keys(guide));
  muState.blockNoted = new Set(blockNoted);
  renderMU(rows);
  // re-hydrate games for anything the user had expanded
  const open = muState.rows.filter((r) => muState.expanded.has(muKey(r)));
  if (open.length) {
    await Promise.all(open.map((r) => ensureMatchupDetails(r)));
    renderMU(muState.rows);
  }
}

async function ensureMatchupDetails(row) {
  await Promise.all([ensureMatchupGames(row), ensureBlockNotes(row)]);
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

async function ensureBlockNotes(row) {
  if (muState.blockNotes.has(row.opp_champion)) return;
  muState.blockNotes.set(row.opp_champion, await getJSON(
    `/api/blocks/game-notes?opp_champion=${encodeURIComponent(row.opp_champion)}`));
}

// ---------- expansion panel ----------

const WL_BAR_W = 14, WL_H = 64, WL_MAX = 20;

function winLossStrip(games, key) {
  if (!games) return `<div class="muted">Loading…</div>`;
  if (!games.length) return `<div class="muted">No games.</div>`;
  const ordered = [...games].sort((a, b) => a.game_creation_ms - b.game_creation_ms);
  const shown = ordered.slice(-WL_MAX);
  const width = shown.length * WL_BAR_W + 8;
  const mid = WL_H / 2;
  const bars = shown.map((g, i) => {
    const win = Boolean(g.win);
    const x = 4 + i * WL_BAR_W;
    const tip = `${fmtDate(g.game_creation_ms)}: ${displayName(g.my_champion)} ` +
      `${win ? "won" : "lost"} vs ${displayName(g.opp_champion || "?")} — click for details`;
    return `<rect class="wl-bar ${win ? "wl-win" : "wl-loss"}" x="${x}" width="${WL_BAR_W - 4}"
        y="${win ? 6 : mid + 2}" height="${mid - 8}" rx="2"/>
      <rect class="wl-hit" x="${x - 2}" width="${WL_BAR_W}" y="0" height="${WL_H}"
        data-key="${escapeHtml(key)}" data-match="${g.match_id}" data-puuid="${g.my_puuid}"
        data-tip="${escapeHtml(tip)}"/>`;
  }).join("");
  const capped = ordered.length > shown.length
    ? ` (last ${shown.length} of ${ordered.length})` : "";
  return `<div class="wl-wrap">
    <div class="muted wl-caption">Win/loss, oldest first${capped} — click a game for details</div>
    <svg width="${width}" height="${WL_H}" role="img" aria-label="Win/loss timeline">
      <line class="wl-mid" x1="0" x2="${width}" y1="${mid}" y2="${mid}"/>${bars}
    </svg></div>`;
}

// rolling window of the last N metric-bearing games, averaged to a percentage
const LANE_ROLL = 10;
const LANE_SERIES = [
  { key: "lane_adv_early", label: "Ahead @ 7 min", color: "var(--series-1)" },
  { key: "lane_adv_late", label: "Ahead @ 14 min", color: "#e08a3c" },
];
const LANE_W = 340, LANE_H = 130;
const LANE_PAD = { l: 32, r: 8, t: 8, b: 16 };

function rollingLanePoints(games, metricKey) {
  const recent = [], points = [];
  games.forEach((g, i) => {
    const value = g[metricKey];
    if (value == null) return;
    recent.push(value);
    if (recent.length > LANE_ROLL) recent.shift();
    points.push({ i, t: g.game_creation_ms,
                  avg: 100 * recent.reduce((a, b) => a + b, 0) / recent.length });
  });
  return points;
}

function laneTrendGraph(games) {
  if (!games || !games.length) return "";
  const ordered = [...games].sort((a, b) => a.game_creation_ms - b.game_creation_ms);
  const series = LANE_SERIES
    .map((s) => ({ ...s, points: rollingLanePoints(ordered, s.key) }))
    .filter((s) => s.points.length > 1);
  if (!series.length) {
    return `<div class="muted">No lane metrics recorded for these games yet.</div>`;
  }
  const iw = LANE_W - LANE_PAD.l - LANE_PAD.r, ih = LANE_H - LANE_PAD.t - LANE_PAD.b;
  const lastIndex = Math.max(1, ordered.length - 1);
  const x = (i) => LANE_PAD.l + (i / lastIndex) * iw;
  const y = (v) => LANE_PAD.t + ih - (v / 100) * ih;
  const grid = [0, 50, 100].map((v) => `
    <line class="rk-grid${v === 50 ? "" : " rk-grid-minor"}" x1="${LANE_PAD.l}"
      x2="${LANE_W - LANE_PAD.r}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/>
    <text class="tl-ylab" x="${LANE_PAD.l - 4}" y="${(y(v) + 3).toFixed(1)}"
      text-anchor="end">${v}%</text>`).join("");
  const lines = series.map((s) => {
    const path = s.points.map((p) => `${x(p.i).toFixed(1)},${y(p.avg).toFixed(1)}`).join(" ");
    const hits = s.points.map((p) => `<circle class="lane-hit" cx="${x(p.i).toFixed(1)}"
        cy="${y(p.avg).toFixed(1)}" r="6"
        data-tip="${escapeHtml(`${fmtDate(p.t)} · ${s.label}: ${p.avg.toFixed(0)}% (last ${LANE_ROLL})`)}"/>`).join("");
    return `<polyline class="rk-line" style="stroke:${s.color}" points="${path}"/>${hits}`;
  }).join("");
  const legend = series.map((s) =>
    `<span><span class="swatch" style="background:${s.color}"></span>${s.label}</span>`).join("");
  return `<div class="lane-wrap">
    <div class="muted wl-caption">Lane won — rolling ${LANE_ROLL}-game average</div>
    <svg viewBox="0 0 ${LANE_W} ${LANE_H}" role="img"
      aria-label="Rolling lane advantage">${grid}${lines}</svg>
    <div class="rank-legend lane-legend">${legend}</div>
  </div>`;
}

function blockNotesBlock(row) {
  const key = muKey(row);
  const all = muState.blockNotes.get(row.opp_champion);
  if (!all) return `<div class="mu-block-notes"><h4>Block notes</h4>
    <div class="muted">Loading…</div></div>`;
  const showAll = muState.blockNotesAll.has(key) || !muState.champion;
  const notes = showAll ? all : all.filter((n) => n.my_champion === muState.champion);
  const checkbox = muState.champion
    ? `<label class="muted bn-all-label"><input type="checkbox" class="bn-all"
        data-key="${escapeHtml(key)}" ${muState.blockNotesAll.has(key) ? "checked" : ""}>
        all my picks vs ${displayName(row.opp_champion)}</label>`
    : "";
  const items = notes.map((n) => {
    const title = n.block_title ? ` — ${escapeHtml(n.block_title)}` : "";
    return `<div class="bn-item">
      <div class="muted bn-meta">
        <a href="#blocks" class="bn-block-link" data-block="${n.block_id}"
          title="Open this block">Block #${n.block_id}${title}</a>
        · ${fmtDate(n.game_creation_ms)} · ${escapeHtml(n.account)} ·
        ${displayName(n.my_champion)} vs ${displayName(n.opp_champion)} ·
        <span class="result-pill ${n.win ? "win" : "loss"}">${n.win ? "W" : "L"}</span>
      </div>
      <div class="bn-text md-body">${renderNotes(n.notes)}</div>
      ${n.block_learnings && n.block_learnings.trim() ? `<details class="bn-learnings">
        <summary>Block learnings</summary>
        <div class="md-body">${renderNotes(n.block_learnings)}</div>
      </details>` : ""}
    </div>`;
  }).join("");
  const empty = `<div class="muted">No block notes ${showAll ? "" : `with ${displayName(muState.champion)} `}vs ${displayName(row.opp_champion)} yet.</div>`;
  return `<div class="mu-block-notes">
    <div class="mu-notes-head"><h4>Block notes</h4>${checkbox}</div>
    ${notes.length ? items : empty}
  </div>`;
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
      html += `<tr class="games-row"><td colspan="10">${
        metricGroupsPanel(muState.statsCache.get(gkey))}</td></tr>`;  // expanded shows every stat
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
    : `<div class="mu-overview-grid">
        <div>${blockNotesBlock(row)}</div>
        <div>${winLossStrip(games, key)}${laneTrendGraph(games)}</div>
      </div>`;
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

// Aggregate stat columns for the main table (the leading toggle, Opponent and
// Notes columns are always shown and handled separately). Each `cell(row)`
// renders its <td>. The metric-average columns (incl. lane deltas) are
// appended from the registry — all off by default; base columns default on.
const MU_BASE_COLS = [
  { key: "games", label: "Games", type: "num", get: (r) => r.games, cell: (r) => `<td>${r.games}</td>` },
  { key: "wins", label: "W–L", type: "num", get: (r) => r.wins, cell: (r) => `<td>${r.wins}–${r.games - r.wins}</td>` },
  { key: "winrate", label: "Winrate", type: "num", cls: "wr-col", get: (r) => r.winrate,
    cell: (r) => `<td class="wr-col">${wrCell(r.winrate)}</td>` },
  { key: "kda", label: "KDA", type: "num", get: (r) => r.kda, cell: (r) => `<td>${fmt(r.kda, 2)}</td>` },
  { key: "cs_min", label: "CS/min", type: "num", get: (r) => r.cs_min, cell: (r) => `<td>${fmt(r.cs_min)}</td>` },
  { key: "gold_min", label: "Gold/min", type: "num", get: (r) => r.gold_min, cell: (r) => `<td>${fmt(r.gold_min, 0)}</td>` },
  { key: "dmg_min", label: "DMG/min", type: "num", get: (r) => r.dmg_min, cell: (r) => `<td>${fmt(r.dmg_min, 0)}</td>` },
  { key: "avg_duration_s", label: "Avg length", type: "num", get: (r) => r.avg_duration_s,
    cell: (r) => `<td>${fmtDuration(r.avg_duration_s)}</td>` },
];

// metric-average columns, keyed "m:<metric>" so they never clash with base keys
function muMetricCols() {
  return (state.metricsMeta || []).map((m) => ({
    key: `m:${m.key}`, label: m.label, type: "num", metricKey: m.key, meta: m,
    get: (r) => (r.metrics ? r.metrics[m.key] : null),
    cell: (r) => `<td>${fmtMetric(r.metrics ? r.metrics[m.key] : null, m)}</td>`,
  }));
}

function muAllCols() { return [...MU_BASE_COLS, ...muMetricCols()]; }
function muVisibleCols() {
  const vis = colPrefs("cp-mucols", muAllCols().map((c) => c.key),
                       MU_BASE_COLS.map((c) => c.key));  // base on, metrics off by default
  return muAllCols().filter((c) => vis.has(c.key));
}

// opponent + notes are always present; combine with visible picker cols for
// both the header (sortable) and sorting lookups
function muHeaderCols() {
  return [
    { key: "opponent", label: "Opponent", type: "text", get: (r) => displayName(r.opp_champion) },
    { key: "notes", label: "Notes", sortable: false },
    ...muVisibleCols(),
  ];
}

function matchupRow(row, cols) {
  const key = muKey(row);
  const expanded = muState.expanded.has(key);
  const hasBlockNotes = muState.blockNoted && muState.blockNoted.has(row.opp_champion);
  // only meaningful once a specific "My champion" is selected — guides are
  // scoped per (my champion, opponent), so "All" has no single guide to show
  const guideLink = muState.champion
    ? `<button class="preset icon-btn mu-guide-link" data-my="${escapeHtml(muState.champion)}"
        data-opp="${escapeHtml(row.opp_champion)}"
        title="${muState.guideFlags.has(row.opp_champion) ? "Open matchup guide" : "Write a matchup guide"}"
        aria-label="Open matchup guide">${muState.guideFlags.has(row.opp_champion) ? "📖" : "📖+"}</button>`
    : "";
  const cdLink = `<button class="preset icon-btn mu-cd-link" data-opp="${escapeHtml(row.opp_champion)}"
      title="Compare ability cooldowns" aria-label="Compare ability cooldowns">⏱</button>`;
  const statCells = muVisibleCols().map((c) => c.cell(row)).join("");
  let html = `<tr>
    <td><button class="preset seg-toggle matchup-toggle" data-key="${escapeHtml(key)}"
      aria-expanded="${expanded}" title="Matchup details">${expanded ? "▾" : "▸"}</button></td>
    <td><span class="champ-cell">${champIcon(row.opp_champion)}${displayName(row.opp_champion)}</span></td>
    <td>${guideLink}${cdLink}${
      hasBlockNotes ? `<span class="note-flag" title="Has block notes">🧱</span>` : ""}</td>
    ${statCells}
  </tr>`;
  if (expanded) {
    html += `<tr class="games-row"><td colspan="${cols}">${matchupPanel(row)}</td></tr>`;
  }
  return html;
}

function renderMU(rows) {
  muState.rows = rows;
  const target = $("#mu-table");
  if (!rows.length) {
    target.innerHTML = `<div class="table-wrap"><div class="empty">No games match the current filters.</div></div>`;
    return;
  }
  const headerCols = muHeaderCols();
  const head = sortableThead(headerCols, muState.sort, "<th></th>");
  const colspan = headerCols.length + 1; // + leading toggle
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
      return `<tr class="rank-header"><td colspan="${colspan}">${titleCase(tier)} — ${games} games, ${pct(wins / games)} WR</td></tr>`
        + sortRows(tierRows, muState.sort, headerCols).map((r) => matchupRow(r, colspan)).join("");
    }).join("");
  } else {
    body = sortRows(rows, muState.sort, headerCols).map((r) => matchupRow(r, colspan)).join("");
  }
  target.innerHTML = `<div class="table-wrap"><table>${head}<tbody>${body}</tbody></table></div>`;
  wireSortable(target, muState.sort, headerCols, () => renderMU(muState.rows));
  wireMUHandlers(target);
}

function wireMUHandlers(target) {
  const rowFor = (key) => muState.rows.find((r) => muKey(r) === key);
  target.querySelectorAll(".matchup-toggle").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const key = btn.dataset.key;
      if (muState.expanded.has(key)) {
        muState.expanded.delete(key);
      } else {
        muState.expanded.add(key);
        const row = rowFor(key);
        renderMU(muState.rows); // show "Loading…" immediately
        if (row) await ensureMatchupDetails(row);
      }
      renderMU(muState.rows);
    }));
  target.querySelectorAll(".bn-all").forEach((cb) =>
    cb.addEventListener("change", () => {
      cb.checked ? muState.blockNotesAll.add(cb.dataset.key)
                 : muState.blockNotesAll.delete(cb.dataset.key);
      renderMU(muState.rows);
    }));
  target.querySelectorAll(".bn-block-link").forEach((a) =>
    a.addEventListener("click", (e) => {
      e.preventDefault();
      focusBlock(+a.dataset.block);
    }));
  target.querySelectorAll(".mu-tab").forEach((btn) =>
    btn.addEventListener("click", () => {
      muState.tab.set(btn.dataset.key, btn.dataset.tab);
      renderMU(muState.rows);
    }));
  target.querySelectorAll(".mu-guide-link").forEach((btn) =>
    btn.addEventListener("click", () => openGuide(btn.dataset.my, btn.dataset.opp)));
  target.querySelectorAll(".mu-cd-link").forEach((btn) =>
    btn.addEventListener("click", async () =>
      // your side prefers the active "My champion" filter, then the pool's
      // first pick; the popup lets you pick either side freely anyway
      openCooldowns(muState.champion || (await poolChampionOrder())[0] || "",
                    btn.dataset.opp)));
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
  target.querySelectorAll(".wl-hit").forEach((el) =>
    el.addEventListener("click", async () => {
      // jump to this game's details on the Games tab
      const gkey = `${el.dataset.match}:${el.dataset.puuid}`;
      muState.tab.set(el.dataset.key, "games");
      muState.statsOpen.add(gkey);
      if (!muState.statsCache.has(gkey)) {
        const response = await fetch(
          `/api/stats/games/metrics?match_id=${encodeURIComponent(el.dataset.match)}&puuid=${encodeURIComponent(el.dataset.puuid)}`);
        muState.statsCache.set(gkey, response.ok ? await response.json() : null);
      }
      $("#chart-tip").classList.add("hidden");
      renderMU(muState.rows);
      const row = $(`#mu-table .mg-stats-toggle[data-gkey="${gkey}"]`);
      if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
    }));
  const tip = $("#chart-tip");
  target.querySelectorAll(".wl-hit, .lane-hit").forEach((el) => {
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
