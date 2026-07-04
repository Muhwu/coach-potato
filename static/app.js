"use strict";

const state = {
  players: [],
  puuid: null,
  range: "all",
  from: null,
  to: null,
  champion: "",
  queue: "",
  rankTier: "",
  minGames: 1,
  view: "flat", // flat | rank
  mainView: "overview", // overview | progress
  progressChampion: null, // null = not initialized yet (defaults to Gwen)
  progressQueue: "",
  ddragonVersion: null,
};

const QUEUE_NAMES = { 400: "Normal Draft", 420: "Ranked Solo", 430: "Normal Blind",
                      440: "Ranked Flex", 490: "Quickplay", 700: "Clash" };
const DISPLAY_NAME_FIXES = { MonkeyKing: "Wukong", FiddleSticks: "Fiddlesticks" };

const $ = (sel) => document.querySelector(sel);

function displayName(champ) { return DISPLAY_NAME_FIXES[champ] || champ; }

function champIcon(champ) {
  if (!state.ddragonVersion || !champ) return "";
  const url = `https://ddragon.leagueoflegends.com/cdn/${state.ddragonVersion}/img/champion/${champ}.png`;
  return `<img src="${url}" alt="" loading="lazy" onerror="this.style.display='none'">`;
}

function fmt(value, digits = 1) {
  return value == null ? "–" : Number(value).toFixed(digits);
}

function pct(value) {
  return value == null ? "–" : (100 * value).toFixed(0) + "%";
}

function fmtDuration(seconds) {
  return `${Math.floor(seconds / 60)}:${String(Math.round(seconds) % 60).padStart(2, "0")}`;
}

function fmtDate(ms) {
  return new Date(ms).toLocaleDateString(undefined, { year: "2-digit", month: "short", day: "numeric" });
}

function titleCase(tier) {
  return tier === "UNKNOWN" ? "Unknown rank" : tier.charAt(0) + tier.slice(1).toLowerCase();
}

function queryString() {
  const params = new URLSearchParams({ puuid: state.puuid });
  if (state.range === "custom") {
    if (state.from) params.set("from", state.from);
    if (state.to) params.set("to", state.to);
  } else if (state.range !== "all") {
    params.set("range", state.range);
  }
  if (state.champion) params.set("champion", state.champion);
  if (state.queue) params.set("queue", state.queue);
  if (state.rankTier) params.set("rank_tier", state.rankTier);
  if (state.minGames > 1) params.set("min_games", state.minGames);
  return params.toString();
}

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} -> ${response.status}`);
  return response.json();
}

// ---------- rendering ----------

function wrCell(winrate) {
  const width = winrate == null ? 0 : Math.round(100 * winrate);
  return `<span class="wr-cell">
      <span class="wr-bar"><span class="fill" style="width:${width}%"></span><span class="tick"></span></span>
      <span class="wr-num">${pct(winrate)}</span>
    </span>`;
}

const MATCHUP_HEADER = `<thead><tr>
  <th>Opponent</th><th>Games</th><th>W–L</th><th class="wr-col">Winrate</th><th>KDA</th>
  <th>CS/min</th><th>Gold/min</th><th>DMG/min</th><th>Avg length</th>
