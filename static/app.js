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
  mainView: "overview", // overview | matchups | progress | trends | blocks | settings
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

const TIER_SHORT = { IRON: "Iron", BRONZE: "Bronze", SILVER: "Silver", GOLD: "Gold",
  PLATINUM: "Plat", EMERALD: "Em", DIAMOND: "Dia", MASTER: "Master",
  GRANDMASTER: "GM", CHALLENGER: "Chal" };

function fmtRank(entry) {
  if (!entry || !entry.tier) return "Unranked";
  const division = ["MASTER", "GRANDMASTER", "CHALLENGER"].includes(entry.tier)
    ? "" : ` ${entry.division}`;
  return `${TIER_SHORT[entry.tier] || entry.tier}${division} ${entry.lp ?? 0}LP`;
}

function fmtRankList(ranks) {
  if (!ranks || !ranks.length) return "–";
  return ranks.map((r) =>
    `${escapeHtml(r.account.split("#")[0])} ${fmtRank(r)}`).join("<br>");
}

// ---------- persisted column choices ----------

function colPrefs(storageKey, allKeys, defaultKeys = allKeys) {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (Array.isArray(saved)) return new Set(saved.filter((k) => allKeys.includes(k)));
  } catch { /* fall through to defaults */ }
  return new Set(defaultKeys);
}