</tr></thead>`;

function matchupRow(row) {
  return `<tr>
    <td><span class="champ-cell">${champIcon(row.opp_champion)}${displayName(row.opp_champion)}</span></td>
    <td>${row.games}</td>
    <td>${row.wins}–${row.games - row.wins}</td>
    <td class="wr-col">${wrCell(row.winrate)}</td>
    <td>${fmt(row.kda, 2)}</td>
    <td>${fmt(row.cs_min)}</td>
    <td>${fmt(row.gold_min, 0)}</td>
    <td>${fmt(row.dmg_min, 0)}</td>
    <td>${fmtDuration(row.avg_duration_s)}</td>
  </tr>`;
}

function renderMatchups(rows) {
  const target = $("#matchup-table");
  if (!rows.length) {
    target.innerHTML = `<div class="table-wrap"><div class="empty">No top-lane games match the current filters.</div></div>`;
    return;
  }
  let body;
  if (state.view === "rank") {
    const groups = new Map();
    for (const row of rows) {
      if (!groups.has(row.rank_tier)) groups.set(row.rank_tier, []);
      groups.get(row.rank_tier).push(row);
    }
    body = [...groups.entries()].map(([tier, tierRows]) => {
      const games = tierRows.reduce((a, r) => a + r.games, 0);
      const wins = tierRows.reduce((a, r) => a + r.wins, 0);
      return `<tr class="rank-header"><td colspan="9">${titleCase(tier)} — ${games} games, ${pct(wins / games)} WR</td></tr>`
        + tierRows.map(matchupRow).join("");
    }).join("");
  } else {
    body = rows.map(matchupRow).join("");
  }
  target.innerHTML = `<div class="table-wrap"><table>${MATCHUP_HEADER}<tbody>${body}</tbody></table></div>`;
}

function renderSummary(s) {
  const player = state.players.find((p) => p.puuid === state.puuid);
  const rank = player && player.solo_tier
    ? `${titleCase(player.solo_tier)} ${player.solo_division ?? ""} ${player.solo_lp ?? 0} LP`
    : "Unranked / unknown";
  $("#summary-tiles").innerHTML = `
    <div class="tile"><div class="label">Top-lane games</div><div class="value">${s.games}</div>
      <div class="sub">${s.wins ?? 0}W ${s.games - (s.wins ?? 0)}L</div></div>
    <div class="tile"><div class="label">Winrate</div><div class="value">${pct(s.winrate)}</div>
      <div class="sub">50% reference on bars</div></div>
    <div class="tile"><div class="label">KDA</div><div class="value">${fmt(s.kda, 2)}</div>
      <div class="sub">${fmt(s.kills)}/${fmt(s.deaths)}/${fmt(s.assists)}</div></div>
    <div class="tile"><div class="label">CS/min</div><div class="value">${fmt(s.cs_min)}</div>
      <div class="sub">gold/min ${fmt(s.gold_min, 0)}</div></div>
    <div class="tile"><div class="label">Current rank</div><div class="value" style="font-size:18px">${rank}</div>
      <div class="sub">solo queue</div></div>`;
}

function renderChampionTable(byChampion) {
  const target = $("#champion-table");
  if (!byChampion.length) {
    target.innerHTML = `<div class="table-wrap"><div class="empty">No games.</div></div>`;
    return;
  }
  const body = byChampion.map((row) => `<tr>
      <td><span class="champ-cell">${champIcon(row.champion)}${displayName(row.champion)}</span></td>
      <td>${row.games}</td>
      <td>${row.wins}–${row.games - row.wins}</td>
      <td class="wr-col">${wrCell(row.winrate)}</td>
      <td>${fmt(row.kda, 2)}</td>
      <td>${fmt(row.cs_min)}</td>
    </tr>`).join("");
  target.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Champion</th><th>Games</th><th>W–L</th><th class="wr-col">Winrate</th><th>KDA</th><th>CS/min</th></tr></thead>
    <tbody>${body}</tbody></table></div>`;
}

function renderRecent(recent) {
  const target = $("#recent-list");
  if (!recent.length) {
    target.innerHTML = `<div class="table-wrap"><div class="empty">No games.</div></div>`;
    return;
  }
  const body = recent.map((g) => `<tr>
      <td>${fmtDate(g.game_creation_ms)}</td>
      <td>${QUEUE_NAMES[g.queue_id] ?? g.queue_id}</td>
      <td><span class="champ-cell">${champIcon(g.my_champion)}${displayName(g.my_champion)}</span></td>
      <td><span class="champ-cell">${g.opp_champion ? champIcon(g.opp_champion) + "vs " + displayName(g.opp_champion) : "–"}</span></td>
      <td>${g.opp_champion ? titleCase(g.rank_tier) : "–"}</td>
      <td><span class="result-pill ${g.win ? "win" : "loss"}">${g.win ? "W" : "L"}</span></td>
      <td>${g.kills}/${g.deaths}/${g.assists}</td>
      <td>${fmtDuration(g.game_duration_s)}</td>
      <td><button class="preset promote-btn" data-match="${g.match_id}"
        data-puuid="${state.puuid}" title="Add to current block">+ Block</button></td>
    </tr>`).join("");
  target.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Date</th><th>Queue</th><th>Me</th><th>Opponent</th><th>Opp. rank</th>
    <th>Result</th><th>K/D/A</th><th>Length</th><th></th></tr></thead>
    <tbody>${body}</tbody></table></div>`;
  wirePromoteButtons(target);
}

function wirePromoteButtons(container) {
  container.querySelectorAll(".promote-btn").forEach((btn) =>
    btn.addEventListener("click", () =>
      promoteGame(btn.dataset.match, btn.dataset.puuid, btn)));
}

function renderTabs() {
  $("#account-tabs").innerHTML = state.players.map((p) => {
    const rank = p.solo_tier ? `${titleCase(p.solo_tier)} ${p.solo_division ?? ""}` : "";
    return `<button data-puuid="${p.puuid}" class="${p.puuid === state.puuid ? "active" : ""}">
        ${p.game_name}#${p.tag_line}${rank ? `<span class="rank-badge">${rank}</span>` : ""}
      </button>`;
  }).join("");
  document.querySelectorAll("#account-tabs button").forEach((btn) =>
    btn.addEventListener("click", () => {
      state.puuid = btn.dataset.puuid;
      state.champion = "";
      renderTabs();
      loadFilterOptions().then(refresh);
    }));
}

// ---------- data loading ----------

async function loadFilterOptions() {
  const opts = await getJSON(`/api/filters?puuid=${encodeURIComponent(state.puuid)}`);
  $("#champion-select").innerHTML = `<option value="">All</option>` +
    opts.champions.map((c) => `<option value="${c}" ${c === state.champion ? "selected" : ""}>${displayName(c)}</option>`).join("");
  $("#queue-select").innerHTML = `<option value="">All</option>` +
    opts.queues.map((q) => `<option value="${q}" ${String(q) === state.queue ? "selected" : ""}>${QUEUE_NAMES[q] ?? q}</option>`).join("");
  $("#rank-select").innerHTML = `<option value="">All</option>` +
    opts.rank_tiers.map((t) => `<option value="${t}" ${t === state.rankTier ? "selected" : ""}>${titleCase(t)}</option>`).join("");
}

async function refresh() {
  const qs = queryString();
  const matchupsUrl = state.view === "rank"
    ? `/api/stats/matchups_by_rank?${qs}` : `/api/stats/matchups?${qs}`;
  const [matchups, summary] = await Promise.all([
    getJSON(matchupsUrl),
    getJSON(`/api/stats/summary?${qs}`),
  ]);
  renderMatchups(matchups);
  renderSummary(summary);
  renderChampionTable(summary.by_champion ?? []);
  renderRecent(summary.recent ?? []);
}

// ---------- coaching progress ----------

function fmtSegmentDates(segment) {
  return `${fmtDate(segment.from_ms)} – ${fmtDate(segment.to_ms)}`;
}

function delta(current, previous, key, digits, suffix = "") {
  if (!current.games || !previous || !previous.games) return "";
  const diff = (current[key] ?? 0) - (previous[key] ?? 0);
  if (!isFinite(diff)) return "";
  const cls = diff >= 0 ? "delta-up" : "delta-down";
  const arrow = diff >= 0 ? "▲" : "▼";
  return `<span class="${cls}">${arrow} ${Math.abs(diff).toFixed(digits)}${suffix}</span>`;
}

const segmentUi = { expanded: new Set(), expandedGames: new Set(), cache: new Map(), segments: [] };

function segKey(segment) {
  return `${segment.from_ms}:${segment.to_ms}`;
}

function progressFilterParams(segment) {
  const params = new URLSearchParams({ from_ms: segment.from_ms, to_ms: segment.to_ms - 1 });
  if (state.progressChampion) params.set("champion", state.progressChampion);
  if (state.progressQueue) params.set("queue", state.progressQueue);
  return params;
}

function prevNonEmpty(segment) {
  const i = segmentUi.segments.indexOf(segment);
  return segmentUi.segments.slice(0, i).reverse().find((s) => s.games > 0) || null;
}

async function ensureSegmentMetrics(segment) {
  const targets = [segment];
  const prev = prevNonEmpty(segment);
  if (prev) targets.push(prev);
  await Promise.all(targets.map(async (s) => {
    const cacheKey = "metrics:" + segKey(s);
    if (segmentUi.cache.has(cacheKey)) return;
    const data = await getJSON(`/api/stats/metrics?${progressFilterParams(s)}`);
    if (!state.metricsMeta) state.metricsMeta = data.meta;
    segmentUi.cache.set(cacheKey, data);
  }));
}

function fmtMetric(value, m) {
  return value == null ? "–" : value.toFixed(m.decimals) + (m.suffix || "");
}

function metricDelta(current, previous, m) {
  if (current == null || previous == null) return "";
  const diff = current - previous;
  if (Number(Math.abs(diff).toFixed(m.decimals)) === 0) return ""; // no visible change
  const arrow = diff >= 0 ? "▲" : "▼";
  const cls = m.direction === 0 ? "delta-neutral"
    : (diff * m.direction >= 0 ? "delta-up" : "delta-down");
  return `<span class="${cls}">${arrow} ${Math.abs(diff).toFixed(m.decimals)}${m.suffix || ""}</span>`;
}

function segmentMetricsPanel(segment) {
  const key = segKey(segment);
  const data = segmentUi.cache.get("metrics:" + key);
  if (!data) return `<div class="muted">Loading…</div>`;
  const prev = prevNonEmpty(segment);
  const prevData = prev ? segmentUi.cache.get("metrics:" + segKey(prev)) : null;
  const meta = state.metricsMeta || [];
  const groups = [...new Set(meta.map((m) => m.group))];
  const coverage = data.metrics_games < data.games
    ? `<div class="muted" style="margin-bottom:8px">Detailed metrics available for
       ${data.metrics_games} of ${data.games} games in this period.</div>` : "";
  const groupHtml = groups.map((g) => {
    const rows = meta.filter((m) => m.group === g).map((m) => `
      <div class="metric-row">
        <span class="metric-label">${m.label}</span>
        <span class="metric-value">${fmtMetric(data.metrics[m.key], m)}
          <span class="delta-slot">${prevData ? metricDelta(data.metrics[m.key], prevData.metrics[m.key], m) : ""}</span>
        </span>
      </div>`).join("");
    return `<div class="metric-group"><h4>${g}</h4>${rows}</div>`;
  }).join("");
  const gamesOpen = segmentUi.expandedGames.has(key);
  return `${coverage}<div class="metric-groups">${groupHtml}</div>
    <button class="preset games-toggle" data-key="${key}" aria-expanded="${gamesOpen}">
      ${gamesOpen ? "▾" : "▸"} Games (${segment.games})</button>
    ${gamesOpen ? `<div class="nested-games">${segmentGamesTable(segmentUi.cache.get("games:" + key))}</div>` : ""}`;
}

async function toggleSegmentGames(segment) {
  const key = segKey(segment);
  if (segmentUi.expandedGames.has(key)) {
    segmentUi.expandedGames.delete(key);
  } else {
    segmentUi.expandedGames.add(key);
    const cacheKey = "games:" + key;
    if (!segmentUi.cache.has(cacheKey)) {
      segmentUi.cache.set(cacheKey, await getJSON(`/api/stats/games?${progressFilterParams(segment)}`));
    }
  }
  renderProgress(segmentUi.segments);
}

function segmentGamesTable(games) {
  if (!games) return `<div class="muted">Loading…</div>`;
  if (!games.length) return `<div class="muted">No games in this period.</div>`;
  const rows = games.map((g) => `<tr>
      <td>${fmtDate(g.game_creation_ms)}</td>
      <td>${escapeHtml(g.account)}</td>
      <td><span class="champ-cell">${champIcon(g.my_champion)}${displayName(g.my_champion)}</span></td>
      <td><span class="champ-cell">${g.opp_champion ? champIcon(g.opp_champion) + "vs " + displayName(g.opp_champion) : "–"}</span></td>
      <td>${g.opp_champion ? titleCase(g.rank_tier) : "–"}</td>
      <td><span class="result-pill ${g.win ? "win" : "loss"}">${g.win ? "W" : "L"}</span></td>
      <td>${g.kills}/${g.deaths}/${g.assists}</td>
      <td>${(g.cs * 60 / g.game_duration_s).toFixed(1)}</td>
      <td>${fmtDuration(g.game_duration_s)}</td>
      <td><button class="preset promote-btn" data-match="${g.match_id}"
        data-puuid="${g.my_puuid}" title="Add to current block">+ Block</button></td>
    </tr>`).join("");
  return `<table class="games-inner">
    <thead><tr><th>Date</th><th>Account</th><th>Me</th><th>Opponent</th><th>Opp. rank</th>
    <th>Result</th><th>K/D/A</th><th>CS/min</th><th>Length</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

async function toggleSegment(segment) {
  const key = segKey(segment);
  if (segmentUi.expanded.has(key)) {
    segmentUi.expanded.delete(key);
    renderProgress(segmentUi.segments);
    return;
  }
  segmentUi.expanded.add(key);
  await ensureSegmentMetrics(segment);
  renderProgress(segmentUi.segments);
}

function renderProgress(segments) {
  segmentUi.segments = segments;
  const target = $("#progress-table");
  if (!segments.length) {
    target.innerHTML = `<div class="table-wrap"><div class="empty">
      No coaching sessions yet — add your first one below.</div></div>`;
    return;
  }
  const rows = segments.map((segment, i) => {
    const previous = segments.slice(0, i).reverse().find((s) => s.games > 0);
    const wrDelta = delta(segment, previous, "winrate_pp", 1, "pp");
    const kdaDelta = delta(segment, previous, "kda", 2);
    const csDelta = delta(segment, previous, "cs_min", 1);
    const empty = !segment.games;
    const key = segKey(segment);
    const expanded = segmentUi.expanded.has(key);
    let html = `<tr${empty ? ' class="muted"' : ""}>
      <td class="period-cell"><div class="period-wrap">
        <button class="preset seg-toggle" data-i="${i}" aria-expanded="${expanded}">${expanded ? "▾" : "▸"}</button>
        <div class="period-text"><strong>${segment.label}</strong><br><span class="muted period-sub">${fmtSegmentDates(segment)}${segment.note ? " · " + escapeHtml(segment.note) : ""}</span></div>
      </div></td>
      <td>${segment.games}</td>
      <td>${empty ? "–" : `${segment.wins}–${segment.games - segment.wins}`}</td>
      <td class="wr-col">${empty ? "–" : wrCell(segment.winrate)}<span class="delta-slot">${wrDelta}</span></td>
      <td>${fmt(segment.kda, 2)}<span class="delta-slot">${kdaDelta}</span></td>
      <td>${fmt(segment.cs_min)}<span class="delta-slot">${csDelta}</span></td>
      <td>${fmt(segment.gold_min, 0)}</td>
      <td>${fmt(segment.dmg_min, 0)}</td>
    </tr>`;
    if (expanded) {
      html += `<tr class="games-row"><td colspan="8">${segmentMetricsPanel(segment)}</td></tr>`;
    }
    return html;
  }).join("");
  target.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Period</th><th>Games</th><th>W–L</th><th class="wr-col">Winrate</th>
    <th>KDA</th><th>CS/min</th><th>Gold/min</th><th>DMG/min</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
  target.querySelectorAll(".seg-toggle").forEach((btn) =>
    btn.addEventListener("click", () => toggleSegment(segments[+btn.dataset.i])));
  target.querySelectorAll(".games-toggle").forEach((btn) =>
    btn.addEventListener("click", () => {
      const segment = segments.find((s) => segKey(s) === btn.dataset.key);
      if (segment) toggleSegmentGames(segment);
    }));
  wirePromoteButtons(target);
}

const sessionUi = { expanded: new Set(), editing: null };

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderNotes(notes) {
  if (!notes) return `<p class="muted">No notes yet — click edit to add some.</p>`;
  if (typeof marked !== "undefined") return marked.parse(notes);
  return `<pre>${escapeHtml(notes)}</pre>`; // fallback if vendor lib missing
}

function sessionCard(s) {
  const expanded = sessionUi.expanded.has(s.id);
  const editing = sessionUi.editing === s.id;
  let body = "";
  if (editing) {
    body = `<div class="session-body">
      <label class="filter-label" for="edit-title-${s.id}">Title</label>
      <input type="text" id="edit-title-${s.id}" value="${escapeHtml(s.title)}" style="width:100%">
      <label class="filter-label" for="edit-notes-${s.id}">Notes (Markdown)</label>
      <textarea id="edit-notes-${s.id}" rows="10">${escapeHtml(s.notes)}</textarea>
      <div class="session-actions">
        <button class="preset session-save" data-id="${s.id}">Save</button>
        <button class="preset session-cancel">Cancel</button>
      </div>
    </div>`;
  } else if (expanded) {
    body = `<div class="session-body md-body">${renderNotes(s.notes)}</div>`;
  }
  return `<div class="session-card">
    <div class="session-head">
      <button class="preset session-toggle" data-id="${s.id}" aria-expanded="${expanded}">
        ${expanded || editing ? "▾" : "▸"}</button>
      <span class="session-date">${s.session_date}</span>
      <span class="session-title">${s.title ? escapeHtml(s.title) : "<span class='muted'>untitled</span>"}</span>
      <span class="session-actions">
        <button class="preset session-edit" data-id="${s.id}">edit</button>
        <button class="preset session-delete" data-id="${s.id}">delete</button>
      </span>
    </div>
    ${body}
  </div>`;
}

function renderSessions(sessionRows) {
  const target = $("#session-list");
  if (!sessionRows.length) {
    target.innerHTML = `<div class="muted">No sessions recorded.</div>`;
    return;
  }
  target.innerHTML = sessionRows.map(sessionCard).join("");
  target.querySelectorAll(".session-toggle").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = +btn.dataset.id;
      sessionUi.expanded.has(id) ? sessionUi.expanded.delete(id) : sessionUi.expanded.add(id);
      if (sessionUi.editing === id) sessionUi.editing = null;
      renderSessions(sessionRows);
    }));
  target.querySelectorAll(".session-edit").forEach((btn) =>
    btn.addEventListener("click", () => {
      sessionUi.editing = +btn.dataset.id;
      sessionUi.expanded.add(+btn.dataset.id);
      renderSessions(sessionRows);
    }));
  target.querySelectorAll(".session-cancel").forEach((btn) =>
    btn.addEventListener("click", () => {
      sessionUi.editing = null;
      renderSessions(sessionRows);
    }));
  target.querySelectorAll(".session-save").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const id = +btn.dataset.id;
      const response = await fetch(`/api/sessions/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: $(`#edit-title-${id}`).value,
          notes: $(`#edit-notes-${id}`).value,
        }),
      });
      if (response.ok) {
        sessionUi.editing = null;
        loadProgress(); // titles appear in the segment table too
      }
    }));
  target.querySelectorAll(".session-delete").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this coaching session?")) return;
      await fetch(`/api/sessions/${btn.dataset.id}`, { method: "DELETE" });
      loadProgress();
    }));
}