function renderColPicker(target, storageKey, columns, visible, onChange) {
  target.innerHTML = `<details class="col-picker"><summary class="preset">Columns ▾</summary>
    <div class="col-menu">` + columns.map((c) =>
      `<label><input type="checkbox" data-col="${c.key}"
         ${visible.has(c.key) ? "checked" : ""}> ${c.label}</label>`).join("") +
    `</div></details>`;
  target.querySelectorAll("input").forEach((cb) =>
    cb.addEventListener("change", () => {
      cb.checked ? visible.add(cb.dataset.col) : visible.delete(cb.dataset.col);
      localStorage.setItem(storageKey, JSON.stringify([...visible]));
      onChange();
    }));
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

// per-game "standard metrics" panel (same groups as the coaching/blocks views)
function metricGroupsPanel(data) {
  if (data === undefined) return `<div class="muted">Loading…</div>`;
  if (data === null) return `<div class="muted">No detailed metrics recorded for this game.</div>`;
  const groups = [...new Set(data.meta.map((m) => m.group))];
  return `<div class="metric-groups">` + groups.map((g) =>
    `<div class="metric-group"><h4>${g}</h4>` +
    data.meta.filter((m) => m.group === g).map((m) => `<div class="metric-row">
        <span class="metric-label">${m.label}</span>
        <span class="metric-value">${fmtMetric(data.metrics[m.key], m)}</span>
      </div>`).join("") + `</div>`).join("") + `</div>`;
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

// ---------- rank-over-time chart ----------

// tier -> base absolute LP (mirror of stats._TIER_BASE); apex tiers collapse
const RANK_TIER_BASES = [
  ["IRON", 0], ["BRONZE", 400], ["SILVER", 800], ["GOLD", 1200],
  ["PLATINUM", 1600], ["EMERALD", 2000], ["DIAMOND", 2400], ["MASTER", 2800],
];
const RANK_SERIES_COLORS = ["var(--series-1)", "#e08a3c", "#3aa876", "#b06fd8", "#d05c5c"];

const RANK_W = 860, RANK_H = 260;
const RK_PAD = { l: 58, r: 12, t: 12, b: 24 };

function overviewRangeBounds() {
  // mirrors the server's period filter for the overview
  const now = Date.now();
  if (state.range === "custom") {
    const from = state.from ? Date.parse(state.from + "T00:00:00Z") : null;
    const to = state.to ? Date.parse(state.to + "T00:00:00Z") + 86_400_000 - 1 : now;
    return [from, Math.min(to, now)];
  }
  if (state.range !== "all") return [now - parseInt(state.range, 10) * 86_400_000, now];
  return [null, now];
}

// clip a series to [fromMs, toMs]: carry the last point before the window in
// at the left edge, and extend the last value to the right edge ("now")
function rankWindow(points, fromMs, toMs) {
  const inWin = points.filter((p) => p.t >= fromMs && p.t <= toMs)
    .map((p) => ({ ...p, x: p.t }));
  const before = points.filter((p) => p.t < fromMs).pop();
  if (before) inWin.unshift({ ...before, x: fromMs, carried: true });
  if (inWin.length) {
    const last = inWin[inWin.length - 1];
    if (last.x < toMs) inWin.push({ ...last, x: toMs, carried: true });
  }
  return inWin;
}

function renderRankChart() {
  const data = state.rankHistory;
  const target = $("#rank-chart");
  const legend = $("#rank-legend");
  if (!data || !data.series.some((s) => s.points.length)) {
    legend.innerHTML = "";
    target.innerHTML = `<div class="table-wrap"><div class="empty">
      No rank history yet — a snapshot is stored on every data update.</div></div>`;
    return;
  }
  let [fromMs, toMs] = overviewRangeBounds();
  if (fromMs == null) {
    fromMs = Math.min(...data.series.flatMap((s) => s.points.map((p) => p.t)));
  }
  const series = data.series
    .map((s, i) => ({ ...s, color: RANK_SERIES_COLORS[i % RANK_SERIES_COLORS.length],
                      pts: rankWindow(s.points, fromMs, toMs) }))
    .filter((s) => s.pts.length);

  legend.innerHTML = data.series.map((s, i) => {
    const player = state.players.find((p) => p.puuid === s.puuid);
    const current = player && player.solo_tier
      ? fmtRank({ tier: player.solo_tier, division: player.solo_division, lp: player.solo_lp })
      : "Unranked";
    return `<span><span class="swatch" style="background:${RANK_SERIES_COLORS[i % RANK_SERIES_COLORS.length]}"></span>
      ${escapeHtml(s.account.split("#")[0])} · ${current}</span>`;
  }).join("");

  if (!series.length) {
    target.innerHTML = `<div class="table-wrap"><div class="empty">
      No rank snapshots in this period.</div></div>`;
    return;
  }

  const values = series.flatMap((s) => s.pts.map((p) => p.value));
  let lo = Math.floor(Math.min(...values) / 100) * 100;
  let hi = Math.ceil(Math.max(...values) / 100) * 100;
  if (lo === hi) hi += 100;
  while (hi - lo < 200) { lo = Math.max(0, lo - 100); hi += 100; }

  const iw = RANK_W - RK_PAD.l - RK_PAD.r, ih = RANK_H - RK_PAD.t - RK_PAD.b;
  const x = (t) => RK_PAD.l + ((t - fromMs) / Math.max(1, toMs - fromMs)) * iw;
  const y = (v) => RK_PAD.t + ih - ((v - lo) / (hi - lo)) * ih;

  // horizontal gridlines: tier boundaries (labelled) + divisions when zoomed in
  let grid = "";
  const minor = hi - lo <= 1000;
  for (let v = lo; v <= hi; v += 100) {
    const boundary = RANK_TIER_BASES.find(([, base]) => base === v);
    if (boundary) {
      grid += `<line class="rk-grid" x1="${RK_PAD.l}" x2="${RANK_W - RK_PAD.r}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/>`;
    } else if (minor) {
      grid += `<line class="rk-grid rk-grid-minor" x1="${RK_PAD.l}" x2="${RANK_W - RK_PAD.r}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/>`;
    }
  }
  // tier band labels, centred in the visible part of each band
  for (const [tier, base] of RANK_TIER_BASES) {
    const top = tier === "MASTER" ? Infinity : base + 400;
    const a = Math.max(lo, base), b = Math.min(hi, top);
    if (b - a >= 60) {
      grid += `<text class="rk-tier-label" x="${RK_PAD.l - 6}" y="${(y((a + b) / 2) + 3).toFixed(1)}"
        text-anchor="end">${TIER_SHORT[tier]}</text>`;
    }
  }

  // coaching sessions as dashed vertical lines
  let sessionLines = "";
  for (const s of data.sessions || []) {
    const t = Date.parse(s.date + "T00:00:00Z");
    if (isNaN(t) || t < fromMs || t > toMs) continue;
    const sx = x(t).toFixed(1);
    sessionLines += `<line class="rk-session" x1="${sx}" x2="${sx}" y1="${RK_PAD.t}" y2="${RANK_H - RK_PAD.b}"/>
      <line class="rk-session-hit" x1="${sx}" x2="${sx}" y1="${RK_PAD.t}" y2="${RANK_H - RK_PAD.b}"
        data-tip="${escapeHtml(`${s.date}: ${s.title || "coaching session"}`)}"/>`;
  }

  // estimated stretches (from win/loss walks) draw fainter than real snapshots
  let anyEstimated = false;
  const lines = series.map((s) => {
    // split the line into runs of segments that are real–real vs touching an
    // estimated point, so estimated stretches can render fainter
    const pairEst = (k) => Boolean(s.pts[k].estimated || s.pts[k + 1].estimated);
    let segments = "";
    for (let i = 0; i < s.pts.length - 1;) {
      const est = pairEst(i);
      let j = i + 1;
      while (j < s.pts.length - 1 && pairEst(j) === est) j++;
      const pts = s.pts.slice(i, j + 1)
        .map((p) => `${x(p.x).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
      segments += `<polyline class="rk-line${est ? " rk-est" : ""}" style="stroke:${s.color}" points="${pts}"/>`;
      i = j;
    }
    if (s.pts.some((p) => p.estimated)) anyEstimated = true;
    const shown = s.pts.filter((p) => !p.carried);
    const estCount = shown.filter((p) => p.estimated).length;
    const dots = shown.map((p) => {
      if (p.estimated && estCount > 300) return ""; // keep the DOM sane on long histories
      const cx = x(p.x).toFixed(1), cy = y(p.value).toFixed(1);
      const tip = `${fmtDate(p.t)} · ${s.account.split("#")[0]}: ` +
        (p.estimated ? `≈ ${fmtRank(p)} (est.)` : fmtRank(p));
      return `<circle class="rk-dot${p.estimated ? " rk-est" : ""}" cx="${cx}" cy="${cy}"
          r="${p.estimated ? 2 : 3}" style="fill:${s.color}"/>
        <circle class="tl-hit" cx="${cx}" cy="${cy}" r="${p.estimated ? 5 : 8}"
          data-tip="${escapeHtml(tip)}"/>`;
    }).join("");
    return segments + dots;
  }).join("");

  target.innerHTML = `<div class="rank-chart-box">
    <svg viewBox="0 0 ${RANK_W} ${RANK_H}" role="img" aria-label="Rank over time">
      ${grid}${sessionLines}${lines}
      <line class="tl-axis" x1="${RK_PAD.l}" x2="${RANK_W - RK_PAD.r}" y1="${RANK_H - RK_PAD.b}" y2="${RANK_H - RK_PAD.b}"/>
      <text class="rk-xlab" x="${RK_PAD.l}" y="${RANK_H - 6}">${escapeHtml(fmtDate(fromMs))}</text>
      <text class="rk-xlab" x="${RANK_W - RK_PAD.r}" y="${RANK_H - 6}" text-anchor="end">${escapeHtml(fmtDate(toMs))}</text>
    </svg>
    ${anyEstimated ? `<div class="muted rk-note">Faint = estimated from ranked
      wins/losses (±20 LP per game); solid = recorded rank snapshots.</div>` : ""}
  </div>`;

  const tip = $("#chart-tip");
  target.querySelectorAll("[data-tip]").forEach((el) => {
    el.addEventListener("mouseenter", () => {
      tip.textContent = el.dataset.tip;
      tip.classList.remove("hidden");
      const r = el.getBoundingClientRect();
      tip.style.left = `${r.left + window.scrollX + 12}px`;
      tip.style.top = `${r.top + window.scrollY - 30}px`;
    });
    el.addEventListener("mouseleave", () => tip.classList.add("hidden"));
  });
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
      if (state.mainView === "matchups") initMatchups();
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
  const [summary, rankHistory] = await Promise.all([
    getJSON(`/api/stats/summary?${qs}`),
    getJSON("/api/stats/rank-history"),
  ]);
  state.rankHistory = rankHistory;
  renderSummary(summary);
  renderChampionTable(summary.by_champion ?? []);
  renderRecent(summary.recent ?? []);
  renderRankChart();
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

const PROGRESS_COLS = [
  { key: "rank", label: "Rank at start" },
  { key: "games", label: "Games" },
  { key: "wl", label: "W–L" },
  { key: "winrate", label: "Winrate" },
  { key: "kda", label: "KDA" },
  { key: "cs", label: "CS/min" },
  { key: "gold", label: "Gold/min" },
  { key: "dmg", label: "DMG/min" },
];
const progressCols = colPrefs("cp-cols-progress", PROGRESS_COLS.map((c) => c.key));

function renderProgress(segments) {
  segmentUi.segments = segments;
  const target = $("#progress-table");
  if (!segments.length) {
    target.innerHTML = `<div class="table-wrap"><div class="empty">
      No coaching sessions yet — add your first one below.</div></div>`;
    return;
  }
  const visible = PROGRESS_COLS.filter((c) => progressCols.has(c.key));
  const rows = segments.map((segment, i) => {
    const previous = segments.slice(0, i).reverse().find((s) => s.games > 0);
    const wrDelta = delta(segment, previous, "winrate_pp", 1, "pp");
    const kdaDelta = delta(segment, previous, "kda", 2);
    const csDelta = delta(segment, previous, "cs_min", 1);
    const empty = !segment.games;
    const key = segKey(segment);
    const expanded = segmentUi.expanded.has(key);
    const cells = {
      rank: `<td class="rank-cell">${fmtRankList(segment.start_ranks)}</td>`,
      games: `<td>${segment.games}</td>`,
      wl: `<td>${empty ? "–" : `${segment.wins}–${segment.games - segment.wins}`}</td>`,
      winrate: `<td class="wr-col">${empty ? "–" : wrCell(segment.winrate)}<span class="delta-slot">${wrDelta}</span></td>`,
      kda: `<td>${fmt(segment.kda, 2)}<span class="delta-slot">${kdaDelta}</span></td>`,
      cs: `<td>${fmt(segment.cs_min)}<span class="delta-slot">${csDelta}</span></td>`,
      gold: `<td>${fmt(segment.gold_min, 0)}</td>`,
      dmg: `<td>${fmt(segment.dmg_min, 0)}</td>`,
    };
    let html = `<tr${empty ? ' class="muted"' : ""}>
      <td class="period-cell"><div class="period-wrap">
        <button class="preset seg-toggle" data-i="${i}" aria-expanded="${expanded}">${expanded ? "▾" : "▸"}</button>
        <div class="period-text"><strong>${segment.label}</strong><br><span class="muted period-sub">${fmtSegmentDates(segment)}${segment.note ? " · " + escapeHtml(segment.note) : ""}</span></div>
      </div></td>` + visible.map((c) => cells[c.key]).join("") + `</tr>`;
    if (expanded) {
      html += `<tr class="games-row"><td colspan="${visible.length + 1}">${segmentMetricsPanel(segment)}</td></tr>`;
    }
    return html;
  }).join("");
  const headers = { winrate: ' class="wr-col"' };
  target.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Period</th>` +
    visible.map((c) => `<th${headers[c.key] || ""}>${c.label}</th>`).join("") +
    `</tr></thead><tbody>${rows}</tbody></table></div>`;
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
        <button class="preset icon-btn session-edit" data-id="${s.id}" title="Edit session" aria-label="Edit session">✎</button>
        <button class="preset icon-btn session-delete" data-id="${s.id}" title="Delete session" aria-label="Delete session">🗑</button>
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
  // re-hydrate anything the user had expanded so panels don't stick on "Loading…"
  let rehydrated = false;
  for (const segment of segments) {
    const key = segKey(segment);
    if (segmentUi.expanded.has(key)) {
      await ensureSegmentMetrics(segment);
      rehydrated = true;
    }
    if (segmentUi.expandedGames.has(key)) {
      segmentUi.cache.set("games:" + key,
        await getJSON(`/api/stats/games?${progressFilterParams(segment)}`));
      rehydrated = true;
    }
  }
  if (rehydrated) renderProgress(segments);
}

function setMainView(view) {
  state.mainView = view;
  if (history.replaceState) {
    const hash = { matchups: "#matchups", progress: "#progress",
                   trends: "#trends", blocks: "#blocks" }[view] || "#";
    history.replaceState(null, "", hash);
  }
  for (const v of ["overview", "matchups", "progress", "trends", "blocks", "settings"]) {
    $(`#nav-${v}`).classList.toggle("active", view === v);
    $(`#${v}-view`).classList.toggle("hidden", view !== v);
  }
  $("#account-tabs").classList.toggle("hidden",
    view !== "overview" && view !== "matchups");
  if (view === "matchups") initMatchups();
  if (view === "progress") loadProgressFilterOptions().then(loadProgress);
  if (view === "trends") initTrends();
  if (view === "blocks") initBlocks();
  if (view === "settings") initSettings();
}

// ---------- settings ----------

const settingsUi = { wired: false, accounts: [] };

// canonical Riot platform id -> human-readable server name
const PLATFORM_LABELS = {
  euw1: "EUW", eun1: "EUNE", tr1: "TR", ru: "RU",
  na1: "NA", br1: "BR", la1: "LAN", la2: "LAS",
  kr: "KR", jp1: "JP",
  oc1: "OCE", ph2: "PH", sg2: "SG", th2: "TH", tw2: "TW", vn2: "VN",
};
const PLATFORM_ORDER = ["euw1", "eun1", "na1", "kr", "br1", "la1", "la2",
                        "jp1", "ru", "tr1", "oc1", "ph2", "sg2", "th2", "tw2", "vn2"];

function renderAccountChips() {
  const box = $("#settings-accounts");
  box.querySelectorAll(".chip").forEach((chip) => chip.remove());
  const input = box.querySelector(".chip-input");
  input.insertAdjacentHTML("beforebegin", settingsUi.accounts.map((a) =>
    `<span class="chip chip-plain">${escapeHtml(a)}
       <button class="chip-x" data-account="${escapeHtml(a)}" title="Remove"
         aria-label="Remove ${escapeHtml(a)}">×</button></span>`).join(""));
  box.querySelectorAll(".chip-x").forEach((btn) =>
    btn.addEventListener("click", () => {
      settingsUi.accounts = settingsUi.accounts.filter((a) => a !== btn.dataset.account);
      renderAccountChips();
    }));
}

function applyHiddenViews(hidden) {
  state.hiddenViews = hidden || [];
  for (const view of ["overview", "matchups", "progress", "trends", "blocks"]) {
    $(`#nav-${view}`).classList.toggle("hidden", state.hiddenViews.includes(view));
  }
  if (state.hiddenViews.includes(state.mainView)) {
    const fallback = ["overview", "matchups", "progress", "trends", "blocks"]
      .find((view) => !state.hiddenViews.includes(view));
    setMainView(fallback || "settings");
  }
}

async function initSettings() {
  const data = await getJSON("/api/settings");
  $("#setting-key").value = data.riot_api_key;
  const platforms = [...data.platforms].sort((a, b) =>
    PLATFORM_ORDER.indexOf(a) - PLATFORM_ORDER.indexOf(b));
  $("#setting-platform").innerHTML = platforms.map((p) =>
    `<option value="${p}" ${p === data.platform ? "selected" : ""}>${PLATFORM_LABELS[p] || p.toUpperCase()}</option>`).join("");
  settingsUi.accounts = data.accounts;
  settingsUi.wasUnconfigured = !data.configured;
  renderAccountChips();
  document.querySelectorAll(".view-toggle-cb").forEach((cb) => {
    cb.checked = !(data.hidden_views || []).includes(cb.value);
  });
  $("#setting-auto-crawl").value = data.auto_crawl_hours;
  $("#settings-banner").classList.toggle("hidden", data.configured);
  if (settingsUi.wired) return;
  settingsUi.wired = true;
  $("#key-reveal").addEventListener("click", () => {
    const input = $("#setting-key");
    const hidden = input.type === "password";
    input.type = hidden ? "text" : "password";
    $("#key-reveal").title = hidden ? "Hide key" : "Show key";
  });
  const input = $("#settings-accounts-input");
  const addAccount = () => {
    const value = input.value.trim();
    if (!value) return;
    if (!value.includes("#")) {
      $("#settings-status").textContent = "accounts must be Name#TAG";
      return;
    }
    if (!settingsUi.accounts.includes(value)) settingsUi.accounts.push(value);
    input.value = "";
    $("#settings-status").textContent = "";
    renderAccountChips();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      addAccount();
    } else if (e.key === "Backspace" && !input.value) {
      settingsUi.accounts.pop();
      renderAccountChips();
    }
  });
  $("#settings-accounts").addEventListener("click", () => input.focus());
  $("#settings-save").addEventListener("click", async () => {
    addAccount(); // commit any half-typed account first
    const hiddenViews = [...document.querySelectorAll(".view-toggle-cb")]
      .filter((cb) => !cb.checked).map((cb) => cb.value);
    const response = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        riot_api_key: $("#setting-key").value,
        accounts: settingsUi.accounts,
        platform: $("#setting-platform").value,
        hidden_views: hiddenViews,
        auto_crawl_hours: Math.max(0, parseInt($("#setting-auto-crawl").value, 10) || 0),
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (response.ok) {
      applyHiddenViews(body.hidden_views);
      $("#settings-banner").classList.add("hidden");
      if (settingsUi.wasUnconfigured && body.configured) {
        settingsUi.wasUnconfigured = false;
        $("#settings-status").textContent = "saved ✓ — fetching your match history now…";
        startCrawl();
      } else {
        $("#settings-status").textContent = "saved ✓";
      }
    } else {
      $("#settings-status").textContent = body.detail || `error ${response.status}`;
    }
  });
}

function wireProgress() {
  $("#nav-overview").addEventListener("click", () => setMainView("overview"));
  $("#nav-matchups").addEventListener("click", () => setMainView("matchups"));
  $("#nav-progress").addEventListener("click", () => setMainView("progress"));
  $("#nav-trends").addEventListener("click", () => setMainView("trends"));
  $("#nav-blocks").addEventListener("click", () => setMainView("blocks"));
  $("#nav-settings").addEventListener("click", () => setMainView("settings"));
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

// ---------- auto crawl ----------

const STARTUP_CRAWL_MIN_GAP_MS = 15 * 60 * 1000; // skip if we crawled minutes ago

async function startCrawl() {
  const status = await getJSON("/api/crawl/status");
  if (!status.running) await fetch("/api/crawl", { method: "POST" });
  pollCrawl();
}

function maybeStartupCrawl(settings) {
  if (!settings.configured) return;
  if (Date.now() - (settings.last_crawl_ms || 0) > STARTUP_CRAWL_MIN_GAP_MS) startCrawl();
}

async function autoCrawlTick() {
  const settings = await getJSON("/api/settings");
  if (!settings.configured || !settings.auto_crawl_hours) return;
  const due = (settings.last_crawl_ms || 0) + settings.auto_crawl_hours * 3_600_000;
  if (Date.now() > due) startCrawl();
}

// ---------- update check ----------

function isNewerVersion(candidate, current) {
  const a = candidate.split(".").map(Number);
  const b = current.split(".").map(Number);
  for (let i = 0; i < 3; i++) {
    if ((a[i] || 0) > (b[i] || 0)) return true;
    if ((a[i] || 0) < (b[i] || 0)) return false;
  }
  return false;
}

async function checkForUpdates() {
  try {
    const info = await getJSON("/api/version");
    $("#app-version").textContent = `v${info.version}`;
    if (info.version === "dev") return;
    let latest = localStorage.getItem("cp-latest-tag") || "";
    const lastCheck = +localStorage.getItem("cp-update-checked") || 0;
    if (Date.now() - lastCheck > 86_400_000) {  // at most one GitHub call per day
      const release = await getJSON(
        `https://api.github.com/repos/${info.repo}/releases/latest`);
      latest = release.tag_name || "";
      localStorage.setItem("cp-latest-tag", latest);
      localStorage.setItem("cp-update-checked", String(Date.now()));
    }
    const version = latest.replace(/^v/, "");
    if (version && isNewerVersion(version, info.version)) {
      const banner = $("#update-banner");
      banner.innerHTML = `⬆ <strong>Coach Potato v${escapeHtml(version)}</strong> is available —
        <a href="https://github.com/${info.repo}/releases/latest" target="_blank"
        rel="noopener">download the update</a> (you have v${escapeHtml(info.version)})`;
      banner.classList.remove("hidden");
    }
  } catch {
    // offline, rate-limited, or no releases published yet — stay quiet
  }
}

// ---------- crawl ----------

let crawlTimer = null;
let crawlPolls = 0;

async function refreshDuringCrawl() {
  // refresh data views as the crawl lands new games — but never a view
  // where the user might have half-finished input
  if (state.mainView === "settings") return;
  if (state.mainView === "blocks") {
    if (typeof blockState !== "undefined" &&
        blockState.editingNotes == null && blockState.editingLearnings == null) {
      await loadBlocks();
    }
    return;
  }
  await init(false);
  if (state.mainView === "matchups" && muState.editingNotes == null) await loadMatchups();
  else if (state.mainView === "progress") await loadProgress();
  else if (state.mainView === "trends") await loadTrends();
}

async function pollCrawl() {
  const status = await getJSON("/api/crawl/status");
  const el = $("#crawl-status");
  $("#crawl-btn").disabled = status.running;
  $("#crawl-indicator").classList.toggle("hidden", !status.running);
  if (status.running) {
    const explain = "Riot limits API requests to 100 per 2 minutes, so large " +
      "updates pause periodically and resume automatically. Progress so far: " +
      (status.message || "starting");
    const warn = $("#rate-warn");
    warn.classList.toggle("hidden", !status.rate_limited);
    warn.title = explain;
    warn.onclick = () => alert(explain);
    el.textContent = "";
    if (!crawlTimer) crawlTimer = setInterval(pollCrawl, 2000);
    if (++crawlPolls % 5 === 0) await refreshDuringCrawl();
  } else {
    if (crawlTimer) { clearInterval(crawlTimer); crawlTimer = null; }
    if (status.error) {
      el.textContent = `crawl failed: ${status.error}`;
    } else if (status.message === "done") {
      el.textContent = "up to date";
      await init(false);
      // refresh whichever view is active so new games appear immediately
      if (state.mainView === "matchups") await loadMatchups();
      else if (state.mainView === "progress") await loadProgress();
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
  renderColPicker($("#progress-cols"), "cp-cols-progress", PROGRESS_COLS, progressCols,
    () => renderProgress(segmentUi.segments));
  $("#crawl-btn").addEventListener("click", startCrawl);
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
  if (firstLoad) {
    await loadDdragonVersion();
    wireFilters();
    wireProgress();
    pollCrawl();
    checkForUpdates();
    const settings = await getJSON("/api/settings");
    applyHiddenViews(settings.hidden_views);
    maybeStartupCrawl(settings);
    setInterval(autoCrawlTick, 10 * 60 * 1000);
  }
  if (!state.players.length) {
    $("#summary-tiles").innerHTML = `<div class="tile" style="min-width:100%">
      <div class="label">No match data yet</div>
      <div class="value" style="font-size:16px">Add your API key and accounts in
        <a href="#settings" id="goto-settings">Settings ⚙</a>, then press <strong>Update data</strong>.</div></div>`;
    const link = $("#goto-settings");
    if (link) link.addEventListener("click", (e) => { e.preventDefault(); setMainView("settings"); });
    if (firstLoad) {
      const settings = await getJSON("/api/settings");
      setMainView(settings.configured ? "overview" : "settings");
    }
    return;
  }
  if (!state.puuid || !state.players.some((p) => p.puuid === state.puuid)) {
    state.puuid = state.players[0].puuid;
  }
  renderTabs();
  await loadFilterOptions();
  await refresh();
  if (firstLoad && location.hash === "#matchups") setMainView("matchups");
  if (firstLoad && location.hash === "#progress") setMainView("progress");
  if (firstLoad && location.hash === "#trends") setMainView("trends");
  if (firstLoad && location.hash === "#blocks") setMainView("blocks");
  if (firstLoad && location.hash === "#settings") setMainView("settings");
}

init();