async function unionFilterOptions() {
  const all = await Promise.all(
    state.players.map((p) => getJSON(`/api/filters?puuid=${encodeURIComponent(p.puuid)}`)));
  return {
    champions: [...new Set(all.flatMap((o) => o.champions))].sort(),
    queues: [...new Set(all.flatMap((o) => o.queues))].sort(),
  };
}

async function loadProgressFilterOptions() {
  const { champions, queues } = await unionFilterOptions();
  if (state.progressChampion === null) {
    state.progressChampion = champions.includes("Gwen") ? "Gwen" : "";
  }
  $("#progress-champion").innerHTML = `<option value="">All</option>` +
    champions.map((c) => `<option value="${c}" ${c === state.progressChampion ? "selected" : ""}>${displayName(c)}</option>`).join("");
  $("#progress-queue").innerHTML = `<option value="">All</option>` +
    queues.map((q) => `<option value="${q}" ${String(q) === state.progressQueue ? "selected" : ""}>${QUEUE_NAMES[q] ?? q}</option>`).join("");
}

async function loadProgress() {
  const params = new URLSearchParams();
  if (state.progressChampion) params.set("champion", state.progressChampion);
  if (state.progressQueue) params.set("queue", state.progressQueue);
  const [segments, sessionRows] = await Promise.all([
    getJSON(`/api/stats/progress?${params}`),
    getJSON("/api/sessions"),
  ]);
  // winrate in percentage points for delta display
  segments.forEach((s) => { s.winrate_pp = s.winrate == null ? null : 100 * s.winrate; });
  segmentUi.cache.clear(); // filters or data changed; refetch game lists on expand
  renderProgress(segments);
  renderSessions(sessionRows);
}

function setMainView(view) {
  state.mainView = view;
  if (history.replaceState) {
    const hash = { progress: "#progress", trends: "#trends", blocks: "#blocks" }[view] || "#";
    history.replaceState(null, "", hash);
  }
  for (const v of ["overview", "progress", "trends", "blocks"]) {
    $(`#nav-${v}`).classList.toggle("active", view === v);
    $(`#${v}-view`).classList.toggle("hidden", view !== v);
  }
  $("#account-tabs").classList.toggle("hidden", view !== "overview");
  if (view === "progress") loadProgressFilterOptions().then(loadProgress);
  if (view === "trends") initTrends();
  if (view === "blocks") initBlocks();
}

function wireProgress() {
  $("#nav-overview").addEventListener("click", () => setMainView("overview"));
  $("#nav-progress").addEventListener("click", () => setMainView("progress"));
  $("#nav-trends").addEventListener("click", () => setMainView("trends"));
  $("#nav-blocks").addEventListener("click", () => setMainView("blocks"));
  $("#progress-champion").addEventListener("change", (e) => {
    state.progressChampion = e.target.value; loadProgress();
  });
  $("#progress-queue").addEventListener("change", (e) => {
    state.progressQueue = e.target.value; loadProgress();
  });
  $("#session-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = $("#session-error");
    errorEl.textContent = "";
    const response = await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date: $("#session-date").value, title: $("#session-title").value }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      errorEl.textContent = body.detail || `error ${response.status}`;
      return;
    }
    $("#session-date").value = "";
    $("#session-title").value = "";
    loadProgress();
  });
}

// ---------- crawl ----------

let crawlTimer = null;

async function pollCrawl() {
  const status = await getJSON("/api/crawl/status");
  const el = $("#crawl-status");
  $("#crawl-btn").disabled = status.running;
  if (status.running) {
    el.textContent = status.message;
    if (!crawlTimer) crawlTimer = setInterval(pollCrawl, 2000);
  } else {
    if (crawlTimer) { clearInterval(crawlTimer); crawlTimer = null; }
    if (status.error) {
      el.textContent = `crawl failed: ${status.error}`;
    } else if (status.message === "done") {
      el.textContent = "up to date";
      await init(false);
      // refresh whichever view is active so new games appear immediately
      if (state.mainView === "progress") await loadProgress();
      else if (state.mainView === "blocks") await loadBlocks();
      else if (state.mainView === "trends") await loadTrends();
    } else {
      el.textContent = "";
    }
  }
}

// ---------- wiring ----------

function wireFilters() {
  document.querySelectorAll("#range-presets .preset").forEach((btn) =>
    btn.addEventListener("click", () => {
      document.querySelectorAll("#range-presets .preset").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.range = btn.dataset.range;
      $("#custom-dates").classList.toggle("hidden", state.range !== "custom");
      if (state.range !== "custom") refresh();
    }));
  $("#date-from").addEventListener("change", (e) => { state.from = e.target.value; refresh(); });
  $("#date-to").addEventListener("change", (e) => { state.to = e.target.value; refresh(); });
  $("#champion-select").addEventListener("change", (e) => { state.champion = e.target.value; refresh(); });
  $("#queue-select").addEventListener("change", (e) => { state.queue = e.target.value; refresh(); });
  $("#rank-select").addEventListener("change", (e) => { state.rankTier = e.target.value; refresh(); });
  $("#min-games").addEventListener("change", (e) => { state.minGames = Math.max(1, +e.target.value || 1); refresh(); });
  $("#view-flat").addEventListener("click", () => setView("flat"));
  $("#view-rank").addEventListener("click", () => setView("rank"));
  $("#crawl-btn").addEventListener("click", async () => {
    await fetch("/api/crawl", { method: "POST" });
    pollCrawl();
  });
}

function setView(view) {
  state.view = view;
  $("#view-flat").classList.toggle("active", view === "flat");
  $("#view-rank").classList.toggle("active", view === "rank");
  refresh();
}

async function loadDdragonVersion() {
  try {
    const cached = localStorage.getItem("ddragon-version");
    const cachedAt = +localStorage.getItem("ddragon-version-at") || 0;
    if (cached && Date.now() - cachedAt < 86_400_000) {
      state.ddragonVersion = cached;
      return;
    }
    const versions = await getJSON("https://ddragon.leagueoflegends.com/api/versions.json");
    state.ddragonVersion = versions[0];
    localStorage.setItem("ddragon-version", versions[0]);
    localStorage.setItem("ddragon-version-at", String(Date.now()));
  } catch {
    state.ddragonVersion = null; // icons silently disabled offline
  }
}

async function init(firstLoad = true) {
  state.players = await getJSON("/api/players");
  if (!state.players.length) {
    document.body.innerHTML = `<div class="empty" style="margin-top:40px">
      No data yet. Run <code>./crawl.sh --limit 20</code> first, then reload.</div>`;
    return;
  }
  if (!state.puuid || !state.players.some((p) => p.puuid === state.puuid)) {
    state.puuid = state.players[0].puuid;
  }
  renderTabs();
  if (firstLoad) {
    await loadDdragonVersion();
    wireFilters();
    wireProgress();
    pollCrawl();
  }
  await loadFilterOptions();
  await refresh();
  if (firstLoad && location.hash === "#progress") setMainView("progress");
  if (firstLoad && location.hash === "#trends") setMainView("trends");
  if (firstLoad && location.hash === "#blocks") setMainView("blocks");
}

init();
